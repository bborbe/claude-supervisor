// Unit tests for the wrapper event-stream adapter.
//
// The load-bearing property is not "the parser works" — it is that a stream which ends for
// the WRONG reason never reaches `runAgentLoop` as a completed turn. The loop settles a
// worker as `done` when iteration ends, so a subscriber that fell behind would mark a still-
// generating worker finished. That is the same class of defect as the mid-turn `result`
// `agent-loop.mjs` already guards, and it is the one behaviour these tests exist to pin.
//
// The second property is chunk-boundary safety: a `data:` frame split across two reads must
// be buffered, not dropped. A parser that only works when each chunk is a whole frame passes
// every happy-path test and loses messages under real network timing.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  StreamExhaustedError,
  StreamGapError,
  StreamLaggedError,
  StreamSessionGoneError,
  WrapperError,
  answerWrappedSession,
  listWrappedSessions,
  parseSSE,
  pendingQuestionOf,
  readParkedQuestion,
  sendWrappedSession,
  spawnWrappedSession,
  streamWrappedSession,
} from './wrapper-stream.mjs'

const SESSION = 'a1b2c3d4-0000-4000-8000-000000000001'

/** A body that yields the given strings as chunks, one per read. */
function bodyOf(...chunks) {
  const encoder = new TextEncoder()
  return (async function* () {
    for (const chunk of chunks) yield encoder.encode(chunk)
  })()
}

function jsonResponse(payload, { status = 200 } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  }
}

function sseResponse(chunks) {
  return {
    ok: true,
    status: 200,
    body: bodyOf(...chunks),
  }
}

/** A body that delivers its chunks and then throws — the shape of a connection dying. */
function dyingResponse(chunks, err = new TypeError('terminated')) {
  const encoder = new TextEncoder()
  return {
    ok: true,
    status: 200,
    body: (async function* () {
      for (const chunk of chunks) yield encoder.encode(chunk)
      throw err
    })(),
  }
}

// Fast, bounded policy so a test never waits on real backoff.
const FAST_RECONNECT = { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 1, maxElapsedMs: 500 }

test('parseSSE buffers a frame split across two chunks', async () => {
  const seen = []
  for await (const message of parseSSE(bodyOf('data: {"type":"assistant","n":1}\n\ndata: {"type":"res', 'ult"}\n\n'))) {
    seen.push(message)
  }
  assert.deepEqual(seen, [
    { type: 'assistant', n: 1 },
    { type: 'result' },
  ])
})

test('parseSSE stops at the end sentinel and ignores frames after it', async () => {
  const seen = []
  for await (const message of parseSSE(bodyOf('data: {"type":"result"}\n\nevent: end\ndata: {}\n\ndata: {"type":"late"}\n\n'))) {
    seen.push(message)
  }
  assert.deepEqual(seen, [{ type: 'result' }])
})

test('parseSSE surfaces an unparseable frame instead of dropping it', async () => {
  const seen = []
  for await (const message of parseSSE(bodyOf('data: not-json\n\n'))) {
    seen.push(message)
  }
  assert.equal(seen.length, 1)
  assert.equal(seen[0].type, 'wrapper_parse_error')
  assert.equal(seen[0].raw, 'not-json')
})

test('streamWrappedSession yields the turn and completes when the child exited', async () => {
  const calls = []
  const fetchImpl = async (url, init) => {
    calls.push({ url, method: init?.method })
    if (url.endsWith('/events')) {
      return sseResponse(['data: {"type":"system","subtype":"init","session_id":"s1"}\n\n', 'data: {"type":"result"}\n\nevent: end\ndata: {}\n\n'])
    }
    return jsonResponse({ id: SESSION, state: 'exited' })
  }

  const seen = []
  for await (const message of streamWrappedSession({ baseUrl: 'http://w', sessionId: SESSION, fetchImpl })) {
    seen.push(message)
  }
  assert.deepEqual(seen.map((m) => m.type), ['system', 'result'])
  assert.equal(calls[0].url, `http://w/api/1.0/sessions/${SESSION}/events`)
  assert.equal(calls[1].url, `http://w/api/1.0/sessions/${SESSION}`)
})

test('streamWrappedSession throws StreamLaggedError when the child is still generating', async () => {
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      return sseResponse(['data: {"type":"result"}\n\nevent: end\ndata: {}\n\n'])
    }
    return jsonResponse({ id: SESSION, state: 'generating' })
  }

  const seen = []
  await assert.rejects(
    (async () => {
      for await (const message of streamWrappedSession({ baseUrl: 'http://w', sessionId: SESSION, fetchImpl })) {
        seen.push(message)
      }
    })(),
    (err) => {
      assert.ok(err instanceof StreamLaggedError)
      assert.equal(err.state, 'generating')
      return true
    },
  )
  // The messages before the end are still delivered — a lag does not retroactively erase
  // the transcript the loop already consumed.
  assert.equal(seen.length, 1)
})

// ⚠️ The stream path no longer throws a plain `WrapperError`: every terminal answer is a GAP
// class, because none of them proves the child finished. A non-gap error here would let a
// captured `result` settle the worker `done` on a stream that never sent its end sentinel.
test('streamWrappedSession reports a gone session as a gap, not a plain failure', async () => {
  const fetchImpl = async () => ({ ok: false, status: 410 })
  await assert.rejects(
    (async () => {
      for await (const _ of streamWrappedSession({ baseUrl: 'http://w', sessionId: SESSION, fetchImpl })) {
        // no messages
      }
    })(),
    (err) => {
      assert.ok(err instanceof StreamSessionGoneError, `expected StreamSessionGoneError, got ${err?.name}`)
      assert.equal(err.gap, true)
      assert.equal(err.status, 410)
      return true
    },
  )
})

// ── Reconnect across a daemon restart ──────────────────────────────────────────────────────
// The wrapper is restarted routinely, so a connection dying mid-turn is a GAP to ride out,
// not the end of the turn. `id:` is the only resume point that survives the restart, and a
// client that ignores it settles a still-working worker as `error` — the defect this exists
// to remove. The 409 case is the opposite: a resume point the wrapper no longer retains must
// never be papered over, because the transcript would carry an invisible hole.

test('parseSSE records the id of every frame and marks the end sentinel', async () => {
  const tracker = { lastId: null, ended: false }
  const seen = []
  for await (const message of parseSSE(
    bodyOf('id: 1\ndata: {"type":"a"}\n\nid: 2\ndata: {"type":"b"}\n\nevent: end\ndata: {}\n\n'),
    tracker,
  )) {
    seen.push(message)
  }
  assert.deepEqual(seen.map((m) => m.type), ['a', 'b'])
  assert.equal(tracker.lastId, '2')
  assert.equal(tracker.ended, true)
})

test('streamWrappedSession resumes with Last-Event-ID and finishes the turn instead of erroring', async () => {
  const calls = []
  let n = 0
  const fetchImpl = async (url, init) => {
    calls.push({ url, headers: init?.headers })
    if (url.endsWith('/events')) {
      n += 1
      if (n === 1) {
        // The daemon dies mid-turn: two frames land, then the socket drops.
        return dyingResponse(['id: 1\ndata: {"type":"system"}\n\n', 'id: 2\ndata: {"type":"assistant"}\n\n'])
      }
      // The successor re-adopts the child and serves from the requested point.
      return sseResponse(['id: 3\ndata: {"type":"result"}\n\nevent: end\ndata: {}\n\n'])
    }
    return jsonResponse({ id: SESSION, state: 'exited' })
  }

  const seen = []
  for await (const message of streamWrappedSession({
    baseUrl: 'http://w',
    sessionId: SESSION,
    fetchImpl,
    reconnect: FAST_RECONNECT,
  })) {
    seen.push(message)
  }

  assert.deepEqual(seen.map((m) => m.type), ['system', 'assistant', 'result'])
  const streamCalls = calls.filter((c) => c.url.endsWith('/events'))
  assert.equal(streamCalls.length, 2, 'the stream must be reopened exactly once')
  assert.equal(streamCalls[0].headers['Last-Event-ID'], undefined, 'the first attempt has nothing to resume from')
  assert.equal(streamCalls[1].headers['Last-Event-ID'], '2', 'the retry resumes from the last id it saw')
})

test('streamWrappedSession throws StreamGapError when the wrapper no longer retains the resume point', async () => {
  let n = 0
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      n += 1
      if (n === 1) return dyingResponse(['id: 7\ndata: {"type":"assistant"}\n\n'])
      return { ok: false, status: 409, json: async () => ({}) }
    }
    return jsonResponse({ id: SESSION, state: 'generating' })
  }

  await assert.rejects(
    (async () => {
      for await (const _ of streamWrappedSession({
        baseUrl: 'http://w',
        sessionId: SESSION,
        fetchImpl,
        reconnect: FAST_RECONNECT,
      })) {
        // drain
      }
    })(),
    (err) => {
      assert.ok(err instanceof StreamGapError, `expected StreamGapError, got ${err?.name}`)
      assert.equal(err.lastEventId, '7')
      return true
    },
  )
  // A gap is never retried: reopening from a different point is the failure it reports.
  assert.equal(n, 2)
})

test('streamWrappedSession gives up in bounded time when the wrapper never returns', async () => {
  let attempts = 0
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      attempts += 1
      return dyingResponse(['id: 1\ndata: {"type":"assistant"}\n\n'])
    }
    return jsonResponse({ id: SESSION, state: 'generating' })
  }

  await assert.rejects(
    (async () => {
      for await (const _ of streamWrappedSession({
        baseUrl: 'http://w',
        sessionId: SESSION,
        fetchImpl,
        reconnect: { maxAttempts: 2, baseDelayMs: 1, maxDelayMs: 1, maxElapsedMs: 500 },
      })) {
        // drain
      }
    })(),
    // ⚠️ Exhaustion surfaces as a GAP class, not the raw body error: the child never said it
    // finished, so the loop must not read this as a completed turn.
    (err) => err instanceof StreamExhaustedError && err.gap === true,
  )
  // 1 initial + 2 retries, then it stops rather than spinning forever.
  assert.equal(attempts, 3)
})

// ⚠️ The spec the blocking review asked for. The budget used to be measured from stream
// OPEN, so a worker whose stream had been running longer than maxElapsedMs had zero retry —
// exactly the parked and long-running workers this change exists to protect. This test fails
// against that implementation and passes once the clock starts at the OUTAGE.
test('reconnects a long-lived stream that has been open far past maxElapsedMs', async () => {
  let clock = 0
  let n = 0
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      n += 1
      if (n === 1) {
        // Healthy for an hour, then the daemon restarts.
        clock += 3_600_000
        return dyingResponse(['id: 1\ndata: {"type":"assistant"}\n\n'])
      }
      return sseResponse(['id: 2\ndata: {"type":"result"}\n\nevent: end\ndata: {}\n\n'])
    }
    return jsonResponse({ id: SESSION, state: 'exited' })
  }

  const seen = []
  for await (const message of streamWrappedSession({
    baseUrl: 'http://w',
    sessionId: SESSION,
    fetchImpl,
    reconnect: { ...FAST_RECONNECT, now: () => clock },
  })) {
    seen.push(message)
  }
  assert.deepEqual(seen.map((m) => m.type), ['assistant', 'result'])
  assert.equal(n, 2, 'the long-lived stream must be reopened exactly once')
})

test('returns cleanly when the signal aborts during the backoff sleep', async () => {
  const controller = new AbortController()
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      setTimeout(() => controller.abort(), 10)
      return dyingResponse(['id: 1\ndata: {"type":"assistant"}\n\n'])
    }
    return jsonResponse({ id: SESSION, state: 'generating' })
  }

  const seen = []
  await assert.doesNotReject(async () => {
    for await (const message of streamWrappedSession({
      baseUrl: 'http://w',
      sessionId: SESSION,
      fetchImpl,
      signal: controller.signal,
      reconnect: { maxAttempts: 5, baseDelayMs: 1_000, maxDelayMs: 1_000, maxElapsedMs: 60_000 },
    })) {
      seen.push(message)
    }
  })
  assert.deepEqual(seen.map((m) => m.type), ['assistant'])
})

test('gives up after the retry budget when the wrapper keeps returning 5xx', async () => {
  let n = 0
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      n += 1
      return { ok: false, status: 503 }
    }
    return jsonResponse({ id: SESSION, state: 'generating' })
  }

  await assert.rejects(
    (async () => {
      for await (const _ of streamWrappedSession({
        baseUrl: 'http://w',
        sessionId: SESSION,
        fetchImpl,
        reconnect: { maxAttempts: 2, baseDelayMs: 1, maxDelayMs: 1, maxElapsedMs: 500 },
      })) {
        // drain
      }
    })(),
    (err) => {
      // ⚠️ Exhaustion must be a GAP, not a plain failure: the child never said it finished,
      // so a captured `result` must not settle the worker `done`.
      assert.ok(err instanceof StreamExhaustedError, `expected StreamExhaustedError, got ${err?.name}`)
      assert.equal(err.gap, true, 'exhaustion carries the gap marker the loop tests')
      return true
    },
  )
  assert.equal(n, 3, 'one initial attempt plus two retries')
})

test('does not reconnect blind when the stream carried frames but no ids', async () => {
  let n = 0
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      n += 1
      // An older wrapper: frames with data but no `id:` line, then the body dies.
      return dyingResponse(['data: {"type":"assistant"}\n\n'])
    }
    return jsonResponse({ id: SESSION, state: 'generating' })
  }

  await assert.rejects(
    (async () => {
      for await (const _ of streamWrappedSession({
        baseUrl: 'http://w',
        sessionId: SESSION,
        fetchImpl,
        reconnect: FAST_RECONNECT,
      })) {
        // drain
      }
    })(),
    (err) => {
      assert.ok(err instanceof StreamGapError, `expected StreamGapError, got ${err?.name}`)
      assert.equal(err.gap, true)
      return true
    },
  )
  assert.equal(n, 1, 'resuming is impossible without an id, so it must not retry')
})

// ⚠️ A body that closes CLEANLY without the end sentinel is the shape a graceful drain
// produces, and it must exhaust as a gap — not settle a captured result `done`.
test('a clean body close without the end sentinel exhausts as a gap', async () => {
  let n = 0
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      n += 1
      return sseResponse(['id: 1\ndata: {"type":"result"}\n\n']) // no event: end
    }
    return jsonResponse({ id: SESSION, state: 'generating' })
  }

  await assert.rejects(
    (async () => {
      for await (const _ of streamWrappedSession({
        baseUrl: 'http://w',
        sessionId: SESSION,
        fetchImpl,
        reconnect: { maxAttempts: 2, baseDelayMs: 1, maxDelayMs: 1, maxElapsedMs: 500 },
      })) {
        // drain
      }
    })(),
    (err) => {
      assert.ok(err instanceof StreamExhaustedError, `expected StreamExhaustedError, got ${err?.name}`)
      assert.equal(err.gap, true)
      assert.match(err.reason, /closed without the end sentinel/)
      return true
    },
  )
  assert.equal(n, 3)
})

// The budget must reset per OUTAGE. Two separate outages, each preceded by real progress,
// each get their own budget — otherwise a long session dies on its second restart.
test('the budget resets after a reconnect that made progress', async () => {
  let clock = 0
  let n = 0
  const fetchImpl = async (url) => {
    if (!url.endsWith('/events')) return jsonResponse({ id: SESSION, state: 'exited' })
    n += 1
    if (n === 1) {
      clock += 10_000
      return dyingResponse(['id: 1\ndata: {"type":"assistant"}\n\n'])
    }
    if (n === 2) {
      // Resumed with a NEW id — real progress, so the next outage gets a fresh budget.
      clock += 100_000
      return dyingResponse(['id: 2\ndata: {"type":"assistant"}\n\n'])
    }
    return sseResponse(['id: 3\ndata: {"type":"result"}\n\nevent: end\ndata: {}\n\n'])
  }

  const seen = []
  for await (const message of streamWrappedSession({
    baseUrl: 'http://w',
    sessionId: SESSION,
    fetchImpl,
    reconnect: { maxAttempts: 1, baseDelayMs: 1, maxDelayMs: 1, maxElapsedMs: 500, now: () => clock },
  })) {
    seen.push(message)
  }
  // maxAttempts 1 means one retry per outage; two outages each got theirs.
  assert.deepEqual(seen.map((m) => m.type), ['assistant', 'assistant', 'result'])
  assert.equal(n, 3)
})

test('treats a 404 during the restart window as terminal, not a blip', async () => {
  let n = 0
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      n += 1
      return { ok: false, status: 404 }
    }
    return jsonResponse({ id: SESSION, state: 'exited' })
  }

  await assert.rejects(
    (async () => {
      for await (const _ of streamWrappedSession({
        baseUrl: 'http://w',
        sessionId: SESSION,
        fetchImpl,
        reconnect: FAST_RECONNECT,
      })) {
        // drain
      }
    })(),
    (err) => {
      // ⚠️ A gap class, not a plain WrapperError: the stream ended without the sentinel,
      // so a captured result must not settle the worker done.
      assert.ok(err instanceof StreamSessionGoneError, `expected StreamSessionGoneError, got ${err?.name}`)
      assert.equal(err.status, 404)
      assert.equal(err.gap, true)
      return true
    },
  )
  assert.equal(n, 1, 'a gone session is not retried')
})

test('streamWrappedSession treats a refused connection as a restart, not a failure', async () => {
  let n = 0
  const fetchImpl = async (url) => {
    if (url.endsWith('/events')) {
      n += 1
      if (n === 1) throw new TypeError('fetch failed')
      return sseResponse(['id: 4\ndata: {"type":"result"}\n\nevent: end\ndata: {}\n\n'])
    }
    return jsonResponse({ id: SESSION, state: 'exited' })
  }

  const seen = []
  for await (const message of streamWrappedSession({
    baseUrl: 'http://w',
    sessionId: SESSION,
    fetchImpl,
    reconnect: FAST_RECONNECT,
  })) {
    seen.push(message)
  }
  assert.deepEqual(seen.map((m) => m.type), ['result'])
})

test('spawnWrappedSession posts the options and returns the session', async () => {
  let sent
  const fetchImpl = async (url, init) => {
    sent = { url, body: JSON.parse(init.body) }
    return jsonResponse({ id: SESSION, state: 'starting' }, { status: 201 })
  }
  const session = await spawnWrappedSession({ baseUrl: 'http://w/', options: { cwd: '/tmp/x' }, fetchImpl })
  assert.equal(session.id, SESSION)
  assert.equal(sent.url, 'http://w/api/1.0/sessions')
  assert.deepEqual(sent.body, { cwd: '/tmp/x' })
})

test('sendWrappedSession posts the turn as a message', async () => {
  let sent
  const fetchImpl = async (url, init) => {
    sent = { url, method: init.method, body: JSON.parse(init.body) }
    return jsonResponse({ status: 'sent' })
  }
  const out = await sendWrappedSession({ baseUrl: 'http://w', sessionId: SESSION, text: 'Reply with ok', fetchImpl })
  assert.equal(out.status, 'sent')
  assert.equal(sent.url, `http://w/api/1.0/sessions/${SESSION}/message`)
  assert.equal(sent.method, 'POST')
  assert.deepEqual(sent.body, { text: 'Reply with ok' })
})

// A refused send is the failure that looks like success: the child exists, streams, and
// never starts a turn.
test('sendWrappedSession reports a refusal instead of returning quietly', async () => {
  const fetchImpl = async () => ({ ok: false, status: 400 })
  await assert.rejects(
    sendWrappedSession({ baseUrl: 'http://w', sessionId: SESSION, text: '', fetchImpl }),
    (err) => err instanceof WrapperError && err.status === 400,
  )
})

test('answerWrappedSession treats 409 as a failure, not a released gate', async () => {
  const fetchImpl = async () => ({ ok: false, status: 409 })
  await assert.rejects(
    answerWrappedSession({ baseUrl: 'http://w', sessionId: SESSION, requestId: 'r1', fetchImpl }),
    (err) => err instanceof WrapperError && err.status === 409,
  )
})

test('readParkedQuestion returns null when nothing is parked', async () => {
  const fetchImpl = async () => jsonResponse({ id: SESSION, state: 'generating' })
  assert.equal(await readParkedQuestion({ baseUrl: 'http://w', sessionId: SESSION, fetchImpl }), null)
})

test('readParkedQuestion returns the question when the child is parked', async () => {
  const question = { request_id: 'r1', tool_name: 'Bash', input: {}, options: ['allow', 'deny'] }
  const fetchImpl = async () => jsonResponse({ id: SESSION, state: 'blocked-on-permission', question })
  assert.deepEqual(await readParkedQuestion({ baseUrl: 'http://w', sessionId: SESSION, fetchImpl }), question)
})

test('listWrappedSessions reads the collection in one call', async () => {
  let seen
  const fetchImpl = async (url) => {
    seen = url
    return jsonResponse([{ id: SESSION, state: 'blocked-on-permission' }])
  }
  const all = await listWrappedSessions({ baseUrl: 'http://w/', fetchImpl })
  assert.equal(seen, 'http://w/api/1.0/sessions')
  assert.equal(all.length, 1)
})

test('listWrappedSessions reports a non-200 rather than returning an empty fleet', async () => {
  const fetchImpl = async () => ({ ok: false, status: 503 })
  await assert.rejects(
    listWrappedSessions({ baseUrl: 'http://w', fetchImpl }),
    (err) => err instanceof WrapperError && err.status === 503,
  )
})

// ── pendingQuestionOf: the three states, which must not collapse ────────────────────────────
// This is what a manager reads to obtain the request id it then passes to `answer_permission`.
// A `null` for a wrapper-backed worker whose wrapper is down would report "not parked" during
// exactly the outage that matters, so the unreadable state is separate and pinned here.
test('pendingQuestionOf is null for a worker that is not wrapper-backed', () => {
  assert.equal(pendingQuestionOf([{ id: SESSION, question: { request_id: 'r1' } }], null), null)
  assert.equal(pendingQuestionOf(null, undefined), null)
})

test('pendingQuestionOf returns the live question for a parked worker', () => {
  const question = { request_id: 'r1', tool_name: 'Bash', input: {} }
  assert.deepEqual(pendingQuestionOf([{ id: SESSION, question }], SESSION), question)
})

test('pendingQuestionOf is null when the wrapper lists the session with nothing parked', () => {
  assert.equal(pendingQuestionOf([{ id: SESSION, state: 'generating' }], SESSION), null)
})

test('pendingQuestionOf is null when the wrapper no longer lists the session', () => {
  // An exited-and-reaped child is not a park either — the distinction is the state, not absence.
  assert.equal(pendingQuestionOf([{ id: 'other-session' }], SESSION), null)
})

test('pendingQuestionOf reports an unreadable wrapper as unreadable, never as "not parked"', () => {
  const out = pendingQuestionOf(null, SESSION)
  assert.equal(out.unreadable, true)
  assert.equal(out.request_id, undefined)
})
