"""O que o servidor vai chamar em processo: recebe dados, devolve resultado, não grava
arquivo de trabalho, não imprime e não encerra o processo."""

import copy
import sys

import pytest

from finance import categorize, config, report, seed
from finance import ledger as L
from finance.errors import run_cli


@pytest.fixture
def demo(local_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(categorize, "DECISIONS_FILE", tmp_path / ".decisions.json")
    monkeypatch.setattr(sys, "argv", ["finance.seed", "--no-invest"])
    seed.main()
    return tmp_path / "reports"


def test_report_build_leaves_the_records_alone_and_writes_nothing(demo):
    records = list(L.load_ledger().values())
    corrected = next(r for r in records if r["signed_amount"] < 0)
    corrected["amount_override"] = -1234.5
    before = copy.deepcopy(records)

    result = report.build(recs=records)

    assert records == before, "o ledger de quem chamou vai ser gravado depois"
    assert not demo.exists()
    month = result["months"][corrected["date"][:7]]
    [shown] = [t for t in month["transactions"] if t["id"] == corrected["id"]]
    assert shown["signed_amount"] == -1234.5


def test_categorize_applies_a_dict_without_files_or_prints(demo, capsys):
    ledger = L.load_ledger()
    target = next(r for r in ledger.values() if not r["category"] and not r.get("splits"))

    result = categorize.apply_decisions({"assignments": [
        {"ids": [target["id"]], "category": "Alimentação", "subcategory": "Restaurante"},
        {"ids": [target["id"]], "splits": [{"amount": 1, "category": "Lazer"}]},
    ]})

    assert L.load_ledger()[target["id"]]["category"] == "Alimentação"
    assert result["assigned"] == 1
    assert [w for w in result["warnings"] if w.startswith(f"split de {target['id']}")]
    assert capsys.readouterr().out == ""
    assert not categorize.DECISIONS_FILE.exists()


def test_cli_shows_a_refusal_without_a_traceback(local_db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["finance.report"])
    assert run_cli(report.main) == 1
    err = capsys.readouterr().err
    assert "Ledger vazio" in err and "Traceback" not in err
