"""`finance.sync` agora roda sozinho no servidor: o que o agente grava enquanto a Pluggy
responde não pode ser apagado quando o sync grava o Ledger."""

import sys
from contextlib import nullcontext
from types import SimpleNamespace

from finance import categorize, seed, sync
from finance import ledger as L


def test_writes_made_while_pluggy_answers_survive_the_sync(local_db, tmp_path, monkeypatch):
    monkeypatch.setattr(categorize, "DECISIONS_FILE", tmp_path / ".decisions.json")
    monkeypatch.setattr(sys, "argv", ["finance.seed", "--no-invest"])
    seed.main()
    target = next(r for r in L.load_ledger().values() if not r["category"])
    template = dict(target)

    def fetch_transactions(client, account_id, var_from=None, created_at_from=None):
        # o agente aplica uma decisão pela CLI enquanto a Pluggy responde
        categorize.apply_decisions({"assignments": [
            {"ids": [target["id"]], "category": "Lazer"}]})
        return [{**template, "id": "novo-da-pluggy", "description": "NOVO"}]

    account = SimpleNamespace(id="conta-1", name="Conta", marketing_name=None, type="BANK")
    monkeypatch.setattr(sync.pc, "item_ids", lambda: ["item-1"])
    monkeypatch.setattr(sync.pc, "get_api_key", lambda: "key")
    monkeypatch.setattr(sync.pc, "build_client", lambda key: nullcontext())
    monkeypatch.setattr(sync.pc, "list_accounts", lambda client, item: [account])
    monkeypatch.setattr(sync.pc, "fetch_transactions", fetch_transactions)
    monkeypatch.setattr(sync.L, "normalize", lambda tx, acc, item, tz: dict(tx))

    result = sync.run(days=30)

    ledger = L.load_ledger()
    assert result["added"] == 1 and "novo-da-pluggy" in ledger
    assert ledger[target["id"]]["category"] == "Lazer", "a decisão do agente sumiu"
