"""Tradução da edição do dashboard (`POST /api/edit`) para o que o pipeline grava.

O payload traz `mode` e `ids`. Cada modo vira uma coisa diferente:

- `queue`: um item na fila do Claude (`queue_item`).
- `reimburse`: abatimentos novos e removidos (`reimbursement_changes`).
- `rule`: decisões do `categorize`, com regra nova (`build_decisions`).
- os demais (`value`, `split`, `tags`...): as células da linha (`row_fields`).

Nota: `None` (ou ausente) quer dizer "não mexer"; string vazia apaga a nota. Com vários
lançamentos selecionados o campo começa vazio, e mandar esse vazio apagaria a nota de
todos, por isso o dashboard só manda a nota que o usuário tocou.
"""

from datetime import datetime, timezone


def _split(part: dict) -> dict:
    out = {"amount": float(part["amount"]), "category": part.get("category"),
           "subcategory": part.get("subcategory") or None, "note": part.get("note") or ""}
    if part.get("settle_with"):
        out["settle_with"] = part["settle_with"]
    return out


def row_fields(p: dict) -> dict:
    """Células a gravar na linha (`finance.edit`). Vazio quando não há o que gravar."""
    fields = {}
    mode = p.get("mode")
    if mode == "tags":
        if p.get("tags_add"):
            fields["tags_add"] = p["tags_add"]
        if p.get("tags_remove"):
            fields["tags_remove"] = p["tags_remove"]
    elif mode == "split" and isinstance(p.get("splits"), list) and p["splits"]:
        fields["splits"] = [_split(part) for part in p["splits"]]
        if p.get("note") is not None:
            fields["note"] = str(p["note"])
    else:
        if p.get("category"):
            fields["category"] = p["category"]
            fields["subcategory"] = p.get("subcategory") or None
        if p.get("note") is not None:
            fields["note"] = str(p["note"])
    if "settle_with" in p and mode != "tags":
        fields["settle_with"] = p["settle_with"] or None
    if "excluded" in p:
        fields["excluded"] = bool(p["excluded"])
    return fields


def build_decisions(p: dict) -> dict:
    """Decisões do `categorize` para a edição em modo regra: a atribuição dos ids e a
    regra nova, que o apply reaplica em todo lançamento não manual."""
    learn = p.get("mode") == "rule"
    rule = p.get("rule") or {}
    decisions = {"assignments": [], "rules": []}
    propagate = learn and bool(rule.get("propagate_note")) and bool(rule.get("note"))
    if propagate:
        note_field = {"note": str(rule["note"])}
    elif p.get("note") is None:
        note_field = {}
    else:
        note_field = {"note": str(p["note"])}

    ids = p["ids"]
    if p.get("mode") == "tags":
        decisions["assignments"].append({
            "ids": ids,
            "tags_add": p["tags_add"] if isinstance(p.get("tags_add"), list) else [],
            "tags_remove": p["tags_remove"] if isinstance(p.get("tags_remove"), list) else [],
        })
    elif p.get("mode") == "split" and isinstance(p.get("splits"), list) and p["splits"]:
        decisions["assignments"].append(
            {"ids": ids, **note_field, "splits": [_split(part) for part in p["splits"]]})
    elif p.get("category"):
        assignment = {"ids": ids, "category": p["category"],
                      "subcategory": p.get("subcategory") or None, "source": "manual",
                      **note_field}
        if "settle_with" in p:
            assignment["settle_with"] = p["settle_with"] or None
        decisions["assignments"].append(assignment)

    if learn and rule.get("value"):
        new_rule = {"field": rule.get("field"), "match": rule.get("match") or "contains",
                    "value": rule["value"], "category": p.get("category"),
                    "subcategory": p.get("subcategory") or None,
                    "note": str(rule["note"]) if propagate else ""}
        if propagate:
            new_rule["propagate_note"] = True
        if rule.get("instruction"):
            new_rule["instruction"] = str(rule["instruction"])
        if rule.get("type"):
            new_rule["type"] = rule["type"]
        for key in ("amount_abs_min", "amount_abs_max"):
            if rule.get(key) is not None:
                new_rule[key] = float(rule[key])
        if p.get("excluded"):
            new_rule["excluded"] = True
        decisions["rules"].append(new_rule)

    # "rasurar": tira o lançamento dos agregados (reversível). Pode vir sozinho ou junto
    # de uma recategorização. Não mexe na nota se não veio nota.
    if "excluded" in p:
        value = bool(p["excluded"])
        if decisions["assignments"]:
            for assignment in decisions["assignments"]:
                assignment["excluded"] = value
        else:
            exclusion = {"ids": ids, "excluded": value}
            if p.get("note") not in (None, ""):
                exclusion["note"] = str(p["note"])
            decisions["assignments"].append(exclusion)
    return decisions


def queue_item(p: dict) -> dict:
    """Linha da fila do Claude: o pedido do usuário com os lançamentos e a sugestão."""
    suggestion = ({"category": p["category"], "subcategory": p.get("subcategory") or None}
                  if p.get("category") else None)
    return {"ts": datetime.now(timezone.utc).isoformat(), "ids": p["ids"],
            "note": p.get("note") or "", "suggestion": suggestion,
            "samples": p.get("samples") or []}


def reimbursement_changes(p: dict) -> tuple[list, list]:
    """Abatimentos a criar (só os campos do vínculo, com a nota da edição) e a remover."""
    keys = ("credit_id", "credit_part", "debit_id", "debit_part", "amount")
    add = [{**{key: link.get(key) for key in keys}, "note": p.get("note") or None}
           for link in p.get("links") or []]
    return add, list(p.get("remove") or [])
