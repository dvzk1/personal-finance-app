"""O servidor sobre o banco de demonstração, pela porta HTTP: quem pode chamar, o que cada
rota grava, o relatório que a tela recebe e o que ela vê quando algo é recusado."""

import base64
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from finance import budgets as B
from finance import categorize, config, edit, report, seed, server, sheets
from finance import ledger as L
from finance.edit_payload import build_decisions
from finance.errors import FinanceError
from finance.invest import operations, quote_sources
from finance.invest import report as invest_report

DASHBOARD = "http://localhost:5273"
SAME_ORIGIN = {"Origin": DASHBOARD, "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    for module in (config, report, invest_report, edit):
        monkeypatch.setattr(module, "REPORTS_DIR", reports)
    monkeypatch.setattr(config, "CLAUDE_QUEUE_FILE", tmp_path / ".claude_queue.jsonl")
    for module in (config, invest_report, operations):
        monkeypatch.setattr(module, "INVEST_PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(categorize, "DECISIONS_FILE", tmp_path / ".decisions.json")
    monkeypatch.setattr(quote_sources, "fetch_all", lambda entries, known=None, fresh=None: (
        {entry["ticker"]: (10.0, "fake") for entry in entries}, {}))
    return reports


@pytest.fixture
def state(local_db, paths, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["finance.seed"])
    seed.main()
    invest_report.generate()
    state = server.AppState(host=server.LOOPBACK, schedule=False)
    yield state
    state.flush_report()


@pytest.fixture
def api(state):
    """O dashboard: mesma origem e com o token da sessão em toda requisição."""
    headers = {**SAME_ORIGIN, "X-Finance-Token": state.session}
    with TestClient(server.create_app(state, dist=None), base_url=DASHBOARD,
                    headers=headers) as client:
        yield client


def _uncategorized():
    return next(r for r in L.load_ledger().values()
                if not r["category"] and not r.get("splits"))


def _month_row(api, record):
    month = api.get(f"/data/{record['date'][:7]}.json").json()
    [row] = [t for t in month["transactions"] if t["id"] == record["id"]]
    return row


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


def test_writes_need_same_origin_and_the_session_token(api, state):
    target = _uncategorized()
    body = {"mode": "value", "ids": [target["id"]], "category": "Lazer"}
    other_site = api.post("/api/edit", headers={"Origin": "https://site.example"}, json=body)
    no_token = api.post("/api/edit", headers={"X-Finance-Token": ""}, json=body)
    stale = api.post("/api/edit", headers={"X-Finance-Token": "de-outra-sessao"}, json=body)
    assert [r.status_code for r in (other_site, no_token, stale)] == [403, 403, 403]
    assert "token da sessão" in stale.json()["error"]
    assert not L.load_ledger()[target["id"]]["category"]

    assert api.get("/api/session").json()["token"] == state.session
    assert api.post("/api/edit", json=body).json()["ok"]


def test_oversized_body_is_refused_before_reading(api):
    response = api.post("/api/budget", content=b"{" + b" " * server.MAX_BODY + b"}",
                        headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_server_mode_needs_a_password_and_then_skips_the_host_check(state, monkeypatch):
    with pytest.raises(FinanceError, match="FINANCE_TOKEN"):
        server.AppState(host="0.0.0.0", token="curta")

    remote = server.AppState(host="0.0.0.0", token="s" * 20, schedule=False)
    client = TestClient(server.create_app(remote, dist=None),
                        base_url="http://finance.tailnet.example:5273")
    assert client.get("/data/dashboard.json").status_code == 401
    wrong = base64.b64encode(b"eu:errada").decode()
    assert client.get("/api/queue", headers={"Authorization": f"Basic {wrong}"}
                      ).status_code == 401
    good = {"Authorization": "Basic " + base64.b64encode(b"eu:" + b"s" * 20).decode()}
    assert client.get("/data/dashboard.json", headers=good).json()["months"]


# ---------------------------------------------------------------------- relatórios
def test_flow_reports_are_computed_without_files(api, paths):
    assert api.get("/data/dashboard.json").json()["total_transactions"] == 94
    assert api.get("/data/rules.json").json()["rules"]
    assert api.get("/data/1999-01.json").status_code == 404
    assert not (paths / "dashboard.json").exists()
    assert api.get("/data/invest.json").json()["positions"], "carteira lida do disco"
    assert api.get("/data/nao-existe.json").status_code == 404
    escape = api.get("/data/..%2F..%2Fpyproject.toml")
    assert escape.status_code == 400 and "finance-control" not in escape.text


def test_report_follows_writes_from_another_process(api, local_db):
    """O agente aplica pela CLI enquanto a tela está aberta: o próximo recarregar mostra."""
    target = _uncategorized()
    assert _month_row(api, target)["category"] is None
    other = sqlite3.connect(local_db)
    with other:
        other.execute('UPDATE "Ledger" SET category = ? WHERE id = ?', ("Lazer", target["id"]))
    other.close()
    assert _month_row(api, target)["category"] == "Lazer"


# -------------------------------------------------------------------------- edição
def test_value_edit_shows_at_once_and_reaches_the_files(api, state, paths):
    target = _uncategorized()
    api.get("/data/dashboard.json")     # relatório em cache antes de editar
    response = api.post("/api/edit", json={
        "mode": "value", "ids": [target["id"]], "category": "Lazer", "note": "show"})
    assert response.json()["ok"], response.text
    saved = L.load_ledger()[target["id"]]
    assert (saved["category"], saved["note"], saved["category_source"]) == (
        "Lazer", "show", "manual")
    assert _month_row(api, target)["category"] == "Lazer", "sem esperar o arquivo"

    state.flush_report()
    month = json.loads((paths / f"{target['date'][:7]}.json").read_text())
    assert [t["category"] for t in month["transactions"] if t["id"] == target["id"]] == [
        "Lazer"], "o agente lê os arquivos"


def test_rule_edit_creates_the_rule_and_classifies(api):
    target = _uncategorized()
    words = target["description"].split()[0]
    response = api.post("/api/edit", json={
        "mode": "rule", "ids": [target["id"]], "category": "Lazer", "note": None,
        "rule": {"field": "description", "match": "contains", "value": words}})
    assert response.json()["new_rules"] == 1
    assert words in [r["value"] for r in sheets.read_records("Rules")]
    assert L.load_ledger()[target["id"]]["category"] == "Lazer"


def test_queue_round_trip(api):
    api.post("/api/edit", json={
        "mode": "queue", "ids": ["a"], "note": "o que é isso?", "category": "Lazer"})
    [item] = api.get("/api/queue").json()["items"]
    assert (item["i"], item["note"], item["suggestion"]["category"]) == (
        0, "o que é isso?", "Lazer")

    api.post("/api/queue/update", json={"index": 0, "note": "outra"})
    assert api.get("/api/queue").json()["items"][0]["note"] == "outra"
    api.post("/api/queue/remove", json={"index": 0})
    assert api.get("/api/queue").json()["items"] == []


def test_refusals_reach_the_screen_as_messages(api):
    cases = [
        (api.post("/api/edit", json={"mode": "value"}), "sem ids"),
        (api.post("/api/edit", json={"mode": "value", "ids": ["x"]}), "nada para gravar"),
        (api.post("/api/edit", json={"mode": "value", "ids": ["x"],
                                     "category": "Inexistente"}), "categoria inexistente"),
        (api.post("/api/edit", json={"mode": "reimburse", "ids": ["x"], "links": [
            {"credit_id": "nao", "debit_id": "existe", "amount": 5}]}),
         "Nenhum abatimento gravado"),
        (api.post("/api/budget", json={"income_plan": {}}), "spending"),
        (api.post("/api/budget", content=b"{nope"), "JSON inválido"),
    ]
    for response, message in cases:
        assert response.status_code == 400, response.text
        assert response.json()["ok"] is False and message in response.json()["error"]


def test_unexpected_error_keeps_the_details_on_the_server(api, monkeypatch):
    def boom(data):
        raise RuntimeError("caminho/secreto")
    monkeypatch.setattr(B, "set_budget", boom)
    response = api.post("/api/budget", json={"spending": {}})
    assert response.status_code == 500
    assert "secreto" not in response.text and "Traceback" not in response.text


# -------------------------------------------------------------- planejamento e carteira
def test_budget_is_saved_and_reported(api):
    plan = B.load()
    plan["spending"]["recurring"]["Lazer"] = 321
    api.get("/data/dashboard.json")
    assert api.post("/api/budget", json=plan).json()["ok"]
    assert B.load()["spending"]["recurring"]["Lazer"] == 321
    dashboard = api.get("/data/dashboard.json").json()
    assert dashboard["budgets"]["spending"]["recurring"]["Lazer"] == 321


def test_invest_apply_saves_and_refuses_an_unbalanced_destination(api):
    response = api.post("/api/invest/apply",
                        json={"contribution": 2500, "contribution_mode": "spread"})
    assert response.json()["saved"]["contribution"] == 2500
    assert api.get("/data/invest.json").json()["contribution"] == 2500

    aporte = next(r for r in L.load_ledger().values() if r["subcategory"] == "Aporte")
    response = api.post("/api/edit", json={
        "mode": "destination", "ids": [aporte["id"]],
        "destinations": [{"ticker": "NAO-EXISTE", "amount": 1}]})
    assert response.status_code == 400 and response.json()["error"]


def test_invest_refresh_and_pending(api):
    assert api.post("/api/invest/refresh").json()["ok"]
    assert api.get("/api/invest/pending").json() == {"pending": []}
    config.INVEST_PENDING_FILE.write_text(json.dumps({"pending": [{"message": "x"}]}))
    assert api.get("/api/invest/pending").json()["pending"] == [{"message": "x"}]


# ------------------------------------------------------------- sincronização agendada
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("upcoming,expected", [
    ([NOW + timedelta(hours=9), NOW + timedelta(hours=3)], NOW + timedelta(hours=3, minutes=15)),
    ([NOW - timedelta(hours=1)], NOW + timedelta(hours=6)),   # já passou: intervalo
    ([], NOW + timedelta(hours=6)),                            # Pluggy não informou
    ([NOW - timedelta(minutes=10)], NOW + timedelta(minutes=15)),  # nunca em sequência
])
def test_next_run_follows_the_pluggy_clock(upcoming, expected):
    assert server.SyncScheduler.next_run(NOW, upcoming) == expected


def test_scheduled_run_reports_its_outcome_and_refreshes_the_report(api, state, monkeypatch):
    target = _uncategorized()
    api.get("/data/dashboard.json")

    def job():
        L.save_ledger({**L.load_ledger(), target["id"]: {
            **L.load_ledger()[target["id"]], "category": "Lazer"}})
        return {"added": 1}
    monkeypatch.setattr(state.scheduler, "job", job)
    state.scheduler.run_once()
    status = api.get("/api/sync/status").json()
    assert (status["last_ok"], status["last_result"], status["running"]) == (
        True, {"added": 1}, False)
    assert _month_row(api, target)["category"] == "Lazer"

    def broken():
        raise RuntimeError("Pluggy fora do ar")
    monkeypatch.setattr(state.scheduler, "job", broken)
    state.scheduler.run_once()
    status = api.get("/api/sync/status").json()
    assert (status["last_ok"], status["last_error"]) == (False, "Pluggy fora do ar")
    assert api.post("/api/sync/run").status_code == 409, "desligada neste teste"


# ------------------------------------------------------------------ o app em si
def test_serves_the_build_without_hiding_the_api(state, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    client = TestClient(server.create_app(state, dist=dist), base_url=DASHBOARD)
    assert "root" in client.get("/").text
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/api/queue").json() == {"items": []}


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
