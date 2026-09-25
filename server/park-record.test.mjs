// Tests for the durable park record.
//
// Two properties carry the weight, and both are negatives about what must NOT happen:
// the record must not carry a summary where the real input belongs (a truncated input
// cannot resume anything), and clearing must not be an omitted key (because the ledger
// merges, so an omission leaves the stale park in place).

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildParkRecord, clearParkPatch, PARK_FIELD } from './park-record.mjs'

const park = (over = {}) =>
  buildParkRecord({ requestId: 'perm_7', toolName: 'Bash', input: { command: 'ls /tmp' }, ...over })

test('the record carries the full input, not a truncated summary', () => {
  // The hook log truncates `detail` to 200 chars, which is why the store item cannot
  // carry the input. A resume needs the real thing.
  const long = { command: 'x'.repeat(5000) }
  const rec = park({ input: long })
  assert.deepEqual(rec.input, long)
  assert.equal(rec.input.command.length, 5000)
})

test('the record carries the reason and the blocked path', () => {
  const rec = park({ decisionReason: 'needs approval', blockedPath: '/etc/hosts' })
  assert.equal(rec.reason, 'needs approval')
  assert.equal(rec.blocked_path, '/etc/hosts')
})

test('absent optional fields are null, never undefined', () => {
  // `undefined` disappears through JSON.stringify, so an absent key and a null one would
  // read the same on disk — but they do not read the same in memory, and this record is
  // read back before it is trusted.
  const rec = park()
  assert.equal(rec.reason, null)
  assert.equal(rec.blocked_path, null)
  assert.notEqual(rec.requested_at, null)
})

test('requested_at defaults to now', () => {
  const before = Date.now()
  const rec = park()
  const at = Date.parse(rec.requested_at)
  assert.ok(at >= before - 1000 && at <= Date.now() + 1000, `requested_at out of range: ${rec.requested_at}`)
})

test('a park with no requestId is refused', () => {
  // The requestId is the handle an answer names; a park without one cannot be answered.
  assert.throws(() => buildParkRecord({ toolName: 'Bash' }), /requestId/)
})

test('a park with no tool is refused', () => {
  assert.throws(() => buildParkRecord({ requestId: 'perm_1' }), /toolName/)
})

test('the record does not restate the session id', () => {
  // It is the ledger record's own key. Two copies of an identity invite them to disagree.
  const rec = park()
  for (const k of ['session_id', 'sessionId', 'agent_id']) {
    assert.equal(k in rec, false, `${k} must not be duplicated into the park record`)
  }
})

test('clearing is a null patch, and the ledger merge actually clears', () => {
  const record = { session_id: 's1', status: 'running', [PARK_FIELD]: park() }
  const patch = clearParkPatch()
  assert.deepEqual(patch, { [PARK_FIELD]: null })
  // `updateRecord` merges with a spread, so this is the real clearing behaviour: an
  // omitted key would leave the park standing.
  assert.equal({ ...record, ...patch }[PARK_FIELD], null)
})
