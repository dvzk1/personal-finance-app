"""O banco de demonstração só usa categorias que existem na taxonomia dele."""

from finance import taxonomy as T
from finance.config import SEED_DIR
from finance.seed import _read_fixtures


def _invalid(fixtures):
    tax = fixtures["taxonomy"]
    pairs = []
    for rec in fixtures["ledger"]:
        pairs.append((f"lançamento {rec['id']}", rec.get("category"), rec.get("subcategory")))
        for i, part in enumerate(rec.get("splits") or []):
            pairs.append((f"parte {i} de {rec['id']}", part.get("category"),
                          part.get("subcategory")))
    for rule in fixtures["rules"]:
        pairs.append((f"regra {rule.get('id')}", rule.get("category"), rule.get("subcategory")))
    return [(where, cat, sub) for where, cat, sub in pairs
            if cat and not T.valid(tax, cat, sub)]


def test_seed_points_only_to_existing_categories():
    assert _invalid(_read_fixtures(SEED_DIR)) == []
