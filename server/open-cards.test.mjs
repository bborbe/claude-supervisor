// Unit tests for a tab worker's open attention cards.
//
// The regression these exist for: `agent_status` reported `pending_permissions: []` for a
// tab worker holding a live permission prompt, because that prompt never enters the park
// queue. They do NOT replace the end-to-end run — that a live gate's card reaches the store
// is the watcher's fact, not this module's.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fetchOpenCards, openCardsOf } from './open-cards.mjs'

const SID = 'sess-1'
const items = [
  { item_id: 'b', producer_id: SID, state: 'open', answer_mechanism: 'permission', payload: 'Write: /x', created_at: '2026-09-27T15:06:33Z' },
  { item_id: 'a', producer_id: SID, state: 'open', answer_mechanism: 'message', payload: 'pick', created_at: '2026-09-27T15:00:00Z' },
  { item_id: 'c', producer_id: SID, state: 'answered', answer_mechanism: 'permission', payload: 'Bash: ls' },
  { item_id: 'd', producer_id: 'other', state: 'open', answer_mechanism: 'permission', payload: 'Edit: /y' },
]

test('returns only this session\'s open cards, oldest first', () => {
  assert.deepEqual(openCardsOf(items, SID).map((c) => c.item_id), ['a', 'b'])
})

test('carries what a manager needs to answer', () => {
  const [, perm] = openCardsOf(items, SID)
  assert.deepEqual(perm, { item_id: 'b', answer_mechanism: 'permission', payload: 'Write: /x', created_at: '2026-09-27T15:06:33Z' })
})

test('no session id or no list reads as empty, never as another session\'s cards', () => {
  assert.deepEqual(openCardsOf(items, null), [])
  assert.deepEqual(openCardsOf(null, SID), [])
})

test('an unreachable store is null, not an empty list', async () => {
  const fetchImpl = async () => { throw new Error('ECONNREFUSED') }
  assert.equal(await fetchOpenCards({ storeUrl: 'http://s', sessionId: SID, fetchImpl }), null)
})

test('a non-2xx store is null', async () => {
  const fetchImpl = async () => ({ ok: false })
  assert.equal(await fetchOpenCards({ storeUrl: 'http://s', sessionId: SID, fetchImpl }), null)
})

test('a reachable store with nothing open is an empty list', async () => {
  const fetchImpl = async () => ({ ok: true, json: async () => ({ items: [] }) })
  assert.deepEqual(await fetchOpenCards({ storeUrl: 'http://s', sessionId: SID, fetchImpl }), [])
})

test('reads the list from the store and filters it', async () => {
  const fetchImpl = async (url) => {
    assert.equal(url, 'http://s/api/1.0/attention')
    return { ok: true, json: async () => items }
  }
  assert.deepEqual((await fetchOpenCards({ storeUrl: 'http://s', sessionId: SID, fetchImpl })).map((c) => c.item_id), ['a', 'b'])
})

test('store switched off is null', async () => {
  assert.equal(await fetchOpenCards({ storeUrl: null, sessionId: SID }), null)
})
