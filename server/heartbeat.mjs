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
// — an append-only event log is not a live-state source. This is a separate directory
// whose entries are transient by construction, and `sweep` exists so a dead server's stamps
// do not accumulate.
//
// The shape follows `scripts/manager-liveness.py`, which solves the sibling problem — a
// lapsed manager vs a deliberately stopped one — the same way: a file whose mtime marks the
// last event that actually happened, read against a period rather than a boolean flag. A
// flag decays silently in the dangerous direction; an age over-reports loudly.
//
// ⚠️ **This directory now holds MORE THAN ONE KIND of session, and the older readers predate
// that.** It was the *headless worker* store — `live-workers.py` says so in its own header —
// and the readers filter on `.json` plus mtime, never on `mode`. The self-stamp added for the
// session the server runs inside is `mode: 'local'`, so it appears among what those readers
// describe as live headless workers. **No instrument is wrong today**, because the commands
// that must count workers go through the ledger-filtered `worker-sessions.py --count` rather
// than the raw directory — but the distinction is carried by `mode` and nothing enforces it.
// A reader that needs one kind must filter on `mode`; a reader that cannot tell them apart
// should say which it is reporting.
//
// This is the same hazard the state directory avoids by being a sibling: everything in ONE
// directory being read as one kind of thing. Here the directory is genuinely shared, so the
// discriminator has to be explicit rather than structural.

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

// The cluster mirror's reachability marker lives in the store directory but is NOT a session.
// Its name has ONE home — here, because this module owns the store and lists it — and it must
// match `REACHABILITY_FILE` in `scripts/live-workers.py`, the other reader that consults it.
// `cluster-heartbeat.mjs` re-exports this rather than restating the literal.
export const REACHABILITY_FILE = '_cluster-reachability.json'

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
export function stampRecord(dir, { sessionId, pid, mode, source, task, vault, location, activity, at = new Date().toISOString() }, { fs = { mkdirSync, writeFileSync, renameSync } } = {}) {
  const path = stampPath(dir, sessionId)
  fs.mkdirSync(dir, { recursive: true })
  const tmp = `${path}.tmp`
  // `source` is carried only when the writer sets it. The readers key their cluster-specific
  // staleness rule on it — a stale cluster stamp whose store could not be read is UNKNOWN, not
  // STALE — so it has to be on the record rather than inferred from the directory. Omitted
  // rather than defaulted, so a headless worker's stamp keeps exactly the shape it had.
  const record = { sessionId, pid, mode, at }
  if (source !== undefined) record.source = source
  // The session-liveness fields, added for the attention store's read path. Same rule as
  // `source`: carried only when the writer sets them, so a stamp from a writer that knows
  // none of them is byte-for-byte what it was before this change.
  //
  // ⚠️ `task` and `vault` are a PAIR and the store rejects one without the other, because a
  // task name collides across vaults — half an anchor cannot be resolved later. They are
  // written together or not at all, which is why the guard tests both rather than each.
  if (task !== undefined && vault !== undefined) {
    record.task = task
    record.vault = vault
  }
  if (location !== undefined) record.location = location
  // ⚠️ `activity`, NOT `state` — the name `state` on a stamp is reserved by the liveness
  // verdict four readers synthesize. They gate on `stamp.get("state", "live") != "live"`
  // (`worker-sessions.py:166`, `fleet-board.py:1096`, `adopt-orphans.py:139`,
  // `session-liveness.py:663/693/803`), where a MISSING key must read as `live` rather than
  // vanish.
  //
  // ⚠️ **No reader consumes a raw stamp's `state` today, and this comment first claimed one
  // did** — asserting that a `busy` written here "reads as NOT-live" and frees a duplicate
  // auto-resume. That was wrong, and it was verified wrong by running the reader rather than
  // reading it: `live-workers.py`'s `readLive` is the single chokepoint that parses a raw
  // stamp, and it builds a FRESH object carrying its own `state` verdict without ever reading
  // the file's — so all four gates see the synthesized value, and a stamp carrying
  // `state: "busy"` is counted live by `live-workers.py --list` exactly like one carrying
  // `activity: "busy"`. The key is therefore WRITE-ONLY, and the rename is PREVENTIVE: it
  // removes a name that collides with the verdict vocabulary before a raw-stamp reader exists
  // to be bitten by it. See `SESSION_STATES`.
  if (activity !== undefined) record.activity = activity
  fs.writeFileSync(tmp, `${JSON.stringify(record, null, 2)}\n`)
  fs.renameSync(tmp, path)
  return path
}

// What a session is doing, as last recorded by a state hook. `null` when no hook has fired.
//
// ⚠️ The AGE is what decides liveness; this only says what the live session is up to. A
// missing or unreadable file is `null` — "no hook has fired" — never a guessed `busy`, because
// a wrong state reads as knowledge while an absent one reads as absence.
//
// ⚠️ The id is shape-checked before it becomes a path component, and the check is stated
// STANDALONE rather than as a mirror of anything: the write side of this contract
// (`safe_session_id` in `scripts/heartbeat-state.py`) ships with the event half of this work,
// in its own PR, so a pointer to it would land on nothing in this tree. The reader must
// refuse an unchecked id either way — a reader that joined one would be the other half of the
// same traversal.
export function readState(sessionId, { dir = config.heartbeatStateDir } = {}) {
  if (!SESSION_ID_PATTERN.test(sessionId ?? '')) return null
  try {
    const record = JSON.parse(readFileSync(join(dir, `${sessionId}.json`), 'utf8'))
    return SESSION_STATES.includes(record?.state) ? record.state : null
  } catch {
    return null
  }
}

// What a session id may look like before it becomes a FILENAME. See `readState` for why the
// check is here as well as in the writer.
export const SESSION_ID_PATTERN = /^[A-Za-z0-9._-]+$/

// The states a session can report, and the vocabulary the heartbeat contract names.
//
// ⚠️ **These ride on the stamp as `activity`, never as `state`.** `state` on a stamp is the
// name the liveness VERDICT uses: `live-workers.py` writes `state: "live" | "unknown"`, and
// four readers gate on `stamp.get("state", "live") != "live"`, where a MISSING key must read
// as `live` rather than vanish.
//
// ⚠️ **Those four gates never see a raw stamp, and an earlier revision of this comment said
// they did** — claiming every live self-stamp was "in fact dropped as not-live". It was not.
// `live-workers.py`'s `readLive` parses a raw stamp and builds a fresh object carrying its own
// verdict; nothing anywhere reads the file's `state`. The rename is PREVENTIVE, not a repair:
// the key is write-only today, which makes this the cheapest moment to stop it sharing a name
// with the verdict. See `stampRecord`.
//
// ⚠️ Membership is checked on READ, not just on write. The state hook validates its own
// argument, but the file is in a shared state directory — a hand-edited or older record must
// not put a value on the wire that no reader's vocabulary contains. An unrecognised state
// returns `null`, and the CALLER turns that into the documented `idle` — this function does
// not, so a reader can still tell "no hook has fired" from "a hook said idle".
export const SESSION_STATES = ['busy', 'idle', 'waiting-on-operator']

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
  // `ageMs` is carried so a caller that needs the age does not have to stat the path a second
  // time. A second stat is not merely wasteful: it is unguarded, so a stamp unlinked between
  // the two calls throws out of the caller — and `listLive` is on the spawn path now.
  if (age < ttlMs) return { live: true, ageMs: age, reason: `heartbeat stamped ${Math.round(age / 1000)}s ago` }
  return { live: false, ageMs: age, reason: `heartbeat stale by ${Math.round((age - ttlMs) / 1000)}s` }
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
    // The cluster mirror's reachability marker lives in this directory and is not a session.
    // Without this the reader reports a phantom `sessionId: "_cluster-reachability"`, which
    // no ledger key can match today but is a divergence from `live-workers.py` — the other
    // reader of this same store — which has always skipped it.
    if (name === REACHABILITY_FILE) continue
    const sessionId = name.replace(/\.json$/, '')
    if (!sessionId) continue
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
    // The age comes from the verdict `readLive` already took, never a second bare `statSync`:
    // that stat was unguarded, so a stamp unlinked between the two calls would throw ENOENT
    // out of `listLive` — and `listLive` is on the spawn path now, via `workerSessions()` and
    // `concurrentLimitError()`, where an uncaught throw is a failed spawn rather than a count.
    live.push({ sessionId, ageMs: verdict.ageMs, ...meta })
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

// The session this server runs INSIDE — stamped on the same interval as the workers.
//
// ⚠️ Lives HERE rather than in `supervisor.mjs` because this module owns the stamp, and the
// lifecycle is only testable where the store is: `supervisor.mjs` has no export surface (a
// test importing it would start an MCP server), so a self-stamp owned there could never be
// covered at all. Moving it here is what makes the null no-op, the re-stamp and the clear on
// stop assertable rather than described.
//
// ⚠️ The per-worker stamps cannot cover this session: they are keyed on workers this server
// SPAWNED, and the interactive tab the server itself runs in is not one of those. Without
// this, every interactive session reads dead to the attention store while a headless worker
// it spawned reads live — the exact asymmetry the session-liveness work exists to remove.
//
// ⚠️ The anchor (`task`/`vault`) is deliberately OMITTED rather than resolved. Filling it
// would mean reading the vault on the liveness path, and a store that exists so a dead
// session is detectable must not depend on anything that can fail independently of the
// session it is reporting on. The store accepts an unanchored row by design; the row still
// answers alive, where, and what state.
//
// Returns `null` for a missing session id, so a server started outside a session stamps
// nothing rather than stamping under an invented key — a row under a made-up id would be a
// permanent phantom reading Live until its TTL, with no session able to clear it.
export function startSelfStamp(sessionId, {
  dir = heartbeatDir,
  intervalMs = HEARTBEAT_INTERVAL_MS,
  readActivity = readState,
  onError = () => {},
} = {}) {
  if (!sessionId) return null
  const write = () => {
    try {
      stampRecord(dir, {
        sessionId,
        pid: process.pid,
        mode: 'local',
        source: 'mcp-timer',
        location: 'local',
        // Read per tick rather than captured once: the hooks write this on events, and a
        // value captured at start would report the state the session had when the server came
        // up for the rest of its life. `idle` is the documented answer for "no hook has fired
        // yet" — see `readState`.
        activity: readActivity(sessionId) ?? 'idle',
      })
    } catch (error) {
      // A session whose stamp cannot be written still runs. Losing the heartbeat degrades a
      // manager's verdict to "could not tell" — which is the honest answer — whereas letting
      // this throw would take down the server that owns the session.
      onError(error)
    }
  }
  write()
  const timer = setInterval(write, intervalMs)
  // Unref'd so a pending refresh never holds the process open after its work is done.
  timer.unref?.()
  return {
    // ⚠️ Clears the STAMP as well as the timer, which is what `clearStamp`'s own doc asks for
    // ("so the next reader does not have to wait out a TTL"). Without it a restarted server
    // leaves its OWN row reading live for a full TTL — and the self-stamp is the one row an
    // operator is most likely to be looking at.
    stop() {
      clearInterval(timer)
      clearStamp(sessionId, { dir })
    },
  }
}
