"""Corrigir só o valor de um lançamento não mexe na classificação dele."""

import json

import pytest

from finance import categorize
from tests.test_pipeline_inmemory import STORE, _seed, install_fake_sheets


@pytest.fixture(autouse=True)
def seeded_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(categorize, "DECISIONS_FILE", tmp_path / ".decisions.json")
    monkeypatch.setattr(categorize, "TO_CATEGORIZE_FILE", tmp_path / ".to_categorize.json")
    install_fake_sheets()
    _seed()


def _apply(assignment):
    categorize.DECISIONS_FILE.write_text(json.dumps({"assignments": [assignment]}))
    categorize.apply(learn=False)
    return {r["id"]: r for r in STORE["Ledger"]}


def _first(predicate):
    return next(r for r in STORE["Ledger"] if predicate(r))


def test_amount_override_alone_keeps_the_category():
    rec = _first(lambda r: r["category"] and not r.get("splits"))

    saved = _apply({"ids": [rec["id"]], "amount_override": -77.0})[rec["id"]]

    assert saved["amount_override"] == -77.0
    assert (saved["category"], saved["subcategory"]) == (rec["category"], rec["subcategory"])
    assert saved["category_source"] == rec["category_source"]


def test_amount_override_alone_keeps_the_split():
    rec = _first(lambda r: r.get("splits"))
    total = round(sum(part["amount"] for part in rec["splits"]), 2)

    saved = _apply({"ids": [rec["id"]], "amount_override": total})[rec["id"]]

    assert saved["amount_override"] == total
    assert saved["splits"] == rec["splits"]
    assert saved["category_source"] == "split"


def test_reapply_brings_an_edited_rule_to_old_transactions_and_keeps_pending_decisions():
    from finance import rules as R

    index, hit = next((i, r) for i, rule in enumerate(STORE["Rules"])
                      for r in STORE["Ledger"] if r["category_source"] != "manual"
                      and not r.get("splits") and R.first_match(r, STORE["Rules"]) == rule)
    STORE["Rules"][index] = {**STORE["Rules"][index], "category": "Lazer",
                             "subcategory": "Viagem"}
    categorize.DECISIONS_FILE.write_text('{"assignments": []}')

    categorize.apply(learn=False, decisions={})

    saved = {r["id"]: r for r in STORE["Ledger"]}[hit["id"]]
    assert (saved["category"], saved["subcategory"]) == ("Lazer", "Viagem")
    assert categorize.DECISIONS_FILE.exists(), "reapply não apaga decisões pendentes"
