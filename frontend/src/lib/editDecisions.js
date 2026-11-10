// Monta o arquivo de decisões (data/.decisions.json) que a edição do dashboard manda para o
// `finance.categorize apply`. Fica fora do vite.config.js para ter teste: é aqui que se decide
// o que o apply grava em cada lançamento.
export function buildDecisions(p) {
  const learn = p.mode === 'rule'
  const decisions = { assignments: [], rules: [] }
  const propagate = learn && !!p.rule?.propagate_note && !!p.rule?.note
  // nota intocada chega como null e fica fora do assignment: com `note` presente, o
  // categorize apply grava o valor, e o vazio apagaria a nota de cada lançamento
  const noteField = propagate ? { note: String(p.rule.note) }
    : p.note == null ? {} : { note: String(p.note) }
  if (p.mode === 'tags') {
    decisions.assignments.push({
      ids: p.ids,
      tags_add: Array.isArray(p.tags_add) ? p.tags_add : [],
      tags_remove: Array.isArray(p.tags_remove) ? p.tags_remove : [],
    })
  } else if (p.mode === 'split' && Array.isArray(p.splits) && p.splits.length) {
    decisions.assignments.push({
      ids: p.ids,
      ...noteField,
      splits: p.splits.map((s) => ({
        amount: Number(s.amount),
        category: s.category,
        subcategory: s.subcategory || null,
        note: s.note || '',
        ...(s.settle_with ? { settle_with: s.settle_with } : {}),
      })),
    })
  } else if (p.category) {
    decisions.assignments.push({
      ids: p.ids, category: p.category,
      subcategory: p.subcategory || null, source: 'manual',
      ...noteField,
      ...(p.settle_with !== undefined ? { settle_with: p.settle_with || null } : {}),
    })
  }
  if (learn && p.rule && p.rule.value) {
    decisions.rules.push({
      field: p.rule.field, match: p.rule.match || 'contains',
      value: p.rule.value, category: p.category,
      subcategory: p.subcategory || null,
      note: propagate ? String(p.rule.note) : '',
      ...(propagate ? { propagate_note: true } : {}),
      ...(p.rule.instruction ? { instruction: String(p.rule.instruction) } : {}),
      ...(p.rule.type ? { type: p.rule.type } : {}),
      ...(p.rule.amount_abs_min != null ? { amount_abs_min: Number(p.rule.amount_abs_min) } : {}),
      ...(p.rule.amount_abs_max != null ? { amount_abs_max: Number(p.rule.amount_abs_max) } : {}),
      ...(p.excluded ? { excluded: true } : {}),
    })
  }
  // "rasurar": tira o lançamento dos agregados (reversível). Pode vir sozinho
  // ou junto de uma recategorização. Não mexe na nota se não veio nota.
  if (p.excluded !== undefined) {
    const val = !!p.excluded
    if (decisions.assignments.length) {
      for (const a of decisions.assignments) a.excluded = val
    } else {
      const ex = { ids: p.ids, excluded: val }
      if (p.note != null && p.note !== '') ex.note = String(p.note)
      decisions.assignments.push(ex)
    }
  }
  return decisions
}
