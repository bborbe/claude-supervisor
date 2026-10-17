// registry-rehydrate.mjs — the roster a fresh server process reads back from disk.
//
// The defect this closes: `agents` in `supervisor.mjs` is a module-scope Map populated only
// by `spawn_agent`. The MCP client reconnecting (`/mcp`) starts a NEW server process, so the
// Map comes up empty and `list_agents` answers `[]` while `agent_status` answers
// `unknown agent <id>` — even though the worker sessions themselves are still running. A
// manager loses its read view of its own fleet.
//
// The durable half already existed; nothing consulted it. `park-record.mjs` states the
// principle for a park ("the spawner exiting is exactly the case that matters, so the park
// must also land on disk") and `ledger.mjs` applies it to the worker identity. This module is
// the read side that was missing.
//
// ⚠️ **Identity is not liveness, and the ledger's `status` is not either.** 1110 of the 1372
// records measured 2026-10-06 still asserted `status: running`, because nothing closes an
// entry whose server went away (`ledger.mjs` says so in as many words). Adopting on the
// ledger's word would resurrect every dead worker the fleet ever opened, and `agent_status`
// would answer for ids that must read `unknown agent` — the discriminating pair this fix is
// required to keep. So the ledger supplies IDENTITY and the liveness channels supply
// LIVENESS, the same split `worker-sessions.mjs` already makes.
//
// ⚠️ **The join is shared with `workerSessions()`; the FILTER is not.** That function drops
// `resumed_from` records, which is right for the fleet-wide cap counter it feeds — an
// auto-resume answers to the resume gate's own crash-loop cap, not to that one — and wrong
// for a roster, where a resumed worker is still a worker the manager has to see and drive.
// Reusing it here would silently hide exactly those rows.
//
// ⚠️ **A rehydrated row is a BOOT-TIME reading, and its caller must not treat it as more.**
// Nothing here refreshes a row once adopted, and the ledger cannot supply the refresh. A
// worker that dies after this process starts therefore keeps its adoption-time status, which
// is why `supervisor.mjs` prunes rehydrated rows at read time and why it must never let one
// count as a live holder it should not be — see the call sites there: `pruneRehydratedAgents`
// (now run on the RESUME path too, not only the read paths, so a live rehydrated row still
// holds the guard while a dead one is already gone) and `stampUnobservedWorkers`.
//
// ⚠️ **And an adopted id shares a namespace with this process's own mints.** `supervisor.mjs`
// numbers workers from a per-process counter that restarts at 0 on every reconnect, so the
// first `spawn_agent` after a reconnect would otherwise mint an id a rehydrated row already
// holds and overwrite a live worker's row — `list_agents` dropping it and `agent_status`
// answering `unknown agent`, which is the exact symptom this module exists to close. The mint
// side advances past anything already held; see the `do … while (agents.has(id))` there.
//
// ⚠️ **What adoption does NOT reach — named, rather than left to be discovered.** A worker is
// adopted only if it is in the ledger AND in a liveness channel at the instant this process
// starts. Two classes fall outside that instant:
//
//   * **A live cluster worker.** It has no registry entry, and its only liveness channel is
//     the heartbeat stamp this server's own `pollCluster()` writes — which first fires ~30s
//     after boot, long after the adoption block has run. Adoption is once, at import, and
//     `pruneRehydratedAgents` can only remove, so it stays invisible for the process's life.
//   * **A headless worker whose stamp has aged out.** Stamps expire at `HEARTBEAT_TTL_MS`
//     (60s), so a `/mcp` run more than ~60s after the old server exited adopts nothing.
//
// ⚠️ Neither is a regression — before this module nothing was adopted at all, so both are the
// pre-existing state rather than a new one. They are named because a reader who takes the
// roster for complete would be wrong about exactly these two rows.
import { config } from './config.mjs'
import { liveSessionIds as liveSessionIdsFromLiveness, pidIsAlive } from './liveness.mjs'
import { ledgerBySessionId, readSessionLedger } from './session-ledger.mjs'

// The live session ids, as a Map — or `null` when a channel could not be read.
//
// `null` rather than an empty Map, mirroring `workerSessions`: a caller must be able to tell
// "nothing is live" from "the store is unreadable", and collapsing the two answers a
// permissions error with a confident empty fleet.
//
// ⚠️ **The registry half is PID-CHECKED here, and that is a deliberate divergence from
// `workerSessions()`.** That function reads presence alone, because over-counting a *cap*
// fails safe — it refuses a spawn rather than permitting one. Adopting a row into a
// *roster* is the opposite direction: the registry is keyed by pid and its file is deleted
// on exit, so a file left behind by a crashed session reads as live forever, and presence
// alone would resurrect precisely the dead worker this module exists to exclude. The
// heartbeat store needs no equivalent check — a stamp is aged out by `listLive`.
export function liveSessionIds(opts = {}) {
  // ⚠️ The union's ONE home is `liveness.mjs`, so this reader and the cap counter cannot come
  // to answer "is this session live?" differently — the hazard a second copy of the rule
  // creates. The pid-check is this caller's one difference from `workerSessions()`, and the
  // reason for it lives with the helper.
  return liveSessionIdsFromLiveness({ ...opts, isAlive: pidIsAlive })
}

// The agent-record shape `agentView` needs, built from a ledger record.
//
// ⚠️ `transcript` and `permissions` are EMPTY ARRAYS rather than absent fields: `agentView`
// dereferences `a.transcript.length` unconditionally, so a missing one takes the WHOLE roster
// down rather than one row. They are genuinely empty here — the in-memory transcript is
// pushed only by a headless worker's SDK loop, and a rehydrated record's loop lives in a
// process this one is not — which is why `agentView` falls back to the worker's transcript on
// disk for both of them.
//
// Everything else `agentView` reads is either carried by the ledger or derived from
// `sessionId` at read time (`current_tool_call`, `session_status`, `last_message`), so a
// rehydrated record answers the full record shape without inventing data.
//
// ⚠️ **`shipping` is hardcoded `false`, and it is unrecoverable rather than merely
// unmapped**: `buildRecord` never persisted the flag, so no ledger record carries it. A
// rehydrated row for a worker that was spawned under `SHIPPING_PERMISSION_MODE` therefore
// reports `shipping: false` while that worker really is running in shipping mode. Nothing
// here can fix that; it is named so a reader does not take the `false` for a measurement.
export function toAgentRecord(record, id = record.agent_id) {
  return {
    id,
    label: record.label ?? record.agent_id,
    cwd: record.cwd ?? null,
    status: rehydratedStatus(record),
    sessionId: record.session_id,
    resumedFrom: record.resumed_from ?? null,
    policyPath: record.policy ?? null,
    modeSource: record.mode_source ?? null,
    paneId: record.pane_id ?? null,
    launcher: record.launcher ?? null,
    // ⚠️ **Both names, because the file and the endpoint spell it differently.** The file
    // ledger writes `parent_session`; the endpoint serves `spawner_session_id` (one name for
    // one thing on the wire). The WRITE side already maps one onto the other, so reading only
    // `parent_session` made every adopted row's `parentSession` null — a silent field loss
    // introduced by moving the read source, and one-directional by construction.
    parentSession: record.parent_session ?? record.spawner_session_id ?? null,
    createdAt: record.spawned_at ?? null,
    transcript: [],
    permissions: [],
    carriedDecision: null,
    shipping: false,
    result: record.result ?? null,
    // The A2A worker id, so a re-adopted row is still the one the liveness mirror stamps. The
    // worker TOKEN is not on the ledger and so cannot be restored: a re-adopted A2A row can be
    // seen live, but cannot be sent another turn by this process.
    workerId: record.worker_id ?? null,
    error: null,
    // Observable rather than inferred, and load-bearing at two call sites in
    // `supervisor.mjs`: a rehydrated row must not be stamped on shutdown
    // (`stampUnobservedWorkers`), and it is pruned wherever liveness is consulted
    // (`pruneRehydratedAgents` — the read paths AND the resume path). ⚠️ It is deliberately
    // NOT excluded from `findLiveHolder`: a row that survives the prune holds the guard
    // exactly as an in-process row does. The acceptance evidence also rests on it — without
    // a marker a reader cannot tell a rehydrated row from one this process spawned itself.
    rehydrated: true,
  }
}

// The status a rehydrated agent carries.
//
// The ledger's `mode` is the durable half of the spawn's own vocabulary, so mapping it here
// keeps a rehydrated row reading the way a freshly spawned one does: `interactive` and
// `cluster` verbatim, and `headless` as `running`, which is the state its agent loop leaves
// it in while it works.
//
// ⚠️ **None of these three is terminal** (`resume-guard.mjs` owns `TERMINAL_STATUSES`), and
// that cuts both ways. It is why a rehydrated row must be PRUNED rather than left to answer
// for itself: a dead worker's row would otherwise assert it is still mid-turn and refuse the
// resume that is that worker's only recovery. It is equally why a row that SURVIVES the prune
// must hold the resume guard like any other — the status cannot tell the two apart, so
// liveness is the only thing that may.
export function rehydratedStatus(record) {
  if (record.mode === 'interactive') return 'interactive'
  if (record.mode === 'cluster') return 'cluster'
  // An A2A worker's status is its TARGET's name (`docker` / `k8s`), mirroring `cluster`, so a
  // reconnect re-adopts it as the same kind of row it was spawned as. A record written before
  // `target` was persisted carries none — `running` is then the honest fallback, not a guess.
  if (record.mode === 'a2a') return record.target ?? 'running'
  return 'running'
}

// The agent records for a set of ledger records, filtered to the live ones.
//
// This is the whole of the adoption RULE, and it is deliberately separated from the read that
// feeds it: the records now arrive from the endpoint, but the rule — live-only, keyed on
// `session_id`, `agent_id` disambiguated rather than dropped — is unchanged, and it must stay
// one implementation so a request-time read and a boot-time one cannot answer differently.
//
// A record with no `agent_id` is skipped rather than adopted under a synthesised id — the id
// is what `agent_status` and `send_agent_message` are keyed on, so an invented one would be
// addressable but meaningless. Every record measured 2026-10-06 carried one.
//
// `log` is injected rather than imported, matching `agent-loop.mjs`: `supervisor.mjs` owns
// the real logger and importing it back would be a cycle. ⚠️ It defaults to `console.warn`
// rather than a no-op — the duplicate-`agent_id` branch exists so a manager can tell why a
// worker answers under `agent_1~<sid8>`, and a silent default would lose exactly that for any
// caller that forgot the argument.
export function agentsFromRecords(records, { live, log = console.warn } = {}) {
  // Sorted, so the adoption order is a property of the data rather than of the read that
  // supplied it. It matters only for the collision branch below, but an order that varies
  // run to run would make that branch's "first wins" undecidable after the fact.
  const agents = []
  const claimed = new Map() // agent_id -> session_id
  for (const sessionId of [...records.keys()].sort()) {
    const record = records.get(sessionId)
    if (!record?.agent_id) continue
    if (!live.has(sessionId)) continue
    const prior = claimed.get(record.agent_id)
    if (prior !== undefined) {
      // ⚠️ `supervisor.mjs` mints ids from a per-process counter (`agent_${++seq}`), so a
      // reconnect restarts that counter and two LIVE records can share one id. Adopting
      // both would put one Map key on two workers, so the loser is disambiguated instead —
      // ⚠️ **not dropped.** Dropping it would leave a genuinely live worker invisible to
      // `list_agents`, `agent_status` and `send_agent_message` for this process's whole
      // life: the same availability outcome this module exists to remove, only
      // deterministic and logged. The suffix is short because it is for addressing, not
      // for reading — the session id is the identity, and the roster carries it in full.
      const disambiguated = `${record.agent_id}~${sessionId.slice(0, 8)}`
      log(`WARNING: two live ledger records share agent id ${record.agent_id} (${prior} and ${sessionId}) — adopting ${prior} under that id, and ${sessionId} as ${disambiguated}`)
      agents.push(toAgentRecord(record, disambiguated))
      continue
    }
    claimed.set(record.agent_id, sessionId)
    agents.push(toAgentRecord(record))
  }
  return agents
}

// Every live worker the ledger knows, as agent records — or `null` when a store could not be
// read.
//
// ⚠️ **This reads the ENDPOINT, and that is the fix rather than a refactor.** The roster used
// to be built once, at import, from the ledger gated on liveness at that instant, and the only
// later mutation could delete but never insert — so a session not in `live` when the server
// started stayed invisible to that process for its whole life, however live it became. A read
// here happens at REQUEST time and cannot freeze; see `session-ledger.mjs`.
//
// ⚠️ **`null` still means "could not read it", from either store.** The caller must be able to
// tell a store outage from a fleet with no workers, and collapsing the two is the direction
// that hides live workers.
export async function rehydratableAgents({
  endpoint,
  registryDir,
  heartbeatDir,
  now,
  fetchImpl,
  log = console.warn,
} = {}) {
  const rows = await readSessionLedger({ endpoint, fetchImpl })
  if (rows === null) return null
  const live = liveSessionIds({ registryDir, heartbeatDir, now })
  if (live === null) return null
  return agentsFromRecords(ledgerBySessionId(rows), { live, log })
}

// The roster AS OF NOW, given this process's own workers and a fresh endpoint read.
//
// ⚠️ **Extracted so it can be TESTED, and that is the reason rather than tidiness.** It lived
// inline in `supervisor.mjs`'s `currentRoster`, which cannot be imported (it starts an MCP
// server at module load), so the heart of this change was pinned only by regexes over the
// source text — and a regex cannot catch a behavioural regression. One did get through: the
// foreign row's id was derived from a set seeded by the LOCAL Map, so a spawn landing between
// a manager's `list_agents` and its `send_agent_message` flipped that row's id and handed the
// message to the wrong worker, silently. The id is now a function of the ROW ALONE.
//
// ⚠️ **This process's own row WINS on a `session_id` collision**, and the direction is
// deliberate rather than incidental: an in-process agent carries the live transcript, the park
// queue and the permission rules, while an endpoint row carries identity alone. Letting the
// fresh row win would replace a working worker's record with a hollow one.
export function unionRoster(inProcess, fresh) {
  const roster = new Map(inProcess)
  const own = new Set([...roster.values()].map((a) => a.sessionId))
  const takenIds = new Set(roster.keys())
  for (const agent of fresh) {
    if (own.has(agent.sessionId)) continue
    // ⚠️ A function of the ROW ALONE — see the note above. Suffixing EVERY foreign row is what
    // makes the id stable across calls, so it can never be shadowed by a later local mint.
    let id = `${agent.id}~${agent.sessionId.slice(0, 8)}`
    while (takenIds.has(id)) id = `${id}~`
    takenIds.add(id)
    roster.set(id, { ...agent, id })
  }
  return roster
}
