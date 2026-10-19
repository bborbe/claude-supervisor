// Unit tests for the session-heartbeat endpoint reader — the server's first HTTP reader.
//
// The property that matters is the THREE-STATE answer. `null` is "could not read it", `[]` is
// "read it, nothing is live", and a populated array is the fleet. The caller refuses a spawn on
// `null` and permits one on `[]`, so a test that only checked the happy path would pass on a
// reader that folded a store outage into an empty fleet — the one direction a concurrent-worker
// cap must never fail in.
//
// The transport is always injected, so this suite dials nothing: a real store on this machine
// cannot turn an assertion green, and the read is exercised in-process.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { HEARTBEAT_PATH, liveFromHeartbeat, readHeartbeat } from './session-heartbeat.mjs'

const FIXTURE = 'http://fixture.invalid'

function serving(body, { status = 200, onUrl } = {}) {
  return async (url) => {
    onUrl?.(url)
    return { status, json: async () => body }
  }
}

test('a 200 with a row array is the fleet', async () => {
  const rows = [{ session_id: 'a', live: true }]
  assert.deepEqual(await readHeartbeat({ endpoint: FIXTURE, fetchImpl: serving(rows) }), rows)
})

test('the read goes to the contract path', async () => {
  let seen = null
  await readHeartbeat({ endpoint: FIXTURE, fetchImpl: serving([], { onUrl: (u) => (seen = u) }) })
  assert.equal(seen, `${FIXTURE}${HEARTBEAT_PATH}`)
})

test('a trailing slash on the endpoint does not double the path', async () => {
  // The config value is operator-supplied, so `http://host:18080/` is a legal spelling and must
  // resolve to the same URL rather than `//api/...`.
  let seen = null
  await readHeartbeat({
    endpoint: `${FIXTURE}/`,
    fetchImpl: serving([], { onUrl: (u) => (seen = u) }),
  })
  assert.equal(seen, `${FIXTURE}${HEARTBEAT_PATH}`)
})

test('a throwing transport is null, not an empty fleet', async () => {
  const fetchImpl = async () => {
    throw new Error('connect ECONNREFUSED')
  }
  assert.equal(await readHeartbeat({ endpoint: FIXTURE, fetchImpl }), null)
})

test('a non-200 is null, not an empty fleet', async () => {
  // A 404 is the PER-ID route's "never posted" and is not an answer to the list question; any
  // other status is the store declining. Both are "could not read it" here.
  for (const status of [404, 500, 503]) {
    assert.equal(await readHeartbeat({ endpoint: FIXTURE, fetchImpl: serving([], { status }) }), null, String(status))
  }
})

test('a body that is not a row array is null', async () => {
  for (const body of [{ error: 'nope' }, 'nope', null, 42]) {
    assert.equal(await readHeartbeat({ endpoint: FIXTURE, fetchImpl: serving(body) }), null, JSON.stringify(body))
  }
})

test('a body that is not JSON is null', async () => {
  const fetchImpl = async () => ({
    status: 200,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON')
    },
  })
  assert.equal(await readHeartbeat({ endpoint: FIXTURE, fetchImpl }), null)
})

// ---- failure diagnostics: cause + elapsed ms ----------------------------------------------
//
// `null` is still the answer; what changed on 2026-10-19 is that it now carries a reason. These
// cases pin the three causes the operator needs to tell apart — a slow store, a dead socket, and
// a store answering something unparseable — because the spawn refusal prints this text and the
// next occurrence has to be diagnosable from it.

test('a timeout is named a timeout, with its elapsed milliseconds', async () => {
  const messages = []
  const fetchImpl = async () => {
    throw Object.assign(new Error('The operation was aborted due to timeout'), {
      name: 'TimeoutError',
    })
  }
  const rows = await readHeartbeat({
    endpoint: FIXTURE,
    fetchImpl,
    onFailure: (message) => messages.push(message),
  })
  assert.equal(rows, null)
  assert.equal(messages.length, 1)
  assert.match(messages[0], /^session-heartbeat read failed after \d+ ms: timeout$/)
})

test('a refused socket is named by its code, including through fetch’s cause wrapper', async () => {
  const messages = []
  // Node's fetch reports a socket failure as `TypeError: fetch failed` and hides the code on
  // `cause`; reporting the TypeError alone is the collapse this change removes.
  const fetchImpl = async () => {
    throw Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } })
  }
  await readHeartbeat({ endpoint: FIXTURE, fetchImpl, onFailure: (m) => messages.push(m) })
  assert.match(messages[0], /ECONNREFUSED/)
})

test('a non-200 is named by its status', async () => {
  const messages = []
  await readHeartbeat({
    endpoint: FIXTURE,
    fetchImpl: serving([], { status: 503 }),
    onFailure: (m) => messages.push(m),
  })
  assert.match(messages[0], /HTTP 503/)
})

test('an unparseable body is named as unparseable, not as a transport failure', async () => {
  const messages = []
  const fetchImpl = async () => ({
    status: 200,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON')
    },
  })
  await readHeartbeat({ endpoint: FIXTURE, fetchImpl, onFailure: (m) => messages.push(m) })
  assert.match(messages[0], /unparseable body/)
})

test('an explicitly-off endpoint is null and dials nothing', async () => {
  // `SUPERVISOR_ATTENTION_STORE=off` resolves to a `null` endpoint. It is NOT "nothing is live":
  // the store is the fleet's only liveness source now, so a deployment that turned it off has no
  // way to answer, and `null` refuses a spawn rather than permitting one.
  let called = false
  const fetchImpl = async () => {
    called = true
    return { status: 200, json: async () => [] }
  }
  assert.equal(await readHeartbeat({ endpoint: null, fetchImpl }), null)
  assert.equal(called, false)
})

// ---- liveFromHeartbeat: the one field that grants liveness -------------------------------

test('only an explicit live: true is live', () => {
  const rows = [
    { session_id: 'a', live: true, source: 'mcp-timer' },
    { session_id: 'b', live: false },
    { session_id: 'c', live: 'true' },
    { session_id: 'd', live: 1 },
    { session_id: 'e' },
  ]
  const live = liveFromHeartbeat(rows)
  assert.deepEqual([...live.keys()], ['a'])
  assert.equal(live.get('a'), 'mcp-timer')
})

test('a malformed row is skipped, never thrown on', () => {
  // This runs on the spawn path: an uncaught throw is a failed spawn, not a wrong count.
  const rows = [null, 'nope', 42, { live: true }, { session_id: 42, live: true }, { session_id: '', live: true }, { session_id: 'ok', live: true }]
  assert.deepEqual([...liveFromHeartbeat(rows).keys()], ['ok'])
})

test('a missing source maps to null rather than undefined', () => {
  const live = liveFromHeartbeat([{ session_id: 'a', live: true }])
  assert.equal(live.get('a'), null)
})
