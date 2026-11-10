"""Backend SQLite local: o que os módulos acima enxergam tem de ser igual ao do Sheets.

Rode: uv run --with pytest python -m pytest tests/test_sqlite_store.py -q
"""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from finance import edit, ledger, sheets, sqlite_store
from finance.config import ROOT, SEED_DIR
from finance.invest import quote_fetch, quotes


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    """Liga o backend local num arquivo temporário, como FINANCE_DB faz no import."""
    path = tmp_path / "finance.db"
    monkeypatch.setattr(sqlite_store, "FINANCE_DB", str(path))
    sqlite_store.reset_cache()
    for name in sqlite_store.INTERFACE:
        monkeypatch.setattr(sheets, name, getattr(sqlite_store, name))
    yield path
    sqlite_store.reset_cache()


def _as_sheets(schema, record):
    """O que a planilha devolveria depois de gravar e reler este registro."""
    out = {}
    for name, kind in schema:
        cell = sheets._val_to_cell(kind, record.get(name))
        out[name] = sheets._cell_to_val(kind, cell)
    return out


def _seed_ledger():
    lines = (SEED_DIR / "ledger.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _seed_rules():
    return json.loads((SEED_DIR / "rules.json").read_text())["rules"]


@pytest.mark.parametrize("tab, load", [("Ledger", _seed_ledger), ("Rules", _seed_rules)])
def test_reads_back_the_same_values_and_types_as_sheets(local_db, tab, load):
    records = load()
    split = {"amount": -1.5, "category": "Lazer", "subcategory": None, "note": "ç"}
    if tab == "Ledger":
        records[0] = {**records[0], "splits": [split], "tags": ["Viagem"],
                      "amount_override": -77.0, "excluded": True, "mcc": 5411}

    sheets.write_records(tab, records)

    expected = [_as_sheets(sheets.SCHEMAS[tab], record) for record in records]
    assert sheets.read_records(tab) == expected


def test_edit_reaches_the_record_it_named(local_db):
    rows = [{"id": name, "date": "2026-05-01", "description": name, "amount": 1.0,
             "signed_amount": -1.0} for name in ("a", "b", "c")]
    sheets.write_records("Ledger", rows)

    result = edit.apply_edits([{"ids": ["b"], "fields": {"note": "só este"}}])

    assert result["missing"] == []
    notes = {record["id"]: record["note"] for record in sheets.read_records("Ledger")}
    assert notes == {"a": None, "b": "só este", "c": None}


def test_partial_save_follows_the_id_when_stored_order_differs(local_db):
    stored = [{"id": "z", "date": "2026-05-02", "description": "z", "note": "z antigo"},
              {"id": "x", "date": "2026-05-01", "description": "x", "note": "x antigo"}]
    sheets.write_records("Ledger", stored)

    records = ledger.load_ledger()
    records["x"]["note"] = "x novo"
    ledger.save_ledger(records, changed_ids={"x"})

    notes = {record["id"]: record["note"] for record in sheets.read_records("Ledger")}
    assert notes == {"z": "z antigo", "x": "x novo"}


def test_partial_save_keeps_a_record_that_was_not_stored_yet(local_db):
    sheets.write_records("Ledger", [{"id": "a", "date": "2026-05-01", "description": "a"}])

    records = ledger.load_ledger()
    records["b"] = {"id": "b", "date": "2026-05-02", "description": "b"}
    ledger.save_ledger(records, changed_ids={"b"})

    assert [record["id"] for record in sheets.read_records("Ledger")] == ["a", "b"]


def test_config_keeps_plain_text_and_json(local_db):
    sheets.write_config("timezone", "America/Sao_Paulo")
    sheets.write_config("budgets", {"spending": {"recurring": {"Lazer": 300}}})
    sheets.write_config("budgets", {"spending": {"recurring": {"Lazer": 450}}})

    assert sheets.read_config("timezone") == "America/Sao_Paulo"
    assert sheets.read_config("budgets") == {"spending": {"recurring": {"Lazer": 450}}}
    assert sheets.read_config("ausente", "padrão") == "padrão"


def test_schema_upgrade_adds_columns_without_losing_rows(local_db):
    conn = sqlite3.connect(local_db)
    conn.execute('CREATE TABLE "Ledger" ("id" TEXT, "description" TEXT)')
    conn.execute("INSERT INTO \"Ledger\" VALUES ('antigo', 'PADARIA')")
    conn.commit()
    conn.close()

    changes = sheets.ensure_current_schema()

    assert "coluna Ledger.settle_with" in changes
    [record] = sheets.read_records("Ledger")
    assert (record["id"], record["description"], record["settle_with"]) == (
        "antigo", "PADARIA", None)
    assert sheets.read_config("schema_version") == sheets.SCHEMA_VERSION


def test_quotes_are_priced_in_reais_and_survive_a_failed_refresh(local_db, monkeypatch):
    quotes.ensure([{"ticker": "BBAS3", "kind": "stock_br"},
                   {"ticker": "VOO", "kind": "stock_us"}])
    assert quotes.load()["BBAS3"]["price"] is None, "fórmula não vira preço nem texto"

    asked = []
    prices = {"BBAS3.SA": 20.0, "VOO": 100.0, "USDBRL=X": 5.0}

    def fake_fetch(symbol):
        asked.append(symbol)
        return prices[symbol]

    monkeypatch.setattr(quote_fetch, "fetch_price", fake_fetch)
    assert quote_fetch.refresh() == {"updated": ["BBAS3", "VOO"], "failed": []}
    loaded = quotes.load()
    assert (loaded["BBAS3"]["price"], loaded["VOO"]["price"]) == (20.0, 500.0)
    assert not loaded["VOO"]["stale"]

    asked.clear()
    quote_fetch.refresh()
    assert asked == [], "cotação recém-buscada não é pedida de novo"

    def offline(symbol):
        raise OSError("sem rede")

    monkeypatch.setattr(quote_fetch, "FRESH_FOR", quote_fetch.FRESH_FOR * 0)
    monkeypatch.setattr(quote_fetch, "fetch_price", offline)
    assert quote_fetch.refresh() == {"updated": [], "failed": ["BBAS3", "VOO"]}
    assert quotes.load()["VOO"]["price"] == 500.0


def test_finance_db_env_switches_the_whole_app_to_the_local_file(tmp_path):
    path = tmp_path / "app.db"
    env = {**os.environ, "FINANCE_DB": str(path), "SHEET_ID": ""}
    code = ("from finance import ledger, sheets;"
            "sheets.write_records('Ledger', [{'id': 'a', 'date': '2026-05-01'}]);"
            "print(sorted(ledger.load_ledger()), sheets.check()['title'])")

    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                            capture_output=True, text=True)

    assert result.stdout.strip() == "['a'] app.db", result.stderr
    assert path.exists()
