// The A2A worker-controller spawn path — what the `docker` and `k8s` targets can and cannot
// honour, how the controller is located, and the shape a spawned A2A worker takes.
//
// ⚠️ THIS PATH IS ASYNCHRONOUS, AND THAT IS THE ONE THING IT CANNOT COPY FROM `cluster`.
// `spawnClusterWorker` holds its request open until the pod answers and writes a FINISHED
// `result` into the agent record, because a cluster turn is over by the time that call returns.
// SC5 requires the opposite here — the send returns in **under 2 s** while the turn runs **over
// 60 s** — so nothing on this path may wait for a turn, and the record a spawn writes must carry
// NO result. A wiring that copied the cluster shape would satisfy the letter of "wire it" and
// fail SC5 outright, which is why that difference is stated here rather than left to the reader.
//
// Every RULE here is pure — no fetch, no clock, no filesystem — because `supervisor.mjs`
// connects a stdio server at load and so cannot be imported by a test at all, and a rule that
// lives only in that file ships uncovered. The two CALLS at the bottom are the module's only
// impure exports, and each takes its `fetchImpl` as an argument for the same reason: a call
// that can only be exercised against a live controller is a call nobody exercises.

import {
  A2A_STATE_FAILED,
  a2aControlRequestFor,
  a2aControlResponseFor,
  a2aContextIdFor,
  a2aFrames,
  a2aOutcome,
  a2aSendRequestFor,
  a2aStreamRequestFor,
  a2aTaskIdOf,
  a2aWorkerList,
  createSseFrameReader,
  resolveA2AEndpoint,
} from './a2a-client.mjs'

// The targets this path serves. Kept as one list so the dispatch and the argument refusals
// cannot disagree about which targets it implements.
export const A2A_SPAWN_TARGETS = ['docker', 'k8s']

// The ledger `mode` for a worker created over A2A.
//
// ⚠️ A THIRD VALUE, NOT A REUSE OF `cluster`, for the reason `cluster` itself is not `headless`:
// the mode says how a worker runs and therefore what its liveness means. A cluster worker is the
// shared long-lived `claude-interactive` pod; an A2A worker is a per-task container or pod the
// controller created. Folding either into `headless` would file it under a mode whose whole
// meaning is "a local process that parks its gates on this server", which is exactly what it is
// not. `unobservedPatch` already answers the generic non-interactive reason for every mode but
// `interactive`, so a new value inherits a correct-if-coarse reason rather than a wrong one.
export const A2A_LEDGER_MODE = 'a2a'

// The roster status an A2A worker carries — the TARGET NAME, mirroring `cluster`.
//
// ⚠️ The status names WHERE the worker runs, and `cluster` already works exactly this way: it is
// both a spawn target and the status a spawned cluster worker carries. A docker worker reported
// as `running` would erase the target from every roster read, and a manager could no longer tell
// a container on this Mac from a pod in nuke dev.
export const a2aWorkerStatus = ({ target } = {}) =>
  A2A_SPAWN_TARGETS.includes(target) ? target : null

// The ledger `mode` for a worker status, or `null` when the status is not an A2A one.
//
// Exported so `writeLedger`'s derivation consults one rule rather than growing a third nested
// ternary — and so the mapping is unit-tested rather than inferred from the dispatch.
export const ledgerModeForStatus = (status) => (A2A_SPAWN_TARGETS.includes(status) ? A2A_LEDGER_MODE : null)

// Arguments the A2A path cannot honour — a refusal string, or `null` to allow the call.
//
// REFUSED RATHER THAN DROPPED, matching the cluster path and for the reason its own refusals
// give: a key accepted and quietly ignored is how a caller ends up believing a worker was
// resumed when a fresh session was opened, or that it runs under a policy nothing consults.
//
// The line every refusal below is drawn on: the controller creates this worker, so the
// supervisor never runs it and never holds a process for it. Anything that names a LOCAL
// process, a local window, or a local policy is therefore not something this path can honour.
export function a2aSpawnArgumentRefusal({ target, resume, policy, interactive, windowId, role, env, shipping, task } = {}) {
  if (!A2A_SPAWN_TARGETS.includes(target)) {
    return `a2aSpawnArgumentRefusal was called for target ${JSON.stringify(target) ?? 'undefined'}, which is not an A2A target (${A2A_SPAWN_TARGETS.join(', ')}).`
  }
  // ⚠️ `env` STAYS REFUSED, AND IT IS THE ONLY ONE OF THE THREE THAT DOES. A raw map is an
  // ungoverned surface: nothing here can tell a harmless variable from a credential, and the value
  // would travel from this server to the controller as plaintext. `mounts` and `secrets` are the
  // sanctioned route to the same need and are NOT refused — a mount is checked against the
  // controller's own host-path allowlist, and a secret is a TeamVault KEY the controller resolves
  // itself, so the caller never holds or transmits the value. The refusal names them, because a
  // caller told only "no" would reach for a local spawn to get what a declared secret already gives.
  if (env && Object.keys(env).length > 0) {
    return (
      `env is not supported with target "${target}": the controller creates this worker and this server ` +
      'never runs it, so there is no local process here to set an environment on. Pass the environment to ' +
      "the worker's own image, or spawn locally. To give the worker a FILE or a CREDENTIAL, declare " +
      '`mounts` (a host path the controller checks against its allowlist) or `secrets` (a TeamVault key ' +
      'the controller resolves itself) instead — neither of those travels as a value from here.'
    )
  }
  if (resume) {
    return (
      `resume is not supported with target "${target}": this path mints a NEW session id, so it cannot ` +
      'continue an existing conversation. To send a further turn to a worker that already exists, call ' +
      '`send_agent_message` on that worker — the refusal names it because a caller told only "not supported" ' +
      'is left with no route at all. Omit `resume`, or spawn locally.'
    )
  }
  if (policy) {
    return (
      `policy is not supported with target "${target}": the policy layer answers permission requests raised on ` +
      'THIS server, and a worker the controller created raises its gates against its own attention route, never ' +
      'against this one. Omit `policy`, or spawn locally.'
    )
  }
  if (interactive !== undefined) {
    return (
      `interactive is not supported with target "${target}": it chooses between a local wezterm tab and a local ` +
      'headless worker, and this target creates neither — the worker is created by the controller. Omit it.'
    )
  }
  if (windowId !== undefined) {
    return (
      `window_id is not supported with target "${target}": it names a local WezTerm window, and this target opens ` +
      'no tab on this machine. Omit it.'
    )
  }
  if (role !== undefined) {
    return (
      `role is not supported with target "${target}": it resolves a colour and a window from the LOCAL role map, ` +
      'and this target opens no local tab for either to apply to. Omit it.'
    )
  }
  if (shipping === true) {
    return (
      `shipping is not supported with target "${target}": it pre-allows \`git add\` / \`commit\` / \`push\` for a ` +
      'worker this server supervises, and a worker the controller created is supervised there. Omit it.'
    )
  }
  if (typeof task !== 'string' || task === '') {
    return (
      `target "${target}" requires \`task\`: it names the vault task the worker is opened for, and it is the name ` +
      'the controller creates one worker for. Pass the task name, plus `vault` when that name is not unique ' +
      'across the configured vaults.'
    )
  }
  return null
}

// A controller base URL, or a refusal. Refused rather than defaulted: the controller runs on
// this Mac for `docker` and in nuke dev for `k8s`, and there is no address that is right for
// both, so a guessed one either fails confusingly or reaches the wrong thing.
function checkControllerUrl(value, label) {
  if (typeof value !== 'string' || value.trim() === '') {
    return {
      error:
        `${label} is ${JSON.stringify(value) ?? 'undefined'}, which is not a controller URL. Set it to the A2A ` +
        'endpoint the controller serves, e.g. "http://127.0.0.1:8080/a2a".',
    }
  }
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    return { error: `${label} is not a parseable URL.` }
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return { error: `${label} must be an http or https URL — got protocol ${JSON.stringify(parsed.protocol)}.` }
  }
  return { url: value.replace(/\/+$/, '') }
}

// The controller's base URL and the token this server presents to it, FOR ONE TARGET — or
// `{ error }`.
//
// ⚠️ `target` is required, and there is deliberately no target-agnostic fallback. The controller
// runs on this Mac for `docker` and in nuke dev for `k8s`: two separate deployments, with separate
// control secrets, and no address that is right for both. So a single `a2a.url` is not a default —
// it is a value that is wrong for one target whichever one it holds. Measured 2026-10-14: a
// `target: "docker"` spawn against the one configured URL created a k8s pod, because that URL was
// the nuke-dev ingress.
//
// BOTH sources are validated, not only the winner, matching `resolveClusterTarget`: a bad value
// in the loser sits dormant until the winner is removed, and then the spawn breaks with an error
// that was present all along.
//
// ⚠️ The TOKEN's value is never echoed, here or anywhere else on this path — not in a refusal,
// not in a log line, not in a tool result. `resolveClusterTarget` redacts a URL's userinfo for
// the same reason, and the refusals below name the KEY rather than repeating what it held.
export function resolveA2AControllerTarget({ target, envUrl, envToken, file, path = 'the supervisor config' } = {}) {
  if (!A2A_SPAWN_TARGETS.includes(target)) {
    return {
      error:
        `${JSON.stringify(target)} is not an A2A spawn target, so no controller can be resolved for it. ` +
        `Expected ${A2A_SPAWN_TARGETS.map((t) => `"${t}"`).join(' or ')}.`,
    }
  }

  const section = file == null ? null : file.a2a
  if (section != null && (typeof section !== 'object' || Array.isArray(section))) {
    return {
      error:
        `"a2a" in ${path} is not an object — refusing rather than guessing, since a half-read controller ` +
        'target is discovered only by noticing it. Expected {"url": {"docker": "…", "k8s": "…"}}.',
    }
  }

  // ⚠️ A bare-string `a2a.url` REFUSES rather than being read as both targets. That shape is what
  // made the 2026-10-14 divergence reachable: one address cannot serve a local container and a
  // remote pod, so whichever target read it, the other silently got the wrong controller. Refusing
  // turns a mis-route into a startup-visible error naming the key to change.
  if (typeof section?.url === 'string') {
    return {
      error:
        `"a2a.url" in ${path} is a single string, and one controller URL cannot serve both spawn targets — ` +
        'the docker controller runs on this Mac and the k8s controller in nuke dev. Replace it with a ' +
        `per-target map, e.g. {"url": {"docker": "http://127.0.0.1:8080/a2a", "k8s": "https://…/a2a"}}.`,
    }
  }

  // ⚠️ A plaintext `a2a.token` REFUSES rather than being read, mirroring `resolveClusterTarget`.
  // The control token reaches the server from TeamVault through the launch wrapper, as
  // `SUPERVISOR_A2A_TOKEN_<TARGET>`; a copy in this file is the exposure being removed, so reading
  // it would leave the very thing being deleted working silently. Unconditional by operator
  // decision (2026-10-14) — the deploy is ordered so this never goes live against a config
  // that still holds one (see the task's # Sequence).
  //
  // `''` is absence, matching `set` below. The VALUE is never echoed: only the key and the
  // file are named.
  if (section?.token !== undefined && section.token !== null && section.token !== '') {
    return {
      error:
        `"a2a.token" is set in ${path}, and the supervisor no longer reads a controller token from the ` +
        'config file. The token now reaches the server from TeamVault through the launch wrapper, as ' +
        `SUPERVISOR_A2A_TOKEN_<TARGET>. Delete the "a2a.token" key in ${path}.`,
    }
  }

  const upper = target.toUpperCase()
  const envUrlLabel = `SUPERVISOR_A2A_URL_${upper}`
  const envTokenLabel = `SUPERVISOR_A2A_TOKEN_${upper}`
  const fileUrlLabel = `"a2a.url.${target}" in ${path}`

  const urlSources = [
    { label: envUrlLabel, value: envUrl },
    { label: fileUrlLabel, value: section?.url?.[target] },
  ]
  // ⚠️ One source, not two: the file's `a2a.token` is refused above, so listing it here would
  // advertise a source that can never win. A per-target token has no file shape at all — the
  // migration removed plaintext tokens from the file, and re-adding one per target would restore
  // exactly the exposure it removed.
  const tokenSources = [
    { label: envTokenLabel, value: envToken },
  ]
  const set = (s) => s.value !== undefined && s.value !== null && s.value !== ''
  const pick = (sources) => sources.find(set)?.value ?? null

  // The URL is checked for every source that is set, then the winner once more when none is —
  // so the refusal always names the key the operator must change rather than a default path.
  let url = null
  for (const source of urlSources) {
    if (!set(source)) continue
    const checked = checkControllerUrl(source.value, source.label)
    if (checked.error) return { error: checked.error }
    if (url === null) url = checked.url
  }
  if (url === null) {
    const checked = checkControllerUrl(undefined, `${envUrlLabel} (or ${fileUrlLabel})`)
    if (checked.error) return { error: checked.error }
  }

  const token = pick(tokenSources)
  if (token === null) {
    return {
      error:
        `no controller token is configured for the "${target}" target — ${envTokenLabel} is not set. The token ` +
        'is resolved from TeamVault by scripts/mcp-start.py, which the supervisor MCP entry must name as its ' +
        '`command`; changing that needs a NEW session, since Claude Code caches the server definition at ' +
        `session start. An "a2a.token" in ${path} is NOT a fallback: it is refused, because a plaintext ` +
        'token there is the exposure this change removes. The controller refuses an unauthenticated control ' +
        'call, so a spawn without one would fail at the controller rather than here, with nothing on this ' +
        'side naming the missing key.',
    }
  }
  if (typeof token !== 'string' || token.trim() === '') {
    return {
      error:
        'the configured controller token is not a usable string — it is set but empty or non-string. The value ' +
        `is deliberately not echoed. Check ${envTokenLabel}.`,
    }
  }
  return { url, token }
}

// The one-shot control call that creates this worker — `{ url, headers, body, contextId }`, or
// `{ error }`.
//
// `create` is a ONE-SHOT `SendMessage`, never the streaming spelling: the controller serves the
// control API on a route that answers a single object, and asking it for a stream reads as a call
// that produced no frames. The turn that follows goes to the WORKER, over the streaming spelling
// — two different routes, and conflating them is the mistake this split exists to prevent.
export function a2aCreateCallFor({ endpoint, task, token, messageId, contextId, mint, mounts, secrets } = {}) {
  const context = a2aContextIdFor({ previous: contextId, mint })
  if (context.error) return { error: context.error }
  // `mounts` / `secrets` are forwarded UNCHANGED — this path's whole job for them is to carry a
  // caller's declaration to the controller, which is the only party that can allow or resolve it.
  // Validating them here as well as in the builder would be a second opinion that can disagree
  // with the first, and the builder is what puts bytes on the wire.
  const control = a2aControlRequestFor({ operation: 'create', task, mounts, secrets })
  if (control.error) return { error: control.error }
  const call = a2aSendRequestFor({ endpoint, contextId: context.contextId, request: control.request, token, messageId })
  if (call.error) return { error: call.error }
  return { ...call, contextId: context.contextId }
}

// Where a worker's Agent Card is served, given the A2A URL `create` returned for it.
//
// ⚠️ THE ORIGIN, NOT THE PATH. `create` returns the worker's `/a2a` endpoint; the card is served
// at the well-known path on the SAME origin. Reading the card is not decoration — it is what
// proves the URL the controller handed back actually answers, and what yields the endpoint a
// turn is sent to, rather than trusting the returned string to be the endpoint as well.
export function a2aCardUrlFor({ a2aUrl } = {}) {
  if (typeof a2aUrl !== 'string' || a2aUrl === '') {
    return { error: `\`a2aUrl\` is ${JSON.stringify(a2aUrl) ?? 'undefined'}, which is not a worker A2A URL.` }
  }
  let parsed
  try {
    parsed = new URL(a2aUrl)
  } catch {
    return { error: 'the worker A2A URL returned by `create` is not a parseable URL.' }
  }
  return { url: `${parsed.origin}/.well-known/agent-card.json` }
}

// The agent record a spawn writes — pure, so the fields that must NOT appear can be asserted in
// a test rather than checked by eye.
//
// ⚠️ NO `result`, AND THAT IS THE ASYNCHRONY MADE EXPLICIT. The cluster path writes a finished
// result here because its turn is over; this path's turn has not been sent yet when the spawn
// returns. A placeholder would be read by `agent_status` and `list_agents` as a finished turn
// that produced nothing — the exact silence `clusterTurnResult` exists to remove, arriving from
// the opposite direction. `null` is the honest value: nothing has been collected yet.
//
// ⚠️ `transcript` AND `permissions` ARE SEEDED, NOT OPTIONAL. `agentView` dereferences both
// unconditionally (`a.transcript.length`, `a.permissions.filter`) and `list_agents` maps over
// EVERY agent, so one record missing either makes the whole roster unreadable rather than just
// that row. A worker the controller created has no transcript on this machine and parks no
// permissions here, which is exactly why they must be EMPTY rather than absent.
export function a2aWorkerRecord({
  id,
  label,
  prompt,
  task,
  target,
  sessionId,
  workerId,
  a2aUrl,
  workerToken,
  parentSession = null,
  createdAt,
} = {}) {
  const status = a2aWorkerStatus({ target })
  if (status === null) {
    return { error: `a2aWorkerRecord was called for target ${JSON.stringify(target) ?? 'undefined'}, which is not an A2A target.` }
  }
  return {
    record: {
      id,
      label: label || id,
      prompt,
      // No local working directory: the controller chose the worker's cwd, and a value here
      // would be this server guessing at a directory it never set.
      cwd: null,
      status,
      rules: null,
      policyPath: null,
      sessionId,
      resumedFrom: null,
      carriedDecision: null,
      parentSession,
      modeSource: 'argument',
      role: null,
      windowId: null,
      launcher: null,
      paneId: null,
      transcript: [],
      permissions: [],
      // See the header: nothing has been collected, and null says so.
      result: null,
      error: null,
      task,
      createdAt,
      // ⚠️ The three fields this path adds, and the three `agentView` and `buildRecord` must
      // never project. Both are explicit allowlists of named fields with no spread, so a token
      // held HERE is invisible to `list_agents` / `agent_status` and is never written to the
      // ledger — provided a later change does not add any of them to either projection.
      // `workerToken` authenticates every follow-up turn to this worker; it is the value this
      // task's own Definition of Done forbids printing, echoing or writing anywhere.
      a2aUrl,
      workerId,
      workerToken,
    },
  }
}

// --- the two calls ---------------------------------------------------------------------------
//
// Both take `fetchImpl`, defaulting to the global, so a test drives them with a stub rather than
// a live controller. Neither logs and neither echoes the token: the response body of `create`
// CARRIES a credential, and a body printed is a transcript write.

// Ask the controller to create this task's worker — `{ worker, token, contextId }`, or `{ error }`.
//
// ⚠️ A FAILED STATUS DOES NOT MEAN NO WORKER WAS CREATED. The controller's store copies a task
// through gob *after* the executor has run, so a `create` can have started a container and still
// report FAILED. The caller must reconcile against the controller's own `list` — or against
// `docker ps --filter label=…` — rather than assume nothing happened, which is why this returns
// the controller's own refusal text rather than a "did not create" claim.
//
// `capture`, when given, receives `{ url, body }` — the exact body this call sends, at the send
// site, which is what SC2 diffs between a `docker` and a `k8s` spawn. ⚠️ NEVER THE HEADERS: the
// bearer token travels in `Authorization`, so handing the headers to a capture sink would write
// the credential this task's Definition of Done forbids writing anywhere. A throwing capture
// never fails the spawn.
export async function createWorkerOnController({ endpoint, task, token, messageId, contextId, mint, mounts, secrets, fetchImpl = fetch, capture } = {}) {
  const call = a2aCreateCallFor({ endpoint, task, token, messageId, contextId, mint, mounts, secrets })
  if (call.error) return { error: call.error }
  if (typeof capture === 'function') {
    try {
      capture({ url: call.url, body: call.body })
    } catch {
      // A capture is evidence, not part of the spawn — losing it must not lose the worker.
    }
  }

  let res
  try {
    res = await fetchImpl(call.url, {
      method: 'POST',
      headers: call.headers,
      body: JSON.stringify(call.body),
    })
  } catch (cause) {
    return { error: `the create call to ${call.url} could not be made: ${cause?.message ?? cause}` }
  }
  if (!res.ok) {
    return { error: `the controller answered HTTP ${res.status} to the create call at ${call.url}.` }
  }
  // ⚠️ `text()`, NOT `json()`. `a2aControlResponseFor` takes the RAW body and parses it itself,
  // because the controller serves the same executor behind two framings — a one-shot JSON-RPC
  // object and an SSE stream — and only the raw body says which arrived. Handing it a parsed
  // object refuses with "the control reply body must be a string", which reads as a malformed
  // reply rather than as the caller having parsed too early.
  let body
  try {
    body = await res.text()
  } catch (cause) {
    return { error: `the create reply body could not be read: ${cause?.message ?? cause}` }
  }

  const parsed = a2aControlResponseFor(body, { sentOperation: 'create' })
  // `controllerFailed` is carried through, not swallowed: the caller's recovery applies only to a
  // call the controller actually answered and refused, never to one that never arrived.
  if (parsed.error) return { error: parsed.error, controllerFailed: parsed.controllerFailed === true }

  const created = parsed.response?.create
  const worker = created?.worker
  if (worker === null || typeof worker !== 'object') {
    return { error: 'the create reply carried no `create.worker`, so there is no worker to reach.' }
  }
  if (typeof worker.a2a_url !== 'string' || worker.a2a_url === '') {
    return {
      error:
        'the create reply named a worker with no `a2a_url`, so there is no route to send a turn to. ' +
        'Refusing rather than falling back to the controller, which would send every worker turn to the ' +
        'controller instead.',
    }
  }
  // ⚠️ The token is required, not optional: every follow-up turn authenticates with it, so a
  // create that returned none would produce a worker nothing can talk to.
  if (typeof created?.token !== 'string' || created.token === '') {
    return { error: 'the create reply carried no worker token, so no turn could authenticate to this worker.' }
  }
  return { worker, token: created.token, contextId: call.contextId }
}

// Read the worker's Agent Card and resolve the endpoint a turn is sent to — `{ endpoint }`, or
// `{ error }`.
//
// ⚠️ THE CARD IS READ FOR A REASON, NOT AS DECORATION. It is what proves the URL the controller
// returned actually answers, and it is where the endpoint comes from — `supportedInterfaces[0].url`
// — rather than trusting the returned string to be the endpoint as well. A worker whose card 404s
// is refused HERE, at spawn, instead of at the first turn.
//
// ⚠️ A REFUSED CONNECTION IS RETRIED until `readyTimeoutMs`, and nothing else is. `create` returns
// once the container is STARTED, not once its server listens — measured 2026-10-13 against
// a2a-worker-controller v0.4.4: the card fetched the instant `create` returned failed with
// `fetch failed`, and the same URL answered 200 five seconds later. Without the retry every live
// spawn lost its worker to that race.
//
// ⚠️ ON k8s THE INGRESS ANSWERS BEFORE THE WORKER DOES, so 404 / 502 / 503 / 504 are retried too.
// Measured 2026-10-14 against the nuke-dev controller: the card answered 404 the instant `create`
// returned (no route while the pod was Pending), 503 once the route existed, and 200 ~45 s after
// create — so a 404 is NOT proof the worker is speaking there, and treating it as final orphaned a
// worker that was coming up. Any other HTTP answer stays final; the deadline bounds a genuine 404.
export const A2A_CARD_READY_TIMEOUT_MS = 120_000
export const A2A_CARD_RETRY_MS = 500
const A2A_CARD_NOT_READY_STATUSES = new Set([404, 502, 503, 504])
export async function fetchWorkerCard({
  a2aUrl,
  fetchImpl = fetch,
  readyTimeoutMs = A2A_CARD_READY_TIMEOUT_MS,
  retryMs = A2A_CARD_RETRY_MS,
  now = Date.now,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
} = {}) {
  const cardUrl = a2aCardUrlFor({ a2aUrl })
  if (cardUrl.error) return { error: cardUrl.error }

  const deadline = now() + readyTimeoutMs
  let res
  for (;;) {
    try {
      res = await fetchImpl(cardUrl.url, { method: 'GET' })
      if (!A2A_CARD_NOT_READY_STATUSES.has(res.status) || now() >= deadline) break
      await sleep(retryMs)
    } catch (cause) {
      if (now() >= deadline) {
        return {
          error: `the worker's Agent Card at ${cardUrl.url} could not be fetched within ${readyTimeoutMs} ms: ${cause?.message ?? cause}`,
        }
      }
      await sleep(retryMs)
    }
  }
  if (!res.ok) {
    return { error: `the worker's Agent Card at ${cardUrl.url} answered HTTP ${res.status}, so the worker is not reachable.` }
  }
  let card
  try {
    card = await res.json()
  } catch {
    return { error: `the worker's Agent Card at ${cardUrl.url} is not JSON.` }
  }
  const endpoint = resolveA2AEndpoint(card)
  if (endpoint.error) return { error: endpoint.error }
  return { card, endpoint: endpoint.url }
}

// ─────────────────────────────────────────────────────────────────────────────────────────
// THE TURN HALF — what happens to the worker once it exists.
//
// ⚠️ TWO CALLS, NOT ONE, AND THE SPLIT **IS** SC5. SC5 measures the SEND's latency against the
// TURN's own duration in the same run — `< 2 s` against `> 60 s` — and names its falsifier
// outright: *"a send whose latency tracks the turn's duration (blocking)"*. A single call that
// returned the reply would BE that falsifier by construction: its latency is the turn's duration,
// and no amount of care in the caller could change it. So the request is issued and acknowledged
// by one call, and its body is consumed by a second the caller may await later — which is what
// "the result is collected afterwards" means when it is a shape rather than a promise.

// How long THE SEND waits for the worker to acknowledge before handing back an unresolved request.
//
// ⚠️ DELIBERATELY BELOW SC5'S 2 s BUDGET rather than equal to it: that budget covers the whole
// spawn call, so a bound set at 2 s would spend the entire allowance on this one step and leave
// the rest of the path nothing. At 1.5 s a worker that acknowledges promptly returns in
// milliseconds, and one that never acknowledges still leaves the spawn inside its budget.
export const A2A_ACK_TIMEOUT_MS = 1500

// A sentinel no real outcome can equal, so `Promise.race` cannot confuse "the worker did not
// answer in time" with "the worker answered with something falsy".
const A2A_NOT_ACKNOWLEDGED = Symbol('a2a-not-acknowledged')

// Send one turn to the worker — THE SEND.
//
// Returns `{ res, contextId }` once the worker has acknowledged, `{ pending, contextId }` when it
// has not acknowledged within `ackTimeoutMs`, or `{ error }` when the request could not be issued
// at all. ⚠️ THE THREE ARE DISTINCT ON PURPOSE: "the worker is thinking" and "the request never
// left" are different facts, and a caller told the second when the first is true will send a
// second turn to a worker already working on the first.
//
// ⚠️ IT NEVER ABORTS. The timeout bounds WAITING, not the request — the fetch is left running and
// handed back as `pending` for `collectWorkerTurn` to await. Aborting would kill the very turn
// this call exists to start, and the caller would be told the send failed while the worker was
// working: the most expensive available way to be wrong.
export async function openWorkerTurn({
  endpoint,
  contextId,
  text,
  token,
  messageId,
  ackTimeoutMs = A2A_ACK_TIMEOUT_MS,
  fetchImpl = fetch,
} = {}) {
  const call = a2aStreamRequestFor({ endpoint, contextId, text, token, messageId })
  if (call.error) return { error: call.error }

  // ⚠️ `issued` RESOLVES TO THE RESPONSE ITSELF, not to a wrapper — because it is handed back as
  // `pending` and `collectWorkerTurn` awaits it directly. A wrapper here would make the collector
  // read `{ res }` as the response and fail with `settled.text is not a function`, which reads as
  // a broken worker rather than as the shape mismatch it is.
  const issued = (async () => {
    try {
      return await fetchImpl(call.url, {
        method: 'POST',
        headers: call.headers,
        body: JSON.stringify(call.body),
      })
    } catch (cause) {
      throw new Error(`the turn to ${call.url} could not be sent: ${cause?.message ?? cause}`)
    }
  })()

  // A non-positive bound disables it outright, which is what a test wants when it asserts the
  // send's own outcome rather than the timing of it.
  if (!(ackTimeoutMs > 0)) return { pending: issued, contextId }

  // `settled` never rejects: both outcomes are folded into one shape so `Promise.race` can
  // compare them, and so an error arriving AFTER the bound has already fired cannot surface as an
  // unhandled rejection on a promise the caller has stopped waiting on.
  const settled = issued.then(
    (res) => ({ res }),
    (cause) => ({ error: cause?.message ?? String(cause) }),
  )

  let timer
  const raced = await Promise.race([
    settled,
    new Promise((resolve) => {
      timer = setTimeout(() => resolve(A2A_NOT_ACKNOWLEDGED), ackTimeoutMs)
    }),
  ])
  clearTimeout(timer)

  if (raced === A2A_NOT_ACKNOWLEDGED) return { pending: issued, contextId }
  if (raced.error) return { error: raced.error }
  return { res: raced.res, contextId }
}

// Register the push callback against a task the WORKER already holds — `{ registered: true }`, or
// `{ error }`.
//
// ⚠️ AGAINST THE WORKER, NEVER THE CONTROLLER, and this is the correction the whole design turned
// on. The controller creates the worker but never sees a task — the SPAWNER's first turn is what
// creates it, on the worker's own A2A server — so a callback URL sent to the controller would be a
// field nothing could act on. See `a2aPushConfigCallFor` for the wire shape and its two traps.
//
// ⚠️ THE FAILURE IS RETURNED, NOT THROWN, and the caller is expected to treat it as non-fatal. The
// channel is reporting-only: a worker whose registration was refused still runs its turn and still
// files an outcome, and failing the spawn over it would lose a live container to an optional
// transport. The refusal is surfaced so the caller can record it rather than swallow it.
export async function registerPushCallbackOnWorker({ endpoint, taskId, push, token, fetchImpl = fetch } = {}) {
  const call = a2aPushConfigCallFor({ endpoint, taskId, push, token })
  if (call.error) return { error: call.error }
  let res
  try {
    res = await fetchImpl(call.url, { method: 'POST', headers: call.headers, body: JSON.stringify(call.body) })
  } catch (cause) {
    return { error: `the push registration to ${call.url} could not be made: ${cause?.message ?? cause}` }
  }
  if (!res.ok) {
    return { error: `the worker answered HTTP ${res.status} to the push registration.` }
  }
  return { registered: true }
}

// Consume the turn's stream — THE COLLECTION. `{ state, contextId, reply }`, or `{ error }`.
//
// `res` may be a settled response OR the still-pending promise `openWorkerTurn` returned as
// `pending`. `await` is the same expression for both, so the collector never has to know which
// case it is in — and that is the point: the two differ only in WHEN they resolve, and a
// collector that cared would be re-deriving a distinction the sender had already made.
export async function collectWorkerTurn({ res, sentContextId, onTaskId } = {}) {
  if (!res) return { error: 'collectWorkerTurn was given no response to collect from.' }

  let settled
  try {
    settled = await res
  } catch (cause) {
    return { error: `the turn's response could not be read: ${cause?.message ?? cause}` }
  }
  if (!settled || typeof settled !== 'object') return { error: 'the turn produced no response object.' }
  if (settled.ok === false) return { error: `the worker answered HTTP ${settled.status} to the turn.` }

  // ⚠️ INCREMENTAL WHEN ASKED FOR AND POSSIBLE; the whole-body path below stays the reference. A
  // caller wanting the task id EARLY — to register a push callback before the mid-turn event fires
  // ~10 s in — passes `onTaskId`, and the stream is then consumed chunk by chunk. Every other
  // caller keeps `.text()`, and that path is what the existing tests stub; the reader's own
  // split-point test is what keeps the two from drifting apart.
  if (typeof onTaskId === 'function' && settled.body && typeof settled.body.getReader === 'function') {
    return collectTurnIncrementally({ body: settled.body, sentContextId, onTaskId })
  }

  // ⚠️ `text()`, NOT `json()`, for the reason `createWorkerOnController` gives: the frames arrive
  // as an SSE body and `a2aFrames` takes the RAW string. `json()` here would throw on the first
  // `data:` line and report a parse failure for a perfectly good stream.
  let body
  try {
    body = await settled.text()
  } catch (cause) {
    return { error: `the turn's reply body could not be read: ${cause?.message ?? cause}` }
  }

  const parsed = a2aFrames(body)
  if (parsed.error) return { error: parsed.error }
  return a2aOutcome(parsed.frames, { sentContextId })
}

// Consume a turn's stream chunk by chunk — `{ state, contextId, reply }`, or `{ error }`.
//
// ⚠️ `onTaskId` IS AWAITED BEFORE THE NEXT CHUNK IS READ, and that ordering IS the feature rather
// than an implementation detail. The callback registers the push config, and the event it must be
// armed for fires a fixed ~10 s into the turn; letting the read run ahead would let the mid-turn
// status be emitted and dropped before the registration landed — the silent failure this whole
// path exists to prevent, and one that leaves a completed turn and a perfectly good outcome behind
// it. A callback that throws does not fail the collection: the turn's outcome is still the turn's.
async function collectTurnIncrementally({ body, sentContextId, onTaskId }) {
  const reader = createSseFrameReader()
  // ⚠️ `{ stream: true }`, so a multi-byte character split across a chunk boundary is held rather
  // than decoded as two replacement characters — which would corrupt a frame's JSON and read as a
  // malformed stream.
  const decoder = new TextDecoder()
  let announced = false
  const stream = body.getReader()
  try {
    for (;;) {
      const { done, value } = await stream.read()
      if (done) break
      reader.push(decoder.decode(value, { stream: true }))
      if (announced || reader.error !== null) continue
      const taskId = a2aTaskIdOf(reader.frames)
      if (taskId !== null) {
        announced = true
        // ⚠️ SWALLOWED, AND `announced` IS SET FIRST. Letting the callback throw would land it in
        // the catch below and be reported as "the turn's stream could not be read" — a channel
        // failure dressed as a worker failure, on a turn that ran fine. Setting the flag before
        // the call also means a throwing callback is not retried on every later chunk.
        try {
          await onTaskId(taskId)
        } catch {
          // The caller owns reporting a registration failure; the turn's outcome is not it.
        }
      }
    }
  } catch (cause) {
    return { error: `the turn's stream could not be read: ${cause?.message ?? cause}` }
  }
  reader.end()
  if (reader.error) return { error: reader.error }
  return a2aOutcome(reader.frames, { sentContextId })
}

// The ledger patch a collected turn produces — `{ result, error }`, both always present.
//
// ⚠️ BOTH ARE ALWAYS SET, AND THE `result` IS NOT DISCARDED ON FAILURE. Writing only `result` on
// success would leave a previously-recorded error standing against a turn that has since
// succeeded; writing only `error` on failure would throw away the state and whatever partial
// reply the worker did produce. A reader must be able to tell what was collected AND whether it
// worked, so neither half is dropped for the other.
//
// ⚠️ A FAILED STATE IS AN ERROR, AND THE OUTCOME IS STILL FILED. `TASK_STATE_FAILED` on a turn is
// the worker reporting its own failure; a record filing that as a result with no error would read
// as a turn that answered.
//
// `at` is passed in rather than read here, because every rule in this module is clock-free so it
// can be tested without freezing time.
export function a2aTurnPatch(collected, { at } = {}) {
  if (!collected || collected.error) {
    return {
      result: null,
      error: collected?.error ?? 'the turn produced neither an outcome nor an error.',
    }
  }
  const { state, contextId, reply } = collected
  return {
    result: {
      // Named, so a reader can tell an A2A turn's outcome from a cluster turn's without inferring
      // it from which fields happen to be present.
      kind: 'a2a-turn',
      state: state ?? null,
      contextId: contextId ?? null,
      reply: reply ?? '',
      collectedAt: at ?? null,
    },
    error: state === A2A_STATE_FAILED ? `the worker reported ${state} for this turn.` : null,
  }
}

// ─────────────────────────────────────────────────────────────────────────────────────────
// LIVENESS — what makes an A2A worker visible to every local liveness reader (SC7).
//
// ⚠️ WITHOUT THIS AN A2A WORKER IS DEAD FROM ITS FIRST SWEEP. The readers judge liveness from a
// stamp in the local heartbeat store or a live registry pid, and an A2A worker has neither: it
// runs in a container or pod the controller created, so nothing on this host ever stamps it. Every
// sweep therefore read a RUNNING worker as not live, and a reconnect's rehydrate pruned it. The
// cluster pod solves the same problem with `cluster-heartbeat.mjs`; this is its A2A equivalent,
// and it needs no ConfigMap — the controller's own `list` is the source of truth.
//
// A killed worker drops out of `list`, so its stamp is not refreshed, ages past the TTL, and reads
// dead — inside one sweep, which is the criterion. Nothing here decides liveness; the readers do.
//
// ⚠️ KNOWN LIMIT, STATED RATHER THAN HIDDEN: an unreachable controller makes every A2A stamp go
// stale too, which reads as dead rather than `UNKNOWN`. The cluster mirror carries a reachability
// marker for exactly that case, because there a STALE verdict authorises a resume onto a worker
// that may be alive behind a network fault. This path refuses `resume` outright
// (`a2aSpawnArgumentRefusal`), so that hazard cannot occur here, and a second marker would have to
// be taught to every reader of the store — four of them name the cluster marker literally.

// The worker states the controller reports for a worker that is not running. `list` may still
// carry a stopped container, and a listed-but-exited worker stamped live is the exact false
// positive SC7's falsifier names. Matched case-insensitively on a substring, because the
// controller's state vocabulary is its own and is not pinned by this module.
const A2A_NOT_RUNNING = /stop|exit|dead|fail|terminat|remov/i

// The `list` control call — `{ url, headers, body, contextId }`, or `{ error }`.
export function a2aListCallFor({ endpoint, token, messageId, contextId, mint } = {}) {
  const context = a2aContextIdFor({ previous: contextId, mint })
  if (context.error) return { error: context.error }
  const control = a2aControlRequestFor({ operation: 'list' })
  if (control.error) return { error: control.error }
  const call = a2aSendRequestFor({ endpoint, contextId: context.contextId, request: control.request, token, messageId })
  if (call.error) return { error: call.error }
  return { ...call, contextId: context.contextId }
}

// The `stop` control call — `{ url, headers, body, contextId }`, or `{ error }`.
//
// ⚠️ IT TAKES THE WORKER ID `create` RECORDED, AND IT IS NEVER RE-DERIVED FROM THE TASK. That is
// the whole of this task's SC4, and it is a property of the shape rather than of a caller's care:
// the id is a required argument, so there is no path here that could reconstruct one from a task
// name — the only other handle a caller holds. `a2aControlRequestFor` refuses a `stop` that names
// a task, so a caller passing the wrong handle is refused at the builder rather than sending a
// request the controller cannot honour.
export function a2aStopCallFor({ endpoint, workerId, token, messageId, contextId, mint } = {}) {
  const context = a2aContextIdFor({ previous: contextId, mint })
  if (context.error) return { error: context.error }
  const control = a2aControlRequestFor({ operation: 'stop', workerId })
  if (control.error) return { error: control.error }
  const call = a2aSendRequestFor({ endpoint, contextId: context.contextId, request: control.request, token, messageId })
  if (call.error) return { error: call.error }
  return { ...call, contextId: context.contextId }
}

// Ask the controller to stop this worker — `{ response, state }`, or `{ error }`.
//
// ⚠️ **THE CONTROLLER TOKEN AUTHENTICATES THIS, NEVER A WORKER TOKEN.** That is what makes this a
// manager-facing act rather than a worker-side one: the caller needs the `worker_id` (durable — the
// ledger carries it and `toAgentRecord` restores it on rehydrate) and the controller target
// (resolved per call from config). It does NOT need the worker's own endpoint or token, so a
// manager can stop an A2A worker it never spawned — the exact row `resolveMessageChannel` refuses
// for a follow-up turn.
//
// ⚠️ Mirrors `listWorkersOnController`, and for the same reasons: the body is read as TEXT because
// `a2aControlResponseFor` parses the raw body itself, and it is never logged — a control reply can
// carry worker detail, and this module's rule is that a body printed is a transcript write.
//
// ⚠️ BOUNDED, and for a sharper reason than `list`'s. A stop that hangs holds a manager's turn
// open; aborting it is not free — the request may already have reached the controller — but an
// unbounded wait is the worse failure, because the caller learns nothing either way. The abort
// bounds the WAIT, never the stop, so a timeout is reported as an unknown outcome rather than as
// "nothing happened": the same discipline `A2A_STATE_FAILED` carries, one layer up.
export const A2A_STOP_TIMEOUT_MS = 10_000
export async function stopWorkerOnController({
  endpoint,
  workerId,
  token,
  messageId,
  contextId,
  mint,
  fetchImpl = fetch,
  timeoutMs = A2A_STOP_TIMEOUT_MS,
  capture,
} = {}) {
  const call = a2aStopCallFor({ endpoint, workerId, token, messageId, contextId, mint })
  if (call.error) return { error: call.error }
  // The exact body this call sends, at the send site — what a test (and SC4) diffs to prove the
  // `worker_id` on the wire is the one the caller was given. ⚠️ NEVER THE HEADERS: the controller
  // token travels in `Authorization`, so a capture sink that took them would write the credential.
  if (typeof capture === 'function') {
    try {
      capture({ url: call.url, body: call.body })
    } catch {
      // A capture is evidence, not part of the stop — losing it must not lose the call.
    }
  }

  let res
  try {
    res = await fetchImpl(call.url, {
      method: 'POST',
      headers: call.headers,
      body: JSON.stringify(call.body),
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch (cause) {
    return { error: `the stop call to ${call.url} could not be made: ${cause?.message ?? cause}` }
  }
  if (!res.ok) return { error: `the controller answered HTTP ${res.status} to the stop call at ${call.url}.` }

  let body
  try {
    body = await res.text()
  } catch (cause) {
    return { error: `the stop reply body could not be read: ${cause?.message ?? cause}` }
  }
  // `sentOperation` is asserted, so a reply naming another operation is refused rather than read
  // as this call's answer — the same check `create` and `list` make, for the same reason.
  return a2aControlResponseFor(body, { sentOperation: 'stop' })
}

// Which ledger sessions the controller still runs — `[{ sessionId, workerId }]`.
//
// Pure. `records` are ledger records; only an A2A one with a `worker_id` and no `ended_at` can
// match, and it matches only a listed worker whose state is not a stopped one. ⚠️ Keyed on the
// WORKER ID, never the task: one task can be spawned again after its first worker died, and a
// task-keyed match would stamp the dead session live on the strength of its successor.
export function a2aLiveSessions({ workers, records } = {}) {
  if (!Array.isArray(workers) || !Array.isArray(records)) return []
  const running = new Set(
    workers
      .filter((w) => w && typeof w.worker_id === 'string' && w.worker_id !== '')
      .filter((w) => !(typeof w.state === 'string' && A2A_NOT_RUNNING.test(w.state)))
      .map((w) => w.worker_id),
  )
  const live = []
  for (const record of records) {
    if (!record || record.mode !== A2A_LEDGER_MODE) continue
    if (record.ended_at) continue
    if (typeof record.session_id !== 'string' || record.session_id === '') continue
    if (!running.has(record.worker_id)) continue
    live.push({ sessionId: record.session_id, workerId: record.worker_id })
  }
  return live
}

// Ask the controller which workers it runs — `{ workers }`, or `{ error }`.
//
// Mirrors `createWorkerOnController`, and for the same reasons reads the body as TEXT and never
// logs it: a control reply is parsed by `a2aControlResponseFor`, which takes the raw body.
//
// ⚠️ BOUNDED, unlike a turn send. The poll skips a tick while one is in flight, so a controller
// that accepts the connection and never answers would otherwise wedge the mirror for the
// process's whole life. Aborting a `list` is harmless — it starts nothing on the worker.
export const A2A_LIST_TIMEOUT_MS = 10_000
export async function listWorkersOnController({ endpoint, token, messageId, contextId, mint, fetchImpl = fetch, timeoutMs = A2A_LIST_TIMEOUT_MS } = {}) {
  const call = a2aListCallFor({ endpoint, token, messageId, contextId, mint })
  if (call.error) return { error: call.error }

  let res
  try {
    res = await fetchImpl(call.url, { method: 'POST', headers: call.headers, body: JSON.stringify(call.body), signal: AbortSignal.timeout(timeoutMs) })
  } catch (cause) {
    return { error: `the list call to ${call.url} could not be made: ${cause?.message ?? cause}` }
  }
  if (!res.ok) return { error: `the controller answered HTTP ${res.status} to the list call at ${call.url}.` }

  let body
  try {
    body = await res.text()
  } catch (cause) {
    return { error: `the list reply body could not be read: ${cause?.message ?? cause}` }
  }
  const parsed = a2aControlResponseFor(body, { sentOperation: 'list' })
  if (parsed.error) return { error: parsed.error }
  return a2aWorkerList(parsed.response)
}

// Stop the worker a failed `create` was refused over — `{ stoppedWorkerId }`, `{ none: true }`, or
// `{ error }`.
//
// ⚠️ THE ID IS NOT IN THE ERROR, WHICH IS WHY THIS EXISTS. The controller refuses a repeat create
// for a task that already has a worker, and its refusal names that worker — but a backend failure
// reaches this side as a bare `TASK_STATE_FAILED` with no detail attached, so neither the refusal
// nor the id it carries is legible here. `list` is the only client-side read that answers the
// question the error cannot: does this task already have a worker? When it does, that worker is
// what the create was refused over.
//
// ⚠️ `{ none: true }` IS NOT A FAILURE. A create can fail for reasons that have nothing to do with
// a duplicate — an unreachable controller, a rejected token, a refused argument — and in those
// cases no worker matches the task. Returning `none` rather than an error is what lets the caller
// report the ORIGINAL failure instead of a reaping failure it invented.
//
// `listWorkers` and `stopWorker` are injected, so this is tested without a controller.
export async function reapExistingWorkerForTask({
  endpoint,
  task,
  token,
  messageId,
  mint,
  fetchImpl = fetch,
  listWorkers = listWorkersOnController,
  stopWorker = stopWorkerOnController,
} = {}) {
  const listed = await listWorkers({ endpoint, token, messageId, mint, fetchImpl })
  if (listed.error) {
    // ⚠️ THIS DOES NOT CLAIM A WORKER EXISTS. A failed list establishes nothing about whether
    // this task has one, so the text says what was lost — the question went unanswered — rather
    // than asserting a worker that was never observed.
    return { error: `the workers could not be listed, so whether this task already has one could not be determined: ${listed.error}` }
  }

  const existing = (listed.workers ?? []).find((w) => w?.task === task)
  if (!existing) return { none: true }

  const stopped = await stopWorker({
    endpoint,
    workerId: existing.worker_id,
    token,
    messageId,
    mint,
    fetchImpl,
  })
  if (stopped.error) {
    return {
      error:
        `worker ${existing.worker_id} already serves task ${JSON.stringify(task)} but could not be ` +
        `stopped: ${stopped.error}`,
    }
  }
  return { stoppedWorkerId: existing.worker_id }
}

// One liveness poll: list the controller's workers and stamp every ledger session it still runs.
// Resolves to `{ ok, reason?, stamped }`; `ok: false` means the controller was not read, and the
// caller must not treat that as "no A2A worker is live" — see the KNOWN LIMIT above.
//
// Every effect is injected — the records, the list call and the stamp — so the poll is tested
// without a controller, a ledger directory or a heartbeat store.
export async function pollA2A({ records, list, stamp } = {}) {
  if (typeof list !== 'function' || typeof stamp !== 'function') {
    return { ok: false, reason: 'pollA2A needs a list and a stamp function', stamped: [] }
  }
  let listed
  try {
    listed = await list()
  } catch (cause) {
    return { ok: false, reason: `the controller list failed: ${cause?.message ?? cause}`, stamped: [] }
  }
  if (!listed || listed.error) {
    return { ok: false, reason: listed?.error ?? 'the controller list returned nothing', stamped: [] }
  }
  const live = a2aLiveSessions({ workers: listed.workers, records })
  for (const { sessionId } of live) stamp(sessionId)
  return { ok: true, stamped: live.map((l) => l.sessionId) }
}
