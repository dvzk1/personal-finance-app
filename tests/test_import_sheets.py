"""Importador da planilha para o banco local: copia tudo, confere e nunca escreve nela."""

import pytest

from finance import backup, import_sheets, sheets, sqlite_store
from finance.config import SEED_DIR
from finance.seed import _read_fixtures

WRITES = ("write_records", "append_rows", "delete_row", "update_fields",
          "update_changed_rows", "write_config", "ensure_tabs", "ensure_current_schema")


@pytest.fixture
def planilha(monkeypatch):
    """Planilha falsa com o banco de demonstração. Qualquer escrita nela falha o teste."""
    fx = _read_fixtures(SEED_DIR)
    tabs = {
        "Ledger": fx["ledger"],
        "Rules": fx["rules"],
        "Taxonomy": [{"Category": c, "Subcategories": ", ".join(s)}
                     for c, s in fx["taxonomy"].items()],
        "Quotes": [{"ticker": "BBAS3", "quote_symbol": "BVMF:BBAS3", "price": 21.5,
                    "currency": "BRL", "kind": "stock_br", "updated_at": 46000.5},
                   {"ticker": "XPTO3", "quote_symbol": "BVMF:XPTO3", "price": "#N/A",
                    "currency": "BRL", "kind": "stock_br", "last_price": 9.0,
                    "last_price_at": "2026-01-02"}],
    }
    settings = {"budgets": fx["budgets"], "timezone": "America/Sao_Paulo",
                "iof": {"rate": 0.035, "match": ["LOJA"]}}

    def read_records(tab):
        if tab not in tabs:
            raise sheets.SheetsError(f"Aba '{tab}' não existe")
        return [dict(r) for r in tabs[tab]]

    def refuse(*args, **kwargs):
        raise AssertionError("o importador escreveu na planilha")

    monkeypatch.setitem(sheets.GOOGLE, "read_records", read_records)
    monkeypatch.setitem(sheets.GOOGLE, "config_keys", lambda: list(settings))
    monkeypatch.setitem(sheets.GOOGLE, "read_config",
                        lambda key, default=None: settings.get(key, default))
    for name in WRITES:
        monkeypatch.setitem(sheets.GOOGLE, name, refuse)
    monkeypatch.setattr(sheets, "SHEET_ID", "planilha-de-teste")
    return tabs, settings


def test_import_copies_everything_and_checks_it(planilha, tmp_path):
    tabs, settings = planilha
    target = tmp_path / "finance.db"

    assert import_sheets.main([str(target)]) == 0

    with sqlite_store.opened(target):
        assert len(sqlite_store.read_records("Ledger")) == len(tabs["Ledger"])
        assert len(sqlite_store.read_records("Rules")) == len(tabs["Rules"])
        assert sqlite_store.read_records("InvestTrades") == [], "aba ausente fica vazia"
        assert sqlite_store.read_config("iof") == settings["iof"]
        assert sqlite_store.read_config("timezone") == "America/Sao_Paulo"
        assert sqlite_store.read_config("schema_version") == sheets.SCHEMA_VERSION


def test_quotes_leave_the_formulas_behind(planilha, tmp_path):
    target = tmp_path / "finance.db"
    import_sheets.main([str(target)])
    with sqlite_store.opened(target):
        quotes = {q["ticker"]: q for q in sqlite_store.read_records("Quotes")}
    assert quotes["BBAS3"]["price"] is None
    assert quotes["BBAS3"]["last_price"] == 21.5, "o preço calculado vira o último bom"
    assert quotes["XPTO3"]["last_price"] == 9.0, "erro da fórmula não apaga o último bom"


def test_existing_database_needs_force_and_is_copied_first(planilha, tmp_path):
    target = tmp_path / "finance.db"
    with sqlite_store.opened(target):
        sqlite_store.write_records("Ledger", [{"id": "meu", "date": "2026-05-01"}])

    with pytest.raises(SystemExit):
        import_sheets.main([str(target)])
    assert import_sheets.main([str(target), "--force"]) == 0

    [saved] = [b for b in backup.list_backups(target) if b["reason"] == "antes-de-importar"]
    with sqlite_store.opened(saved["path"]):
        assert [r["id"] for r in sqlite_store.read_records("Ledger")] == ["meu"]


def test_mismatch_is_reported(planilha, tmp_path, monkeypatch):
    target = tmp_path / "finance.db"
    real_write = import_sheets.write_db

    def lose_a_row(path, tabs, settings, today):
        real_write(path, {**tabs, "Ledger": tabs["Ledger"][1:]}, settings, today)

    monkeypatch.setattr(import_sheets, "write_db", lose_a_row)
    assert import_sheets.main([str(target)]) == 1
    problems = import_sheets.verify(target, *planilha)
    assert any(p.startswith("Ledger:") and "linhas" in p for p in problems)
    assert any("soma" in p for p in problems)


def test_without_a_spreadsheet_it_says_what_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sheets, "SHEET_ID", "")
    with pytest.raises(SystemExit, match="--sheet-id"):
        import_sheets.main([str(tmp_path / "x.db")])
