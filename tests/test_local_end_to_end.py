"""O app inteiro no banco local, sem conta Google e sem rede: banco de demonstração,
categorização, relatórios e carteira. É o critério de pronto do backend SQLite."""

import json
import sys

from finance import categorize, config, seed
from finance import ledger as L
from finance import report as flow_report
from finance.invest import quote_sources as QS
from finance.invest import quotes as Q
from finance.invest import report as invest_report


def test_demo_runs_end_to_end_on_sqlite(local_db, tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    for module in (config, flow_report, invest_report):
        monkeypatch.setattr(module, "REPORTS_DIR", reports)
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(invest_report, "INVEST_PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(categorize, "DECISIONS_FILE", tmp_path / ".decisions.json")
    monkeypatch.setattr(categorize, "TO_CATEGORIZE_FILE", tmp_path / ".to_categorize.json")
    # cotações: os provedores reais são de rede; aqui todo ativo vale R$ 10
    monkeypatch.setattr(QS, "fetch_all", lambda entries, known=None: (
        {entry["ticker"]: (10.0, "fake") for entry in entries}, {}))

    monkeypatch.setattr(sys, "argv", ["finance.seed"])
    seed.main()
    ledger = L.load_ledger()
    assert len(ledger) == 94

    target = next(r["id"] for r in ledger.values()
                  if not r["category"] and not r.get("splits"))
    categorize.DECISIONS_FILE.write_text(json.dumps({"assignments": [
        {"ids": [target], "category": "Alimentação", "subcategory": "Restaurante"}]}))
    categorize.apply(learn=False)
    assert L.load_ledger()[target]["category"] == "Alimentação"

    flow_report.generate()
    dashboard = json.loads((reports / "dashboard.json").read_text())
    assert dashboard["months"], "relatório do fluxo sem meses"

    carteira = invest_report.generate()
    assert carteira["positions"], "carteira sem posições"
    quoted = {t: q["price"] for t, q in Q.load().items()}
    assert quoted and set(quoted.values()) == {10.0}, "toda cotação veio do provedor"
    assert (reports / "invest.json").exists()
