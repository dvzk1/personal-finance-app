"""Cópias do banco local: diária, antes de regravar tudo, rotação e restauração."""

import sys
from datetime import datetime, timedelta

import pytest

from finance import backup, seed, sheets, sqlite_store
from finance import ledger as L


def _ledger(ids):
    sheets.write_records("Ledger", [{"id": i, "date": "2026-05-01", "description": i}
                                    for i in ids])


def test_copy_restores_the_previous_content(local_db):
    _ledger(["a", "b"])
    saved = backup.create("manual")
    _ledger(["c"])

    before = backup.restore(saved)

    assert sorted(L.load_ledger()) == ["a", "b"]
    assert before and "antes-de-restaurar" in before.name, "restaurar também se desfaz"
    backup.restore(before)
    assert sorted(L.load_ledger()) == ["c"]


def test_list_resolves_by_number_and_name(local_db):
    _ledger(["a"])
    old = backup.create("um", now=datetime(2026, 1, 1, 10))
    new = backup.create("dois", now=datetime(2026, 1, 2, 10))
    names = [b["name"] for b in backup.list_backups()]
    assert names == [new.name, old.name]
    assert backup.resolve("1") == new
    assert backup.resolve(old.name) == old
    with pytest.raises(FileNotFoundError):
        backup.resolve("nao-existe")


def test_daily_copy_happens_once_per_day(local_db):
    _ledger(["a"])
    day = datetime(2026, 3, 10, 9)
    assert backup.daily(now=day) is not None
    assert backup.daily(now=day + timedelta(hours=5)) is None
    assert backup.daily(now=day + timedelta(days=1)) is not None


def test_opening_an_existing_database_makes_the_daily_copy(tmp_path):
    path = tmp_path / "finance.db"
    with sqlite_store.opened(path):
        sqlite_store.write_records("Ledger", [{"id": "a", "date": "2026-05-01"}])
    assert backup.list_backups(path) == [], "banco novo não tem o que copiar"

    sqlite_store._daily_checked.discard(path)
    with sqlite_store.opened(path):
        sqlite_store.read_records("Ledger")
    assert [b["reason"] for b in backup.list_backups(path)] == [backup.DAILY]


def test_rotation_keeps_the_latest_of_each_kind(local_db, monkeypatch):
    _ledger(["a"])
    monkeypatch.setattr(backup, "KEEP_DAILY", 2)
    monkeypatch.setattr(backup, "KEEP_OTHER", 1)
    start = datetime(2026, 1, 1)
    for n in range(4):
        backup.create(backup.DAILY, now=start + timedelta(days=n))
        backup.create("manual", now=start + timedelta(days=n, hours=1))
    kept = backup.list_backups()
    assert [b["reason"] for b in kept] == ["manual", backup.DAILY, backup.DAILY]
    assert kept[0]["created"].day == 4


def test_seed_force_copies_the_database_first(local_db, monkeypatch, tmp_path):
    _ledger(["dado-real"])
    monkeypatch.setattr(sys, "argv", ["finance.seed", "--force", "--no-invest"])
    seed.main()
    [saved] = [b for b in backup.list_backups() if b["reason"] == "antes-do-seed"]
    backup.restore(saved["path"])
    assert sorted(L.load_ledger()) == ["dado-real"]


def test_restore_refuses_what_is_not_a_finance_database(local_db, tmp_path):
    _ledger(["a"])
    other = tmp_path / "outro.db"
    other.write_text("não é sqlite")
    with pytest.raises(ValueError):
        backup.restore(other)


def test_export_and_import_move_the_data_between_machines(local_db, tmp_path):
    _ledger(["daqui"])
    exported = backup.export(tmp_path / "levar.db")
    _ledger(["outra coisa"])
    backup.restore(exported, reason="antes-de-importar")
    assert sorted(L.load_ledger()) == ["daqui"]
