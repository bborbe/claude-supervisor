// Unit tests for the refusal `answer_permission` gives when the request is not this server's park.
//
// The regression these exist for: a manager holding a worker another manager spawned got a bare
// `no pending permission <id> (already answered, or expired)`, which named neither the owner nor
// the route — so the one caller who could act on it had nothing to act on. They do NOT replace
// the end-to-end run: that manager B is actually refused, and that the store route then unblocks
// A's worker, is a live two-manager fact this module cannot establish.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ANSWER_ROUTE, findParkRow, foreignParkMessage, foreignParkRefusal } from './park-refusal.mjs'

const ROW = {
  session_id: 'worker-1',
  spawner_session_id: 'manager-a',
  parked_permission: { request_id: 'perm_7', tool: 'Bash' },
}

test('names the worker and its spawning session — the whole point of the refusal', () => {
  const msg = foreignParkMessage('perm_7', ROW)
  assert.match(msg, /perm_7/)
  assert.match(msg, /worker-1/)
  assert.match(msg, /manager-a/)
})

test('names the store route, so the refusal is actionable', () => {
  assert.ok(foreignParkMessage('perm_7', ROW).includes(ANSWER_ROUTE))
  assert.ok(foreignParkMessage('perm_7', null).includes(ANSWER_ROUTE))
})

test('says plainly that a cross-session answer_permission cannot release it', () => {
  assert.match(foreignParkMessage('perm_7', ROW), /cannot release another server's park/)
})

test('a row with no spawner still names the worker, and says the spawner is unrecorded', () => {
  const msg = foreignParkMessage('perm_7', { session_id: 'worker-1', spawner_session_id: '' })
  assert.match(msg, /worker-1/)
  assert.match(msg, /no recorded spawner/)
})

test('an absent row keeps the route rather than degrading to the old bare id', () => {
  const msg = foreignParkMessage('perm_9', null)
  assert.match(msg, /perm_9/)
  assert.ok(msg.includes(ANSWER_ROUTE))
  assert.doesNotMatch(msg, /^no pending permission perm_9 \(already answered, or expired\)$/)
})

test('the row is found by the park it carries, not by session or position', () => {
  const rows = [{ session_id: 'x' }, ROW, { session_id: 'y', parked_permission: { request_id: 'perm_8' } }]
  assert.equal(findParkRow(rows, 'perm_7'), ROW)
  assert.equal(findParkRow(rows, 'perm_8').session_id, 'y')
  assert.equal(findParkRow(rows, 'perm_9'), null)
})

test('a row with no park at all is not a match, and does not throw', () => {
  assert.equal(findParkRow([{ session_id: 'x' }], 'perm_7'), null)
  assert.equal(findParkRow([null, undefined], 'perm_7'), null)
})

test('an unreadable ledger is null rows — and still gets the route, never a crash', async () => {
  assert.equal(findParkRow(null, 'perm_7'), null)
  assert.equal(findParkRow([], 'perm_7'), null)
  const msg = await foreignParkRefusal({ requestId: 'perm_7', readLedger: async () => null })
  assert.match(msg, /perm_7/)
  assert.ok(msg.includes(ANSWER_ROUTE))
})

test('reads the ledger once and renders the owning row', async () => {
  let calls = 0
  const msg = await foreignParkRefusal({
    requestId: 'perm_7',
    readLedger: async () => {
      calls += 1
      return [ROW]
    },
  })
  assert.equal(calls, 1)
  assert.match(msg, /manager-a/)
})

test('an empty request id never matches a row that also has none', () => {
  assert.equal(findParkRow([{ parked_permission: { request_id: '' } }], ''), null)
})

// ⚠️ The call-site pin, and the WIRING is the feature. Every test above exercises the module
// directly, so reverting `answer_permission` to its bare string would leave this suite green
// with the refusal unnamed — the same remedy `registry-rehydrate.test.mjs` and
// `attention-poll.test.mjs` already use, because `supervisor.mjs` starts an MCP server at
// import and cannot be imported here.
test('supervisor.mjs routes a missed request through the refusal, not a bare id', () => {
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  // ⚠️ Anchored on the ARM THAT PRECEDES IT, not on a byte window. A window cannot tell
  // "reached from the not-found arm" from "reached from somewhere else in the same function",
  // and the wrapper answer path added a second refusal arm inside the old 700-byte span. The
  // foreign park must follow the answered/error arm and nothing else may sit between them.
  assert.match(
    src,
    /if \(viaWrapper\.answered \|\| viaWrapper\.error\) return reply\([\s\S]{0,400}?error: await foreignParkRefusal\(\{ requestId: args\.request_id, readLedger: readSessionLedger \}\)/,
  )
  // And the outage arm must still be tested BEFORE it, or it is dead code.
  assert.match(
    src,
    /case 'answer_permission': \{[\s\S]{0,900}?if \(viaWrapper\.unreadable\) \{[\s\S]{0,400}?if \(viaWrapper\.answered \|\| viaWrapper\.error\)/,
  )
  assert.ok(
    !/no pending permission \$\{args\.request_id\} \(already answered, or expired\)/.test(src),
    'the bare refusal must not come back — it names neither the owner nor the route',
  )
})
