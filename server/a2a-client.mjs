// The supervisor's A2A client — BOTH faces of the `docker` and `k8s` spawn targets: the
// WORKER-facing turn (traps 1–4, below) and the CONTROLLER-facing control call (traps 5–6, at
// the foot of this file).
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

// ─────────────────────────────────────────────────────────────────────────────────────────
// THE CONTROLLER-FACING HALF — the control API of the A2A worker controller
// (`bborbe/a2a-worker-controller`), whose `create` / `list` / `stop` a spawn calls before any
// worker exists.
//
// The controller is a DIFFERENT SERVICE from a worker, and it differs on exactly two wire
// points. Both were read at source — `pkg/types.go`, `pkg/a2a.go`, `pkg/a2a-executor.go` and
// the `a2a-go` v2 SDK's `a2a/core.go` — rather than inferred from the worker's shape, because
// the worker's shape is exactly what would have produced a request that looks right and is not:
//
//   5. THE CONTROL CALL CARRIES A **DATA** PART, NOT A TEXT PART. The controller reads the
//      `ControlRequest` from the message's FIRST data part and refuses a message carrying none.
//      A text part is therefore not a near miss: the controller answers `ErrInvalidParams` and
//      no operation runs at all.
//
//   6. THE CONTROL CALL IS ONE-SHOT — the bare method `SendMessage`, never
//      `SendStreamingMessage`. A control call is a single request and response with no live turn
//      to interrupt, which the controller's own `Cancel` states outright, so there is no stream
//      to consume. (`NewJSONRPCHandler` serves both spellings; the one-shot name is the one that
//      matches the executor's semantics.)
//
// ⚠️ The flattened-part rule is the same on both faces, and the SDK enforces it more sharply
// than trap 2 alone says: `Part.UnmarshalJSON` requires EXACTLY ONE of `text` / `raw` / `data` /
// `url`. Zero yields `unknown part content type` — the literal text of the `-32700` this
// module's worker half was built around, so that defect's cause is now pinned rather than
// suspected — and more than one yields `expected exactly one of text, raw, data, or url`.

// The JSON-RPC method for a ONE-SHOT turn. ⚠️ BARE, like trap 4's streaming name — the v2 SDK
// dispatches bare names, and the controller was measured to answer this one.
export const A2A_SEND_METHOD = 'SendMessage'

// The operations the controller's schema defines, and the only values its
// `ControlRequest.operation` accepts.
export const A2A_CONTROL_OPERATIONS = ['create', 'list', 'stop']

// The terminal state a control call ends in when the controller could not complete it. It earns its
// own constant because it is load-bearing here: the controller reports a failure as a FAILED status
// carrying NO artifact and returns no detail, so a client that looked only for a data part would
// find none and have to invent a reason for it.
//
// ⚠️ IT DOES NOT MEAN THE OPERATION DID NOT HAPPEN, and this comment said that until 2026-10-13.
// The failure can land in the controller's TASK STORE, after the executor has already run — measured
// that day against the controller's own `v0.4.0`, where every control call including `list` answered
// FAILED because the store's gob copy of the artifact was missing a type registration. A `create` on
// that build could have started a container and still reported FAILED. Treat this status as "the
// controller could not tell me the outcome", never as "nothing happened".
const A2A_STATE_FAILED = 'TASK_STATE_FAILED'

// The `ControlRequest` for one operation — `{ request }`, or `{ error }`.
//
// ⚠️ EXACTLY ONE OPERATION-SPECIFIC FIELD, and that is the controller's own rule rather than
// this module's preference: it refuses a request that sets none or more than one instead of
// guessing at the intent. A builder that silently dropped a stray field would turn a caller's
// mistake into a different operation than the one it asked for.
export function a2aControlRequestFor({ operation, task, workerId } = {}) {
  if (!A2A_CONTROL_OPERATIONS.includes(operation)) {
    return {
      error:
        `\`operation\` must be one of ${A2A_CONTROL_OPERATIONS.map((o) => JSON.stringify(o)).join(', ')} ` +
        `— got ${JSON.stringify(operation) ?? 'undefined'}.`,
    }
  }
  if (operation === 'list') {
    if (task !== undefined || workerId !== undefined) {
      return { error: '`list` takes no arguments — it reports every worker the controller owns.' }
    }
    return { request: { operation } }
  }
  if (operation === 'create') {
    if (typeof task !== 'string' || task === '') {
      return { error: '`create` requires a non-empty `task` name.' }
    }
    if (workerId !== undefined) {
      return { error: '`create` takes no `workerId` — the controller mints the worker id.' }
    }
    return { request: { operation, create: { task } } }
  }
  if (typeof workerId !== 'string' || workerId === '') {
    return { error: '`stop` requires a non-empty `workerId`.' }
  }
  if (task !== undefined) {
    return { error: '`stop` names a worker, not a task — pass `workerId`, never `task`.' }
  }
  return { request: { operation, stop: { worker_id: workerId } } }
}

// A message part carrying structured data — `{ part }`, or `{ error }`.
//
// ⚠️ `{ data }`, NOT `{ text }` — see trap 5. The payload must be a non-null object: the SDK
// hydrates `data` into a `*any`, so a JSON `null` leaves that pointer nil and counts as NO
// discriminator at all, which the SDK refuses as `unknown part content type`. A null payload
// therefore fails on the wire exactly as a missing part does — quietly enough to be worth
// refusing here instead.
export function a2aDataPart(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return {
      error:
        `a data part must carry a non-null object — got ${JSON.stringify(value) ?? 'undefined'}. ⚠️ A ` +
        '`null` payload hydrates to no discriminator at all and is refused as an unknown part ' +
        'content type, so it fails on the wire exactly as a missing part does.',
    }
  }
  return { part: { data: value } }
}

// The JSON-RPC request for one control call — `{ url, headers, body }`, or `{ error }`.
//
// ⚠️ ONE-SHOT, NOT STREAMING — see trap 6. No `accept: text/event-stream`: a one-shot route
// answers a single object, and asking it for a stream reads as a call that produced no frames.
// `contextId` is sent for the same reason the worker half sends it — a control call is a turn on
// the controller's conversation, and a follow-up must land on the one it started.
export function a2aSendRequestFor({ endpoint, contextId, request, token, messageId } = {}) {
  for (const [name, value] of Object.entries({ endpoint, contextId, token, messageId })) {
    if (value === undefined || value === null || value === '') {
      return { error: `\`${name}\` is required to send a control call over A2A.` }
    }
  }
  const { part, error } = a2aDataPart(request)
  if (error) return { error }

  return {
    url: endpoint,
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${token}`,
    },
    body: {
      jsonrpc: '2.0',
      id: 1,
      method: A2A_SEND_METHOD,
      params: {
        message: {
          messageId,
          role: A2A_USER_ROLE,
          parts: [part],
          contextId,
        },
      },
    },
  }
}

// The parts and terminal state a control reply carries, from EITHER framing.
//
// ⚠️ TWO FRAMINGS, AND THE CALLER MUST NOT HAVE TO KNOW WHICH. `NewJSONRPCHandler` serves the
// streaming spelling beside the one-shot one and the executor is identical behind either: a
// one-shot reply is a single JSON-RPC object whose `result` is the task, while a streaming reply
// is SSE frames carrying the same task, artifact and status events. Accepting both is not
// guesswork — both are genuinely served — and refusing the wrong one would be.
function a2aControlReply(body) {
  if (typeof body !== 'string') return { error: 'the control reply body must be a string.' }
  if (!/^\s*data:/m.test(body)) {
    let payload
    try {
      payload = JSON.parse(body)
    } catch (err) {
      return { error: `the control reply is neither SSE nor JSON: ${body.slice(0, 200)}` }
    }
    if (payload?.error) {
      return {
        error:
          `the controller refused the control call: ${JSON.stringify(payload.error)}. ⚠️ A refusal ` +
          'here is a JSON-RPC error, not a FAILED task — the request never reached a backend.',
      }
    }
    const task = payload?.result ?? payload
    return { task, frames: [task] }
  }
  const { frames, error } = a2aFrames(body)
  if (error) return { error }
  return { task: frames.map((f) => f?.task ?? f).find((t) => t?.id) ?? {}, frames }
}

// The `ControlResponse` a control call produced — `{ response, state }`, or `{ error }`.
//
// `sentOperation` is asserted against the operation that comes back. ⚠️ THAT ASSERTION IS THE
// POINT OF PASSING IT, exactly as `sentContextId` is in `a2aOutcome`: a reply naming a different
// operation is the only client-side observable that the answer does not belong to the call that
// asked for it, and nothing else on this side reads as wrong.
export function a2aControlResponseFor(body, { sentOperation } = {}) {
  const { task, frames, error } = a2aControlReply(body)
  if (error) return { error }

  // ⚠️ THE STATUS IS READ BEFORE THE PAYLOAD, and the order is load-bearing: a backend failure
  // yields `TASK_STATE_FAILED` with NO artifact, so a client that hunted for the data part first
  // would find none and have to invent a reason, while the status states the real one.
  const statusUpdates = frames.map((f) => f?.statusUpdate).filter((s) => s?.state)
  const state = statusUpdates.at(-1)?.state ?? task?.status?.state

  if (state === A2A_STATE_FAILED) {
    return {
      error:
        'the controller reported the control call FAILED. It returns no detail on this path — the ' +
        'error is logged controller-side and never sent — so this status is all the caller gets. ' +
        '⚠️ It does NOT mean the operation did not happen: the failure can land in the controller\'s ' +
        'task store AFTER the executor has run, measured 2026-10-13 when a store-side gob failure ' +
        'failed every call including `list`. Treat it as an UNKNOWN outcome and reconcile against ' +
        'the controller\'s own `list` rather than assuming nothing happened.',
    }
  }

  const parts = [
    ...(task?.artifacts ?? []),
    ...frames.map((f) => f?.artifactUpdate?.artifact ?? f?.artifact),
  ].flatMap((a) => a?.parts ?? [])
  const response = parts
    .filter((p) => p !== null && typeof p === 'object' && 'data' in p)
    .map((p) => p.data)
    .find((d) => d !== null && d !== undefined)

  if (response === undefined) {
    return {
      error:
        `the control call ended ${JSON.stringify(state ?? 'with no status')} but carried no data ` +
        'part, so there is no `ControlResponse` to read. ⚠️ A text part here is not a near miss: the ' +
        'controller answers with a data part, and the SDK refuses any part that does not carry ' +
        'exactly one of text, raw, data or url.',
    }
  }
  if (sentOperation && response.operation !== sentOperation) {
    return {
      error:
        `the control call asked for ${JSON.stringify(sentOperation)} but the reply names ` +
        `${JSON.stringify(response.operation)} — the answer does not belong to this call.`,
    }
  }
  return { response, state }
}

// The workers a `list` response carries — `{ workers }`, or `{ error }`.
//
// ⚠️ `null` AND `[]` ARE BOTH VALID HERE, AND THE DIFFERENCE IS NOT PRESERVED. The controller's
// store copies a task through gob, and **gob does not transmit zero values** — so an empty worker
// list round-trips as `{"workers": null}` rather than `{"workers": []}`. Measured 2026-10-13 by the
// controller's own session against its `v0.4.1`, and confirmed as a property of gob rather than a
// defect in that build. A caller that iterated `workers` without a guard would throw on the EMPTY
// case — the most common case at the start of a run, and therefore the one least likely to be
// exercised before it ships. The normalising happens here, once, so no call site has to remember it.
export function a2aWorkerList(response) {
  if (response === null || typeof response !== 'object' || Array.isArray(response)) {
    return {
      error:
        'a list result must be the `ControlResponse` object — pass what `a2aControlResponseFor` ' +
        'returned as `response`, not the raw body.',
    }
  }
  if (response.operation !== 'list') {
    return {
      error:
        `expected a \`list\` response, got operation ${JSON.stringify(response.operation)} — the ` +
        'call that produced this did not ask for a list.',
    }
  }
  const workers = response.list?.workers
  // ⚠️ `null` and a missing key are the same fact here — no workers — because gob collapsed the
  // empty slice into the first of them. Normalised rather than returned as-is: `null` is not
  // iterable, and every caller's first move is to iterate.
  if (workers === null || workers === undefined) return { workers: [] }
  if (!Array.isArray(workers)) {
    return {
      error:
        `\`list.workers\` must be an array or null, got ${JSON.stringify(workers)}. The controller ` +
        'returns null for an empty list, so anything else means this is not the shape it sends.',
    }
  }
  return { workers }
}
