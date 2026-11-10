"""Backend SQLite local: o que os módulos acima enxergam tem de ser igual ao do Sheets."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from finance import edit, ledger, sheets
from finance.config import ROOT, SEED_DIR


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


def _run(code, tmp_path, **env):
    """Roda o app num processo novo, porque a escolha do backend acontece no import.
    Todas as variáveis vão explícitas (mesmo vazias): o .env de quem roda não interfere."""
    base = {**os.environ, "HOME": str(tmp_path), "XDG_DATA_HOME": "", "APPDATA": "",
            "SHEET_ID": "", "FINANCE_DB": "", "FINANCE_BACKEND": ""}
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**base, **env},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("env, backend", [
    ({}, "sqlite"),                                      # quem chega: sem conta Google
    ({"SHEET_ID": "abc"}, "sheets"),                     # quem já usa a planilha
    ({"SHEET_ID": "abc", "FINANCE_DB": "x.db"}, "sqlite"),
    ({"SHEET_ID": "abc", "FINANCE_BACKEND": "sqlite"}, "sqlite"),
    ({"FINANCE_BACKEND": "sheets"}, "sheets"),
])
def test_backend_choice(tmp_path, env, backend):
    code = "from finance import config, sheets; print(config.BACKEND, sheets.LOCAL)"
    assert _run(code, tmp_path, **env) == f"{backend} {backend == 'sqlite'}"


def test_default_database_lives_in_the_user_data_folder(tmp_path):
    path = _run("from finance import config; print(config.db_path())", tmp_path)
    if sys.platform == "darwin":
        expected = tmp_path / "Library/Application Support/finance-control/finance.db"
    elif sys.platform == "win32":
        expected = tmp_path / "AppData/Roaming/finance-control/finance.db"
    else:
        expected = tmp_path / ".local/share/finance-control/finance.db"
    assert path == str(expected)
    assert not str(path).startswith(str(ROOT)), "o banco não pode ficar dentro do clone"


def test_local_backend_writes_to_the_chosen_file(tmp_path):
    path = tmp_path / "app.db"
    code = ("from finance import ledger, sheets;"
            "sheets.write_records('Ledger', [{'id': 'a', 'date': '2026-05-01'}]);"
            "print(sorted(ledger.load_ledger()), sheets.check()['title'])")
    assert _run(code, tmp_path, FINANCE_DB=str(path)) == "['a'] app.db"
    assert path.exists()
