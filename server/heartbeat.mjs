// Is a headless worker still being worked? — answered from a file ANY process can read.
//
// A headless worker is an in-process SDK `query()` owned by the supervisor server that
// spawned it. It has no process of its own, holds no socket, and writes no registry entry,
// so every probe a THIRD PARTY can run — the session registry, `pgrep`, pane existence —
// reads the same decisive "closed" for a worker mid-turn and for one that finished an hour
// ago. The owning server can tell those apart from its own in-process `agents` Map, but that
// Map is per-process: the one caller that must not guess, a manager that did not spawn the
// worker, is exactly the one that cannot see it. See `liveness.mjs`'s header, and the two
// characterisation tests in `liveness.test.mjs` that pin this blind spot as the defect.
//
// This closes it by making the owner's knowledge durable and world-readable. While a worker
// is live, its owning server re-stamps a small file keyed by session id; any process then
// decides liveness from the stamp's AGE alone, with no channel back to the owner.
//
// ⚠️ Age, not existence — and that is the whole design. A marker written at spawn and
// unlinked on a graceful exit answers only the graceful case: `kill -9` leaves it behind
// forever, which is precisely the defect being replaced. The spawn ledger's `status:
// running` is the ABSENCE of a close, so a worker killed mid-turn reads running for good.
// A heartbeat that stops being refreshed goes stale on its own, so the ungraceful case needs
// no second mechanism — which is also why the tests must exercise the kill path rather than
// only the graceful one.
//
// ⚠️ Deliberately NOT the spawn ledger. `stateDir/sessions` is an append-only record of what
// happened, and stamping liveness into it would make an append-only log a live-state source
// — [[An Append-Only Event Log Is Not a Live-State Source]]. This is a separate directory
// whose entries are transient by construction, and `sweep` exists so a dead server's stamps
// do not accumulate.
//
// The shape follows `scripts/manager-liveness.py`, which solves the sibling problem — a
// lapsed manager vs a deliberately stopped one — the same way: a file whose mtime marks the
// last event that actually happened, read against a period rather than a boolean flag. A
// flag decays silently in the dangerous direction; an age over-reports loudly.

import { mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { config } from './config.mjs'

// The two constants are a PAIR, and the relationship between them is the contract.
//
// A stamp is refreshed every INTERVAL and read as live until TTL, so TTL must exceed
// INTERVAL by enough that a single missed tick is not a death verdict. With TTL === INTERVAL
// a live worker can read dead for up to 2x, because the newest stamp is already one full
// interval old at the moment the next one is due. 30s against 60s leaves exactly one whole
// missed tick of slack, and keeps the worst case — killed immediately after a stamp — inside
// the 60s bound the task's SC2 states.
export const HEARTBEAT_INTERVAL_MS = 30_000
export const HEARTBEAT_TTL_MS = 60_000

export const heartbeatDir = config.heartbeatDir

export function stampPath(dir, sessionId) {
  return join(dir, `${sessionId}.json`)
}

// The stamp's mtime is the age authority, not the `at` field inside it. A reader that trusts
// the field can be fooled by a file whose clock is wrong or whose writer died between
// composing the JSON and renaming it; mtime is set by the write itself, so it cannot
// disagree with the fact that a write happened. `at` is carried for the reader's benefit —
// it names the worker — never as the thing the verdict is computed from.
//
// Written via a temporary file and renamed, matching `writeRecord`: a reader must never see
// a half-written stamp and read it as a worker with no id.
export function stampRecord(dir, { sessionId, pid, mode, at = new Date().toISOString() }, { fs = { mkdirSync, writeFileSync, renameSync } } = {}) {
  const path = stampPath(dir, sessionId)
  fs.mkdirSync(dir, { recursive: true })
  const tmp = `${path}.tmp`
  fs.writeFileSync(tmp, `${JSON.stringify({ sessionId, pid, mode, at }, null, 2)}\n`)
  fs.renameSync(tmp, path)
  return path
}

// null = no information (the directory could not be read), false = read, and no fresh stamp.
//
// The distinction is load-bearing and matches `registeredAsLive` above it: a caller that
// cannot read the directory must be able to tell "confirmed stale" from "could not tell",
// because the guard acts on `false` by ALLOWING a resume. Folding an unreadable directory
// into `false` would turn an I/O error into permission to start a second writer.
export function readLive(sessionId, { dir = heartbeatDir, ttlMs = HEARTBEAT_TTL_MS, now = Date.now() } = {}) {
  let age
  try {
    age = now - statSync(stampPath(dir, sessionId)).mtimeMs
  } catch (error) {
    // ENOENT is a real negative — no stamp, so not live. Anything else means the directory
    // itself is unreadable, which is "no information" rather than "not live".
    if (error.code === 'ENOENT') return { live: false, reason: 'no heartbeat stamp' }
    return { live: null, reason: `the heartbeat directory could not be read: ${error.code ?? error.message}` }
  }
  if (age < ttlMs) return { live: true, reason: `heartbeat stamped ${Math.round(age / 1000)}s ago` }
  return { live: false, reason: `heartbeat stale by ${Math.round((age - ttlMs) / 1000)}s` }
}

// Every fresh stamp, with its metadata — the reader a non-spawning process runs.
//
// An unreadable directory returns null, not `[]`, for the same reason as above: "the store
// could not be read" and "no worker is live" are different answers, and a manager acting on
// the second will spawn onto live work.
export function listLive({ dir = heartbeatDir, ttlMs = HEARTBEAT_TTL_MS, now = Date.now() } = {}) {
  let names
  try {
    names = readdirSync(dir)
  } catch (error) {
    if (error.code === 'ENOENT') return []
    return null
  }
  const live = []
  for (const name of names) {
    if (!name.endsWith('.json')) continue
    const sessionId = name.replace(/\.json$/, '')
    const verdict = readLive(sessionId, { dir, ttlMs, now })
    if (verdict.live !== true) continue
    let meta = {}
    try {
      meta = JSON.parse(readFileSync(stampPath(dir, sessionId), 'utf8'))
    } catch {
      // The stamp is fresh but unreadable as JSON. The AGE is what the verdict rests on, so
      // the worker is still live; only the descriptive fields are lost. Reporting it as dead
      // because its metadata is malformed would invert the failure into the dangerous one.
      meta = {}
    }
    live.push({ sessionId, ageMs: now - statSync(stampPath(dir, sessionId)).mtimeMs, ...meta })
  }
  return live
}

// Called when a worker ends, so the common case leaves nothing behind and the next reader
// does not have to wait out a TTL to learn what the server already knows.
export function clearStamp(sessionId, { dir = heartbeatDir, fs = { rmSync } } = {}) {
  try {
    fs.rmSync(stampPath(dir, sessionId), { force: true })
    return true
  } catch {
    return false
  }
}

// Stamps older than the TTL, removed. A server killed with `kill -9` never runs its clear,
// so without this the directory grows by one file per worker forever. Deliberately NOT
// called from the read path: a reader must not need write permission on a store it only
// reads, and the sweep gate is the process that owns the directory's hygiene.
export function sweepStale({ dir = heartbeatDir, ttlMs = HEARTBEAT_TTL_MS, now = Date.now(), fs = { readdirSync, rmSync } } = {}) {
  let names
  try {
    names = fs.readdirSync(dir)
  } catch {
    return []
  }
  const removed = []
  for (const name of names) {
    if (!name.endsWith('.json')) continue
    const sessionId = name.replace(/\.json$/, '')
    if (readLive(sessionId, { dir, ttlMs, now }).live === false) {
      try {
        fs.rmSync(stampPath(dir, sessionId), { force: true })
        removed.push(sessionId)
      } catch {
        // A file another process removed between the read and the unlink is already gone.
      }
    }
  }
  return removed
}
