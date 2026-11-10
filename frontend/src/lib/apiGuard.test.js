import assert from 'node:assert/strict'
import test from 'node:test'

import { rejectReason } from './apiGuard.js'

const dashboard = { host: 'localhost:5273', origin: 'http://localhost:5273',
  secFetchSite: 'same-origin' }

test('the dashboard itself can read and write', () => {
  assert.equal(rejectReason({ ...dashboard, method: 'GET' }), null)
  assert.equal(rejectReason({ ...dashboard, method: 'POST' }), null)
  assert.equal(rejectReason({ method: 'POST', host: '127.0.0.1:5273',
    origin: 'http://127.0.0.1:5273' }), null)
  assert.equal(rejectReason({ method: 'POST', host: '[::1]:5273',
    origin: 'http://[::1]:5273' }), null)
})

test('a local tool without browser headers can write', () => {
  assert.equal(rejectReason({ method: 'POST', host: 'localhost:5273' }), null)
})

test('another site cannot write, even with a simple request', () => {
  assert.match(rejectReason({ method: 'POST', host: 'localhost:5273',
    origin: 'https://site.example' }), /origem/)
  assert.match(rejectReason({ method: 'POST', host: 'localhost:5273',
    secFetchSite: 'cross-site' }), /outro site/)
  // outra porta do mesmo computador é outra origem
  assert.match(rejectReason({ method: 'POST', host: 'localhost:5273',
    origin: 'http://localhost:3000' }), /origem/)
})

test('a rebound domain cannot read nor write', () => {
  for (const method of ['GET', 'POST'])
    assert.match(rejectReason({ method, host: 'site.example:5273',
      origin: 'http://site.example:5273', secFetchSite: 'same-origin' }), /host/)
  assert.match(rejectReason({ method: 'GET', host: '' }), /host/)
  assert.match(rejectReason({ method: 'GET', host: 'localhost.site.example' }), /host/)
})
