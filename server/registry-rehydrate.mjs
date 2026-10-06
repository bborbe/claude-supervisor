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
// count as an in-process live holder — see the two call sites there.
import { config } from './config.mjs'
import { heartbeatDir as defaultHeartbeatDir, listLive } from './heartbeat.mjs'
import { pidIsAlive, readRegistry } from './liveness.mjs'
import { readLedger } from './worker-sessions.mjs'

// The live session ids, as a Set — or `null` when a channel could not be read.
//
// `null` rather than an empty Set, mirroring `workerSessions`: a caller must be able to tell
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
export function liveSessionIds({ registryDir, heartbeatDir = defaultHeartbeatDir, now } = {}) {
  const registry = readRegistry(registryDir)
  if (registry === null) return null
  const stamps = listLive({ dir: heartbeatDir, now })
  if (stamps === null) return null

  // The union, not the intersection: the registry sees every session holding a socket, and
  // the heartbeat store covers the ones with no pid of their own. Either channel alone
  // loses a population — see worker-sessions.mjs's header.
  const live = new Set()
  for (const entry of registry) {
    if (pidIsAlive(entry.pid)) live.add(entry.sessionId)
  }
  for (const stamp of stamps) live.add(stamp.sessionId)
  return live
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
export function toAgentRecord(record) {
  return {
    id: record.agent_id,
    label: record.label ?? record.agent_id,
    cwd: record.cwd ?? null,
    status: rehydratedStatus(record),
    sessionId: record.session_id,
    resumedFrom: record.resumed_from ?? null,
    policyPath: record.policy ?? null,
    modeSource: record.mode_source ?? null,
    paneId: record.pane_id ?? null,
    launcher: record.launcher ?? null,
    parentSession: record.parent_session ?? null,
    createdAt: record.spawned_at ?? null,
    transcript: [],
    permissions: [],
    carriedDecision: null,
    shipping: false,
    result: record.result ?? null,
    error: null,
    // Observable rather than inferred, and load-bearing at two call sites in
    // `supervisor.mjs`: a rehydrated row must not count as an in-process live holder
    // (`findLiveHolder`) and must not be stamped on shutdown (`stampUnobservedWorkers`).
    // The acceptance evidence also rests on it — without a marker a reader cannot tell a
    // rehydrated row from one this process spawned itself.
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
// ⚠️ **None of these three is terminal** (`resume-guard.mjs` owns `TERMINAL_STATUSES`), which
// is exactly why a rehydrated row must be excluded from the resume guard by its `rehydrated`
// marker rather than by its status: a dead worker's row would otherwise assert it is still
// mid-turn and refuse the resume that is that worker's only recovery.
export function rehydratedStatus(record) {
  if (record.mode === 'interactive') return 'interactive'
  if (record.mode === 'cluster') return 'cluster'
  return 'running'
}

// Every live worker the ledger knows, as agent records — or `null` when a store could not be
// read.
//
// A record with no `agent_id` is skipped rather than adopted under a synthesised id — the id
// is what `agent_status` and `send_agent_message` are keyed on, so an invented one would be
// addressable but meaningless. Every record measured 2026-10-06 carried one.
//
// `log` is injected rather than imported, matching `agent-loop.mjs`: `supervisor.mjs` owns
// the real logger and importing it back would be a cycle.
export function rehydratableAgents({
  registryDir,
  ledgerDir = config.ledgerDir,
  heartbeatDir = defaultHeartbeatDir,
  now,
  log = () => {},
} = {}) {
  const ledger = readLedger(ledgerDir)
  if (ledger === null) return null
  const live = liveSessionIds({ registryDir, heartbeatDir, now })
  if (live === null) return null

  // Sorted, so the adoption order is a property of the data rather than of `readdirSync`.
  // It matters only for the collision branch below, but an order that varies run to run
  // would make that branch's "first wins" undecidable after the fact.
  const agents = []
  const claimed = new Map() // agent_id -> session_id
  for (const sessionId of Object.keys(ledger).sort()) {
    const record = ledger[sessionId]
    if (!record?.agent_id) continue
    if (!live.has(sessionId)) continue
    const prior = claimed.get(record.agent_id)
    if (prior !== undefined) {
      // ⚠️ `supervisor.mjs` mints ids from a per-process counter (`agent_${++seq}`), so a
      // reconnect restarts that counter and two LIVE records can share one id. Adopting
      // both would put one Map key on two workers — silently hiding one from the roster and
      // pointing `send_agent_message` at whichever won. First-wins keeps it deterministic
      // and the warning keeps it observable.
      log(`WARNING: two live ledger records share agent id ${record.agent_id} (${prior} and ${sessionId}) — adopting ${prior}, skipping ${sessionId}`)
      continue
    }
    claimed.set(record.agent_id, sessionId)
    agents.push(toAgentRecord(record))
  }
  return agents
}
