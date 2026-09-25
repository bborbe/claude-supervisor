// Tests for the durable park record.
//
// Two properties carry the weight, and both are negatives about what must NOT happen:
// the record must not carry a summary where the real input belongs (a truncated input
// cannot resume anything), and clearing must not be an omitted key (because the ledger
// merges, so an omission leaves the stale park in place).

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildParkRecord, clearParkPatch, PARK_FIELD, REDACTED, redactInput } from './park-record.mjs'

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

// --- redaction -------------------------------------------------------------------
//
// This record is a new place a tool input lands on disk. The hook that already persists
// tool detail scrubs it first, so the same convention applies here — and the tests below
// check BOTH directions: that secrets go, and that ordinary input is left alone. A
// redactor that scrubs everything is not safer, it is a resume that cannot work.

test('a secret-named key is redacted, keeping the name', () => {
  const rec = park({ input: { Authorization: 'Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig' } })
  assert.equal(rec.input.Authorization, REDACTED)
  assert.equal('Authorization' in rec.input, true, 'the field must stay — absent and redacted are different facts')
})

test('redaction reaches nested keys', () => {
  const rec = park({ input: { headers: { 'x-api-key': 'sk-live-abc123' }, url: 'https://x.test' } })
  assert.equal(rec.input.headers['x-api-key'], REDACTED)
  assert.equal(rec.input.url, 'https://x.test', 'a non-secret sibling must survive')
})

test('a bare bearer token in a command line is redacted', () => {
  const rec = park({ input: { command: 'curl -H "X: 1" https://api.test -d "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"' } })
  assert.match(rec.input.command, /Bearer \[REDACTED\]/)
  assert.equal(rec.input.command.includes('eyJhbGciOiJIUzI1NiJ9'), false)
})

test('ordinary input is left exactly as it was', () => {
  const input = { command: 'ls -la /tmp', description: 'list files', timeout: 5000 }
  assert.deepEqual(park({ input }).input, input)
})

test('redaction preserves shape — an object stays an object', () => {
  // Flattening to a redacted string would trade a leak for a resume that cannot work.
  const rec = park({ input: { command: 'echo hi', password: 'hunter2' } })
  assert.equal(typeof rec.input, 'object')
  assert.equal(Array.isArray(rec.input), false)
  assert.equal(rec.input.command, 'echo hi')
  assert.equal(rec.input.password, REDACTED)
})

test('arrays keep their length and their order', () => {
  const rec = park({ input: { args: ['--token', 'Bearer eyJhbGciOiJIUzI1NiJ9.x.y', '--verbose'] } })
  assert.equal(rec.input.args.length, 3)
  assert.equal(rec.input.args[0], '--token')
  assert.match(rec.input.args[1], /\[REDACTED\]/)
  assert.equal(rec.input.args[2], '--verbose')
})

test('null and non-object inputs pass through untouched', () => {
  assert.equal(redactInput(null), null)
  assert.equal(redactInput(42), 42)
  assert.equal(redactInput(true), true)
})
