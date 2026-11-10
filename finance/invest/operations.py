"""Operações da carteira que gravam: aplicar decisões, conferir com a Pluggy e cotar.

Cada uma recebe dados e devolve o que fez, sem imprimir e sem ler arquivo de trabalho.
A CLI (`__main__`) formata o resultado; o servidor devolve o mesmo dicionário.
"""

import json
from datetime import date

from .. import ledger as L
from .. import sheets
from .. import taxonomy as TX
from ..config import INVEST_PENDING_FILE
from . import accounts as ACC
from . import assets as A
from . import decisions as D
from . import ledger_link as LL
from . import pluggy_sync as PS
from . import policy as P
from . import portfolio as PF
from . import quote_sources as QS
from . import quotes as Q
from . import report as R
from . import trades as T


def _load_state():
    return A.load(), ACC.load(), P.load(), T.load()


def apply(data: dict) -> dict:
    """Aplica decisões (o formato do `.invest_decisions.json`) e regera os relatórios.

    Devolve quantos itens foram gravados em cada aba, os problemas encontrados
    (`problems`, inclui os de destino) e `link_problems`, os destinos que não fecharam:
    quem chama decide se isso é falha."""
    assets, accounts, tree, trades = _load_state()
    records = treatments = None
    subcategories = LL.DESTINATION_SUBCATEGORIES
    if D.needs_ledger(data):
        records = L.load_ledger()
        treatments = TX.load_treatments()
        subcategories = LL.load_destination_subcategories()
    result = D.plan_changes(data, assets, accounts, tree, trades, records, treatments,
                            subcategories)

    saved = {}
    if result["accounts"] != accounts:
        ACC.save(result["accounts"])
        saved["accounts"] = len(result["accounts"])
    if result["policy"] != tree:
        P.save(result["policy"])
        saved["policy"] = len(result["policy"])
    if result["assets"] != assets:
        A.save(result["assets"])
        saved["assets"] = len(result["assets"])
    if result["removed_trades"]:
        kept = [trade for trade in trades if trade["id"] not in result["removed_trades"]]
        T.save(kept + result["trades"])
        saved["trades_replaced"] = {"removed": len(result["removed_trades"]),
                                    "added": len(result["trades"])}
    elif result["trades"]:
        T.append(result["trades"])
        saved["trades"] = len(result["trades"])
    if result["contribution_settings"]:
        settings = result["contribution_settings"]
        sheets.write_config(R.CONTRIBUTION_KEY, settings["amount"])
        sheets.write_config(R.CONTRIBUTION_MODE_KEY, settings["mode"])
        saved["contribution"] = settings["amount"]
    if result["allocation_sim"] is not None:
        sheets.write_config(R.ALLOCATION_SIM_KEY, result["allocation_sim"])
        saved["allocation_sim"] = len(result["allocation_sim"]["items"])
    pending = Q.missing(Q.load(), result["assets"])
    if pending:
        Q.ensure(pending)
        saved["quotes_created"] = [entry["ticker"] for entry in pending]

    invest_report = R.generate()
    flow_report = None
    if records is not None:
        from .. import report as flow
        flow_report = flow.generate(recs=list(records.values()))
    return {"saved": saved, "problems": result["problems"],
            "link_problems": result["link_problems"],
            "invest_report": invest_report, "flow_report": flow_report}


def sync(apply_balances: bool = False, apply_income: bool = False,
         today: str | None = None) -> dict:
    """Confere a carteira com o que as corretoras informam e grava as pendências em
    INVEST_PENDING_FILE. Só escreve na carteira o que for pedido: saldos das corretoras
    (`apply_balances`) e proventos reconhecidos no extrato (`apply_income`)."""
    assets, accounts, tree, trades = _load_state()
    positions = PF.build(assets, trades, Q.load())
    today = today or date.today().isoformat()

    records = list(L.load_ledger().values())
    destinations = LL.destination_pending(records, trades, TX.load_treatments(), assets,
                                          LL.load_destination_subcategories())

    investments, failed_items = PS.fetch()
    bucket_items = {account["pluggy_item_id"] for account in accounts.values()
                    if account["kind"] == "bucket" and account["pluggy_item_id"]}
    pending = PS.reconcile(investments, positions, assets, today, bucket_items)
    pending.extend(PS.reconcile_accounts(PS.fetch_accounts(), positions, assets, today,
                                         records))
    pending.extend(destinations)

    buckets, unchecked = {}, []
    for account in accounts.values():
        if account["kind"] != "bucket" or not account["pluggy_item_id"]:
            continue
        if str(account["pluggy_item_id"]) in failed_items:
            # Sem a resposta do banco não existe total para ratear. Somar zero aqui
            # faria o `apply_balances` gravar saldo zero em todas as caixinhas.
            unchecked.append(account["name"])
            continue
        total = PS.account_total(investments, account["pluggy_item_id"])
        held = PS.bucket_balances(positions, assets, account["id"])
        report = PS.bucket_report(total, held)
        report["blocked"] = len(PS.blocking_destinations(destinations,
                                                         account["pluggy_item_id"]))
        report["name"] = account["name"]
        buckets[account["id"]] = report

    income, income_pending = LL.income_from_ledger(records, assets, positions, trades)
    pending.extend(income_pending)

    updates = PS.suggested_trades(pending)
    for report in buckets.values():
        updates.extend(T.normalize(row) for row in PS.bucket_updates(report, today))

    INVEST_PENDING_FILE.write_text(json.dumps(
        {"generated_at": today, "pending": pending, "buckets": buckets,
         "suggested_trades": updates, "income": income},
        ensure_ascii=False, indent=2) + "\n")

    to_write = []
    if apply_balances:
        to_write += [t for t in updates if t["side"] == "BALANCE"]
    if apply_income:
        to_write += income
    invest_report = None
    if to_write:
        T.append(to_write)
        invest_report = R.generate()
    return {"pending": pending, "buckets": buckets, "unchecked_buckets": unchecked,
            "income": income, "suggested_trades": updates, "written": len(to_write),
            "invest_report": invest_report}


def refresh_quotes(force: bool = False) -> dict | None:
    """Busca as cotações agora. Na planilha quem cota é o GOOGLEFINANCE, então devolve
    None; no banco local é o mesmo refresh que o relatório roda, sem esperar."""
    if not sheets.LOCAL:
        return None
    pending = Q.missing(Q.load(), A.load())
    if pending:
        Q.ensure(pending)
    return QS.refresh(force=force)
