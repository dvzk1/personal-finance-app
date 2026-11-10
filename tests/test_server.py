"""A API do dashboard sobre o banco de demonstração, pela porta HTTP: quem pode chamar,
o que cada rota grava e o que a tela recebe quando algo é recusado."""

import json
import sys

import pytest
from fastapi.testclient import TestClient

from finance import budgets as B
from finance import categorize, config, edit, report, seed, server, sheets
from finance import ledger as L
from finance.edit_payload import build_decisions
from finance.invest import operations, quote_sources
from finance.invest import report as invest_report

DASHBOARD = "http://localhost:5273"
SAME_ORIGIN = {"Origin": DASHBOARD, "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def api(local_db, tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    for module in (config, report, invest_report, edit):
        monkeypatch.setattr(module, "REPORTS_DIR", reports)
    monkeypatch.setattr(config, "CLAUDE_QUEUE_FILE", tmp_path / ".claude_queue.jsonl")
    monkeypatch.setattr(config, "INVEST_PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(invest_report, "INVEST_PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(operations, "INVEST_PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(categorize, "DECISIONS_FILE", tmp_path / ".decisions.json")
    monkeypatch.setattr(quote_sources, "fetch_all", lambda entries, known=None, fresh=None: (
        {entry["ticker"]: (10.0, "fake") for entry in entries}, {}))
    monkeypatch.setattr(sys, "argv", ["finance.seed"])
    seed.main()
    report.generate()
    invest_report.generate()
    with TestClient(server.app, base_url=DASHBOARD) as client:
        yield client
    server.flush_report()


def _uncategorized():
    return next(r for r in L.load_ledger().values()
                if not r["category"] and not r.get("splits"))


# ------------------------------------------------------------------ quem pode chamar
@pytest.mark.parametrize("method,host,origin,site,refused", [
    ("GET", "localhost:5273", DASHBOARD, "same-origin", None),
    ("POST", "localhost:5273", DASHBOARD, "same-origin", None),
    ("POST", "127.0.0.1:5273", "http://127.0.0.1:5273", None, None),
    ("POST", "[::1]:5273", "http://[::1]:5273", None, None),
    ("POST", "localhost:5273", None, None, None),           # curl, script local
    ("POST", "localhost:5273", "https://site.example", None, "origem"),
    ("POST", "localhost:5273", None, "cross-site", "outro site"),
    ("POST", "localhost:5273", "http://localhost:3000", None, "origem"),  # outra porta
    ("GET", "site.example:5273", "http://site.example:5273", "same-origin", "host"),
    ("POST", "site.example:5273", "http://site.example:5273", "same-origin", "host"),
    ("GET", "", None, None, "host"),
    ("GET", "localhost.site.example", None, None, "host"),  # DNS rebinding
])
def test_who_may_call(method, host, origin, site, refused):
    reason = server.reject_reason(method, host, origin, site)
    assert (reason is None) if refused is None else (refused in reason)


def test_another_site_cannot_write(api):
    target = _uncategorized()
    response = api.post("/api/edit", headers={"Origin": "https://site.example"},
                        json={"mode": "value", "ids": [target["id"]], "category": "Lazer"})
    assert response.status_code == 403
    assert not L.load_ledger()[target["id"]]["category"]


# ---------------------------------------------------------------------- relatórios
def test_reports_are_served_and_paths_stay_inside(api):
    assert api.get("/data/dashboard.json").json()["total_transactions"] == 94
    assert api.get("/data/nao-existe.json").status_code == 404
    escape = api.get("/data/..%2F..%2Fpyproject.toml")
    assert escape.status_code == 400 and "finance-control" not in escape.text


# -------------------------------------------------------------------------- edição
def test_value_edit_writes_the_row_and_refreshes_the_report(api):
    target = _uncategorized()
    response = api.post("/api/edit", headers=SAME_ORIGIN, json={
        "mode": "value", "ids": [target["id"]], "category": "Lazer", "note": "show"})
    assert response.json()["ok"], response.text
    saved = L.load_ledger()[target["id"]]
    assert (saved["category"], saved["note"], saved["category_source"]) == (
        "Lazer", "show", "manual")

    server.flush_report()
    month = api.get(f"/data/{target['date'][:7]}.json").json()
    [row] = [t for t in month["transactions"] if t["id"] == target["id"]]
    assert row["category"] == "Lazer"


def test_rule_edit_creates_the_rule_and_classifies(api):
    target = _uncategorized()
    words = target["description"].split()[0]
    response = api.post("/api/edit", headers=SAME_ORIGIN, json={
        "mode": "rule", "ids": [target["id"]], "category": "Lazer", "note": None,
        "rule": {"field": "description", "match": "contains", "value": words}})
    assert response.json()["new_rules"] == 1
    assert words in [r["value"] for r in sheets.read_records("Rules")]
    assert L.load_ledger()[target["id"]]["category"] == "Lazer"


def test_queue_round_trip(api):
    api.post("/api/edit", headers=SAME_ORIGIN, json={
        "mode": "queue", "ids": ["a"], "note": "o que é isso?", "category": "Lazer"})
    [item] = api.get("/api/queue").json()["items"]
    assert (item["i"], item["note"], item["suggestion"]["category"]) == (
        0, "o que é isso?", "Lazer")

    api.post("/api/queue/update", headers=SAME_ORIGIN, json={"index": 0, "note": "outra"})
    assert api.get("/api/queue").json()["items"][0]["note"] == "outra"
    api.post("/api/queue/remove", headers=SAME_ORIGIN, json={"index": 0})
    assert api.get("/api/queue").json()["items"] == []


def test_refusals_reach_the_screen_as_messages(api):
    def post(url, body):
        return api.post(url, headers=SAME_ORIGIN, json=body)

    cases = [
        (post("/api/edit", {"mode": "value"}), "sem ids"),
        (post("/api/edit", {"mode": "value", "ids": ["x"]}), "nada para gravar"),
        (post("/api/edit", {"mode": "value", "ids": ["x"], "category": "Inexistente"}),
         "categoria inexistente"),
        (post("/api/edit", {"mode": "reimburse", "ids": ["x"], "links": [
            {"credit_id": "nao", "debit_id": "existe", "amount": 5}]}),
         "Nenhum abatimento gravado"),
        (post("/api/budget", {"income_plan": {}}), "spending"),
        (api.post("/api/budget", headers=SAME_ORIGIN, content=b"{nope"), "JSON inválido"),
    ]
    for response, message in cases:
        assert response.status_code == 400, response.text
        assert response.json()["ok"] is False and message in response.json()["error"]


def test_unexpected_error_keeps_the_details_on_the_server(api, monkeypatch):
    def boom(data):
        raise RuntimeError("caminho/secreto")
    monkeypatch.setattr(B, "set_budget", boom)
    response = api.post("/api/budget", headers=SAME_ORIGIN, json={"spending": {}})
    assert response.status_code == 500
    assert "secreto" not in response.text and "Traceback" not in response.text


# -------------------------------------------------------------- planejamento e carteira
def test_budget_is_saved_and_reported(api):
    plan = B.load()
    plan["spending"]["recurring"]["Lazer"] = 321
    assert api.post("/api/budget", headers=SAME_ORIGIN, json=plan).json()["ok"]
    assert B.load()["spending"]["recurring"]["Lazer"] == 321
    dashboard = api.get("/data/dashboard.json").json()
    assert dashboard["budgets"]["spending"]["recurring"]["Lazer"] == 321


def test_invest_apply_saves_and_refuses_an_unbalanced_destination(api):
    response = api.post("/api/invest/apply", headers=SAME_ORIGIN,
                        json={"contribution": 2500, "contribution_mode": "spread"})
    assert response.json()["saved"]["contribution"] == 2500
    assert api.get("/data/invest.json").json()["contribution"] == 2500

    aporte = next(r for r in L.load_ledger().values() if r["subcategory"] == "Aporte")
    response = api.post("/api/edit", headers=SAME_ORIGIN, json={
        "mode": "destination", "ids": [aporte["id"]],
        "destinations": [{"ticker": "NAO-EXISTE", "amount": 1}]})
    assert response.status_code == 400 and response.json()["error"]


def test_invest_refresh_and_pending(api):
    assert api.post("/api/invest/refresh", headers=SAME_ORIGIN).json()["ok"]
    assert api.get("/api/invest/pending").json() == {"pending": []}
    config.INVEST_PENDING_FILE.write_text(json.dumps({"pending": [{"message": "x"}]}))
    assert api.get("/api/invest/pending").json()["pending"] == [{"message": "x"}]


# --------------------------------------------------------- tradução da edição em regra
RULE = {"field": "description", "match": "contains", "value": "PADARIA"}


def test_rule_mode_leaves_an_untouched_note_out():
    decisions = build_decisions({"mode": "rule", "ids": ["a", "b"], "category": "Alimentação",
                                 "subcategory": "Café/Padaria", "note": None, "rule": RULE})
    assert "note" not in decisions["assignments"][0]
    assert decisions["rules"][0]["value"] == "PADARIA"


def test_rule_mode_writes_an_edited_note_even_emptied():
    for note in ("aniversário", ""):
        decisions = build_decisions({"mode": "rule", "ids": ["a"], "category": "Lazer",
                                     "note": note, "rule": RULE})
        assert decisions["assignments"][0]["note"] == note


def test_propagated_rule_note_goes_to_transactions_and_rule():
    decisions = build_decisions({"mode": "rule", "ids": ["a"], "category": "Lazer",
                                 "note": None, "rule": {**RULE, "note": "da esquina",
                                                        "propagate_note": True}})
    assert decisions["assignments"][0]["note"] == "da esquina"
    rule = decisions["rules"][0]
    assert (rule["note"], rule["propagate_note"]) == ("da esquina", True)
