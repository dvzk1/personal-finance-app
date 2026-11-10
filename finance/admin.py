"""Administração pela CLI: o que antes só se editava abrindo a planilha.

Vale para os dois backends, e é o caminho do agente até existirem telas para isso.

  uv run python -m finance.admin taxonomy list
  uv run python -m finance.admin taxonomy add "Pets" Ração Veterinário --treatment fluxo
  uv run python -m finance.admin taxonomy add "Lazer" Shows          # sub em categoria existente
  uv run python -m finance.admin taxonomy set "Pets" --color "#aa5500" --icon PawPrint
  uv run python -m finance.admin taxonomy rename "Pets" "Animais"
  uv run python -m finance.admin taxonomy rename "Animais" --sub Ração Alimentação
  uv run python -m finance.admin taxonomy remove "Animais" [--sub Veterinário]

  uv run python -m finance.admin rules list [--search texto]
  uv run python -m finance.admin rules show r_0003
  uv run python -m finance.admin rules set r_0003 category=Lazer subcategory=Viagem note=
  uv run python -m finance.admin rules move r_0003 1
  uv run python -m finance.admin rules remove r_0003

  uv run python -m finance.admin pluggy-map list
  uv run python -m finance.admin pluggy-map set "Food delivery" Alimentação Delivery
  uv run python -m finance.admin pluggy-map remove "Food delivery"

  uv run python -m finance.admin config show
  uv run python -m finance.admin config set timezone America/Sao_Paulo
  uv run python -m finance.admin config set iof '{"rate": 0.035, "match": ["LOJA"]}'
  uv run python -m finance.admin config unset min_transaction_date

Renomear categoria ou subcategoria leva o nome novo a todo lugar que usava o antigo:
lançamentos (e partes de divisão), regras, mapa da Pluggy, cores de subcategoria, orçamento
e as subcategorias de destino da carteira. Remover recusa o que ainda está em uso e diz onde.
"""

import argparse
import json
import re
import sys
from datetime import date

from . import budgets as B
from . import ledger as L
from . import sheets
from . import taxonomy as T
from .invest import ledger_link as LL
from .transaction_dates import TimezoneConfigError, resolve_timezone


class AdminError(ValueError):
    """Pedido que não dá para cumprir, com o motivo para quem pediu."""


REAPPLY_HINT = ("Para os lançamentos refletirem: uv run python -m finance.categorize "
                "&& uv run python -m finance.report")


# --------------------------------------------------------------------------- taxonomia
def _taxonomy_rows() -> list[dict]:
    return [dict(r) for r in sheets.read_records("Taxonomy")
            if (r.get("Category") or "").strip()]


def _find(rows: list[dict], category: str) -> dict:
    for row in rows:
        if row["Category"].strip() == category:
            return row
    raise AdminError(f"Categoria não existe: {category}")


def _subs(row: dict) -> list[str]:
    return T._split_subs(row.get("Subcategories"))


def _set_subs(row: dict, subs: list[str]) -> None:
    row["Subcategories"] = ", ".join(subs)


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise AdminError("Nome vazio.")
    if "," in name:
        raise AdminError(f"Nome com vírgula não cabe na taxonomia: {name}")
    return name


def add_taxonomy(category: str, subs: list[str], treatment: str | None = None) -> str:
    category = _check_name(category)
    subs = [_check_name(s) for s in subs]
    rows = _taxonomy_rows()
    existing = next((r for r in rows if r["Category"].strip() == category), None)
    if existing is None:
        if treatment and treatment not in T.TREATMENTS:
            raise AdminError(f"Tratamento inválido: {treatment} (use {', '.join(T.TREATMENTS)})")
        rows.append({"Category": category, "Subcategories": ", ".join(dict.fromkeys(subs)),
                     "Treatment": treatment or "fluxo", "Color": None, "Icon": None,
                     "Essential": False})
        sheets.write_records("Taxonomy", rows)
        return f"Categoria criada: {category}" + (f" ({', '.join(subs)})" if subs else "")
    if treatment:
        raise AdminError(f"{category} já existe; para mudar o tratamento use taxonomy set.")
    current = _subs(existing)
    new = [s for s in dict.fromkeys(subs) if s not in current]
    if not new:
        raise AdminError(f"Nada a acrescentar em {category}.")
    _set_subs(existing, current + new)
    sheets.write_records("Taxonomy", rows)
    return f"{category}: subcategorias acrescentadas: {', '.join(new)}"


def set_taxonomy(category: str, treatment=None, color=None, icon=None,
                 essential=None) -> str:
    rows = _taxonomy_rows()
    row = _find(rows, category)
    if treatment is not None:
        if treatment not in T.TREATMENTS:
            raise AdminError(f"Tratamento inválido: {treatment} (use {', '.join(T.TREATMENTS)})")
        row["Treatment"] = treatment
    if color is not None:
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise AdminError(f"Cor deve ser #rrggbb: {color}")
        row["Color"] = color or None
    if icon is not None:
        row["Icon"] = icon or None
    if essential is not None:
        row["Essential"] = essential
    sheets.write_records("Taxonomy", rows)
    return f"{category} atualizada."


def _parts(record: dict) -> list[dict]:
    return [record] + list(record.get("splits") or [])


def usage(category: str, sub: str | None = None) -> dict:
    """Onde a categoria (ou a subcategoria dela) aparece. {lugar: quantidade}."""
    def hits(cat, s):
        return cat == category and (sub is None or s == sub)

    found = {}
    ledger = L.load_ledger()
    found["lançamentos"] = sum(any(hits(p.get("category"), p.get("subcategory"))
                                   for p in _parts(r)) for r in ledger.values())
    found["regras"] = sum(hits(r.get("category"), r.get("subcategory"))
                          for r in sheets.read_records("Rules"))
    found["mapa da Pluggy"] = sum(hits(r.get("Category"), r.get("Subcategory"))
                                  for r in sheets.read_records("PluggyMap"))
    if sub is None:
        plan = B.load()
        found["orçamento"] = sum(category in block for block in _budget_blocks(plan))
    return {where: n for where, n in found.items() if n}


def _budget_blocks(plan: dict) -> list[dict]:
    """Cada dicionário {categoria: valor} do orçamento."""
    blocks = []
    for section in ("spending", "savings_plan"):
        part = plan.get(section) or {}
        blocks.append(part.get("recurring") or {})
        blocks.extend((part.get("months") or {}).values())
    return blocks


def remove_taxonomy(category: str, sub: str | None = None) -> str:
    rows = _taxonomy_rows()
    row = _find(rows, category)
    if sub is not None and sub not in _subs(row):
        raise AdminError(f"{category} não tem a subcategoria {sub}")
    used = usage(category, sub)
    if used:
        where = ", ".join(f"{n} em {place}" for place, n in used.items())
        name = f"{category}/{sub}" if sub else category
        raise AdminError(f"{name} ainda está em uso ({where}). Reclassifique ou renomeie antes.")
    if sub is None:
        rows.remove(row)
        meta = [m for m in sheets.read_records("SubcategoryMeta")
                if m.get("Category") != category]
    else:
        _set_subs(row, [s for s in _subs(row) if s != sub])
        meta = [m for m in sheets.read_records("SubcategoryMeta")
                if not (m.get("Category") == category and m.get("Subcategory") == sub)]
    sheets.write_records("Taxonomy", rows)
    sheets.write_records("SubcategoryMeta", meta)
    return f"Removida: {category}/{sub}" if sub else f"Removida: {category}"


def rename_taxonomy(category: str, new: str, sub: str | None = None) -> str:
    """Renomeia a categoria (ou, com `sub`, a subcategoria dela) em todo lugar."""
    new = _check_name(new)
    rows = _taxonomy_rows()
    row = _find(rows, category)
    if sub is None:
        if any(r["Category"].strip() == new for r in rows):
            raise AdminError(f"Já existe a categoria {new}.")
        row["Category"] = new
    else:
        subs = _subs(row)
        if sub not in subs:
            raise AdminError(f"{category} não tem a subcategoria {sub}")
        if new in subs:
            raise AdminError(f"{category} já tem a subcategoria {new}.")
        _set_subs(row, [new if s == sub else s for s in subs])

    def rename(record, cat_key, sub_key):
        if record.get(cat_key) != category:
            return False
        if sub is None:
            record[cat_key] = new
            return True
        if record.get(sub_key) == sub:
            record[sub_key] = new
            return True
        return False

    def rename_all(records, cat_key, sub_key):
        """Quantos registros mudaram. Todos passam pelo rename (sem curto-circuito)."""
        return sum(rename(r, cat_key, sub_key) for r in records)

    ledger = L.load_ledger()
    changed = {tid for tid, rec in ledger.items()
               if rename_all(_parts(rec), "category", "subcategory")}
    rules = sheets.read_records("Rules")
    n_rules = rename_all(rules, "category", "subcategory")
    pmap = sheets.read_records("PluggyMap")
    n_map = rename_all(pmap, "Category", "Subcategory")
    meta = sheets.read_records("SubcategoryMeta")
    rename_all(meta, "Category", "Subcategory")

    sheets.write_records("Taxonomy", rows)
    if changed:
        L.save_ledger(ledger, changed_ids=changed)
    if n_rules:
        sheets.write_records("Rules", rules)
    if n_map:
        sheets.write_records("PluggyMap", pmap)
    sheets.write_records("SubcategoryMeta", meta)

    extra = []
    if sub is None:
        plan = B.load()
        moved = 0
        for block in _budget_blocks(plan):
            if category in block:
                block[new] = block.pop(category)
                moved += 1
        if moved:
            B.save(plan)
            extra.append(f"{moved} teto(s) no orçamento")
    else:
        destinations = list(LL.load_destination_subcategories())
        treatment = T.treatment_of(T.load_treatments(), category)
        if sub in destinations and treatment == "poupança":
            sheets.write_config(LL.DESTINATION_KEY,
                                [new if d == sub else d for d in destinations])
            extra.append("subcategorias de destino da carteira")

    old = f"{category}/{sub}" if sub else category
    target = f"{category}/{new}" if sub else new
    done = [f"{len(changed)} lançamento(s)", f"{n_rules} regra(s)", f"{n_map} no mapa da Pluggy"]
    return f"{old} → {target}: " + ", ".join(done + extra) + "."


def list_taxonomy() -> str:
    lines = []
    for row in _taxonomy_rows():
        flags = [T._norm_treatment(row.get("Treatment"), row["Category"])]
        if row.get("Essential"):
            flags.append("essencial")
        if row.get("Color"):
            flags.append(row["Color"])
        if row.get("Icon"):
            flags.append(row["Icon"])
        lines.append(f"{row['Category']}  [{', '.join(flags)}]")
        for sub in _subs(row):
            lines.append(f"    {sub}")
    return "\n".join(lines) or "Taxonomia vazia."


# ------------------------------------------------------------------------------ regras
RULE_FIELDS = ("field", "match", "value", "category", "subcategory", "note", "instruction",
               "propagate_note", "excluded", "type", "amount_abs_min", "amount_abs_max")
MATCHES = ("contains", "exact", "startswith", "regex")


def _rules() -> list[dict]:
    return sheets.read_records("Rules")


def _rule_index(rules: list[dict], rule_id: str) -> int:
    for i, rule in enumerate(rules):
        if rule.get("id") == rule_id:
            return i
    raise AdminError(f"Regra não existe: {rule_id}")


def _parse_rule_value(key: str, raw: str):
    raw = raw.strip()
    if key in ("propagate_note", "excluded"):
        if raw.lower() in ("true", "sim", "1", "yes"):
            return True
        if raw.lower() in ("false", "não", "nao", "0", "no", ""):
            return False
        raise AdminError(f"{key} aceita true ou false: {raw}")
    if key in ("amount_abs_min", "amount_abs_max"):
        if raw == "":
            return None
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            raise AdminError(f"{key} precisa ser número: {raw}") from None
    if key == "type":
        if raw.upper() not in ("", "DEBIT", "CREDIT"):
            raise AdminError(f"type aceita DEBIT ou CREDIT: {raw}")
        return raw.upper() or None
    if key == "match" and raw not in MATCHES:
        raise AdminError(f"match aceita {', '.join(MATCHES)}: {raw}")
    if key in ("field", "value", "category", "match") and raw == "":
        raise AdminError(f"{key} não pode ficar vazio.")
    return raw or (None if key == "subcategory" else "")


def set_rule(rule_id: str, assignments: list[str]) -> str:
    rules = _rules()
    rule = rules[_rule_index(rules, rule_id)]
    changes = {}
    for item in assignments:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or key not in RULE_FIELDS:
            raise AdminError(f"Use campo=valor, com campo em: {', '.join(RULE_FIELDS)}")
        changes[key] = _parse_rule_value(key, raw)
    updated = {**rule, **changes}
    if updated.get("match") == "regex":
        try:
            re.compile(updated["value"])
        except re.error as error:
            raise AdminError(f"Regex inválida: {error}") from None
    lo, hi = updated.get("amount_abs_min"), updated.get("amount_abs_max")
    if lo is not None and hi is not None and lo > hi:
        raise AdminError("amount_abs_min maior que amount_abs_max.")
    if "category" in changes or "subcategory" in changes:
        if not T.valid(T.load(), updated.get("category"), updated.get("subcategory")):
            raise AdminError(f"{updated.get('category')}/{updated.get('subcategory')} "
                             "não existe na taxonomia.")
    rule.update(changes)
    sheets.write_records("Rules", rules)
    return f"{rule_id} atualizada: {', '.join(changes)}. {REAPPLY_HINT}"


def remove_rule(rule_id: str) -> str:
    rules = _rules()
    rules.pop(_rule_index(rules, rule_id))
    sheets.write_records("Rules", rules)
    return (f"{rule_id} removida. Lançamentos que ela categorizou mantêm a categoria até "
            f"outra regra casar. {REAPPLY_HINT}")


def move_rule(rule_id: str, position: int) -> str:
    """Posição 1 = primeira. A ordem decide quem vence entre regras do mesmo grupo (as
    com faixa de valor são sempre testadas antes das sem faixa)."""
    rules = _rules()
    rule = rules.pop(_rule_index(rules, rule_id))
    position = max(1, min(position, len(rules) + 1))
    rules.insert(position - 1, rule)
    sheets.write_records("Rules", rules)
    return f"{rule_id} agora é a regra {position} de {len(rules)}. {REAPPLY_HINT}"


def _describe_rule(n: int, rule: dict) -> str:
    target = rule.get("category") or "?"
    if rule.get("subcategory"):
        target += f"/{rule['subcategory']}"
    extra = []
    if rule.get("amount_abs_min") is not None or rule.get("amount_abs_max") is not None:
        extra.append(f"valor {rule.get('amount_abs_min')}..{rule.get('amount_abs_max')}")
    if rule.get("type"):
        extra.append(rule["type"])
    if rule.get("excluded"):
        extra.append("rasura")
    if rule.get("instruction"):
        extra.append("instrução")
    tail = f"  ({', '.join(extra)})" if extra else ""
    return (f"{n:>3}. {rule.get('id')}  {rule.get('field')} {rule.get('match', 'contains')} "
            f"\"{rule.get('value')}\" → {target}{tail}")


def list_rules(search: str | None = None) -> str:
    needle = (search or "").lower()
    lines = [_describe_rule(n, r) for n, r in enumerate(_rules(), 1)
             if not needle or needle in json.dumps(r, ensure_ascii=False).lower()]
    return "\n".join(lines) or "Nenhuma regra."


def show_rule(rule_id: str) -> str:
    rules = _rules()
    rule = rules[_rule_index(rules, rule_id)]
    return "\n".join(f"{key}: {json.dumps(rule.get(key), ensure_ascii=False)}"
                     for key in ("id",) + RULE_FIELDS + ("created_at",))


# --------------------------------------------------------------------- mapa da Pluggy
def list_pluggy_map() -> str:
    rows = sheets.read_records("PluggyMap")
    return "\n".join(f"{r['PluggyCategory']} → {r.get('Category')}"
                     + (f"/{r['Subcategory']}" if r.get("Subcategory") else "")
                     for r in rows) or "Mapa vazio."


def set_pluggy_map(pluggy_category: str, category: str, sub: str | None = None) -> str:
    if not T.valid(T.load(), category, sub):
        raise AdminError(f"{category}/{sub} não existe na taxonomia.")
    rows = sheets.read_records("PluggyMap")
    row = next((r for r in rows if r.get("PluggyCategory") == pluggy_category), None)
    if row is None:
        rows.append({"PluggyCategory": pluggy_category, "Category": category,
                     "Subcategory": sub})
    else:
        row.update(Category=category, Subcategory=sub)
    sheets.write_records("PluggyMap", rows)
    return f"{pluggy_category} → {category}" + (f"/{sub}" if sub else "")


def remove_pluggy_map(pluggy_category: str) -> str:
    rows = sheets.read_records("PluggyMap")
    kept = [r for r in rows if r.get("PluggyCategory") != pluggy_category]
    if len(kept) == len(rows):
        raise AdminError(f"Não está no mapa: {pluggy_category}")
    sheets.write_records("PluggyMap", kept)
    return f"Removido do mapa: {pluggy_category}"


# ----------------------------------------------------------------------------- Config
def _timezone(value):
    if not isinstance(value, str):
        raise AdminError("timezone é um nome IANA, por exemplo America/Sao_Paulo.")
    try:
        resolve_timezone(value)
    except TimezoneConfigError as error:
        raise AdminError(str(error)) from None
    return value


def _min_date(value):
    try:
        date.fromisoformat(str(value))
    except ValueError:
        raise AdminError("min_transaction_date usa o formato YYYY-MM-DD.") from None
    return str(value)


def _iof(value):
    if not isinstance(value, dict):
        raise AdminError('iof é {"rate": 0.035, "match": ["TEXTO DO LOJISTA"]}.')
    rate = value.get("rate")
    if not isinstance(rate, (int, float)) or not 0 < rate < 1:
        raise AdminError("iof.rate é uma fração entre 0 e 1 (3,5% = 0.035).")
    match = value.get("match")
    if not isinstance(match, list) or not all(isinstance(m, str) and m.strip() for m in match):
        raise AdminError("iof.match é uma lista de textos de lojista.")
    return {"rate": float(rate), "match": [m.strip() for m in match]}


def _destinations(value):
    if not isinstance(value, list) or not value \
            or not all(isinstance(v, str) and v.strip() for v in value):
        raise AdminError('invest_destination_subcategories é uma lista: ["Aporte", "Resgate"].')
    return [v.strip() for v in value]


# Chaves de Config que não têm outro escritor (orçamento, âncora do saldo e as da carteira
# já têm o seu). Cada uma com o validador que impede gravar algo que o app não lê.
EDITABLE_CONFIG = {
    "timezone": _timezone,
    "min_transaction_date": _min_date,
    "iof": _iof,
    LL.DESTINATION_KEY: _destinations,
}


def _parse_config_value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def set_config(key: str, raw: str) -> str:
    if key not in EDITABLE_CONFIG:
        raise AdminError(f"Esta CLI edita: {', '.join(EDITABLE_CONFIG)}. "
                         "Orçamento, âncora do saldo e carteira têm comandos próprios.")
    value = EDITABLE_CONFIG[key](_parse_config_value(raw))
    sheets.write_config(key, value)
    return f"Config[{key}] = {json.dumps(value, ensure_ascii=False)}"


def unset_config(key: str) -> str:
    if key not in EDITABLE_CONFIG:
        raise AdminError(f"Esta CLI edita: {', '.join(EDITABLE_CONFIG)}.")
    sheets.write_config(key, "")
    return f"Config[{key}] removida: vale o padrão do app."


def show_config() -> str:
    lines = []
    for key in sheets.config_keys():
        value = sheets.read_config(key)
        if value is None:
            continue
        text = json.dumps(value, ensure_ascii=False)
        if len(text) > 100:
            text = text[:97] + "..."
        mark = "*" if key in EDITABLE_CONFIG else " "
        lines.append(f"{mark} {key}: {text}")
    lines.append("(* = editável aqui com config set/unset)")
    return "\n".join(lines)


# -------------------------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finance.admin", description=__doc__.split("\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    groups = parser.add_subparsers(dest="group", required=True)

    tax = groups.add_parser("taxonomy", help="categorias e subcategorias")
    tax_cmd = tax.add_subparsers(dest="command", required=True)
    tax_cmd.add_parser("list")
    add = tax_cmd.add_parser("add", help="cria categoria ou acrescenta subcategorias")
    add.add_argument("category")
    add.add_argument("subs", nargs="*")
    add.add_argument("--treatment", choices=T.TREATMENTS)
    set_ = tax_cmd.add_parser("set", help="tratamento, cor, ícone, essencial")
    set_.add_argument("category")
    set_.add_argument("--treatment", choices=T.TREATMENTS)
    set_.add_argument("--color", help="#rrggbb (vazio remove)")
    set_.add_argument("--icon", help="nome lucide-react (vazio remove)")
    set_.add_argument("--essential", action=argparse.BooleanOptionalAction, default=None)
    ren = tax_cmd.add_parser("rename", help="renomeia em todo lugar")
    ren.add_argument("category")
    ren.add_argument("--sub", help="renomeia esta subcategoria da categoria")
    ren.add_argument("new")
    rem = tax_cmd.add_parser("remove", help="remove o que não está em uso")
    rem.add_argument("category")
    rem.add_argument("--sub")

    rules = groups.add_parser("rules", help="regras de categorização")
    rules_cmd = rules.add_subparsers(dest="command", required=True)
    lst = rules_cmd.add_parser("list")
    lst.add_argument("--search")
    rules_cmd.add_parser("show").add_argument("id")
    rset = rules_cmd.add_parser("set", help="campo=valor (vazio limpa)")
    rset.add_argument("id")
    rset.add_argument("assignments", nargs="+")
    rules_cmd.add_parser("remove").add_argument("id")
    mv = rules_cmd.add_parser("move", help="muda a posição (1 = primeira)")
    mv.add_argument("id")
    mv.add_argument("position", type=int)

    pmap = groups.add_parser("pluggy-map", help="categoria da Pluggy → sua taxonomia")
    pmap_cmd = pmap.add_subparsers(dest="command", required=True)
    pmap_cmd.add_parser("list")
    pset = pmap_cmd.add_parser("set")
    pset.add_argument("pluggy_category")
    pset.add_argument("category")
    pset.add_argument("sub", nargs="?")
    pmap_cmd.add_parser("remove").add_argument("pluggy_category")

    cfg = groups.add_parser("config", help=f"chaves: {', '.join(EDITABLE_CONFIG)}")
    cfg_cmd = cfg.add_subparsers(dest="command", required=True)
    cfg_cmd.add_parser("show")
    cset = cfg_cmd.add_parser("set", help="valor em JSON ou texto")
    cset.add_argument("key")
    cset.add_argument("value")
    cfg_cmd.add_parser("unset").add_argument("key")
    return parser


def run(args) -> str:
    g, c = args.group, args.command
    if g == "taxonomy":
        if c == "list":
            return list_taxonomy()
        if c == "add":
            return add_taxonomy(args.category, args.subs, args.treatment)
        if c == "set":
            return set_taxonomy(args.category, args.treatment, args.color, args.icon,
                                args.essential)
        if c == "rename":
            return (rename_taxonomy(args.category, args.new, args.sub)
                    + " Para os relatórios refletirem: uv run python -m finance.report")
        return remove_taxonomy(args.category, args.sub)
    if g == "rules":
        if c == "list":
            return list_rules(args.search)
        if c == "show":
            return show_rule(args.id)
        if c == "set":
            return set_rule(args.id, args.assignments)
        if c == "remove":
            return remove_rule(args.id)
        return move_rule(args.id, args.position)
    if g == "pluggy-map":
        if c == "list":
            return list_pluggy_map()
        if c == "set":
            return set_pluggy_map(args.pluggy_category, args.category, args.sub)
        return remove_pluggy_map(args.pluggy_category)
    if c == "show":
        return show_config()
    if c == "set":
        return set_config(args.key, args.value)
    return unset_config(args.key)


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(run(args))
    except AdminError as error:
        print(f"Não feito: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
