"""Sincroniza transações da Pluggy para o ledger.

Uso:
  uv run python -m finance.sync               # incremental (só o novo desde o último sync)
  uv run python -m finance.sync --backfill    # histórico completo (~12 meses) — 1ª vez
  uv run python -m finance.sync --days 90      # backfill de janela customizada
"""

import argparse
from datetime import date, datetime, timedelta, timezone

from . import iof
from . import ledger as L
from . import pluggy_client as pc
from . import sheets
from .config import BACKFILL_DAYS, ensure_dirs
from .errors import FinanceError, run_cli
from .transaction_dates import load_timezone


def _load_state() -> dict:
    return sheets.read_config("sync_state", {}) or {}


def _save_state(state: dict) -> None:
    sheets.write_config("sync_state", state)


def _load_min_date() -> str | None:
    """Piso de data opcional: descarta lançamentos anteriores, mesmo em backfill."""
    value = sheets.read_config("min_transaction_date", None)
    if value in (None, ""):
        return None
    value = str(value).strip()
    try:
        date.fromisoformat(value)
    except ValueError:
        raise FinanceError(
            f"Config[min_transaction_date] inválido: {value!r}. Use o formato YYYY-MM-DD."
        )
    return value


def run(days: int = BACKFILL_DAYS, backfill: bool = False) -> dict:
    """Puxa as transações da Pluggy para o Ledger e devolve o que entrou por conta.

    Sem `backfill`, cada conta já sincronizada busca só o que entrou desde o último sync
    (com 2 dias de folga; o upsert deduplica). Conta nova ou `backfill` buscam `days`."""
    ensure_dirs()
    items = pc.item_ids()
    if not items:
        raise FinanceError("ITEM_IDS vazio no .env. Adicione o(s) ID(s) do(s) item(ns).")
    sheets.ensure_current_schema()

    api_key = pc.get_api_key()
    state = _load_state()
    ledger = L.load_ledger()
    known_ids = set(ledger)
    local_timezone = load_timezone()
    min_date = _load_min_date()
    run_started = datetime.now(timezone.utc)

    accounts = []
    with pc.build_client(api_key) as client:
        for item_id in items:
            for acc in pc.list_accounts(client, item_id):
                acc_id = str(acc.id)
                name = acc.marketing_name or acc.name
                st = state.get(acc_id)
                if backfill or st is None:
                    var_from = datetime.now(timezone.utc) - timedelta(days=days)
                    created_at_from = None
                    mode = f"backfill {days}d"
                else:
                    last = datetime.fromisoformat(st["last_synced_at"])
                    created_at_from = last - timedelta(days=2)  # overlap; upsert deduplica
                    var_from = None
                    mode = f"incremental desde {created_at_from.date()}"

                txs = pc.fetch_transactions(
                    client, acc_id, var_from=var_from, created_at_from=created_at_from
                )
                recs = [L.normalize(t, acc, item_id, local_timezone) for t in txs]
                discarded = 0
                if min_date:
                    kept = [r for r in recs if r["date"] >= min_date]
                    discarded = len(recs) - len(kept)
                    recs = kept
                added, updated = L.upsert(ledger, recs)
                accounts.append({"item_id": str(item_id), "name": name,
                                 "type": str(acc.type), "mode": mode,
                                 "received": len(txs), "added": added,
                                 "updated": updated, "discarded": discarded})
                state[acc_id] = {
                    "account_name": name,
                    "item_id": str(item_id),
                    "last_synced_at": run_started.isoformat(),
                }

    iof_settings = iof.load_settings()
    iof_changed = set()
    if iof_settings:
        iof_changed = iof.apply(ledger, iof_settings, set(ledger) - known_ids)

    L.save_ledger(ledger)
    _save_state(state)
    return {"accounts": accounts, "total": len(ledger), "min_date": min_date,
            "added": sum(a["added"] for a in accounts),
            "updated": sum(a["updated"] for a in accounts),
            "discarded": sum(a["discarded"] for a in accounts),
            "iof_changed": len(iof_changed),
            "iof_conflicts": iof.conflict_lines(ledger) if iof_settings else [],
            "uncategorized": sum(1 for r in ledger.values() if not r["category"])}


def summary(result: dict) -> list[str]:
    lines = []
    if result["min_date"]:
        lines.append(f"Piso de data ativo: descartando lançamentos anteriores a "
                     f"{result['min_date']}.")
    for a in result["accounts"]:
        suffix = f", -{a['discarded']} antes do piso" if a["discarded"] else ""
        lines.append(f"  {a['name']} [{a['type']}] ({a['mode']}): {a['received']} "
                     f"recebidas, +{a['added']} novas, ~{a['updated']} atualizadas{suffix}")
    lines.append(f"\nLedger: {result['total']} transações (+{result['added']} novas, "
                 f"~{result['updated']} atualizadas).")
    if result["discarded"]:
        lines.append(f"{result['discarded']} descartadas por serem anteriores a "
                     f"{result['min_date']}.")
    if result["iof_changed"]:
        lines.append(f"{result['iof_changed']} com IOF estimado somado (Config[iof]).")
    lines += [f"  {line}" for line in result["iof_conflicts"]]
    if result["uncategorized"]:
        lines.append(f"{result['uncategorized']} sem categoria → rode a skill "
                     "finance-categorize.")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Sincroniza transações da Pluggy.")
    ap.add_argument("--backfill", action="store_true", help="Puxa histórico completo (~12 meses).")
    ap.add_argument("--days", type=int, default=BACKFILL_DAYS, help="Janela de backfill (máx ~365).")
    args = ap.parse_args()
    print("Sincronizando com a Pluggy...")
    print("\n".join(summary(run(args.days, args.backfill))))


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
