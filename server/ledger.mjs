// The durable record per spawned worker: its SHAPE, and the two derivations that fill it.
//
// Two stores already exist and neither answers the question this one does. Claude
// Code's live registry at `~/.claude/sessions/<pid>.json` is keyed by pid and is
// **deleted when the session exits** — measured 2026-09-14: 13 entries against 13 live
// processes and zero stale — so it forgets a session exactly when a record would first
// be useful. The transcript survives but holds only the conversation: it cannot say who
// started the session, in what mode, from which manager, or how it ended. Nothing
// records the spawn edge.
//
// So the record is keyed by the session uuid — the resume handle, which makes it
// useful to the resume path and not only to a process table — and it is filed at spawn,
// so a long-running worker is recorded while it is still running.
//
// ⚠️ **This module no longer reads or writes anything.** It held a per-worker file store until
// 2026-10-14; the record now lives in attention-controller's session-ledger store, written by
// `supervisor.mjs`'s `writeLedger` and read by `session-ledger.mjs`. What stays here is what is
// pure and testable: the row shape, the spawn edge, and the unobserved stamp. The file helpers
// were DELETED rather than left unused — a dead writer is how a later reader gets re-wired to a
// store nobody maintains.
//
// ⚠️ **The retired path and variable are named in `docs/fleet-surface.md`, not here, and that is
// deliberate.** SC2 of the task that retired this store asserts each accessor with a pinned
// per-file probe that matches those two literals; a comment naming them would trip the probe on a
// file whose code is already clean, which is a false failure that costs a reader a re-derivation.
// The operator-facing note belongs where an operator looks.
//
// It is deliberately NOT a second liveness source: a row here must never be read as
// proof a session is alive. `liveness.mjs` owns that question, and it answers from the
// session registry plus the server's own in-process record of the workers it spawned.

import { spawnSync } from 'node:child_process'
import { readRegistry } from './liveness.mjs'

// ⚠️ Three, not two. `cluster` is a third kind of worker — a session inside the
// `claude-interactive` service in the cluster — and it is neither a tab nor an in-process
// query. It was omitted when the cluster target landed, and the omission was SILENT in the
// worst way: `buildRecord` throws on an unknown mode, `writeLedger` catches that throw and
// logs a warning, and the spawn still returned a working-looking `{agent_id, session_id}` —
// so every cluster worker ran with no ledger record at all, which is the durable half the
// fleet reads to answer "who started this". A rejected value that only warns is the failure
// this file's own callers refuse elsewhere.
// ⚠️ `a2a` is a THIRD worker kind, added 2026-10-13 with the docker / k8s spawn targets, and it
// is neither of the other two. A cluster worker is the shared long-lived `claude-interactive`
// pod; an A2A worker is a per-task container or pod the controller created and this server
// reaches over A2A. Folding it into `headless` would file it under a mode whose whole meaning is
// "a local process that parks its gates on this server", which is exactly what it is not.
//
// Consumers read this field tolerantly — `fleet-sessions.py`'s `spawn_mode` passes it through and
// `session-liveness.py` does not read it — and `unobservedPatch` answers the generic
// non-interactive reason for every mode but `interactive`, so a new value inherits a correct-if-
// coarse reason rather than a wrong one. Checked before adding, not assumed.
export const MODES = ['interactive', 'headless', 'cluster', 'a2a']

// The spawn edge: the session that called spawn_agent, resolved from the live registry
// (keyed by pid) and stamped into a record that outlives it.
//
// The supervisor is NOT a direct child of the manager session, which is what a bare
// `process.ppid` lookup assumes and why that lookup returned null for every spawn ever
// recorded. `.mcp.json` launches it through a `bun run` wrapper (`server/package.json`
// `start`), so `process.ppid` is the wrapper — a pid no registry entry ever names — and
// the manager is one or more levels above it. Measured 2026-09-22 across 42 live
// servers: 0/42 had their `ppid` in the registry and 42/42 had their grandparent in it,
// uniformly, so the indirection is the launch path rather than a race.
//
// The NEAREST registered ancestor wins. The first session up the chain is the one that
// started this process tree; anything above it merely launched that session, so it is
// not the spawn edge. Returns null rather than guessing when no ancestor is registered —
// a manager that has exited, or a chain that never passed through a session.
//
// `CLAUDE_CODE_SESSION_ID` is deliberately not read here, though the MCP server inherits
// it. Measured the same day: it agreed with the registry in 41 of 42 live servers, and in
// the 42nd it named a session present in no registry entry — so it would have written a
// wrong-but-plausible id exactly where the registry lookup is authoritative.
export function parentSessionId({
  ppid = process.ppid,
  dir,
  registry = readRegistry,
  parentOf = systemParentOf,
  maxDepth = 10,
} = {}) {
  const entries = registry(dir)
  if (!entries) return null
  const byPid = new Map(entries.map((entry) => [entry.pid, entry.sessionId]))
  let pid = ppid
  for (let depth = 0; depth < maxDepth; depth++) {
    const sessionId = byPid.get(pid)
    if (sessionId !== undefined) return sessionId
    const next = parentOf(pid)
    // `1`/`0` end the chain — neither is ever a Claude session — and a pid that is its
    // own parent would spin, so both stop the walk rather than extend it.
    if (!next || next === pid || next <= 1) break
    pid = next
  }
  return null
}

// The real process tree, read the way the rest of this repo shells out (`spawnSync`, as
// `tab.mjs` and `supervisor.mjs` do). Best-effort: a pid that has exited produces no
// output, which ends the walk rather than raising into the spawn path.
function systemParentOf(pid) {
  const result = spawnSync('ps', ['-o', 'ppid=', '-p', String(pid)], { encoding: 'utf8' })
  const parent = Number.parseInt(result.stdout?.trim() ?? '', 10)
  return Number.isInteger(parent) ? parent : null
}

// Pure. `ended_at`/`result` stay absent until the worker finishes, so "still running"
// is the absence of a field rather than a status that could go stale.
export function buildRecord({
  sessionId,
  agentId,
  label,
  mode,
  modeSource = null,
  cwd,
  launcher = null,
  paneId = null,
  resumedFrom = null,
  policy = null,
  parentSession = null,
  role = null,
  result = null,
  workerId = null,
  target = null,
  spawnedAt,
}) {
  if (!sessionId) throw new Error('buildRecord needs a sessionId — it is the key')
  if (!MODES.includes(mode)) throw new Error(`buildRecord: unknown mode "${mode}"`)
  return {
    session_id: sessionId,
    agent_id: agentId ?? null,
    label: label ?? null,
    mode,
    // Which source decided the mode — argument, env, config or default. The mode alone
    // cannot say whether a headless worker was asked for or merely inherited, and that
    // is the question asked when a fleet turns out to be running the wrong way.
    mode_source: modeSource,
    cwd: cwd ?? null,
    launcher,
    pane_id: paneId,
    resumed_from: resumedFrom,
    // Which policy the worker ran under, when spawn_agent named one. Null means the
    // server policy — the same absence-means-default the other spawn fields use.
    policy,
    parent_session: parentSession,
    // The role this spawn declared, as resolved by `spawn_agent` from the role map.
    //
    // The spawn edge alone cannot say whether a spawned session is a worker or a
    // manager, and `gate-owner-filter.py`'s `is_manager()` used to read a record as
    // proof of "worker" for exactly that reason. `spawn_agent(role="manager")` is a
    // documented call that writes a record, so a manager opened that way was
    // indistinguishable from the workers it spawns — every peer resolved its workers
    // `worker-spawner` and re-escalated gates it had already handled. Measured
    // 2026-10-06: the CDB in Weldall Manager (`1d379d23`, spawned by the Fleet
    // Manager) had its workers' gates escalated by UI Manager and Fleet Manager at once.
    //
    // Null means no role reached this call — a record written before the field
    // existed. `spawn_agent` defaults the role to `agent` before this point, so a
    // null here is a pre-field record rather than a spawn that declared nothing.
    role: role ?? null,
    spawned_at: spawnedAt ?? new Date().toISOString(),
    ended_at: null,
    status: 'running',
    // Null at spawn for every local mode — a tab or headless worker has no result yet, and
    // its terminal patch supplies one later. A CLUSTER worker is the exception and the reason
    // this is a parameter rather than a constant: its turn completes inside the spawn call, so
    // its result is final before the record is written. Hard-coding null here is what left the
    // durable half asserting nothing about a turn that had already answered.
    result,
    // The A2A worker this session runs in, and which target created it. Null for every local
    // and cluster mode. ⚠️ CARRIED BECAUSE NOTHING ELSE CAN MAP A CONTROLLER WORKER BACK TO ITS
    // SESSION: liveness lists the controller's workers by id, and a reconnected server has no
    // in-memory record to consult — without these the A2A mirror stamps nothing and every A2A
    // worker reads dead after a restart. The worker's TOKEN is deliberately absent: it
    // authenticates turns, and this record is a file anyone on the machine can read.
    worker_id: workerId,
    target,
  }
}

// The state a worker gets when its supervisor goes away without observing its outcome.
//
// `running` is not evidence of liveness: nothing closes an entry whose server died, was
// restarted, or never saw the exit, so the record keeps asserting `running` indefinitely
// — worse than absence, because it reads as an affirmative claim. This is the honest
// replacement. The record stops claiming the worker is alive and says only that this
// server stopped watching it, at a stamped time.
//
// Deliberately not `done`/`error`: those assert an outcome nobody observed. Deliberately
// not `orphaned` either, which would assert *alive but unparented* — itself an unknown.
// The name mirrors `liveness.mjs`'s `{ live: null }`, which callers must already keep
// distinct from `false`; the reason field mirrors its `reason`.
export const UNOBSERVED_STATUS = 'unknown'

// Why the two worker kinds differ, kept in the record rather than in a caller's head: a
// headless worker is this process's own `query()` and dies with it mid-turn, while an
// interactive one is a separate wezterm process that outlives the server. One status
// plus a per-kind reason is the same shape checkLiveness returns.
export function unobservedPatch({ mode, at = new Date().toISOString() }) {
  if (!MODES.includes(mode)) throw new Error(`unobservedPatch: unknown mode "${mode}"`)
  return {
    status: UNOBSERVED_STATUS,
    supervisor_exited_at: at,
    unknown_reason:
      mode === 'interactive'
        ? 'interactive tab worker outlives its supervisor; this server stopped watching it here'
        : 'headless worker terminated with its supervisor mid-turn; its outcome was never observed',
  }
}

// The field a deliberate stop is recorded under, and the patch that writes it.
//
// ⚠️ **Its own field, deliberately, and neither of the two candidates beside it would do.**
// `status` is not available: a ledger row is permanent by contract, and `session-ledger.mjs`
// records 1480 legacy rows still saying `running` for workers long dead, so a reader filtering on
// it "would resurrect the whole fleet". `ended_at` is worse than unavailable — it is already
// load-bearing elsewhere: `a2a-spawn.mjs:823` skips any record carrying it and
// `supervisor.mjs:2858` treats `!record.ended_at` as still-live, so setting it here would change
// the spawn-dedupe and mirror paths as a side effect of recording a stop. A new field changes
// exactly one thing.
//
// ⚠️ **Written by `stop_agent` because the reply is not a record.** The controller reports
// `stopped` only in the reply to the stop that removed it — `list` never returns it — so a
// supervisor that echoes the reply and files nothing leaves the durable row silent, and the sweep
// then reads a deliberately stopped worker exactly as it reads a dead one. That is the defect
// this field exists to close: without it an ended worker reads as an orphan candidate and the
// drive leg stop+recreates it, resurrecting work someone ended on purpose.
export const STOPPED_AT_FIELD = 'stopped_at'

export function stoppedPatch({ at = new Date().toISOString() } = {}) {
  return { [STOPPED_AT_FIELD]: at }
}

// Merge a patch into the record the writer already holds.
//
// ⚠️ **Merge, never replace, and that is the whole reason this is a function rather than a spread
// at the call site.** A worker's completion fields arrive minutes after its spawn fields and from
// a different call site — the terminal patch knows the outcome and nothing else — so a replace
// would erase the spawn edge, which nothing else records. The same property is what keeps a PARK
// alive across a later `{result}` patch, and that one is not cosmetic: a dropped park is a
// resumed worker carrying no decision.
//
// ⚠️ **An explicit `null` is a VALUE here, not a deletion.** `clearParkPatch` sends
// `{ parked_permission: null }` precisely because a finished worker must stop advertising a park
// nobody holds, and a spread is what makes that expressible.
//
// `null` in, `null` out: a writer with no record yet has nothing to merge INTO, and inventing a
// partial row from the patch alone would file a record missing the spawn edge.
export function mergeRecord(current, patch) {
  if (!current) return null
  return { ...current, ...patch }
}
