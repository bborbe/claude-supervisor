// Unit tests for the session-ledger endpoint reader — the read side that replaces the
// filesystem roster.
//
// The property that matters is the THREE-STATE answer, the same one `session-heartbeat.test.mjs`
// pins beside it. `null` is "could not read it", `[]` is "read it, nothing was ever spawned",
// and a populated array is the ledger. A caller that folded the two would turn a store outage
// into a confident empty roster — the direction that hides live workers, which is the exact
// failure this module exists to remove.
//
// ⚠️ The second property is the KEY, and it is a regression guard rather than a convenience.
// `agent_id` is a per-process counter that restarts at 0 on every reconnect — the live ledger
// holds 1891 rows across 187 distinct values, 215 claiming `agent_1` — so a roster keyed on it
// collapses ~90% of the ledger. A future re-key to `agent_id` must fail loudly here.
//
// The transport is always injected, so this suite dials nothing.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  ledgerBySessionId,
  patchSessionLedgerRow,
  postSessionLedger,
  readSessionLedger,
  readSessionLedgerRow,
  recordToWire,
  SESSION_LEDGER_PATH,
} from './session-ledger.mjs'

const FIXTURE = 'http://fixture.invalid'

function serving(body, { status = 200, onUrl } = {}) {
  return async (url) => {
    onUrl?.(url)
    return { status, json: async () => body }
  }
}

// ---- readSessionLedger: 200 / null, never folded -----------------------------------------

test('a 200 with a row array is the ledger', async () => {
  const rows = [{ session_id: 'a' }, { session_id: 'b' }]
  assert.deepEqual(await readSessionLedger({ endpoint: FIXTURE, fetchImpl: serving(rows) }), rows)
})

test('the read goes to the contract path', async () => {
  let seen = null
  await readSessionLedger({ endpoint: FIXTURE, fetchImpl: serving([], { onUrl: (u) => (seen = u) }) })
  assert.equal(seen, `${FIXTURE}${SESSION_LEDGER_PATH}`)
})

test('a trailing slash on the endpoint does not double the path', async () => {
  let seen = null
  await readSessionLedger({
    endpoint: `${FIXTURE}/`,
    fetchImpl: serving([], { onUrl: (u) => (seen = u) }),
  })
  assert.equal(seen, `${FIXTURE}${SESSION_LEDGER_PATH}`)
})

test('a throwing transport is null, not an empty ledger', async () => {
  const fetchImpl = async () => {
    throw new Error('connect ECONNREFUSED')
  }
  assert.equal(await readSessionLedger({ endpoint: FIXTURE, fetchImpl }), null)
})

test('a failed read names its cause and elapsed milliseconds', async () => {
  // The ledger is the identity half of the worker count, so its failure reaches the operator
  // through the same refusal as the heartbeat's. Until 2026-10-19 both were a bare `null`, and
  // the refusal could not say which endpoint had failed, or why.
  const messages = []
  const fetchImpl = async () => {
    throw Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } })
  }
  await readSessionLedger({
    endpoint: FIXTURE,
    fetchImpl,
    onFailure: (message) => messages.push(message),
  })
  assert.equal(messages.length, 1)
  assert.match(messages[0], /^session-ledger read failed after \d+ ms: ECONNREFUSED/)
})

test('a read that fails once succeeds on the retry', async () => {
  let calls = 0
  const fetchImpl = async () => {
    calls += 1
    if (calls === 1) throw Object.assign(new Error('connect refused'), { code: 'ECONNREFUSED' })
    return { status: 200, json: async () => [{ session_id: 'a' }] }
  }
  const rows = await readSessionLedger({ endpoint: FIXTURE, fetchImpl })
  assert.equal(calls, 2)
  assert.deepEqual(rows, [{ session_id: 'a' }])
})

test('a non-200 and an unparseable body are named separately', async () => {
  const statusMessages = []
  await readSessionLedger({
    endpoint: FIXTURE,
    fetchImpl: serving([], { status: 500 }),
    onFailure: (m) => statusMessages.push(m),
  })
  assert.match(statusMessages[0], /HTTP 500/)

  const bodyMessages = []
  const fetchImpl = async () => ({
    status: 200,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON')
    },
  })
  await readSessionLedger({
    endpoint: FIXTURE,
    fetchImpl,
    onFailure: (m) => bodyMessages.push(m),
  })
  assert.match(bodyMessages[0], /unparseable body/)
})

test('a non-200 is null, not an empty ledger', async () => {
  for (const status of [404, 500, 503]) {
    assert.equal(await readSessionLedger({ endpoint: FIXTURE, fetchImpl: serving([], { status }) }), null, String(status))
  }
})

test('a body that is not a row array is null', async () => {
  for (const body of [{ error: 'nope' }, 'nope', null, 42]) {
    assert.equal(await readSessionLedger({ endpoint: FIXTURE, fetchImpl: serving(body) }), null, JSON.stringify(body))
  }
})

test('a body that is not JSON is null', async () => {
  const fetchImpl = async () => ({
    status: 200,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON')
    },
  })
  assert.equal(await readSessionLedger({ endpoint: FIXTURE, fetchImpl }), null)
})

test('an explicitly-off endpoint is null and dials nothing', async () => {
  // `SUPERVISOR_ATTENTION_STORE=off` resolves to a `null` endpoint — a deliberate opt-out of
  // the attention stack. It is NOT "nothing was spawned": with the store off there is no way
  // to answer, and `null` is the honest reply rather than an empty roster a caller would read
  // as a fleet with no workers.
  let called = false
  const fetchImpl = async () => {
    called = true
    return { status: 200, json: async () => [] }
  }
  assert.equal(await readSessionLedger({ endpoint: null, fetchImpl }), null)
  assert.equal(called, false)
})

// ---- readSessionLedgerRow: one row, `null` for BOTH "absent" and "unreadable" ------------

test('a 200 with an object is the row', async () => {
  const row = { session_id: 'a', parked_permission: { tool: 'Bash' } }
  assert.deepEqual(await readSessionLedgerRow('a', { endpoint: FIXTURE, fetchImpl: serving(row) }), row)
})

test('the single-row read goes to the per-id route, URL-encoded', async () => {
  let seen = null
  await readSessionLedgerRow('a b/c', {
    endpoint: FIXTURE,
    fetchImpl: serving({ session_id: 'x' }, { onUrl: (u) => (seen = u) }),
  })
  assert.equal(seen, `${FIXTURE}${SESSION_LEDGER_PATH}/a%20b%2Fc`)
})

test('a 404 is null — "no such row", which is exactly what an absent file answered', async () => {
  assert.equal(
    await readSessionLedgerRow('a', { endpoint: FIXTURE, fetchImpl: serving(null, { status: 404 }) }),
    null,
  )
})

test('a 500 is null — the store declining to answer is not a row', async () => {
  assert.equal(
    await readSessionLedgerRow('a', { endpoint: FIXTURE, fetchImpl: serving(null, { status: 500 }) }),
    null,
  )
})

test('a throwing transport is null, never a throw', async () => {
  const fetchImpl = async () => {
    throw new Error('boom')
  }
  assert.equal(await readSessionLedgerRow('a', { endpoint: FIXTURE, fetchImpl }), null)
})

test('a body that is not an object is null — an array or a scalar answers another question', async () => {
  for (const body of [[], 'row', 7, null]) {
    assert.equal(
      await readSessionLedgerRow('a', { endpoint: FIXTURE, fetchImpl: serving(body) }),
      null,
      `${JSON.stringify(body)} is not a row`,
    )
  }
})

test('a body that is not JSON is null', async () => {
  const fetchImpl = async () => ({
    status: 200,
    json: async () => {
      throw new Error('bad json')
    },
  })
  assert.equal(await readSessionLedgerRow('a', { endpoint: FIXTURE, fetchImpl }), null)
})

test('an empty or missing session id dials nothing', async () => {
  let called = false
  const fetchImpl = async () => {
    called = true
    return { status: 200, json: async () => ({}) }
  }
  assert.equal(await readSessionLedgerRow('', { endpoint: FIXTURE, fetchImpl }), null)
  assert.equal(await readSessionLedgerRow(undefined, { endpoint: FIXTURE, fetchImpl }), null)
  assert.equal(called, false)
})

test('an explicitly-off endpoint is null and dials nothing', async () => {
  let called = false
  const fetchImpl = async () => {
    called = true
    return { status: 200, json: async () => ({}) }
  }
  assert.equal(await readSessionLedgerRow('a', { endpoint: null, fetchImpl }), null)
  assert.equal(called, false)
})

// ---- ledgerBySessionId: the key is session_id, never agent_id ----------------------------

test('rows are keyed by session_id', () => {
  const known = ledgerBySessionId([{ session_id: 'a', agent_id: 'agent_1' }])
  assert.deepEqual([...known.keys()], ['a'])
  assert.equal(known.get('a').agent_id, 'agent_1')
})

test('a colliding agent_id keeps every row — the regression the old roster lost', () => {
  // Two rows sharing `agent_1` is the NORMAL shape of the live ledger (215 of 1891 rows), not
  // an edge case. A Map keyed on `agent_id` would hold one row here and silently drop the
  // other, which is the defect this endpoint replaces.
  const rows = [
    { session_id: 'aaaa1111', agent_id: 'agent_1' },
    { session_id: 'bbbb2222', agent_id: 'agent_1' },
  ]
  const known = ledgerBySessionId(rows)
  assert.equal(known.size, 2)
  assert.deepEqual([...known.keys()].sort(), ['aaaa1111', 'bbbb2222'])
})

test('a later row for one session replaces the earlier one', () => {
  // The store keys by `session_id`, so one session has one row; a list that carried two is a
  // last-write-wins read rather than two entries.
  const known = ledgerBySessionId([
    { session_id: 'a', label: 'first' },
    { session_id: 'a', label: 'second' },
  ])
  assert.equal(known.size, 1)
  assert.equal(known.get('a').label, 'second')
})

test('a malformed row is skipped, never thrown on', () => {
  const rows = [null, 'nope', 42, {}, { session_id: 42 }, { session_id: '' }, { session_id: 'ok' }]
  assert.deepEqual([...ledgerBySessionId(rows).keys()], ['ok'])
})

// ---- recordToWire: the two vocabularies, named -------------------------------------------

test('the spawner is renamed to the endpoint vocabulary', () => {
  // One name for one thing: the file record says `parent_session`, the endpoint says
  // `spawner_session_id`. Both must never appear on the wire — a row carrying two names for
  // the same fact is a row two readers can disagree about.
  const wire = recordToWire({ session_id: 'a', parent_session: 'p', label: 'x' })
  assert.equal(wire.spawner_session_id, 'p')
  assert.equal('parent_session' in wire, false)
  assert.equal(wire.label, 'x')
})

test('an absent spawner is the empty string, not a dropped key', () => {
  // 448 of the legacy rows have no spawner at all, so an unanchored spawn is a real state
  // rather than a malformed row, and the endpoint carries the field as optional.
  const wire = recordToWire({ session_id: 'a' })
  assert.equal(wire.spawner_session_id, '')
})

test('location defaults to local, because the endpoint requires it', () => {
  // The file record never carried a location, and the endpoint 400s without one. Every row
  // this supervisor writes is local; `pod` belongs to the sibling pod task.
  assert.equal(recordToWire({ session_id: 'a' }).location, 'local')
  assert.equal(recordToWire({ session_id: 'a', location: 'pod' }).location, 'pod')
})

test('a record with no session id has no wire row at all', () => {
  for (const record of [null, undefined, {}, { session_id: '' }, { session_id: 42 }]) {
    assert.equal(recordToWire(record), null, JSON.stringify(record))
  }
})

// ---- postSessionLedger: answers, never throws --------------------------------------------

test('a POST goes to the contract path and a 2xx is true', async () => {
  let seen = null
  let method = null
  const fetchImpl = async (url, opts) => {
    seen = url
    method = opts.method
    return { ok: true, status: 200 }
  }
  assert.equal(await postSessionLedger({ session_id: 'a' }, { endpoint: FIXTURE, fetchImpl }), true)
  assert.equal(seen, `${FIXTURE}${SESSION_LEDGER_PATH}`)
  assert.equal(method, 'POST')
})

test('a non-2xx is false rather than a throw', async () => {
  // A 400 is the store refusing a malformed row and a 503 is it being down; both are the
  // caller's to log, neither is an exception a spawn path may die on.
  for (const status of [400, 500, 503]) {
    const fetchImpl = async () => ({ ok: false, status })
    assert.equal(await postSessionLedger({ session_id: 'a' }, { endpoint: FIXTURE, fetchImpl }), false, String(status))
  }
})

test('a throwing transport is false, never an unhandled rejection', async () => {
  const fetchImpl = async () => {
    throw new Error('connect ECONNREFUSED')
  }
  assert.equal(await postSessionLedger({ session_id: 'a' }, { endpoint: FIXTURE, fetchImpl }), false)
})

test('an explicitly-off endpoint writes nothing and dials nothing', async () => {
  let called = false
  const fetchImpl = async () => {
    called = true
    return { ok: true, status: 200 }
  }
  assert.equal(await postSessionLedger({ session_id: 'a' }, { endpoint: null, fetchImpl }), false)
  assert.equal(called, false)
})

test('a null row is refused without dialling', async () => {
  let called = false
  const fetchImpl = async () => {
    called = true
    return { ok: true, status: 200 }
  }
  assert.equal(await postSessionLedger(null, { endpoint: FIXTURE, fetchImpl }), false)
  assert.equal(called, false)
})

// ---- patchSessionLedgerRow: the merge, and the body it must NOT send -----------------------

test('a PATCH goes to the per-id route and carries the patch alone', async () => {
  // ⚠️ **The body is the assertion that matters, not the method.** This function exists so a
  // server that did not spawn the row can write to it without blanking it, and the whole
  // guarantee lives in the body: the endpoint's `Post` replaces a row, so anything that looked
  // like a full record here would destroy the owner's fields. Sending `{ stopped_at }` and
  // nothing else is the contract, so the test pins the exact key set rather than a subset.
  let seen = null
  let method = null
  let sent = null
  const fetchImpl = async (url, opts) => {
    seen = url
    method = opts.method
    sent = JSON.parse(opts.body)
    return { ok: true, status: 200 }
  }
  const patch = { stopped_at: '2026-10-17T14:05:00.000Z' }
  const ok = await patchSessionLedgerRow('a5fefe95-6709-4da5-96ac-745f1625f442', patch, {
    endpoint: FIXTURE,
    fetchImpl,
  })
  assert.equal(ok, true)
  assert.equal(seen, `${FIXTURE}${SESSION_LEDGER_PATH}/a5fefe95-6709-4da5-96ac-745f1625f442`)
  assert.equal(method, 'PATCH')
  assert.deepEqual(sent, patch)
  assert.deepEqual(Object.keys(sent), ['stopped_at'])
})

test('the session id in the path is URL-encoded', async () => {
  let seen = null
  const fetchImpl = async (url) => {
    seen = url
    return { ok: true, status: 200 }
  }
  await patchSessionLedgerRow('a/b', { stopped_at: 'x' }, { endpoint: FIXTURE, fetchImpl })
  assert.equal(seen, `${FIXTURE}${SESSION_LEDGER_PATH}/a%2Fb`)
})

test('a trailing slash on the endpoint does not double the path', async () => {
  let seen = null
  const fetchImpl = async (url) => {
    seen = url
    return { ok: true, status: 200 }
  }
  await patchSessionLedgerRow('a', { stopped_at: 'x' }, { endpoint: `${FIXTURE}/`, fetchImpl })
  assert.equal(seen, `${FIXTURE}${SESSION_LEDGER_PATH}/a`)
})

test('a 404 is false — "no such row", the same answer a store outage gets', async () => {
  // ⚠️ A worker spawned before the ledger existed has no row, and a stop on it is a real state
  // rather than a bug. The caller's next move is identical to the outage case — log and carry
  // on — so collapsing the two here is deliberate, not a lost distinction.
  const fetchImpl = async () => ({ ok: false, status: 404 })
  assert.equal(
    await patchSessionLedgerRow('a', { stopped_at: 'x' }, { endpoint: FIXTURE, fetchImpl }),
    false,
  )
})

test('a non-2xx is false rather than a throw', async () => {
  for (const status of [400, 500, 503]) {
    const fetchImpl = async () => ({ ok: false, status })
    assert.equal(
      await patchSessionLedgerRow('a', { stopped_at: 'x' }, { endpoint: FIXTURE, fetchImpl }),
      false,
      String(status),
    )
  }
})

test('a throwing transport is false, never an unhandled rejection', async () => {
  const fetchImpl = async () => {
    throw new Error('connect ECONNREFUSED')
  }
  assert.equal(
    await patchSessionLedgerRow('a', { stopped_at: 'x' }, { endpoint: FIXTURE, fetchImpl }),
    false,
  )
})

test('an explicitly-off endpoint writes nothing and dials nothing', async () => {
  let called = false
  const fetchImpl = async () => {
    called = true
    return { ok: true, status: 200 }
  }
  assert.equal(
    await patchSessionLedgerRow('a', { stopped_at: 'x' }, { endpoint: null, fetchImpl }),
    false,
  )
  assert.equal(called, false)
})

test('a missing session id, or a patch that is not an object, is refused without dialling', async () => {
  let called = false
  const fetchImpl = async () => {
    called = true
    return { ok: true, status: 200 }
  }
  for (const [sessionId, patch] of [
    [undefined, { stopped_at: 'x' }],
    ['', { stopped_at: 'x' }],
    [null, { stopped_at: 'x' }],
    ['a', null],
    ['a', undefined],
    ['a', 'stopped_at'],
    ['a', []],
  ]) {
    assert.equal(
      await patchSessionLedgerRow(sessionId, patch, { endpoint: FIXTURE, fetchImpl }),
      false,
      `${String(sessionId)} / ${JSON.stringify(patch)}`,
    )
  }
  assert.equal(called, false)
})
