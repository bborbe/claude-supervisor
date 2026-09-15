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
