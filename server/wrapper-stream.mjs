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
//
// ⚠️ **A third case: the connection died and the child did not.** The wrapper is restarted
// routinely (`launchctl kickstart -k`), and every restart used to end every streaming worker
// as `error` — a `TypeError: terminated` from undici reaching `runAgentLoop`'s catch while the
// child was still generating. The wrapper re-adopts that child and serves its event file
// again, so the correct behaviour is to RECONNECT, not to fail: every frame carries `id:` and
// the retry sends `Last-Event-ID`, which resumes at exactly the event this client stopped at.
// A `409` is the one answer that is not retried — it means the wrapper no longer retains that
// point, and serving a different one would leave an invisible hole in the transcript.

import { Agent, fetch as undiciFetch } from 'undici'

const DEFAULT_TIMEOUT_MS = 10_000

// ⚠️ **The event stream must never carry a body timeout, and undici's default is five minutes.**
// `fetch` here is Node's global (undici), whose `bodyTimeout` defaults to `300e3` — see
// `undici/lib/dispatcher/client.js:316`. An SSE body is IDLE BY DESIGN for exactly the states
// this adapter exists to serve: a child between turns, and a child parked on a question while a
// manager answers it. Five minutes of silence therefore destroys the body and undici raises
// `TypeError: terminated`, which reaches `runAgentLoop`'s catch and settles a healthy worker as
// `error` — measured 2026-11-01 at 300.85 s / 301.27 s / 301.18 s after the last frame, three
// runs of three, on a child that was still `idle` afterwards.
//
// `bodyTimeout: 0` disables it. ⚠️ **The accepted trade-off, stated rather than implied:** with no
// body timeout there is no upper bound on a body that HANGS — a wrapper that accepts the request,
// sends headers, and then wedges (e.g. the child's output pipe stops) leaves the stream open and
// the worker `running` with nothing to time it out. An UNREACHABLE wrapper is still covered:
// `headersTimeout` keeps its default and a refused or failed connect throws immediately. A hung
// body is traded away deliberately, because a false termination every five minutes is the worse
// failure and the one this change exists to remove; bounding it would need an application-level
// idle watchdog, not a shorter HTTP timeout.
//
// ⚠️ **This dispatcher only works with `undici`'s own `fetch`, never Node's global one.** The two
// are different implementations: passing an npm-`undici` `Agent` to the built-in `fetch` fails
// every request with `TypeError: fetch failed` / `UND_ERR_INVALID_ARG` — measured, and it is why
// `streamWrappedSession` defaults to `undiciFetch` rather than the global `fetch` the other
// helpers use. A caller that passes its own `fetchImpl` must pass one that understands
// `dispatcher`, or drop the option.
const SSE_DISPATCHER = new Agent({ bodyTimeout: 0 })

/**
 * Marker for "this stream did not end because the child finished".
 *
 * ⚠️ **Consumers must test `gap`, never a list of class names.** A name set has to be
 * updated by hand every time a new gap class is added, and the failure when it is not is
 * silent and in the dangerous direction: `runAgentLoop` settles `done` on any error it does
 * not recognise, so a forgotten name reports a holed transcript as a completed turn. Every
 * class below carries `gap = true`, and the consumer asks that one question.
 */
export class StreamGapBaseError extends Error {
  constructor(name, message) {
    super(message)
    this.name = name
    this.gap = true
  }
}

/** Raised when the stream stopped for a reason other than the child exiting. */
export class StreamLaggedError extends StreamGapBaseError {
  constructor(sessionId, state) {
    super('StreamLaggedError', `session ${sessionId} stream ended while the child was still ${state} — the subscriber fell behind, so this is not a completed turn`)
    this.sessionId = sessionId
    this.state = state
  }
}

/**
 * Raised when the reconnect budget is spent before the stream reached its end sentinel.
 *
 * ⚠️ This is a gap, not a failure to report as `done`: the child never said it finished, and
 * the events between the last frame this client saw and wherever the successor got to are
 * simply missing. It covers all three exhaustion shapes — a persistent non-ok response, a
 * body that kept throwing, and a body that closed cleanly without the sentinel (the shape a
 * graceful drain produces).
 */
export class StreamExhaustedError extends StreamGapBaseError {
  constructor(sessionId, reason) {
    super('StreamExhaustedError', `session ${sessionId} reconnect budget exhausted before the end sentinel (${reason}) — the transcript may have a hole and this turn cannot be settled as done`)
    this.sessionId = sessionId
    this.reason = reason
  }
}

/**
 * Raised when the wrapper answers `404`/`410` for a stream.
 *
 * ⚠️ **A gap, not a plain failure.** The child is gone or unknown, and the stream ended
 * without the end sentinel — so nothing here proves the turn completed. A `result` captured
 * before the session vanished must not settle the worker `done`.
 */
export class StreamSessionGoneError extends StreamGapBaseError {
  constructor(sessionId, status) {
    super('StreamSessionGoneError', `session ${sessionId} stream ended without the end sentinel: the wrapper reports the session gone (HTTP ${status}) — this turn cannot be settled as done`)
    this.sessionId = sessionId
    this.status = status
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

/**
 * Raised when the wrapper no longer retains the event the client asked to resume from.
 *
 * ⚠️ This is deliberately NOT a reconnect: the wrapper answers `409` when the sequence the
 * client holds has fallen out of its retained window, and re-opening the stream would replay
 * from a different point. The turn's transcript would then have an invisible hole — every
 * event before the gap delivered twice, or the gap itself skipped — and a worker settled
 * `done` on that stream reports a completed turn that never actually completed. Surfacing it
 * is the only honest outcome: the caller must not treat this worker as finished.
 */
export class StreamGapError extends StreamGapBaseError {
  constructor(sessionId, lastEventId) {
    super('StreamGapError', `session ${sessionId} stream resume point ${lastEventId} is no longer retained — the transcript has a gap and this turn cannot be settled as done`)
    this.sessionId = sessionId
    this.lastEventId = lastEventId
  }
}

/**
 * Reconnect policy for a stream whose connection died under a live child.
 *
 * ⚠️ **The defaults are sized to a daemon restart, not to a blip.** `launchctl kickstart -k`
 * sends SIGTERM, waits out the exit timeout, then SIGKILLs, and the successor re-adopts the
 * child and opens its listener again — so the gap a client must ride out is seconds, not
 * milliseconds. Two minutes of budget at a 5 s ceiling is what makes "restart the wrapper as
 * often as you like" true for a worker that is mid-turn.
 */
export const DEFAULT_RECONNECT = {
  // Attempts, not seconds: each attempt is one fetch, and a refused connection fails fast.
  maxAttempts: 24,
  // Exponential, so a fast restart is picked up in ~100 ms instead of waiting out a fixed poll.
  baseDelayMs: 100,
  maxDelayMs: 5_000,
  // The wall-clock ceiling, so a wrapper that never comes back fails the worker in bounded
  // time rather than leaving it `running` forever.
  maxElapsedMs: 120_000,
  // Injectable clock. It exists so a spec can prove the budget is measured per OUTAGE rather
  // than from stream open — the defect that made every long-lived stream unretryable.
  now: () => Date.now(),
}

/** Resolve after `ms`, or `false` as soon as `signal` aborts. */
function sleepUnlessAborted(ms, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve(false)
      return
    }
    const done = (value) => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      resolve(value)
    }
    const onAbort = () => done(false)
    const timer = setTimeout(() => done(true), ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
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
export async function* parseSSE(stream, tracker) {
  const decoder = new TextDecoder()
  let buffer = ''
  let ended = false
  for await (const chunk of stream) {
    buffer += decoder.decode(chunk, { stream: true })
    let sep
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const out = frameOf(frame, tracker)
      if (out === END) {
        ended = true
        if (tracker) tracker.ended = true
        break
      }
      if (out !== null) yield out
    }
    if (ended) return
  }
  // A final frame without its blank-line terminator still counts.
  const tail = frameOf(buffer, tracker)
  if (tail === END) {
    if (tracker) tracker.ended = true
  } else if (tail !== null) {
    yield tail
  }
}

const END = Symbol('end')

/**
 * Decode one SSE frame.
 *
 * ⚠️ `tracker` is how the caller learns where it got to. The wrapper writes `id:` on every
 * frame — the `events.jsonl` line number — and that value is the ONLY thing a reconnect can
 * resume from: it survives a daemon restart because the file is append-only and a given
 * line's index never changes. A parser that drops the id leaves the caller with no resume
 * point and no way to tell a clean end from a dropped connection.
 *
 * `tracker.ended` records that the wrapper sent its `event: end` sentinel, which is the one
 * end that means "the child finished" rather than "the connection died".
 */
function frameOf(frame, tracker) {
  let data = null
  let sawEnd = false
  for (const line of frame.split('\n')) {
    if (line.startsWith('id:')) {
      const id = line.slice(3).trim()
      if (tracker && id !== '') {
        tracker.lastId = id
        // Distinguishes "no id on this connection" from "the same id again": only the
        // former makes the stream unresumable, the latter is a stuck wrapper that must
        // consume the budget instead.
        tracker.sawId = true
      }
    }
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
export async function* streamWrappedSession({
  baseUrl,
  sessionId,
  fetchImpl = undiciFetch,
  signal,
  reconnect = {},
}) {
  const policy = { ...DEFAULT_RECONNECT, ...reconnect }
  const now = policy.now
  const url = `${baseOf(baseUrl)}/api/1.0/sessions/${encodeURIComponent(sessionId)}/events`

  // ⚠️ **A daemon restart is not the end of a turn.** The wrapper re-adopts the child and
  // serves its event file again, so the connection dying mid-turn is a gap to ride out, not a
  // terminal state. Every frame carries `id:`, and `Last-Event-ID` is what makes the successor
  // resume from exactly where this client stopped instead of replaying — or, worse, starting
  // over and duplicating the transcript.
  let lastId = null

  // ⚠️ **The budget bounds ONE OUTAGE, not the stream's lifetime.** A parked or long-running
  // worker's stream is open for hours, and measuring from stream open gave such a stream ZERO
  // retry once it passed `maxElapsedMs` — so a restart still failed exactly the workers this
  // change exists to protect.
  //
  // So the clock starts when a connection FAILS, not when it opens: `attempt === 0` means the
  // stream is healthy, and the first failure of an outage stamps `outageStartedAt`. A
  // connection that delivers an event ends the outage (`attempt` back to 0), so the next
  // failure gets its own budget. A connection that opens and dies without a frame does NOT
  // reset, so a wrapper that accepts and immediately closes keeps consuming one budget instead
  // of looping forever.
  let attempt = 0
  let outageStartedAt = 0
  // Whether ANY frame ever arrived. A stream that carried frames but no ids cannot be
  // resumed, and reconnecting blind would duplicate or skip the transcript.
  let sawAnyFrame = false

  for (;;) {
    if (signal?.aborted) return

    const headers = { accept: 'text/event-stream' }
    if (lastId !== null) headers['Last-Event-ID'] = lastId

    let resp
    try {
      resp = await fetchImpl(url, {
        method: 'GET',
        headers,
        // Load-bearing: without it undici tears the body down after five idle minutes and a
        // healthy parked child is recorded as a dead worker. See SSE_DISPATCHER above.
        dispatcher: SSE_DISPATCHER,
        ...(signal ? { signal } : {}),
      })
    } catch (err) {
      if (signal?.aborted) return
      // A refused connection is the normal shape of a daemon that is restarting: the port is
      // briefly not listening. Retry it on the same budget as a dropped body.
      if (await waitForRetry()) continue
      if (signal?.aborted) return
      throw new StreamExhaustedError(sessionId, `connect failed: ${err?.message ?? err}`)
    }

    // ⚠️ 409 is the wrapper saying the resume point fell out of its retained window. Re-opening
    // would hand back a DIFFERENT point, so the transcript would carry an invisible hole — the
    // one failure this whole stream exists to make impossible. Never retried.
    if (resp.status === 409) {
      throw new StreamGapError(sessionId, lastId)
    }
    if (!resp.ok) {
      // A 404/410 is a settled answer about the session, not a transport blip: the child is
      // gone or unknown, and retrying cannot change that.
      if (resp.status === 404 || resp.status === 410) {
        throw new StreamSessionGoneError(sessionId, resp.status)
      }
      if (await waitForRetry()) continue
      if (signal?.aborted) return
      throw new StreamExhaustedError(sessionId, `HTTP ${resp.status}`)
    }

    const tracker = { lastId, ended: false, sawId: false }
    // Where this connection started. A connection that delivers an id we did not already
    // have has genuinely resumed the transcript — that is progress, and it earns a fresh
    // budget. One that re-delivers the SAME id has not: a wrapper stuck re-sending one event
    // and dropping the body would otherwise reset the budget on every attempt and reconnect
    // forever.
    const lastIdAtConnect = lastId
    let sawProgress = false
    let sawFramesThisConnection = false
    let bodyError = null
    try {
      for await (const message of parseSSE(resp.body, tracker)) {
        sawAnyFrame = true
        sawFramesThisConnection = true
        if (!sawProgress && tracker.lastId !== null && tracker.lastId !== lastIdAtConnect) {
          sawProgress = true
          attempt = 0
        }
        yield message
      }
    } catch (err) {
      bodyError = err
    }
    if (tracker.lastId !== null) lastId = tracker.lastId

    // The wrapper sent its end sentinel: the child really finished, so stop reconnecting.
    if (tracker.ended) break
    if (signal?.aborted) return

    // ⚠️ **A connection that delivered frames but no `id:` cannot be resumed.** That is an
    // older wrapper or a deploy skew, and reconnecting blind would either replay the whole
    // transcript (firing `onTurnComplete` again for every replayed result) or silently skip
    // the gap. Checked per CONNECTION, not once for the stream: a successor that starts
    // serving frames without ids is exactly as unresumable as a first connection that never
    // had them.
    if (sawFramesThisConnection && !tracker.sawId) {
      throw new StreamGapError(sessionId, lastId)
    }

    // The body ended or threw without the sentinel — the connection died under a live child.
    if (await waitForRetry()) continue
    if (signal?.aborted) return
    throw new StreamExhaustedError(
      sessionId,
      bodyError
        ? `body failed: ${bodyError?.message ?? bodyError}`
        : 'body closed without the end sentinel',
    )
  }

  // The stream ended on its own terms. Ask why before letting the caller settle a terminal
  // state — a lag is still not a finished turn.
  //
  // ⚠️ The read itself can fail (the daemon restarts between the sentinel and this call), and
  // an unguarded throw here would be a non-gap error: a captured `result` would then settle
  // the worker `done` on an end nobody confirmed. Confirming is part of the classification,
  // so a failed confirmation is a gap too.
  let session
  try {
    session = await readWrappedSession({ baseUrl, sessionId, fetchImpl })
  } catch (err) {
    throw new StreamExhaustedError(
      sessionId,
      `end-of-stream confirmation failed: ${err?.message ?? err}`,
    )
  }
  if (session.state !== 'exited') {
    throw new StreamLaggedError(sessionId, session.state)
  }

  /**
   * Advance the backoff, or report that this outage's budget is spent.
   *
   * Returns `false` for BOTH exhaustion and an aborted signal — the callers distinguish them
   * by re-checking `signal.aborted`, so an abort returns cleanly instead of being reported as
   * a failed reconnect.
   */
  async function waitForRetry() {
    if (attempt === 0) {
      // First failure of a new outage: this is where its clock starts.
      outageStartedAt = now()
    }
    attempt += 1
    const elapsed = now() - outageStartedAt
    if (attempt > policy.maxAttempts || elapsed >= policy.maxElapsedMs) return false
    const delay = Math.min(policy.baseDelayMs * 2 ** (attempt - 1), policy.maxDelayMs)
    // Never sleep past the wall-clock ceiling: a 5 s wait when 200 ms of budget remains would
    // overshoot it and turn a bounded failure into an unbounded one.
    const remaining = policy.maxElapsedMs - elapsed
    return sleepUnlessAborted(Math.min(delay, remaining), signal)
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
