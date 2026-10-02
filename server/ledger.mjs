// A durable record per spawned worker.
//
// Two stores already exist and neither answers the question this one does. Claude
// Code's live registry at `~/.claude/sessions/<pid>.json` is keyed by pid and is
// **deleted when the session exits** — measured 2026-09-14: 13 entries against 13 live
// processes and zero stale — so it forgets a session exactly when a record would first
// be useful. The transcript survives but holds only the conversation: it cannot say who
// started the session, in what mode, from which manager, or how it ended. Nothing
// records the spawn edge.
//
// So this is keyed by the session uuid — the resume handle, which makes the record
// useful to the resume path and not only to a process table — and it is written at
// spawn, so a long-running worker is recorded while it is still running.
//
// It is deliberately NOT a second liveness source: an entry here must never be read as
// proof a session is alive. `liveness.mjs` owns that question, and it answers from the
// session registry plus the server's own in-process record of the workers it spawned.

import { spawnSync } from 'node:child_process'
import { mkdirSync, readdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { readRegistry } from './liveness.mjs'

// ⚠️ Three, not two. `cluster` is a third kind of worker — a session inside the
// `claude-interactive` service in the cluster — and it is neither a tab nor an in-process
// query. It was omitted when the cluster target landed, and the omission was SILENT in the
// worst way: `buildRecord` throws on an unknown mode, `writeLedger` catches that throw and
// logs a warning, and the spawn still returned a working-looking `{agent_id, session_id}` —
// so every cluster worker ran with no ledger record at all, which is the durable half the
// fleet reads to answer "who started this". A rejected value that only warns is the failure
// this file's own callers refuse elsewhere.
export const MODES = ['interactive', 'headless', 'cluster']

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
    spawned_at: spawnedAt ?? new Date().toISOString(),
    ended_at: null,
    status: 'running',
    result: null,
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

export function recordPath(dir, sessionId) {
  return join(dir, `${sessionId}.json`)
}

// Written via a temporary file and renamed, so a reader never sees a half-written
// record and reads it as a session with no fields.
export function writeRecord(dir, record, { fs = { mkdirSync, writeFileSync, renameSync } } = {}) {
  const path = recordPath(dir, record.session_id)
  fs.mkdirSync(dir, { recursive: true })
  const tmp = `${path}.tmp`
  fs.writeFileSync(tmp, `${JSON.stringify(record, null, 2)}\n`)
  fs.renameSync(tmp, path)
  return path
}

export function readRecord(dir, sessionId) {
  try {
    return JSON.parse(readFileSync(recordPath(dir, sessionId), 'utf8'))
  } catch {
    return null
  }
}

// Merge rather than replace: the completion fields arrive long after the spawn fields,
// and a caller that only knows the outcome must not have to restate the rest.
export function updateRecord(dir, sessionId, patch, opts = {}) {
  const current = readRecord(dir, sessionId)
  if (!current) return null
  const next = { ...current, ...patch }
  writeRecord(dir, next, opts)
  return next
}

export function listRecords(dir) {
  try {
    return readdirSync(dir)
      .filter((name) => name.endsWith('.json'))
      .map((name) => readRecord(dir, name.replace(/\.json$/, '')))
      .filter(Boolean)
  } catch {
    return []
  }
}
