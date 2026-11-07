// Unit tests for a tab worker's open attention cards.
//
// The regression these exist for: `agent_status` reported `pending_permissions: []` for a
// tab worker holding a live permission prompt, because that prompt never enters the park
// queue. They do NOT replace the end-to-end run — that a live gate's card reaches the store
// is the watcher's fact, not this module's.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fetchAttentionItems, fetchOpenCards, openCardsOf } from './open-cards.mjs'

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

// The roster read. `list_agents` maps over every worker, so the property that matters is that
// the store is hit ONCE for the whole sweep — N calls against a 3 s timeout each is the shape
// that makes the roster unusable with the store down.
test('the roster read hits the store once, whatever the session count', async () => {
  let calls = 0
  const fetchImpl = async (url) => {
    calls += 1
    assert.equal(url, 'http://s/api/1.0/attention')
    return { ok: true, json: async () => items }
  }
  const all = await fetchAttentionItems({ storeUrl: 'http://s', fetchImpl })
  assert.equal(calls, 1)
  assert.equal(all.length, items.length)
  // The per-session filter stays the caller's, and it is the same pure one `fetchOpenCards` uses.
  assert.deepEqual(openCardsOf(all, SID).map((c) => c.item_id), ['a', 'b'])
  assert.deepEqual(openCardsOf(all, 'other').map((c) => c.item_id), ['d'])
})

test('an unreadable roster read is null, never an empty fleet', async () => {
  const fetchImpl = async () => { throw new Error('ECONNREFUSED') }
  assert.equal(await fetchAttentionItems({ storeUrl: 'http://s', fetchImpl }), null)
  assert.equal(await fetchAttentionItems({ storeUrl: null, fetchImpl }), null)
})

test('a body that is not a list is null, not a partial roster', async () => {
  const fetchImpl = async () => ({ ok: true, json: async () => ({ nope: 1 }) })
  assert.equal(await fetchAttentionItems({ storeUrl: 'http://s', fetchImpl }), null)
})

// The seam `list_agents` relies on: a null roster read must stay null per row rather than be
// laundered through `openCardsOf` into `[]`, which reads as "this worker is not parked".
test('a null roster read never becomes an empty card list', async () => {
  const fetchImpl = async () => { throw new Error('ECONNREFUSED') }
  const all = await fetchAttentionItems({ storeUrl: 'http://s', fetchImpl })
  assert.equal(all === null ? null : openCardsOf(all, SID), null)
})

// ⚠️ The call-site pin, and the WIRING is the feature. Every test above exercises the module
// directly, so reverting `list_agents` to `agentView` alone would leave this whole suite green
// with the manager-visible half dead — the same remedy `registry-rehydrate.test.mjs` and
// `attention-poll.test.mjs` already use, because `supervisor.mjs` starts an MCP server at
// import and cannot be imported here.
test('supervisor.mjs wires the roster read into list_agents', () => {
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(src, /case 'list_agents': \{[\s\S]{0,900}?const items = await fetchAttentionItems\(\{ storeUrl: config\.attentionStoreUrl \}\)/)
  assert.match(src, /open_cards: items === null \? null : openCardsOf\(items, agent\.sessionId\)/)
  // ⚠️ The per-row null guard is the load-bearing half, not decoration. `openCardsOf(items, …)`
  // alone answers `[]` for an unreadable store — which a manager reads as "this worker is not
  // parked", the exact false negative `open_cards` exists to remove.
  assert.ok(
    !/open_cards: openCardsOf\(items, agent\.sessionId\)/.test(src),
    'the null guard must not be dropped — [] for an unreadable store reads as "not parked"',
  )
  // One store read for the whole roster: a per-agent `fetchOpenCards` in the map would be N
  // round-trips, and with the store down N × 3 s in front of every manager sweep.
  assert.ok(
    !/open_cards: await fetchOpenCards\(/.test(src),
    'the roster must not fetch the store once per worker',
  )
})
