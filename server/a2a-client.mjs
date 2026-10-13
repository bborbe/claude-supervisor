// The supervisor's A2A client — the WORKER-facing half of the `docker` and `k8s` spawn targets.
//
// Why this is a pure module and not code inside `supervisor.mjs`: that file connects a stdio
// server at load, so no test can import it at all. Everything here is a pure function over
// values — no fetch, no clock, no filesystem — and the caller does the I/O. The wire rules are
// therefore unit-tested rather than discovered by hand against a live worker.
//
// ⚠️ EVERY TRAP BELOW WAS MEASURED ON THE REAL WIRE, AND EACH ONE PRODUCED A REQUEST THAT
// LOOKED CORRECT AND WAS NOT. That shape — a well-formed response to a request the service
// never honoured — is what this module exists to make impossible:
//
//   1. THE CARD'S ENDPOINT IS `supportedInterfaces[0].url`, NOT A TOP-LEVEL `url`. The v2 SDK's
//      `a2a.AgentCard` carries no top-level `url` field at all, so reading the obvious one
//      yields `undefined` — and a fallback would turn that into a turn sent somewhere the card
//      never named. `resolveA2AEndpoint` refuses instead.
//
//   2. THE PART IS FLATTENED — `{ text }`, never `{ content: … }`. The `content` key exists only
//      on the Go struct and never on the wire. A `content` part was refused with
//      `-32700 parse error: unknown part content type` **inside a well-formed `HTTP 200
//      text/event-stream` envelope**, so every transport check passed while the request had been
//      rejected outright. See `a2aFrames` — an error frame is fatal here, not a log line.
//
//   3. `role` IS THE ENUM STRING `ROLE_USER`, not `"user"`. The SDK rejects an empty role with
//      `ErrInvalidParams` before the executor runs, so a lowercase guess fails the whole turn
//      for a reason that never reaches the agent.
//
//   4. `contextId` MUST BE CHOSEN UP FRONT AND SENT ON EVERY TURN. A first message without one
//      runs under the server's default session, and a follow-up that then introduces a fresh id
//      opens a DIFFERENT conversation — the worker legitimately reports no memory of turn 1 and
//      is correct to. ⚠️ The client cannot detect this from its own side: a `SendMessage` sent
//      without a `contextId` still returns one, so the result looks well-formed and there is
//      nothing to compare it against. The cheapest defence is the one implemented here — send
//      the id explicitly, then ASSERT the returned id equals the sent one.
//
// The reference for shapes 2–4 is the vault's working client, `.claude/scripts/a2a-mcp.py`,
// read rather than guessed. The `content`-part defect above is what guessing produced.
//
// Nothing here reads the filesystem, the environment, or the clock.

// The well-known path the card is served at — `a2asrv.WellKnownAgentCardPath`.
export const A2A_AGENT_CARD_PATH = '/.well-known/agent-card.json'

// The JSON-RPC method for a streaming turn.
//
// ⚠️ BARE, NOT THE SPEC'S `message/stream`. The v2 SDK dispatches the bare names; the slash
// spellings survive only in the `a2acompat/a2av0` bridge a v2 service does not mount, and a
// client using them gets `-32601 method not found` — measured against the deployed pod.
export const A2A_STREAM_METHOD = 'SendStreamingMessage'

// The role every part of this client's messages carries. ⚠️ The enum string, not `"user"`.
const A2A_USER_ROLE = 'ROLE_USER'

// The A2A endpoint a worker's Agent Card advertises — or `{ error }` when it advertises none.
//
// ⚠️ REFUSES RATHER THAN FALLING BACK, and the refusal names the nested field, because the
// wrong read is invisible: a top-level `url` is a plausible-looking key that a correct card
// simply does not carry, so a fallback would quietly send every turn to whatever it named.
export function resolveA2AEndpoint(card) {
  const interfaces = card?.supportedInterfaces
  if (!Array.isArray(interfaces) || interfaces.length === 0) {
    return {
      error:
        'the Agent Card carries no `supportedInterfaces` array, so no A2A endpoint can be read — ' +
        'a correct card advertises it at `supportedInterfaces[0].url`.',
    }
  }
  const url = interfaces[0]?.url
  if (typeof url !== 'string' || url === '') {
    return {
      error:
        '`supportedInterfaces[0]` carries no `url`, so no A2A endpoint can be read. ⚠️ Do not fall ' +
        'back to a top-level `url`: a correct card does not carry one, so that read returns ' +
        'nothing, and a fallback would send every turn somewhere the card never named.',
    }
  }
  return { url }
}

// The `contextId` to send, given the one this conversation already used.
//
// ⚠️ MINTED ON THE FIRST CALL, NEVER OMITTED — see trap 4 in the header. `mint` is passed in
// rather than called here because this module holds no clock and no random source; the caller
// owns both, and passing them keeps the decision testable.
export function a2aContextIdFor({ previous, mint } = {}) {
  if (typeof previous === 'string' && previous !== '') return { contextId: previous }
  if (typeof mint !== 'function') {
    return {
      error:
        'no `contextId` was supplied and no `mint` was given to create one — refusing to send a ' +
        'first turn without one, because a turn sent without a `contextId` opens a conversation ' +
        'the follow-up cannot rejoin, and the worker will correctly report no memory of it.',
    }
  }
  const contextId = mint()
  if (typeof contextId !== 'string' || contextId === '') {
    return { error: `mint() returned ${JSON.stringify(contextId)}, which is not a contextId.` }
  }
  return { contextId }
}

// The JSON-RPC request for one streaming turn — `{ url, headers, body }`, or `{ error }`.
//
// The body's shape is the vault client's, verbatim: `params.message` carries `messageId`, `role`
// and a flattened `parts` array. `contextId` is REQUIRED here even though the vault client makes
// it optional, because that client can send to a worker's default conversation by design while
// this one must not — the supervisor always knows which conversation it is continuing.
export function a2aStreamRequestFor({ endpoint, contextId, text, token, messageId } = {}) {
  const missing = (name, value) =>
    value === undefined || value === null || value === ''
      ? `\`${name}\` is required to send a turn over A2A.`
      : null
  for (const [name, value] of Object.entries({ endpoint, contextId, text, token, messageId })) {
    const err = missing(name, value)
    if (err) return { error: err }
  }
  if (typeof text !== 'string') return { error: '`text` must be a string.' }

  return {
    url: endpoint,
    headers: {
      'content-type': 'application/json',
      // The SSE half is requested explicitly. A client that omits this gets the one-shot
      // response from a streaming route, which reads as a turn that produced no frames.
      accept: 'text/event-stream',
      authorization: `Bearer ${token}`,
    },
    body: {
      jsonrpc: '2.0',
      id: 1,
      method: A2A_STREAM_METHOD,
      params: {
        message: {
          messageId,
          role: A2A_USER_ROLE,
          parts: [{ text }],
          contextId,
        },
      },
    },
  }
}

// The frames carried by an SSE body — `{ frames }`, or `{ error }` when one of them is an error.
//
// ⚠️ AN ERROR FRAME IS FATAL HERE, AND THAT IS THE POINT. The measured defect this guards is a
// refused request arriving inside a well-formed `HTTP 200 text/event-stream` envelope: every
// transport-level check goes green while nothing was executed. A parser that returned the error
// frame as data would reproduce exactly that, one layer up.
export function a2aFrames(sseBody) {
  if (typeof sseBody !== 'string') return { error: 'the SSE body must be a string.' }
  const frames = []
  for (const line of sseBody.split('\n')) {
    if (!line.startsWith('data:')) continue
    const raw = line.slice('data:'.length).trim()
    if (raw === '' || raw === '[DONE]') continue
    let payload
    try {
      payload = JSON.parse(raw)
    } catch (err) {
      return { error: `an SSE frame is not JSON: ${raw.slice(0, 200)}` }
    }
    if (payload?.error) {
      return {
        error:
          `the service refused the turn: ${JSON.stringify(payload.error)}. ⚠️ This arrives inside a ` +
          'well-formed HTTP 200 SSE envelope, so the transport status proves nothing — a `content` ' +
          'part and a slash-form method name both fail exactly here.',
      }
    }
    frames.push(payload?.result ?? payload)
  }
  return { frames }
}

// What a turn produced — `{ state, contextId, reply }`, or `{ error }`.
//
// `sentContextId` is the id the caller sent, and it is asserted against the one that came back.
// ⚠️ THAT ASSERTION IS THE WHOLE POINT OF PASSING IT. A turn that ran under the server's default
// session returns a `contextId` just as happily as one that ran where it was asked to, so a
// mismatch is the ONLY client-side observable that the conversation is not the one intended —
// and it surfaces here rather than as unexplained memory loss two turns later.
export function a2aOutcome(frames, { sentContextId } = {}) {
  if (!Array.isArray(frames) || frames.length === 0) {
    return { error: 'the turn produced no frames — nothing executed.' }
  }
  // The task object may be nested under `task` or be the frame itself; the vault client allows
  // for both, and this mirrors it rather than assuming the nesting.
  const task = frames.map((f) => f?.task ?? f).find((t) => t?.id) ?? {}
  const contextId = task.contextId ?? frames.find((f) => f?.contextId)?.contextId
  if (sentContextId && contextId && contextId !== sentContextId) {
    return {
      error:
        `the turn ran under contextId ${JSON.stringify(contextId)} but was sent under ` +
        `${JSON.stringify(sentContextId)} — the conversation is not the one intended. This is the ` +
        'only client-side observable of a dropped `contextId`, and it is silent otherwise: the ' +
        'server returns an id either way and the reply looks well-formed.',
    }
  }
  const reply = frames
    .map((f) => f?.artifactUpdate ?? f?.artifact ?? f)
    .flatMap((f) => f?.parts ?? [])
    .map((p) => p?.text ?? '')
    .filter((t) => t !== '')
    .join('\n')
  // ⚠️ THE LAST `statusUpdate` WINS, NOT THE TASK'S OWN STATUS. The frame order is
  // `task(SUBMITTED)` → `artifactUpdate` → `statusUpdate(COMPLETED)`, so the `task` frame is the
  // FIRST one and it carries a status too — reading `task.status.state` therefore reports
  // SUBMITTED on a turn that completed. Measured by this module's own test before it shipped.
  const statusUpdates = frames.map((f) => f?.statusUpdate).filter((s) => s?.state)
  const state = statusUpdates.at(-1)?.state ?? task.status?.state
  return { state, contextId, reply }
}
