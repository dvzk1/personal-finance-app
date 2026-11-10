"""Provedores de cotação do banco local: ordem, troca de provedor, câmbio e falhas."""

from datetime import date

import pytest

from finance import sheets
from finance.invest import quote_sources as QS
from finance.invest import quotes as Q
from finance.invest.quote_sources import bcb, brapi, yahoo


def _entry(ticker, kind):
    return {"ticker": ticker, "kind": kind, "quote_symbol": Q.symbol_for(ticker, kind)}


# ---------------------------------------------------------------- ordem de tentativa
def test_default_order_skips_a_provider_without_its_key():
    assert QS.order_for("stock_br", env={}) == ["yahoo"]
    assert QS.order_for("stock_br", env={"BRAPI_TOKEN": "t"}) == ["brapi", "yahoo"]
    assert QS.order_for("fx", env={}) == ["bcb", "yahoo"]
    assert QS.order_for("manual", env={}) == []


def test_order_is_configuration_not_code():
    env = {"QUOTE_PROVIDERS": "stock_br=yahoo,brapi; fx=yahoo", "BRAPI_TOKEN": "t"}
    assert QS.order_for("stock_br", env=env) == ["yahoo", "brapi"]
    assert QS.order_for("fx", env=env) == ["yahoo"]
    assert QS.order_for("crypto", env=env) == ["yahoo"], "tipo não citado mantém o padrão"


def test_unknown_or_unfit_provider_is_ignored():
    env = {"QUOTE_PROVIDERS": "stock_us=nao_existe,bcb,yahoo"}
    assert QS.order_for("stock_us", env=env) == ["yahoo"]


def test_a_new_provider_only_needs_to_be_registered(monkeypatch):
    class Fake:
        NAME, KINDS, REQUIRES = "fake", ("stock_us",), None

        @staticmethod
        def fetch(entry):
            return 10.0, "BRL"

    monkeypatch.setitem(QS.PROVIDERS, "fake", Fake)
    monkeypatch.setenv("QUOTE_PROVIDERS", "stock_us=fake")
    assert QS.fetch_native(_entry("VOO", "stock_us")) == (10.0, "BRL", "fake")


# ----------------------------------------------------------- falha e câmbio
def _providers(monkeypatch, **answers):
    """Troca o fetch de cada provedor por uma tabela {símbolo: (preço, moeda)}."""
    for name, table in answers.items():
        def fetch(entry, table=table, name=name):
            key = entry["ticker"]
            if key not in table:
                raise OSError(f"{name} fora do ar")
            return table[key]
        monkeypatch.setattr(QS.PROVIDERS[name], "fetch", fetch)


def test_next_provider_answers_when_the_first_fails(monkeypatch):
    monkeypatch.setenv("BRAPI_TOKEN", "t")
    _providers(monkeypatch, brapi={}, yahoo={"BBAS3": (20.0, "BRL")})
    assert QS.fetch_native(_entry("BBAS3", "stock_br")) == (20.0, "BRL", "yahoo")


def test_every_provider_failing_says_why(monkeypatch):
    monkeypatch.delenv("BRAPI_TOKEN", raising=False)
    _providers(monkeypatch, yahoo={})
    with pytest.raises(LookupError, match="yahoo fora do ar"):
        QS.fetch_native(_entry("BBAS3", "stock_br"))


def test_dollar_assets_are_converted_with_the_rate_fetched_together(monkeypatch):
    _providers(monkeypatch, yahoo={"VOO": (100.0, "USD"), "BTC": (70000.0, "USD")},
               bcb={"USDBRL": (5.0, "BRL")})
    entries = [_entry("VOO", "stock_us"), _entry("BTC", "crypto"), _entry("USDBRL", "fx")]
    priced, failed = QS.fetch_all(entries)
    assert failed == {}
    assert {t: p for t, (p, _) in priced.items()} == {
        "VOO": 500.0, "BTC": 350000.0, "USDBRL": 5.0}


def test_rate_is_fetched_when_no_row_asks_for_it(monkeypatch):
    _providers(monkeypatch, yahoo={"VOO": (100.0, "USD")}, bcb={"USDBRL": (5.5, "BRL")})
    priced, _ = QS.fetch_all([_entry("VOO", "stock_us")])
    assert priced["VOO"][0] == 550.0


def test_last_known_rate_is_used_when_the_rate_fails(monkeypatch):
    _providers(monkeypatch, yahoo={"VOO": (100.0, "USD")}, bcb={})
    priced, failed = QS.fetch_all([_entry("VOO", "stock_us")])
    assert "VOO" in failed and "câmbio" in failed["VOO"]
    priced, failed = QS.fetch_all([_entry("VOO", "stock_us")], {"USDBRL": 5.2})
    assert priced["VOO"][0] == 520.0


# ------------------------------------------------------------- formato de cada fonte
def test_yahoo_symbols():
    assert yahoo.symbol(_entry("WEGE3", "stock_br")) == "WEGE3.SA"
    assert yahoo.symbol(_entry("HGLG11", "fii_br")) == "HGLG11.SA"
    assert yahoo.symbol(_entry("VOO", "stock_us")) == "VOO"
    assert yahoo.symbol(_entry("BTC", "crypto")) == "BTC-USD"
    assert yahoo.symbol(_entry("USDBRL", "fx")) == "USDBRL=X"


@pytest.mark.parametrize("ticker", ["BTC", "BTCUSD", "BTC-USD", "btc/brl", "BTCUSDT"])
def test_crypto_ticker_written_with_the_pair_still_finds_the_coin(ticker):
    assert Q.guess_kind(ticker) == "crypto"
    assert Q.symbol_for(ticker, "crypto") == "CURRENCY:BTCUSD"
    assert yahoo.symbol(_entry(ticker, "crypto")) == "BTC-USD"


def test_yahoo_reads_price_and_currency(monkeypatch):
    payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 687.2,
                                              "currency": "USD"}}]}}
    monkeypatch.setattr(yahoo, "get_json", lambda url, headers=None: payload)
    assert yahoo.fetch(_entry("VOO", "stock_us")) == (687.2, "USD")


def test_brapi_sends_the_token_and_reads_the_price(monkeypatch):
    seen = {}

    def get_json(url, headers=None):
        seen.update(url=url, headers=headers)
        return {"results": [{"regularMarketPrice": 45.08, "currency": "BRL"}]}

    monkeypatch.setenv("BRAPI_TOKEN", "segredo")
    monkeypatch.setattr(brapi, "get_json", get_json)
    assert brapi.fetch(_entry("PETR4", "stock_br")) == (45.08, "BRL")
    assert seen["url"].endswith("/PETR4")
    assert seen["headers"] == {"Authorization": "Bearer segredo"}


def test_bcb_takes_the_latest_bulletin_of_the_week(monkeypatch):
    rows = [{"cotacaoVenda": 5.49, "dataHoraCotacao": "2026-11-09 13:06:31"},
            {"cotacaoVenda": 5.59, "dataHoraCotacao": "2026-11-10 13:06:31"},
            {"cotacaoVenda": 5.50, "dataHoraCotacao": "2026-11-10 10:04:14"}]
    urls = []
    monkeypatch.setattr(bcb, "get_json",
                        lambda url, headers=None: urls.append(url) or {"value": rows})
    assert bcb.fetch(_entry("USDBRL", "fx"), today=date(2026, 11, 10)) == (5.59, "BRL")
    assert "'USD'" in urls[0] and "'11-03-2026'" in urls[0] and "'11-10-2026'" in urls[0]


def test_bcb_only_quotes_against_the_real():
    with pytest.raises(ValueError):
        bcb.fetch({"ticker": "EURUSD", "kind": "fx", "quote_symbol": "CURRENCY:EURUSD"})


# ---------------------------------------------------------- refresh no banco local
def test_refresh_fills_prices_skips_fresh_ones_and_keeps_the_old_on_failure(
        local_db, monkeypatch):
    Q.ensure([_entry("BBAS3", "stock_br"), _entry("VOO", "stock_us")])
    assert Q.load()["BBAS3"]["price"] is None, "linha local nasce sem fórmula nem preço"

    asked = []

    def fetch_all(entries, known=None):
        asked.extend(entry["ticker"] for entry in entries)
        return {"BBAS3": (20.0, "yahoo"), "VOO": (500.0, "yahoo")}, {}

    monkeypatch.setattr(QS, "fetch_all", fetch_all)
    assert QS.refresh() == {"updated": ["BBAS3", "VOO"], "failed": {}}
    loaded = Q.load()
    assert (loaded["BBAS3"]["price"], loaded["VOO"]["price"]) == (20.0, 500.0)
    assert not loaded["VOO"]["stale"]

    asked.clear()
    assert QS.refresh()["updated"] == []
    assert asked == [], "cotação recém-buscada não é pedida de novo"

    monkeypatch.setattr(QS, "fetch_all",
                        lambda entries, known=None: ({}, {"VOO": "fora do ar"}))
    assert QS.refresh(force=True) == {"updated": [], "failed": {"VOO": "fora do ar"}}
    assert Q.load()["VOO"]["price"] == 500.0, "falha não apaga o preço anterior"


def test_refresh_writes_to_the_row_of_each_ticker(local_db, monkeypatch):
    Q.ensure([_entry("BBAS3", "stock_br"), _entry("WEGE3", "stock_br")])
    sheets.update_fields(Q.TAB, [(2, {"ticker": ""})])   # linha sem ticker antes das outras
    Q.ensure([_entry("ITUB3", "stock_br")])
    monkeypatch.setattr(QS, "fetch_all",
                        lambda entries, known=None: ({"ITUB3": (30.0, "yahoo")}, {}))
    QS.refresh()
    assert Q.load()["ITUB3"]["price"] == 30.0
    assert Q.load()["WEGE3"]["price"] is None
