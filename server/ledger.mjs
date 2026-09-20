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
// live registry plus pgrep.

import { mkdirSync, readdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { readRegistry } from './liveness.mjs'

export const MODES = ['interactive', 'headless']

// The spawn edge. The supervisor is spawned by the MCP client, so its own parent pid is
// the manager session that called spawn_agent — the live registry is exactly the right
// tool for that one lookup, and the answer is then stamped into a record that outlives
// it. Returns null rather than guessing when the parent is not in the registry.
export function parentSessionId({ ppid = process.ppid, dir, registry = readRegistry } = {}) {
  const entries = registry(dir)
  if (!entries) return null
  return entries.find((entry) => entry.pid === ppid)?.sessionId ?? null
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
