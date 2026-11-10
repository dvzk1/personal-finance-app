"""Administração pela CLI sobre o banco de demonstração: o que antes exigia abrir a planilha."""

import sys

import pytest

from finance import admin, seed, sheets
from finance import budgets as B
from finance import ledger as L
from finance import taxonomy as T
from finance.invest import ledger_link as LL


@pytest.fixture
def demo(local_db, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["finance.seed", "--no-invest"])
    seed.main()
    plan = B.load()
    plan["spending"]["recurring"]["Alimentação"] = 1500
    plan["spending"]["months"]["2026-05"] = {"Alimentação": 1800}
    B.save(plan)


def _pairs(records, cat="category", sub="subcategory"):
    out = []
    for r in records:
        for p in [r] + list(r.get("splits") or []):
            out.append((p.get(cat), p.get(sub)))
    return out


# ------------------------------------------------------------------------ taxonomia
def test_rename_category_reaches_every_place(demo):
    before = _pairs(L.load_ledger().values()).count(("Alimentação", "Restaurante"))

    admin.rename_taxonomy("Alimentação", "Comida")

    assert "Comida" in T.load() and "Alimentação" not in T.load()
    pairs = _pairs(L.load_ledger().values())
    assert not [p for p in pairs if p[0] == "Alimentação"]
    assert pairs.count(("Comida", "Restaurante")) == before, "inclui partes de divisão"
    pmap = _pairs(sheets.read_records("PluggyMap"), "Category", "Subcategory")
    assert ("Comida", "Delivery") in pmap and not [p for p in pmap if p[0] == "Alimentação"]
    plan = B.load()
    assert plan["spending"]["recurring"] == {"Comida": 1500}
    assert plan["spending"]["months"]["2026-05"] == {"Comida": 1800}


def test_rename_subcategory_reaches_rules_and_destinations(demo):
    admin.rename_taxonomy("Impostos/Taxas", "Tributos", sub="Impostos")
    rules = _pairs(sheets.read_records("Rules"))
    assert rules.count(("Impostos/Taxas", "Tributos")) == 2
    assert ("Impostos/Taxas", "Impostos") not in rules

    admin.rename_taxonomy("Investimentos", "Aplicação", sub="Aporte")
    assert LL.load_destination_subcategories() == ("Aplicação", "Resgate")


def test_rename_refuses_a_name_that_exists(demo):
    with pytest.raises(admin.AdminError, match="Já existe"):
        admin.rename_taxonomy("Alimentação", "Lazer")
    with pytest.raises(admin.AdminError, match="já tem"):
        admin.rename_taxonomy("Alimentação", "Delivery", sub="Restaurante")


def test_remove_refuses_what_is_in_use_and_says_where(demo):
    with pytest.raises(admin.AdminError, match="lançamentos.*orçamento"):
        admin.remove_taxonomy("Alimentação")
    admin.add_taxonomy("Pets", ["Ração"], treatment="fluxo")
    admin.remove_taxonomy("Pets", sub="Ração")
    assert T.load()["Pets"] == []
    admin.remove_taxonomy("Pets")
    assert "Pets" not in T.load()


def test_add_and_set(demo):
    admin.add_taxonomy("Lazer", ["Shows"])
    assert T.load()["Lazer"][-1] == "Shows"
    with pytest.raises(admin.AdminError, match="Nada a acrescentar"):
        admin.add_taxonomy("Lazer", ["Shows"])
    admin.set_taxonomy("Lazer", treatment="movimento", color="#112233", essential=True)
    _, treatments, meta = T.load_full()
    assert treatments["Lazer"] == "movimento"
    assert (meta["Lazer"]["color"], meta["Lazer"]["essential"]) == ("#112233", True)
    with pytest.raises(admin.AdminError, match="#rrggbb"):
        admin.set_taxonomy("Lazer", color="azul")


# --------------------------------------------------------------------------- regras
def test_rule_edit_validates_against_the_taxonomy(demo):
    first = sheets.read_records("Rules")[0]["id"]
    admin.set_rule(first, ["category=Lazer", "subcategory=Viagem", "amount_abs_max=50"])
    rule = sheets.read_records("Rules")[0]
    assert (rule["category"], rule["subcategory"], rule["amount_abs_max"]) == (
        "Lazer", "Viagem", 50.0)

    with pytest.raises(admin.AdminError, match="não existe na taxonomia"):
        admin.set_rule(first, ["subcategory=Inexistente"])
    with pytest.raises(admin.AdminError, match="Regex inválida"):
        admin.set_rule(first, ["match=regex", "value=("])
    with pytest.raises(admin.AdminError, match="campo=valor"):
        admin.set_rule(first, ["cor=azul"])

    admin.set_rule(first, ["amount_abs_max="])
    assert sheets.read_records("Rules")[0]["amount_abs_max"] is None


def test_rule_move_and_remove(demo):
    ids = [r["id"] for r in sheets.read_records("Rules")]
    admin.move_rule(ids[-1], 1)
    assert [r["id"] for r in sheets.read_records("Rules")] == [ids[-1]] + ids[:-1]
    admin.remove_rule(ids[0])
    assert ids[0] not in [r["id"] for r in sheets.read_records("Rules")]
    with pytest.raises(admin.AdminError, match="não existe"):
        admin.remove_rule(ids[0])


# ------------------------------------------------------------------- mapa e Config
def test_pluggy_map_set_and_remove(demo):
    admin.set_pluggy_map("Pet shop", "Compras")
    admin.set_pluggy_map("Groceries", "Alimentação", "Delivery")
    rows = {r["PluggyCategory"]: (r["Category"], r["Subcategory"])
            for r in sheets.read_records("PluggyMap")}
    assert rows["Pet shop"] == ("Compras", None)
    assert rows["Groceries"] == ("Alimentação", "Delivery")
    with pytest.raises(admin.AdminError):
        admin.set_pluggy_map("Pet shop", "Inexistente")
    admin.remove_pluggy_map("Pet shop")
    assert "Pet shop" not in {r["PluggyCategory"] for r in sheets.read_records("PluggyMap")}


def test_config_keys_are_validated(demo):
    admin.set_config("iof", '{"rate": 0.035, "match": ["LOJA X"]}')
    assert sheets.read_config("iof") == {"rate": 0.035, "match": ["LOJA X"]}
    admin.set_config("timezone", "America/Manaus")
    assert sheets.read_config("timezone") == "America/Manaus"
    admin.set_config("min_transaction_date", "2026-01-01")
    admin.unset_config("min_transaction_date")
    assert sheets.read_config("min_transaction_date") is None

    for key, value in [("timezone", "Lua/Base"), ("iof", '{"rate": 3.5, "match": []}'),
                       ("min_transaction_date", "01/02/2026"),
                       ("invest_destination_subcategories", "[]"),
                       ("budgets", "{}")]:
        with pytest.raises(admin.AdminError):
            admin.set_config(key, value)


def test_cli_reports_refusals_without_a_traceback(demo, capsys):
    assert admin.main(["taxonomy", "remove", "Alimentação"]) == 1
    assert "Não feito" in capsys.readouterr().err
    assert admin.main(["taxonomy", "list"]) == 0
    assert "Alimentação" in capsys.readouterr().out
