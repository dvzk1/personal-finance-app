"""`invest sync` com a Pluggy fora do ar num banco: as caixinhas dele ficam intactas."""

from argparse import Namespace

from finance.invest import __main__ as cli
from finance.invest import accounts as ACC
from finance.invest import assets as A
from finance.invest import pluggy_sync as PS
from finance.invest import policy as P
from finance.invest import trades as T
from tests.test_invest_policy import TEMPLATE


class _BrokenApi:
    def __init__(self, client):
        pass

    def investments_list(self, item_id):
        raise RuntimeError("Pluggy fora do ar")


def test_failed_item_is_reported_apart_from_an_empty_one(monkeypatch):
    monkeypatch.setattr(PS.pc, "build_client", lambda key: None)
    monkeypatch.setattr(PS.pc, "get_api_key", lambda: "key")
    monkeypatch.setattr(PS.pluggy_sdk, "InvestmentApi", _BrokenApi)

    assert PS.fetch(["item-1"]) == ([], {"item-1"})


def test_sync_leaves_buckets_alone_when_their_bank_fails(monkeypatch, tmp_path):
    assets = A.load([{"ticker": "RESERVA", "node": "rf", "account": "banco",
                      "valuation": "balance", "target_pct": 1.0}])
    accounts = ACC.load([{"id": "banco", "kind": "bucket", "pluggy_item_id": "item-1"}])
    trades = T.load([{"date": "2026-09-01", "ticker": "RESERVA", "side": "BALANCE",
                      "price": 20000.0}])
    written = []
    monkeypatch.setattr(cli, "_load_state",
                        lambda: (assets, accounts, P.load(TEMPLATE), trades))
    monkeypatch.setattr(cli.Q, "load", lambda: {})
    monkeypatch.setattr(cli.L, "load_ledger", lambda: {})
    monkeypatch.setattr(cli.TX, "load_treatments", lambda: {})
    monkeypatch.setattr(cli.LL, "load_destination_subcategories",
                        lambda: cli.LL.DESTINATION_SUBCATEGORIES)
    monkeypatch.setattr(cli.PS, "fetch", lambda: ([], {"item-1"}))
    monkeypatch.setattr(cli.PS, "fetch_accounts", lambda: [])
    monkeypatch.setattr(cli, "INVEST_PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(cli.T, "append", written.extend)
    monkeypatch.setattr(cli.R, "generate", lambda **kw: None)

    cli.cmd_sync(Namespace(apply_balances=True, apply_income=False))

    assert written == []
