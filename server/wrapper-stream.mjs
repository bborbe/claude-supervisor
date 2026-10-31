// Read a claude-wrapper child's event stream as the same async iterable `query()` hands
// `runAgentLoop`.
//
// Why this module exists: a headless worker is currently an in-process SDK `query()`, and
// its `canUseTool` callback parks a permission request into THIS process's `pending` Map.
// That is the dead end the wrapper was built to remove — a park belongs to whoever holds the
// child's stdin, and a request parked by another manager's server is refused. The wrapper
// owns the child instead, so `POST /api/1.0/sessions/{id}/answer` can release a park that no
// amount of MCP plumbing can reach here.
//
// The seam is narrow on purpose. `runAgentLoop` is not rewritten: it consumes an
// `AsyncIterable` of SDK messages and keys everything off message shape — `system`/`init`
// carries the session id and starts the heartbeat, `result` is captured rather than settled,
// and the terminal state is settled when the stream ENDS. The wrapper already emits the
// child's raw JSONL verbatim over SSE, and those lines are the same SDK message shapes
// (`claude --output-format stream-json` produces them). So this is an adapter, and the loop,
// the ledger, the heartbeat and the resume guards stay exactly as they are.
//
// ⚠️ **The one thing the adapter must not blur: "stream ended" is not one condition.** The
// wrapper ends the response in two cases that look identical on the wire — the child exited,
// and the subscriber fell so far behind that the stream stopped being a faithful account of
// it. The loop settles a worker as `done` when its stream ends, so treating a lag as an exit
// would mark a worker finished while it is still generating, which is the same class of
// defect as the mid-turn `result` that `agent-loop.mjs` already guards against. The end
// frame therefore carries no reason and this module asks the wrapper directly: `exited` is a
// real end, anything else is a `StreamLaggedError` the caller must not settle as success.

const DEFAULT_TIMEOUT_MS = 10_000

/** Raised when the stream stopped for a reason other than the child exiting. */
export class StreamLaggedError extends Error {
  constructor(sessionId, state) {
    super(`session ${sessionId} stream ended while the child was still ${state} — the subscriber fell behind, so this is not a completed turn`)
    this.name = 'StreamLaggedError'
    this.sessionId = sessionId
    this.state = state
  }
}

/** Raised when the wrapper cannot be reached or refuses the request. */
export class WrapperError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'WrapperError'
    this.status = status
  }
}

function baseOf(baseUrl) {
  return String(baseUrl).replace(/\/+$/, '')
}

/**
 * Ask the wrapper to spawn a child.
 *
 * @param {object} args
 * @param {string} args.baseUrl   wrapper origin, e.g. `http://localhost:18090`
 * @param {object} args.options   `{cwd, model, permission_mode, permission_prompt}`
 * @param {Function} [args.fetchImpl]
 * @returns {Promise<object>} the wrapper's session record
 */
export async function spawnWrappedSession({ baseUrl, options = {}, fetchImpl = fetch, timeoutMs = DEFAULT_TIMEOUT_MS }) {
  const resp = await fetchImpl(`${baseOf(baseUrl)}/api/1.0/sessions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(options),
    signal: AbortSignal.timeout(timeoutMs),
  })
  if (!resp.ok) {
    throw new WrapperError(`wrapper spawn failed: HTTP ${resp.status}`, resp.status)
  }
  return resp.json()
}

/**
 * Answer the permission request a wrapped child is parked on.
 *
 * The behaviour is deliberately a parameter rather than a boolean: the wrapper's own
 * vocabulary is `allow` / `deny`, and a caller that maps it here keeps one translation
 * point instead of one per call site.
 */
export async function answerWrappedSession({ baseUrl, sessionId, requestId, behavior = 'allow', fetchImpl = fetch, timeoutMs = DEFAULT_TIMEOUT_MS }) {
  const resp = await fetchImpl(`${baseOf(baseUrl)}/api/1.0/sessions/${encodeURIComponent(sessionId)}/answer`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ request_id: requestId, behavior }),
    signal: AbortSignal.timeout(timeoutMs),
  })
  if (!resp.ok) {
    // 409 answered-already / no open question, 410 child exited, 503 child unreachable,
    // 404 unknown session — all four mean the answer did not land, and a caller that treats
    // any of them as success reports a released gate that never moved.
    throw new WrapperError(`wrapper answer failed: HTTP ${resp.status}`, resp.status)
  }
  return resp.json()
}

/**
 * Deliver one turn to a wrapped child.
 *
 * ⚠️ This exists because the wrapper has no `prompt` field on spawn — a turn arrives as a
 * message. `query({ prompt })` delivered the opening turn as part of creating the session,
 * so a caller that spawns through the wrapper and then only streams gets a child that
 * starts, initialises and sits idle forever. A failed send is never a no-op for that
 * reason: the idle child looks exactly like one that is thinking.
 */
export async function sendWrappedSession({ baseUrl, sessionId, text, fetchImpl = fetch, timeoutMs = DEFAULT_TIMEOUT_MS }) {
  const resp = await fetchImpl(`${baseOf(baseUrl)}/api/1.0/sessions/${encodeURIComponent(sessionId)}/message`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text }),
    signal: AbortSignal.timeout(timeoutMs),
  })
  if (!resp.ok) {
    // 400 (empty text), 404 (unknown session), 410 (child exited) all mean the opening
    // turn did not land.
    throw new WrapperError(`wrapper message failed: HTTP ${resp.status}`, resp.status)
  }
  return resp.json()
}

/**
 * Project the wrapper's roster read onto ONE agent's parked question.
 *
 * ⚠️ Three states, and collapsing any two of them is a false negative a manager acts on:
 *   `null`                  — not wrapper-backed (the field does not apply), or wrapper-backed
 *                             and the wrapper reports nothing parked;
 *   `{request_id, …}`       — parked, and this is the id `answer_permission` takes;
 *   `{unreadable: true, …}` — wrapper-backed but the wrapper could not be read.
 * `wrapper_session_id` on the same row is what separates the first from the third, which is
 * why the two ship together: an outage that reads as "not parked" is the exact defect
 * `open_cards: null` exists to prevent on the attention store.
 *
 * @param {Array<object>|null} sessions the wrapper's session list, or `null` when unreadable
 * @param {string|null|undefined} wrapperSessionId this agent's wrapper session
 */
export function pendingQuestionOf(sessions, wrapperSessionId) {
  if (!wrapperSessionId) return null
  if (sessions === null) return { unreadable: true, error: 'wrapper session list unreadable' }
  // A session the wrapper no longer lists has exited and been reaped — not parked either.
  return sessions.find((s) => s?.id === wrapperSessionId)?.question ?? null
}

/**
 * Stop a wrapped child, so a session that will never produce a turn stops holding stdin.
 *
 * ⚠️ This is the teardown for a spawn that FAILED between creating the child and delivering
 * its opening turn. The child is live by then — it holds stdin and emits nothing — so a
 * caller that just throws leaves an idle process behind that looks exactly like a thinking
 * worker. `204` is the success shape; `404` means it was already gone, which is the state
 * this call exists to reach, so it is not reported as a failure.
 */
export async function stopWrappedSession({ baseUrl, sessionId, fetchImpl = fetch, timeoutMs = DEFAULT_TIMEOUT_MS }) {
  const resp = await fetchImpl(`${baseOf(baseUrl)}/api/1.0/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
    signal: AbortSignal.timeout(timeoutMs),
  })
  if (resp.status === 404) return { stopped: false, already_gone: true }
  if (!resp.ok) {
    throw new WrapperError(`wrapper stop failed: HTTP ${resp.status}`, resp.status)
  }
  return { stopped: true }
}

/**
 * Read EVERY child's current record in one call.
 *
 * ⚠️ One read for the whole roster, never one per worker. The status surface fans this out
 * over every agent, so a per-agent call is N round-trips with the wrapper down being N × the
 * timeout in front of every manager sweep — the same reason `fetchAttentionItems` exists
 * beside `fetchOpenCards`. The wrapper's list handler returns `snapshot()` per session, the
 * identical record `readWrappedSession` returns for one, so nothing is lost by batching.
 */
export async function listWrappedSessions({ baseUrl, fetchImpl = fetch, timeoutMs = DEFAULT_TIMEOUT_MS }) {
  const resp = await fetchImpl(`${baseOf(baseUrl)}/api/1.0/sessions`, {
    method: 'GET',
    signal: AbortSignal.timeout(timeoutMs),
  })
  if (!resp.ok) {
    throw new WrapperError(`wrapper list failed: HTTP ${resp.status}`, resp.status)
  }
  return resp.json()
}

/** Read one child's current record. Used only to classify an end, never to poll state. */
export async function readWrappedSession({ baseUrl, sessionId, fetchImpl = fetch, timeoutMs = DEFAULT_TIMEOUT_MS }) {
  const resp = await fetchImpl(`${baseOf(baseUrl)}/api/1.0/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'GET',
    signal: AbortSignal.timeout(timeoutMs),
  })
  if (!resp.ok) {
    throw new WrapperError(`wrapper read failed: HTTP ${resp.status}`, resp.status)
  }
  return resp.json()
}

/**
 * Parse an SSE body into the JSON payloads it carries.
 *
 * Frames are `data: <json>` followed by a blank line; the wrapper's `event: end` sentinel
 * arrives as its own frame. Kept as a generator over text chunks so a partial frame split
 * across two reads is buffered rather than dropped — a chunk boundary is not a frame boundary.
 */
export async function* parseSSE(stream) {
  const decoder = new TextDecoder()
  let buffer = ''
  let ended = false
  for await (const chunk of stream) {
    buffer += decoder.decode(chunk, { stream: true })
    let sep
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const out = frameOf(frame)
      if (out === END) {
        ended = true
        break
      }
      if (out !== null) yield out
    }
    if (ended) return
  }
  // A final frame without its blank-line terminator still counts.
  const tail = frameOf(buffer)
  if (tail !== null && tail !== END) yield tail
}

const END = Symbol('end')

function frameOf(frame) {
  let data = null
  let sawEnd = false
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:') && line.slice(6).trim() === 'end') sawEnd = true
    if (line.startsWith('data:')) data = line.slice(5).trim()
  }
  if (sawEnd) return END
  if (data === null || data === '') return null
  try {
    return JSON.parse(data)
  } catch {
    // A frame the wrapper could not encode is not a message; dropping it silently would
    // hide a real gap, so surface it as a parse marker the caller can log.
    return { type: 'wrapper_parse_error', raw: data }
  }
}

/**
 * Stream one wrapped child's events as an async iterable of SDK-shaped messages.
 *
 * Resolves the same contract `runAgentLoop` already expects from `query()`: iterate the
 * messages, and treat iteration ending as the end of the turn. A non-exit end throws
 * `StreamLaggedError` so a lagging subscriber is never mistaken for a finished worker.
 */
export async function* streamWrappedSession({ baseUrl, sessionId, fetchImpl = fetch, signal }) {
  const resp = await fetchImpl(`${baseOf(baseUrl)}/api/1.0/sessions/${encodeURIComponent(sessionId)}/events`, {
    method: 'GET',
    headers: { accept: 'text/event-stream' },
    ...(signal ? { signal } : {}),
  })
  if (!resp.ok) {
    throw new WrapperError(`wrapper stream failed: HTTP ${resp.status}`, resp.status)
  }
  for await (const message of parseSSE(resp.body)) {
    yield message
  }
  // The stream ended. Ask why before letting the caller settle a terminal state.
  const session = await readWrappedSession({ baseUrl, sessionId, fetchImpl })
  if (session.state !== 'exited') {
    throw new StreamLaggedError(sessionId, session.state)
  }
}

/**
 * Poll for a child's first parked question.
 *
 * The wrapper reports a question through the child's own event stream and its session
 * record; this helper exists so a caller does not have to re-derive the shape. It returns
 * `null` rather than throwing when nothing is parked yet, because "no question" is the
 * normal state of a working child.
 */
export async function readParkedQuestion({ baseUrl, sessionId, fetchImpl = fetch }) {
  const session = await readWrappedSession({ baseUrl, sessionId, fetchImpl })
  return session.question ?? null
}

export { DEFAULT_TIMEOUT_MS }
