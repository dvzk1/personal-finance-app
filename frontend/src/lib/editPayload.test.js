import assert from 'node:assert/strict'
import test from 'node:test'

import { noteToSave } from './editPayload.js'

test('untouched note field keeps each transaction note', () => {
  assert.equal(noteToSave('', false), null)
})

test('edited note field is saved, even when cleared', () => {
  assert.equal(noteToSave('  jantar  ', true), 'jantar')
  assert.equal(noteToSave('', true), '')
})
