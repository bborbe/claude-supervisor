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

// One SSE line — `{ frame }`, `{ skip: true }` for a line carrying no frame, or `{ error }`.
//
// ⚠️ EXTRACTED FROM `a2aFrames` SO THERE IS EXACTLY ONE PARSER. The incremental reader below has
// to reach the same verdicts on the same bytes as the whole-body path does, and two copies of an
// error-frame rule is precisely how one of them drifts into treating a refusal as data.
export function a2aFrameFor(line) {
  if (!line.startsWith('data:')) return { skip: true }
  const raw = line.slice('data:'.length).trim()
  if (raw === '' || raw === '[DONE]') return { skip: true }
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
  return { frame: payload?.result ?? payload }
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
    const parsed = a2aFrameFor(line)
    if (parsed.error) return { error: parsed.error }
    if (parsed.skip) continue
    frames.push(parsed.frame)
  }
  return { frames }
}

// The task id a turn's frames carry, or `null` — the value a push registration needs.
//
// ⚠️ IT ACCEPTS EVERY SHAPE THE ID ARRIVES IN, because the FIRST frame is the one that matters and
// which shape it takes is not fixed: the opening `task` frame carries `{task: {id}}` while a later
// `statusUpdate` carries `{statusUpdate: {taskId}}`. `a2aOutcome` resolves the same ambiguity for
// the terminal read (`.task ?? f` then `.id`); this mirrors it rather than inventing a second rule,
// and it scans in ORDER so the earliest id wins — which is what makes it usable mid-stream, where
// only a prefix of the frames exists yet.
export function a2aTaskIdOf(frames) {
  if (!Array.isArray(frames)) return null
  for (const frame of frames) {
    const id = frame?.task?.id ?? frame?.id ?? frame?.statusUpdate?.taskId ?? frame?.artifactUpdate?.taskId
    if (typeof id === 'string' && id !== '') return id
  }
  return null
}

// A frame reader fed a turn's stream CHUNK BY CHUNK — `{ push, end, frames, error }`.
//
// ⚠️ WHY THIS EXISTS AT ALL, AND IT IS NOT AN OPTIMISATION. The push callback must be registered
// against the worker's task *after* that task exists and *before* the worker emits its mid-turn
// status, which fires a fixed ~10 s into the turn. The task id is in the turn's FIRST frame, so a
// reader that only yields at the end of the stream — which is what `.text()` does — learns it
// minutes too late and arms the channel for a turn that has already reported. Every failure in
// that direction is silent: the turn still completes, the outcome is still filed, and the only
// symptom is a status channel that never carries anything.
//
// ⚠️ THE BUFFER IS THE WHOLE CORRECTNESS PROBLEM. A chunk boundary falls wherever the network
// puts it, so a `data:` line routinely arrives split across two reads. Only text up to the LAST
// newline is parsed; the remainder is held and prefixed to the next chunk. Parsing eagerly would
// fail on a truncated JSON line and report a malformed stream for a perfectly good one.
export function createSseFrameReader() {
  let buffer = ''
  let error = null
  const frames = []

  const consume = (text) => {
    buffer += text
    const lastBreak = buffer.lastIndexOf('\n')
    if (lastBreak === -1) return
    const complete = buffer.slice(0, lastBreak)
    buffer = buffer.slice(lastBreak + 1)
    for (const line of complete.split('\n')) {
      const parsed = a2aFrameFor(line)
      // The first error is kept and the reader stops yielding: an error frame is fatal in
      // `a2aFrames` too, so returning frames collected before it would let a caller read a refused
      // turn as a partial success.
      if (parsed.error) {
        if (error === null) error = parsed.error
        return
      }
      if (!parsed.skip) frames.push(parsed.frame)
    }
  }

  return {
    frames,
    get error() {
      return error
    },
    push: (chunk) => {
      if (error !== null) return
      consume(chunk)
    },
    // Flush whatever is left. ⚠️ A trailing line with NO newline is still a frame: the last
    // `data:` line of a stream often arrives exactly so, and dropping it would lose the terminal
    // completion event — the one SC2 asserts on.
    end: () => {
      if (error !== null) return
      const rest = buffer
      buffer = ''
      if (rest !== '') consume(`${rest}\n`)
    },
  }
}

// What a turn produced — `{ state, contextId, reply }`, or `{ error }`.
//
// `sentContextId` is the id the caller sent, and it is asserted against the one that came back.
// ⚠️ THAT ASSERTION IS THE WHOLE POINT OF PASSING IT. A turn that ran under the server's default
// session returns a `contextId` just as happily as one that ran where it was asked to, so a
// mismatch is the ONLY client-side observable that the conversation is not the one intended —
// and it surfaces here rather than as unexplained memory loss two turns later.
// The state a `statusUpdate` frame carries. ⚠️ It is NESTED — `{ status: { state } }`, a2a-go
// v2.6.0 `TaskStatusUpdateEvent` — and the parts of an `artifactUpdate` sit one level down too, at
// `artifact.parts`. Measured 2026-10-13 against a real worker: reading both one level too shallow
// found no status update and no reply, so a turn that ran to completion was filed as SUBMITTED
// with an empty reply. The flat `{ state }` shape is still accepted.
export function a2aEventState(update) {
  return update?.status?.state ?? update?.state ?? null
}

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
    .map((f) => f?.artifactUpdate?.artifact ?? f?.artifactUpdate ?? f?.artifact ?? f)
    .flatMap((f) => f?.parts ?? [])
    .map((p) => p?.text ?? '')
    .filter((t) => t !== '')
    .join('\n')
  // ⚠️ THE LAST `statusUpdate` WINS, NOT THE TASK'S OWN STATUS. The frame order is
  // `task(SUBMITTED)` → `artifactUpdate` → `statusUpdate(COMPLETED)`, so the `task` frame is the
  // FIRST one and it carries a status too — reading `task.status.state` therefore reports
  // SUBMITTED on a turn that completed. Measured by this module's own test before it shipped.
  const statusUpdates = frames.map((f) => a2aEventState(f?.statusUpdate)).filter(Boolean)
  const state = statusUpdates.at(-1) ?? task.status?.state
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
// Exported so a caller recording a turn's outcome does not re-spell the literal: the spawn path
// needs the same value to decide whether a collected turn is a finished one or a failed one, and
// two copies of a wire constant is how they drift.
export const A2A_STATE_FAILED = 'TASK_STATE_FAILED'

// Why a `push` descriptor cannot be sent, or `null` when it can.
//
// Exported so the supervisor's own wiring refuses a half-configured receiver at the place the
// wire shape is decided, rather than discovering it as a controller-side 4xx at spawn time.
//
// ⚠️ BOTH HALVES ARE REQUIRED AND NEITHER IS A DEFAULT. A URL with no token would register a
// callback any sender could POST to; a token with no URL would register nothing and read as a
// configured channel. The two travel together or not at all.
export function a2aPushRefusal({ push } = {}) {
  if (push === null || typeof push !== 'object' || Array.isArray(push)) {
    return `\`push\` must be an object carrying \`url\` and \`token\` — got ${JSON.stringify(push) ?? 'undefined'}.`
  }
  if (typeof push.url !== 'string' || push.url === '') {
    return '`push.url` must be a non-empty string — the controller has nothing to POST to otherwise.'
  }
  let parsed
  try {
    parsed = new URL(push.url)
  } catch {
    return `\`push.url\` is not a parseable URL: ${JSON.stringify(push.url)}.`
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return `\`push.url\` must be http or https — got ${JSON.stringify(parsed.protocol)}.`
  }
  if (typeof push.token !== 'string' || push.token === '') {
    return '`push.token` must be a non-empty string — it is the only credential the receiver checks.'
  }
  return null
}

// The `ControlRequest` for one operation — `{ request }`, or `{ error }`.
//
// ⚠️ EXACTLY ONE OPERATION-SPECIFIC FIELD, and that is the controller's own rule rather than
// this module's preference: it refuses a request that sets none or more than one instead of
// guessing at the intent. A builder that silently dropped a stray field would turn a caller's
// mistake into a different operation than the one it asked for.
// The declared `mounts` / `secrets` a `create` carries, validated — `{ declared }`, or `{ error }`.
//
// ⚠️ THESE ARE THE ONLY ROUTE BY WHICH A CALLER PUTS SOMETHING INTO A WORKER'S ENVIRONMENT, and
// that is deliberate: the raw `env` argument stays REFUSED on this path (`a2aSpawnArgumentRefusal`),
// because a bare map is an ungoverned surface. A mount is a host path the controller checks against
// its own allowlist, and a secret is a TeamVault KEY the controller resolves itself — so the caller
// names what it wants and the controller decides whether it may have it. Passing a secret VALUE is
// not expressible here, which is the point rather than a limitation.
//
// ⚠️ VALIDATED HERE, NOT ONLY AT THE TOOL BOUNDARY. The tool schema is what a caller reads, but
// this builder is what puts bytes on the wire, and a test drives it directly — so a shape that
// only the schema rejected would be one the request builder would happily send.
export function declaredCreateArguments({ mounts, secrets } = {}) {
  const declared = {}

  if (mounts !== undefined) {
    if (!Array.isArray(mounts)) return { error: '`mounts` must be an array.' }
    const checked = []
    for (const [index, mount] of mounts.entries()) {
      if (mount === null || typeof mount !== 'object' || Array.isArray(mount)) {
        return { error: `\`mounts[${index}]\` must be an object.` }
      }
      const { host_path: hostPath, volume, container_path: containerPath, read_only: readOnly } = mount
      // ⚠️ EXACTLY ONE SOURCE, and `volume` is named in the refusal rather than ignored. The
      // controller refuses a named volume with its own `mount_not_allowed: volume <name>`; a
      // caller told only "not supported" here would have no idea which of its fields was the
      // problem, and the two refusals must agree about what a volume is.
      if (volume !== undefined) {
        return {
          error:
            `\`mounts[${index}]\` declares \`volume\`, which is refused: the mount allowlist governs ` +
            'host paths only, so a named volume would be an ungoverned mount surface. Use `host_path`.',
        }
      }
      if (typeof hostPath !== 'string' || hostPath === '') {
        return { error: `\`mounts[${index}].host_path\` must be a non-empty string.` }
      }
      if (typeof containerPath !== 'string' || containerPath === '') {
        return { error: `\`mounts[${index}].container_path\` must be a non-empty string.` }
      }
      if (readOnly !== undefined && typeof readOnly !== 'boolean') {
        return { error: `\`mounts[${index}].read_only\` must be a boolean when given.` }
      }
      checked.push({ host_path: hostPath, container_path: containerPath, ...(readOnly === undefined ? {} : { read_only: readOnly }) })
    }
    if (checked.length > 0) declared.mounts = checked
  }

  if (secrets !== undefined) {
    if (!Array.isArray(secrets)) return { error: '`secrets` must be an array.' }
    const checked = []
    for (const [index, secret] of secrets.entries()) {
      if (secret === null || typeof secret !== 'object' || Array.isArray(secret)) {
        return { error: `\`secrets[${index}]\` must be an object.` }
      }
      const { name, teamvault_key: teamVaultKey } = secret
      if (typeof name !== 'string' || name === '') {
        return { error: `\`secrets[${index}].name\` must be a non-empty string.` }
      }
      // ⚠️ THE KEY, NEVER THE VALUE. A caller cannot pass a secret's value through this field even
      // by accident: the controller resolves the key against TeamVault itself, so the value never
      // travels from here at all.
      if (typeof teamVaultKey !== 'string' || teamVaultKey === '') {
        return {
          error:
            `\`secrets[${index}].teamvault_key\` must be a non-empty TeamVault KEY. The value is never ` +
            'passed by the caller — the controller resolves the key itself.',
        }
      }
      checked.push({ name, teamvault_key: teamVaultKey })
    }
    if (checked.length > 0) declared.secrets = checked
  }

  return { declared }
}

export function a2aControlRequestFor({ operation, task, workerId, mounts, secrets } = {}) {
  if (!A2A_CONTROL_OPERATIONS.includes(operation)) {
    return {
      error:
        `\`operation\` must be one of ${A2A_CONTROL_OPERATIONS.map((o) => JSON.stringify(o)).join(', ')} ` +
        `— got ${JSON.stringify(operation) ?? 'undefined'}.`,
    }
  }
  // ⚠️ REFUSED, NOT DROPPED, and checked BEFORE the per-operation branches — which is the whole
  // point of its position. `list` and `stop` have no mount or secret surface at all, so a caller
  // that passed one has misunderstood the call; a value silently ignored is how a caller concludes
  // a worker was given access it never received. ⚠️ Placed after the `list` branch it would be
  // UNREACHABLE for `list`, because that branch returns first — a guard that can never fire reads
  // exactly like one that passed.
  if (mounts !== undefined || secrets !== undefined) {
    if (operation !== 'create') {
      return { error: `\`mounts\` and \`secrets\` are \`create\` arguments — \`${operation}\` takes neither.` }
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
    // ⚠️ NO PUSH DESCRIPTOR RIDES HERE, and that is a correction rather than an omission. The
    // controller never sees a task: it creates the worker and the SPAWNER sends the first turn,
    // which is what creates the task on the worker's own A2A server. The push config is registered
    // against that task, on that server, by the spawner — see `a2aPushConfigCallFor`. A callback
    // URL sent to the controller would be a field nothing could act on.
    const declared = declaredCreateArguments({ mounts, secrets })
    if (declared.error) return { error: declared.error }
    return { request: { operation, create: { task, ...declared.declared } } }
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

// The JSON-RPC method that registers a push callback against a task.
//
// ⚠️ NOT the `tasks/pushNotificationConfig/set` spelling. That name belongs to a DIFFERENT
// transport — the v0 compat binding, `a2acompat/a2av0/jsonrpc.go:24` — where the params wrap the
// config in a `pushNotificationConfig` object. This binding's params are the flat `a2a.PushConfig`;
// see the builder below for why that distinction is worth a paragraph.
export const A2A_PUSH_CONFIG_METHOD = 'CreateTaskPushNotificationConfig'

// The registration call that arms the worker→spawner status channel — `{ url, headers, body }`,
// or `{ error }`.
//
// ⚠️ THE PARAMS ARE FLAT, AND CLOSING THAT TRAP IS THIS BUILDER'S WHOLE REASON TO EXIST. The
// obvious A2A spelling is `{ taskId, pushNotificationConfig: { url, token } }` — and it is WRONG
// here. The handler unmarshals its params as `a2a.PushConfig` **directly**
// (`a2asrv/jsonrpc.go:334-340`), and that struct carries `taskId`, `url` and `token` as its OWN
// fields (`a2a/push.go:71,82,85`). So the task id is a SIBLING of the url, never a wrapper around
// it. A wrapped body is perfectly well-formed JSON that the handler refuses as an unmarshal
// error, which reads downstream as "malformed request" rather than as "wrong protocol version" —
// the misdiagnosis this comment is here to prevent.
//
// ⚠️ TWO DIFFERENT CREDENTIALS RIDE ON THIS ONE CALL, and conflating them is the other silent
// failure. The bearer in the headers authenticates to the WORKER; `params.token` is the credential
// the RECEIVER verifies, which the SDK later echoes back as the `A2A-Notification-Token` header.
// Swapping them would either hand the receiver's token to the worker or register the worker's
// token as the push credential — and either one still returns a 200.
export function a2aPushConfigCallFor({ endpoint, taskId, push, token, requestId = 1 } = {}) {
  for (const [name, value] of Object.entries({ endpoint, taskId, token })) {
    if (typeof value !== 'string' || value === '') {
      return { error: `\`${name}\` is required to register a push callback over A2A.` }
    }
  }
  const refusal = a2aPushRefusal({ push })
  if (refusal) return { error: refusal }

  return {
    url: endpoint,
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${token}`,
    },
    body: {
      jsonrpc: '2.0',
      id: requestId,
      method: A2A_PUSH_CONFIG_METHOD,
      params: { taskId, url: push.url, token: push.token },
    },
  }
}

export const A2A_PUSH_CONFIG_LIST_METHOD = 'ListTaskPushNotificationConfigs'

// The read-back half of the round trip — `{ url, headers, body }`, or `{ error }`.
//
// ⚠️ LIST, NOT GET, AND THAT IS A MEASURED CORRECTION RATHER THAN A PREFERENCE. The first version
// of this read-back used `GetTaskPushNotificationConfig` with `params: {taskId}` and every call
// returned "push config not found" against a worker whose pushes were demonstrably arriving. The
// cause is in the SDK: the handler reads `h.pushConfigStore.Get(ctx, req.TaskID, req.ID)`
// (`a2asrv/handler.go:419`) — the store is keyed by task id **and config id** — and the set path
// never sends a config `id`, so a get looks up an empty one and can never match. `List` is keyed by
// `taskId` alone and returns `{configs: […]}`.
//
// ⚠️ `CreateTaskPushNotificationConfig` IS ONLY EVIDENCE THAT THE WORKER ACCEPTED A REGISTRATION.
// It says nothing about what the worker then stored, and an accepted-then-dropped config is
// indistinguishable from a working one until something reads it back. This builder is that read.
export function a2aPushConfigListCallFor({ endpoint, taskId, token, requestId = 1 } = {}) {
  for (const [name, value] of Object.entries({ endpoint, taskId, token })) {
    if (typeof value !== 'string' || value === '') {
      return { error: `\`${name}\` is required to read a push callback back over A2A.` }
    }
  }
  return {
    url: endpoint,
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${token}`,
    },
    body: {
      jsonrpc: '2.0',
      id: requestId,
      method: A2A_PUSH_CONFIG_LIST_METHOD,
      params: { taskId },
    },
  }
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
    // ⚠️ The result is a `SendMessageResponse` ONEOF — `{ "task": {…} }` or `{ "message": {…} }` —
    // not the task itself. Measured 2026-10-13 against a2a-worker-controller v0.4.4 (a2a-go
    // v2.6.0): reading `result` as the task found no status and no artifact on a `create` that had
    // in fact started a container, so every real control call failed and the worker was orphaned.
    // A bare task is still accepted, so a server that does not wrap keeps working.
    const result = payload?.result ?? payload
    const task = result?.task ?? result?.message ?? result
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
  const statusUpdates = frames.map((f) => a2aEventState(f?.statusUpdate)).filter(Boolean)
  const state = statusUpdates.at(-1) ?? task?.status?.state

  if (state === A2A_STATE_FAILED) {
    return {
      // ⚠️ THE FLAG IS WHAT LETS A CALLER BRANCH ON THIS WITHOUT PARSING THE MESSAGE. A call that
      // reached the controller and was refused is a different fact from one that never arrived,
      // and the supervisor's recovery keys on exactly that difference — matching on the error
      // text instead would break the first time this sentence is reworded.
      controllerFailed: true,
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
