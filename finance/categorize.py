"""Categorização: regras determinísticas + dica da Pluggy + loop com a IA.

Fluxo (orquestrado pela skill finance-categorize):

  uv run python -m finance.categorize            # prepare: aplica regras + mapa Pluggy,
                                                 # escreve data/.to_categorize.json
  -> o agente lê o arquivo, propõe/clusteriza, o usuário confirma, gravamos decisões
  uv run python -m finance.categorize apply --learn   # aplica decisões + aprende regras
  uv run python -m finance.categorize stats      # situação atual

Precedência: manual > regra > mapa-pluggy. `reviewed` indica confirmação humana.
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from statistics import median

from . import ledger as L
from . import pluggy_map
from . import reimbursements as RB
from . import rules as R
from . import sheets
from . import taxonomy as T
from .config import DECISIONS_FILE, TO_CATEGORIZE_FILE, ensure_dirs
from .errors import FinanceError, run_cli

_COMPACT = ("id", "date", "description", "signed_amount", "type", "account_name",
            "pluggy_category", "merchant_name", "counterparty", "mcc",
            "payment_method", "installment", "tags")


def _compact(rec: dict) -> dict:
    return {k: rec[k] for k in _COMPACT}


def _anomaly(rec: dict, rule_id: str, ledger: dict) -> bool:
    """Sinaliza se o valor foge muito do histórico daquela regra."""
    hist = [abs(x["signed_amount"]) for x in ledger.values()
            if x.get("rule_id") == rule_id and x["category"] and x["id"] != rec["id"]]
    if len(hist) < 4:
        return False
    med = median(hist)
    return med > 0 and abs(rec["signed_amount"]) > 4 * med


# --------------------------------------------------------------------------- prepare
def prepare() -> None:
    ensure_dirs()
    sheets.ensure_current_schema()
    tax = T.load()
    rules_data = R.load_rules()
    rules = rules_data["rules"]
    ledger = L.load_ledger()
    if not ledger:
        raise FinanceError("Ledger vazio. Rode antes: uv run python -m finance.sync --backfill")

    by_rule = by_map = still = 0
    for rec in ledger.values():
        if rec["category"] or rec.get("splits"):
            continue
        m = R.first_match(rec, rules)
        if m:
            note = m["note"] if m.get("propagate_note") else rec.get("note")
            rec.update(category=m["category"], subcategory=m["subcategory"],
                       note=note or None,
                       category_source="rule", rule_id=m["id"],
                       needs_review=_anomaly(rec, m["id"], ledger),
                       reviewed=not _anomaly(rec, m["id"], ledger))
            by_rule += 1
            continue
        s = pluggy_map.suggest(rec["pluggy_category"], rec["type"], rec.get("description"))
        if s and s[0] in tax:
            cat, sub = s
            if sub is not None and not T.valid(tax, cat, sub):
                sub = None
            rec.update(category=cat, subcategory=sub, category_source="pluggy-map",
                       rule_id=None, needs_review=False, reviewed=False)
            by_map += 1
            continue
        still += 1

    L.save_ledger(ledger)

    # ---- monta o arquivo de trabalho para o agente
    prov = [r for r in ledger.values()
            if r["category_source"] == "pluggy-map" and not r["reviewed"]]
    clusters: dict = {}
    for r in prov:
        key = (r["category"], r["subcategory"])
        c = clusters.setdefault(key, {"category": r["category"],
                                      "subcategory": r["subcategory"],
                                      "count": 0, "merchants": Counter(), "sum": 0.0})
        c["count"] += 1
        c["sum"] = round(c["sum"] + r["signed_amount"], 2)
        c["merchants"][r["description"][:40]] += 1
    provisional_clusters = sorted(
        ({"category": c["category"], "subcategory": c["subcategory"],
          "count": c["count"], "sum": c["sum"],
          "top_merchants": c["merchants"].most_common(8)}
         for c in clusters.values()),
        key=lambda x: -x["count"])

    need_sub: dict = {}
    for r in (x for x in ledger.values()
              if x["category"] and x["subcategory"] is None and not x["reviewed"]):
        b = need_sub.setdefault(r["category"], {"category": r["category"], "count": 0,
                                                "merchants": Counter(), "ids": []})
        b["count"] += 1
        b["merchants"][r["description"][:40]] += 1
        if len(b["ids"]) < 60:
            b["ids"].append(r["id"])
    needs_subcategory = sorted(
        ({"category": b["category"], "count": b["count"],
          "top_merchants": b["merchants"].most_common(12), "ids_sample": b["ids"]}
         for b in need_sub.values()), key=lambda x: -x["count"])

    uncategorized = [_compact(r) for r in ledger.values()
                     if not r["category"] and not r.get("splits")]
    freq = Counter(R.norm(r["description"]) for r in ledger.values()
                   if not r["category"] and not r.get("splits"))
    needs_review = [{**_compact(r), "category": r["category"],
                     "subcategory": r["subcategory"], "rule_id": r["rule_id"]}
                    for r in ledger.values() if r["needs_review"]]

    src = Counter(r["category_source"] or "uncategorized" for r in ledger.values())
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "taxonomy": tax,
        "summary": {"total": len(ledger), "by_source": dict(src),
                    "provisional_unconfirmed": len(prov),
                    "applied_this_run": {"by_rule": by_rule, "by_pluggy_map": by_map,
                                         "still_uncategorized": still}},
        "provisional_clusters": provisional_clusters,
        "needs_subcategory": needs_subcategory,
        "uncategorized": uncategorized,
        "uncategorized_merchant_freq": freq.most_common(40),
        "needs_review": needs_review,
    }
    TO_CATEGORIZE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    print(f"Aplicado agora: {by_rule} por regra, {by_map} por mapa-Pluggy, "
          f"{still} ainda sem categoria.")
    print(f"Provisórias a confirmar (mapa-Pluggy): {len(prov)} | "
          f"sem subcategoria: {sum(b['count'] for b in need_sub.values())} | "
          f"anomalias: {len(needs_review)}")
    print(f"\nDetalhes em: {TO_CATEGORIZE_FILE}")
    print("Agente: leia esse arquivo, agrupe e proponha ao usuário; depois grave "
          f"as decisões em {DECISIONS_FILE} e rode `apply --learn`.")


# ----------------------------------------------------------------------------- apply
def _statement_trade(row: dict, ledger_id: str) -> dict | None:
    """Movimentação da carteira pedida na linha do extrato (`invest`)."""
    invest = row.get("invest")
    if not invest:
        return None
    amount = abs(float(row["amount"]))
    side = str(invest.get("side") or "DIVIDEND").upper()
    quantity = float(invest.get("quantity") or 0.0)
    price = amount / quantity if quantity and side in ("DIVIDEND", "JCP") else amount
    return {"date": row["date"], "ticker": invest["ticker"], "side": side,
            "quantity": quantity if side in ("DIVIDEND", "JCP") else 0.0,
            "price": price, "account": invest.get("account"), "source": "extrato",
            "ledger_id": ledger_id, "note": row.get("note") or row["description"]}


def _import_statement(ledger: dict, rows: list[dict],
                      warnings: list | None = None) -> tuple[int, list[dict]]:
    """Grava as linhas de extrato novas; as que já existem são puladas e avisadas."""
    added, trades = 0, []
    for row in rows:
        duplicate = L.duplicate_of(ledger, row)
        if duplicate:
            if warnings is not None:
                warnings.append(f"já no Ledger: {row['date']} {row['description']} "
                                f"({duplicate})")
            continue
        record = L.manual_record(row, ledger)
        ledger[record["id"]] = record
        added += 1
        trade = _statement_trade(row, record["id"])
        if trade:
            trades.append(trade)
    return added, trades


def apply_decisions(dec: dict, learn: bool = False, report: bool = False) -> dict:
    """Aplica decisões (o formato do `.decisions.json`) e devolve o que mudou.

    `{}` só reaplica as regras em todo lançamento não manual. Com `report`, regera os
    relatórios reaproveitando o que já está em memória e devolve o resultado em
    `report`. Avisos (divisão que não fecha, abatimento recusado) vêm em `warnings`."""
    sheets.ensure_current_schema()
    tax, treats = T.load_all()   # 1 leitura da Taxonomy p/ taxonomia + tratamentos
    rules_data = R.load_rules()
    ledger = L.load_ledger()
    warnings = []
    # foto do estado antes das mudanças p/ gravar só as linhas que de fato mudarem
    before = {tid: L.snapshot(rec) for tid, rec in ledger.items()}

    # 1) aprende regras novas
    new_rules = []
    for rd in dec.get("rules", []):
        r = R.add_rule(rules_data, rd["field"], rd.get("match", "contains"),
                       rd["value"], rd["category"], rd.get("subcategory"),
                       rd.get("note", ""), rd.get("type"),
                       rd.get("amount_abs_min"), rd.get("amount_abs_max"),
                       bool(rd.get("excluded")), bool(rd.get("propagate_note")),
                       rd.get("instruction", ""))
        new_rules.append(r)
    if learn or new_rules:
        R.save_rules(rules_data)

    # 1b) lançamentos de extrato que a Pluggy não trouxe
    imported, invest_rows = _import_statement(ledger, dec.get("manual_transactions", []),
                                              warnings)

    # 2) reaplica todas as regras (regra vence mapa-pluggy; manual é intocável)
    rules = rules_data["rules"]
    reapplied = 0
    for rec in ledger.values():
        if rec["category_source"] == "manual" or rec.get("splits"):
            continue
        m = R.first_match(rec, rules)
        if m:
            note = m["note"] if m.get("propagate_note") else rec.get("note")
            rec.update(category=m["category"], subcategory=m["subcategory"],
                       note=note or None,
                       category_source="rule", rule_id=m["id"],
                       needs_review=_anomaly(rec, m["id"], ledger),
                       reviewed=not _anomaly(rec, m["id"], ledger))
            if m.get("excluded"):    # regra rasura: tira dos relatórios (retroativo)
                rec["excluded"] = True
            reapplied += 1

    # 3) atribuições explícitas (id a id)
    assigned = 0
    for a in dec.get("assignments", []):
        cat, sub = a.get("category"), a.get("subcategory")
        splits = a.get("splits")
        # Só categoria ou divisão reclassificam o lançamento. Uma decisão que traz
        # apenas `amount_override` corrige o valor e mantém categoria, subcategoria,
        # divisão e origem; antes ela caía no ramo de baixo e apagava tudo isso.
        changes_classification = bool(cat or splits)
        for tid in a["ids"]:
            rec = ledger.get(tid)
            if not rec:
                continue
            if "excluded" in a:
                rec["excluded"] = bool(a["excluded"])
            if "tags_add" in a or "tags_remove" in a:
                rec["tags"] = L.update_tags(
                    rec.get("tags"), a.get("tags_add"), a.get("tags_remove"))
            if "settle_with" in a:
                rec["settle_with"] = (a["settle_with"] or "").strip() or None
            # Valor corrigido à mão (ex.: compra em dólar que a Pluggy gravou sem
            # converter). Entra antes da divisão, que é conferida contra o valor novo.
            if "amount_override" in a:
                rec["amount_override"] = a["amount_override"]
            if not changes_classification:
                # A divisão que já existia foi feita sobre o valor antigo. Não dá para
                # redistribuir as partes sem saber a intenção, então só avisa.
                if rec.get("splits") and "amount_override" in a:
                    tot = round(sum(s["amount"] for s in rec["splits"]), 2)
                    if abs(tot - L.effective_amount(rec)) > 0.01:
                        warnings.append(f"{tid}: a divisão soma {tot} e o valor agora é "
                                        f"{L.effective_amount(rec)}; refaça a divisão")
                if "note" in a:
                    rec["note"] = (a["note"] or None)
                assigned += 1
                continue
            if splits:
                # divide o lançamento; a soma deve bater com o valor efetivo
                eff = L.effective_amount(rec)
                tot = round(sum(s["amount"] for s in splits), 2)
                if abs(tot - eff) > 0.01:
                    warnings.append(f"split de {tid}: soma {tot} ≠ {eff} — ignorado")
                    continue
                rec.update(splits=splits, category_source="split",
                           rule_id=None, needs_review=False, reviewed=True)
                if cat:
                    rec.update(category=cat, subcategory=sub)
            else:
                rec.update(category=cat, subcategory=sub, splits=None,
                           category_source=a.get("source", "manual"),
                           rule_id=None, needs_review=False, reviewed=True)
            if "note" in a:
                rec["note"] = (a["note"] or None)
            assigned += 1

    # 4) confirma as provisórias do mapa-Pluggy
    confirmed = 0
    if dec.get("confirm_provisional"):
        for rec in ledger.values():
            if rec["category_source"] == "pluggy-map" and rec["category"] \
                    and not rec["reviewed"]:
                rec["reviewed"] = True
                confirmed += 1

    # 5) valida contra a taxonomia (não quebra; só reporta)
    invalid = [(r["id"], r["category"], r["subcategory"]) for r in ledger.values()
               if r["category"] and not T.valid(tax, r["category"], r["subcategory"])]

    changed = {tid for tid, rec in ledger.items()
               if L.snapshot(rec) != before.get(tid)}
    L.save_ledger(ledger, changed_ids=None if imported else changed)
    if invest_rows:
        from .invest import trades as invest_trades
        invest_trades.append(invest_rows)
    reimbursed = None
    if dec.get("reimbursements") or dec.get("reimbursements_remove"):
        created, removed, problems = RB.apply_changes(
            ledger, dec.get("reimbursements"), dec.get("reimbursements_remove"))
        warnings += [f"abatimento não gravado: {problem}" for problem in problems]
        reimbursed = {"created": created, "removed": removed}

    result = {"new_rules": len(new_rules), "imported": imported,
              "invest_trades": len(invest_rows), "reapplied": reapplied,
              "assigned": assigned, "confirmed": confirmed, "written": len(changed),
              "reimbursements": reimbursed, "invalid": invalid, "warnings": warnings,
              "stats": _stats(ledger), "report": None}
    # regera os relatórios reaproveitando o ledger e a taxonomia já em memória,
    # em vez de reler tudo do backend
    if report:
        from . import report as RP
        result["report"] = RP.generate(recs=list(ledger.values()), taxonomy=tax,
                                       treatments=treats, rules=rules)
    return result


def summary(result: dict) -> list[str]:
    """Linhas para o terminal com o resultado de `apply_decisions`."""
    lines = [f"  ⚠ {warning}" for warning in result["warnings"]]
    if result["invest_trades"]:
        lines.append(f"Carteira: {result['invest_trades']} movimentação(ões) "
                     "ligadas ao extrato.")
    if result["reimbursements"]:
        lines.append(f"Abatimentos: +{result['reimbursements']['created']} criados, "
                     f"-{result['reimbursements']['removed']} removidos.")
    if result["imported"]:
        lines.append(f"Extrato: +{result['imported']} lançamento(s) novo(s).")
    lines.append(f"Regras novas: {result['new_rules']} | reaplicadas por regra: "
                 f"{result['reapplied']} | atribuições: {result['assigned']} | "
                 f"provisórias confirmadas: {result['confirmed']} | "
                 f"linhas gravadas: {result['written']}")
    invalid = result["invalid"]
    if invalid:
        lines.append(f"\n⚠ {len(invalid)} fora da taxonomia "
                     "(corrija a taxonomia ou as decisões):")
        lines += [f"  {tid}: {c} / {s}" for tid, c, s in invalid[:15]]
    lines.append(_stats_line(result["stats"]))
    if result["report"]:
        from . import report as RP
        lines += RP.summary(result["report"])
    return lines


def apply(learn: bool, do_report: bool = False, decisions: dict | None = None) -> dict:
    """Linha de comando: aplica o arquivo de decisões (ou `decisions`, sem tocar no
    arquivo), imprime o resultado e apaga os arquivos de trabalho."""
    from_file = decisions is None
    if from_file:
        if not DECISIONS_FILE.exists():
            raise FinanceError(f"Não encontrei {DECISIONS_FILE}. O agente deve gravá-lo antes.")
        decisions = json.loads(DECISIONS_FILE.read_text())
    result = apply_decisions(decisions, learn=learn, report=do_report)
    if from_file:
        TO_CATEGORIZE_FILE.unlink(missing_ok=True)
        DECISIONS_FILE.unlink(missing_ok=True)
    print("\n".join(summary(result)))
    return result


# ----------------------------------------------------------------------------- stats
def _stats(ledger: dict) -> dict:
    total = len(ledger)
    cat = sum(1 for r in ledger.values() if r["category"] or r.get("splits"))
    return {"total": total, "categorized": cat, "uncategorized": total - cat,
            "reviewed": sum(1 for r in ledger.values() if r["reviewed"]),
            "needs_review": sum(1 for r in ledger.values() if r["needs_review"])}


def _stats_line(counts: dict) -> str:
    return (f"\nLedger: {counts['total']} | categorizadas: {counts['categorized']} | "
            f"confirmadas: {counts['reviewed']} | sem categoria: "
            f"{counts['uncategorized']} | a revisar: {counts['needs_review']}")


def stats() -> None:
    ledger = L.load_ledger()
    if not ledger:
        raise FinanceError("Ledger vazio.")
    src = Counter(r["category_source"] or "uncategorized" for r in ledger.values())
    print("Por origem:", dict(src))
    by_cat = Counter(r["category"] or "(sem)" for r in ledger.values())
    for c, n in by_cat.most_common():
        print(f"  {n:5d}  {c}")
    print(_stats_line(_stats(ledger)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Categorização (regras + IA).")
    ap.add_argument("command", nargs="?", default="prepare",
                    choices=["prepare", "apply", "reapply", "stats"],
                    help="reapply: reaplica as regras em todo lançamento não manual "
                         "(depois de editar regras) e regera os relatórios")
    ap.add_argument("--learn", action="store_true",
                    help="(apply) persiste as regras novas em rules.json")
    ap.add_argument("--report", action="store_true",
                    help="(apply) já regera os relatórios no mesmo processo")
    args = ap.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "apply":
        apply(args.learn, args.report)
    elif args.command == "reapply":
        apply(learn=False, do_report=True, decisions={})
    else:
        stats()


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
