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
  StreamLaggedError,
  WrapperError,
  answerWrappedSession,
  parseSSE,
  readParkedQuestion,
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

test('streamWrappedSession reports a non-200 as a WrapperError', async () => {
  const fetchImpl = async () => ({ ok: false, status: 404 })
  await assert.rejects(
    (async () => {
      for await (const _ of streamWrappedSession({ baseUrl: 'http://w', sessionId: SESSION, fetchImpl })) {
        // no messages
      }
    })(),
    (err) => err instanceof WrapperError && err.status === 404,
  )
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
