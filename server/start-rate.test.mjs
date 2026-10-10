// Tests for the session start-rate limit.
//
// The property that matters is that the limit bounds ARRIVALS, which the concurrency caps
// structurally cannot — the fleet was never full during the 2026-10-10 incident. So the
// cases below pin the pacing itself (a reservation lands exactly one interval after the
// last), the ceiling (past it a start is refused, not parked forever), and the resolution
// rule this repo applies everywhere: every source is validated, not only the winner.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_MAX_STARTS_PER_MINUTE,
  MAX_STARTS_ENV,
  MAX_START_DELAY_MS,
  readReservedAt,
  resolveMaxStarts,
  startRateDecision,
  writeReservedAt,
} from './start-rate.mjs'

const NOW = Date.parse('2026-10-10T12:00:00.000Z')
const iso = (ms) => new Date(ms).toISOString()
const file = (maxStartsPerMinute) => ({ spawn: { maxStartsPerMinute } })

// ── resolution ───────────────────────────────────────────────────────────────────────────

test('with nothing configured, the rate is the documented default', () => {
  const r = resolveMaxStarts({})
  assert.equal(r.limit, DEFAULT_MAX_STARTS_PER_MINUTE)
  assert.equal(r.intervalMs, 60_000 / DEFAULT_MAX_STARTS_PER_MINUTE)
  assert.equal(r.source, 'default')
})

test('the config file decides when nothing outranks it', () => {
  const r = resolveMaxStarts({ file: file(10) })
  assert.equal(r.limit, 10)
  assert.equal(r.intervalMs, 6_000)
  assert.equal(r.source, 'config')
})

test('the env var outranks the config file', () => {
  const r = resolveMaxStarts({ env: '2', file: file(10) })
  assert.equal(r.limit, 2)
  assert.equal(r.source, 'env')
})

test('0 is the off switch, and reads as unlimited rather than as a rate of zero', () => {
  assert.equal(resolveMaxStarts({ file: file(0) }).limit, null)
  assert.equal(resolveMaxStarts({ env: '0' }).limit, null)
})

// ⚠️ The signature property of this repo's resolvers: an invalid value refuses even when it
// is NOT the source that would have won. A typo in config.json must not stay invisible
// merely because this spawn was paced by the environment instead — that is how a file gets
// accepted, reported as applied, and silently ignored.
test('an invalid value refuses even in a source that would have lost', () => {
  const r = resolveMaxStarts({ env: '2', file: file('fast') })
  assert.match(r.error, /spawn\.maxStartsPerMinute/)
  assert.match(r.error, /not a start rate/)
})

test('a non-integer, a negative and a boolean are each refused, naming their source', () => {
  assert.match(resolveMaxStarts({ file: file(1.5) }).error, /spawn\.maxStartsPerMinute/)
  assert.match(resolveMaxStarts({ file: file(-1) }).error, /spawn\.maxStartsPerMinute/)
  // `Number(true)` is 1, so a blind coercion would read this as one start per minute.
  assert.match(resolveMaxStarts({ file: file(true) }).error, /spawn\.maxStartsPerMinute/)
  assert.match(resolveMaxStarts({ env: 'nope' }).error, /SUPERVISOR_MAX_STARTS_PER_MINUTE/)
})

test('the env var name is the documented one', () => {
  assert.equal(MAX_STARTS_ENV, 'SUPERVISOR_MAX_STARTS_PER_MINUTE')
})

// ── the pacing decision ──────────────────────────────────────────────────────────────────

test('a free slot starts immediately', () => {
  const rate = resolveMaxStarts({ file: file(4) })
  assert.deepEqual(startRateDecision({ rate, lastReservedAt: null, now: NOW }), {
    action: 'allow',
    reservedAt: iso(NOW),
    waitMs: 0,
  })
})

test('a start inside the interval is delayed by exactly the remainder, never dropped', () => {
  const rate = resolveMaxStarts({ file: file(4) }) // 15s apart
  const d = startRateDecision({ rate, lastReservedAt: iso(NOW - 5_000), now: NOW })
  assert.equal(d.action, 'delay')
  assert.equal(d.waitMs, 10_000)
  assert.equal(d.reservedAt, iso(NOW + 10_000))
  assert.match(d.message, /4\/min/)
  assert.match(d.message, /source: config/)
})

test('successive reservations land exactly one interval apart', () => {
  const rate = resolveMaxStarts({ file: file(4) }) // 15s apart
  const first = startRateDecision({ rate, lastReservedAt: null, now: NOW })
  const second = startRateDecision({ rate, lastReservedAt: first.reservedAt, now: NOW })
  const third = startRateDecision({ rate, lastReservedAt: second.reservedAt, now: NOW })
  assert.equal(Date.parse(second.reservedAt) - Date.parse(first.reservedAt), 15_000)
  assert.equal(Date.parse(third.reservedAt) - Date.parse(second.reservedAt), 15_000)
})

test('a start already at the rate is allowed rather than delayed by zero', () => {
  const rate = resolveMaxStarts({ file: file(4) })
  const d = startRateDecision({ rate, lastReservedAt: iso(NOW - 15_000), now: NOW })
  assert.equal(d.action, 'allow')
  assert.equal(d.waitMs, 0)
})

test('past the ceiling a start is refused, and the refusal names the rate and the file', () => {
  const rate = resolveMaxStarts({ file: file(4) })
  const d = startRateDecision({
    rate,
    lastReservedAt: iso(NOW + MAX_START_DELAY_MS),
    now: NOW,
    configFile: '/tmp/config.json',
  })
  assert.equal(d.action, undefined)
  assert.match(d.error, /4\/min/)
  assert.match(d.error, /source: config/)
  assert.match(d.error, /\/tmp\/config\.json/)
  assert.match(d.error, /0 disables the rate limit/)
})

test('the ceiling is not reached one interval short of it', () => {
  const rate = resolveMaxStarts({ file: file(4) })
  const d = startRateDecision({
    rate,
    lastReservedAt: iso(NOW + MAX_START_DELAY_MS - 30_000),
    now: NOW,
  })
  assert.equal(d.action, 'delay')
})

test('the off switch allows every start regardless of the last reservation', () => {
  const rate = resolveMaxStarts({ file: file(0) })
  const d = startRateDecision({ rate, lastReservedAt: iso(NOW + 10_000_000), now: NOW })
  assert.equal(d.action, 'allow')
  assert.equal(d.waitMs, 0)
})

// An unreadable stamp is the state of a fleet that has never been paced. Treating it as
// "unknown, therefore block" would make the first start after every state-file loss fail.
test('an unparseable reservation stamp frees the slot rather than blocking', () => {
  const rate = resolveMaxStarts({ file: file(4) })
  assert.equal(startRateDecision({ rate, lastReservedAt: 'not-a-date', now: NOW }).action, 'allow')
})

test('a rate error propagates out of the decision unchanged', () => {
  const rate = resolveMaxStarts({ file: file('fast') })
  const d = startRateDecision({ rate, lastReservedAt: null, now: NOW })
  assert.match(d.error, /not a start rate/)
})

// ── the state file ───────────────────────────────────────────────────────────────────────

test('the reservation round-trips through the state file', () => {
  const writes = []
  const fs = {
    mkdirSync: () => {},
    writeFileSync: (p, body) => writes.push([p, body]),
    renameSync: () => {},
    readFileSync: () => writes[0][1],
  }
  writeReservedAt('/state/start-rate.json', iso(NOW), { fs })
  // Written to a temp path first, so a reader never sees a partial file.
  assert.equal(writes[0][0], '/state/start-rate.json.tmp')
  assert.equal(readReservedAt('/state/start-rate.json', { fs }), iso(NOW))
})

test('an absent, unreadable or malformed state file reads as no reservation', () => {
  const boom = () => {
    throw new Error('ENOENT')
  }
  assert.equal(readReservedAt('/state/start-rate.json', { fs: { readFileSync: boom } }), null)
  assert.equal(readReservedAt('/state/start-rate.json', { fs: { readFileSync: () => 'not json' } }), null)
  assert.equal(readReservedAt('/state/start-rate.json', { fs: { readFileSync: () => '{"other":1}' } }), null)
})
