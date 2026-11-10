import assert from 'node:assert/strict'
import test from 'node:test'

import { buildDecisions } from './editDecisions.js'

const rule = { field: 'description', match: 'contains', value: 'PADARIA' }

test('rule mode leaves an untouched note out, so each transaction keeps its own', () => {
  const { assignments, rules } = buildDecisions({
    mode: 'rule', ids: ['a', 'b'], category: 'Alimentação', subcategory: 'Café/Padaria',
    note: null, rule })
  assert.equal('note' in assignments[0], false)
  assert.equal(rules[0].value, 'PADARIA')
})

test('rule mode writes a note the user edited, even an emptied one', () => {
  const edited = buildDecisions({ mode: 'rule', ids: ['a'], category: 'Lazer',
    note: 'aniversário', rule })
  assert.equal(edited.assignments[0].note, 'aniversário')
  const emptied = buildDecisions({ mode: 'rule', ids: ['a'], category: 'Lazer',
    note: '', rule })
  assert.equal(emptied.assignments[0].note, '')
})

test('a propagated rule note goes to the transactions and to the rule', () => {
  const { assignments, rules } = buildDecisions({ mode: 'rule', ids: ['a'],
    category: 'Lazer', note: null, rule: { ...rule, note: 'padaria da esquina',
      propagate_note: true } })
  assert.equal(assignments[0].note, 'padaria da esquina')
  assert.deepEqual([rules[0].note, rules[0].propagate_note], ['padaria da esquina', true])
})
