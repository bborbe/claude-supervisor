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
import { config } from './config.mjs'
import { heartbeatDir as defaultHeartbeatDir, listLive } from './heartbeat.mjs'
import { readRegistry } from './liveness.mjs'
import { readLedger } from './worker-sessions.mjs'

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
export function toAgentRecord(record, { status } = {}) {
  return {
    id: record.agent_id,
    label: record.label ?? record.agent_id,
    cwd: record.cwd ?? null,
    status: status ?? rehydratedStatus(record),
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
    // Observable rather than inferred. The acceptance evidence is that a row was resolved
    // FROM THE LEDGER, and without a marker a reader cannot tell a rehydrated row from one
    // this process spawned itself.
    rehydrated: true,
  }
}

// The status a rehydrated agent carries.
//
// The ledger's `mode` is the durable half of the spawn's own vocabulary, so mapping it here
// keeps a rehydrated row reading the way a freshly spawned one does: `interactive` and
// `cluster` verbatim, and `headless` as `running`, which is the state its agent loop leaves
// it in while it works.
export function rehydratedStatus(record) {
  if (record.mode === 'interactive') return 'interactive'
  if (record.mode === 'cluster') return 'cluster'
  return 'running'
}

// Every live worker the ledger knows, as agent records — or `null` when a store could not be
// read.
//
// `null` rather than `[]` on an unreadable channel, mirroring `workerSessions`: a caller must
// be able to tell "no worker has ever been spawned" from "the ledger is unreadable", and
// collapsing the two answers a permissions error with a confident empty roster.
//
// A record with no `agent_id` is skipped rather than adopted under a synthesised id — the id
// is what `agent_status` and `send_agent_message` are keyed on, so an invented one would be
// addressable but meaningless. Every record measured 2026-10-06 carried one.
export function rehydratableAgents({
  registryDir,
  ledgerDir = config.ledgerDir,
  heartbeatDir = defaultHeartbeatDir,
  now,
} = {}) {
  const ledger = readLedger(ledgerDir)
  if (ledger === null) return null

  const registry = readRegistry(registryDir)
  if (registry === null) return null
  const stamps = listLive({ dir: heartbeatDir, now })
  if (stamps === null) return null

  // The union, not the intersection: the registry sees every session holding a socket, and
  // the heartbeat store is what covers the ones with no pid of their own. Either channel
  // alone loses a population — see worker-sessions.mjs's header.
  const live = new Set()
  for (const entry of registry) live.add(entry.sessionId)
  for (const stamp of stamps) live.add(stamp.sessionId)

  const agents = []
  for (const [sessionId, record] of Object.entries(ledger)) {
    if (!record?.agent_id) continue
    if (!live.has(sessionId)) continue
    agents.push(toAgentRecord(record))
  }
  return agents
}
