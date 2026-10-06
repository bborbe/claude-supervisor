// Is a session id still running?
//
// ONE probe: the session registry. Claude Code writes `~/.claude/sessions/<pid>.json`
// for a session, carrying the session id it belongs to, and DELETES the entry when the
// session exits. That deletion is the property that makes absence meaningful here — a
// pid-keyed file that is gone is a session that is gone, and no other channel on this
// machine has it.
//
// A `pgrep -f <id>` probe used to sit beside it, on the theory that a session launched
// as `claude --resume <id>` carries its id in argv. It is gone, and the reason is
// specific rather than "argv is unreliable": the question this module answers is
// whether a HEADLESS worker is live, and a headless worker has no process of its own —
// it is an in-process SDK `query()` owned by the supervisor server. There is no argv
// to match, so every hit that probe could produce was a bystander: a shell, a watcher,
// a grep, another session's command line. A finished worker whose id was merely
// MENTIONED anywhere on the machine read as live and became unresumable.
//
// Do not reintroduce an argv probe for the resumed-interactive case. A resumed
// interactive session does carry its id in argv and a `pgrep` would find it — a true
// positive for a different question. Precision was never the problem; argv cannot
// answer the question this module asks, and a probe that is right for some other
// question is the most persuasive kind of wrong one.
//
// The registry is keyed by pid, so a file left behind by a crashed session would read
// as live forever; every hit is therefore confirmed against the pid, never the JSON
// alone. And "no probe could be read" is reported as its own answer rather than
// folded into "not live" — an unguarded resume corrupts a conversation, so a caller
// must be able to tell "confirmed closed" from "could not tell".
//
// A headless worker is still invisible to the REGISTRY by construction — it is an in-process
// SDK `query()` with no pid of its own to key a registry entry on. This module covers it with
// a second probe instead: the heartbeat store (`heartbeat.mjs`), refreshed by the owning
// server while the worker is live and readable by ANY process, which is the half a
// non-spawning manager needs. The supervisor still supplies the same-server half from its
// in-process `agents` Map — that answers without touching the filesystem, but only for the
// server's own workers.

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { config } from './config.mjs'
import { HEARTBEAT_TTL_MS, heartbeatDir, listLive, readLive } from './heartbeat.mjs'

// Read from the config module rather than the environment: a library module has no
// business consulting ambient process state, and a default resolved here would make
// every caller depend on it. Tests pass `dir` explicitly — see config.test.mjs, which
// fails the build if this file reads `process.env` again.
export const SESSIONS_DIR = config.sessionsDir

// A pid we are not allowed to signal still exists — EPERM means "there, but not
// yours", which is a live process every bit as much as ESRCH is a dead one.
export function pidIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error.code === 'EPERM'
  }
}

// Returns null for "no information" — an unreadable directory — which callers must
// keep distinct from `[]`, "the registry exists and holds nothing". A foreign or
// half-written file inside is skipped rather than raised: it is not ours to fail on.
export function readRegistry(dir = SESSIONS_DIR) {
  let names
  try {
    names = readdirSync(dir)
  } catch {
    return null
  }
  const entries = []
  for (const name of names) {
    if (!name.endsWith('.json')) continue
    try {
      const entry = JSON.parse(readFileSync(join(dir, name), 'utf8'))
      if (entry && typeof entry.sessionId === 'string' && Number.isInteger(entry.pid)) entries.push(entry)
    } catch {
      continue
    }
  }
  return entries
}

// The registry ∪ heartbeat union — the ONE home for "which sessions are live?".
//
// Two readers ask it: `worker-sessions.mjs` counts the fleet against the concurrent-worker
// cap, and `registry-rehydrate.mjs` decides which ledger records to adopt into a roster.
// They differ on ONE input and must not differ on the union itself — a second copy of the
// rule is how the two would come to answer "is this session live?" differently, and the cap
// counter would pick up a new liveness channel that the roster silently did not.
//
// `isAlive` is that one input. The default is presence, which is what the cap counter reads
// and wants: over-counting a *cap* fails safe, because it refuses a spawn rather than
// permitting one. A *roster* must pid-check instead — a registry file left behind by a
// crashed session would otherwise be adopted as a live worker, the opposite direction — so
// it passes `pidIsAlive`.
//
// A Map rather than a Set, because the counter needs the per-session status the two channels
// disagree about; a caller that only needs membership uses `.has`.
//
// `null` from either channel is "could not read", returned rather than folded into an empty
// map: callers refuse on `null` and open on an empty one, so collapsing the two turns a
// permissions error into permission to spawn onto live work.
export function liveSessionIds({ registryDir, heartbeatDir: beatsDir, now, isAlive } = {}) {
  // ⚠️ `isAlive` is REQUIRED, deliberately, and it is the one thing the two callers disagree
  // about: `worker-sessions.mjs` wants presence (over-counting a *cap* fails safe — it refuses
  // a spawn), while a *roster* wants pid-checked liveness (presence would resurrect a crashed
  // session's left-behind file). A default would silently pick one of those for a caller that
  // forgot, and this module's own header calls presence "the most persuasive kind of wrong".
  // It also keeps this signature honest beside `registeredAsLive` below, whose `isAlive`
  // defaults to `pidIsAlive` — two functions, one parameter name, opposite defaults, is a trap
  // for a reader scanning top-down.
  if (typeof isAlive !== 'function') {
    throw new Error('liveSessionIds needs an explicit isAlive — presence and pid-checked liveness are different questions')
  }
  const registry = readRegistry(registryDir)
  if (registry === null) return null
  // `??` rather than a default parameter: a default fires only on `undefined`, so the
  // `heartbeatDir: null` idiom — which `checkLiveness` still uses — would reach
  // `listLive({ dir: null })`, where readdirSync throws a non-ENOENT error and the union
  // returns null. Fail-safe, but the contract this consolidation is supposed to keep.
  const stamps = listLive({ dir: beatsDir ?? heartbeatDir, now })
  if (stamps === null) return null

  const live = new Map()
  for (const entry of registry) {
    if (!isAlive(entry.pid)) continue
    live.set(entry.sessionId, entry.status)
  }
  for (const stamp of stamps) {
    // The registry wins where both speak: a session holding a socket is the case the readers
    // already understood, and its `status` is the richer descriptor.
    if (!live.has(stamp.sessionId)) live.set(stamp.sessionId, stamp.mode ?? null)
  }
  return live
}

// null = could not tell, false = registered but its pid is gone (a stale file).
export function registeredAsLive(sessionId, { dir = SESSIONS_DIR, isAlive = pidIsAlive } = {}) {
  const entries = readRegistry(dir)
  if (entries === null) return null
  return entries.some((entry) => entry.sessionId === sessionId && isAlive(entry.pid))
}

// The other direction: given the name we set on a tab, which session is it?
//
// A tab worker is a separate process the supervisor does not create, so it never
// learns that worker's session id from the SDK the way it does for a headless one.
// The registry carries `name`, and `spawnInteractiveAgent` sets the tab title, so the
// name is the join. Verified 2026-09-14: a worker spawned as `⚙ colour-probe` appears
// in the registry with exactly that `name` and `nameSource: "user"`.
//
// null means "not there yet" as well as "unreadable" — the caller polls, so the two
// are not worth separating here; the caller's timeout is the answer either way.
//
// ⚠️ A NAME IS NOT UNIQUE, and `find` returns the first match. A session that already
// held this name before the spawn therefore satisfies the poll just as well as the one
// the caller just started, and it is the OLD session's id that gets recorded. `exclude`
// is how a caller that snapshotted the pre-existing holders refuses them — see
// `sessionIdsNamed` and `uniqueTabName`, which together make the join unambiguous by
// construction rather than by luck.
export function findRegisteredByName(name, { dir = SESSIONS_DIR, registry = readRegistry, exclude = null } = {}) {
  const entries = registry(dir)
  if (!entries) return null
  return entries.find((entry) => entry.name === name && !exclude?.has(entry.sessionId))?.sessionId ?? null
}

// Every session id currently registered under `name` — the companion to the join above.
// A caller about to create a name needs the pre-existing set, both to derive a name
// nothing holds and to refuse a poll match that is an old holder rather than its own
// session; `findRegisteredByName` cannot tell those two apart on its own.
//
// null is "no information" (unreadable directory) and must stay distinct from `[]`,
// "read, and nothing holds this name" — a caller renames on `[]` and must not on null.
export function sessionIdsNamed(name, { dir = SESSIONS_DIR, registry = readRegistry } = {}) {
  const entries = registry(dir)
  if (!entries) return null
  return entries.filter((entry) => entry.name === name).map((entry) => entry.sessionId)
}

// A tab name that nothing currently holds, derived BEFORE the process starts.
//
// The name is the only join back from a tab to its session id, so a name two sessions
// can answer to makes that join ambiguous — and the ambiguity is not theoretical: it is
// how a new worker's ledger record ends up carrying another live session's id, and how a
// poll that should resolve resolves to a stranger instead. Suffixing until the name is
// free removes the ambiguity whatever produced the earlier holder, which is why this is a
// guard on the NAME rather than a fix for one cause of a collision.
//
// An unreadable registry returns the base name unchanged: "no information" is not
// "nothing holds it", and renaming on that answer would rename every spawn whenever the
// directory is briefly unreadable.
export function uniqueTabName(baseName, { dir = SESSIONS_DIR, registry = readRegistry, limit = 100 } = {}) {
  const held = sessionIdsNamed(baseName, { dir, registry })
  if (held === null || held.length === 0) return baseName
  for (let n = 2; n < limit; n++) {
    const candidate = `${baseName} (${n})`
    const ids = sessionIdsNamed(candidate, { dir, registry })
    if (ids !== null && ids.length === 0) return candidate
  }
  return baseName
}

// `live: true` is the only answer that forbids a resume; `live: null` is "could not
// tell" and belongs to the caller to resolve, never to be read as false.
//
// Two probes, and they answer different halves. The registry sees every session that holds a
// socket — which is every interactive one — and the heartbeat sees the headless workers the
// registry structurally cannot. A non-spawning manager needs the second, and before it
// existed both halves of its answer were unavailable: no registry entry (a headless worker
// has no pid), and no access to the spawning server's `agents` Map.
export function checkLiveness(sessionId, opts = {}) {
  const registered = registeredAsLive(sessionId, opts)
  const beat = readLive(sessionId, {
    dir: opts.heartbeatDir ?? heartbeatDir,
    ttlMs: opts.heartbeatTtlMs ?? HEARTBEAT_TTL_MS,
    now: opts.now,
  })

  // A fresh heartbeat is decisive ON ITS OWN, and is reported even when the registry could
  // not be read: a positive from one channel is an answer, and "could not tell" is not.
  if (beat.live === true) {
    return {
      live: true,
      probes: registered === null ? ['heartbeat'] : ['registry', 'heartbeat'],
      reason: `a live headless worker — ${beat.reason}`,
    }
  }
  if (registered === true) {
    return { live: true, probes: ['registry'], reason: 'the session registry lists it against a running pid' }
  }

  // No probe said live. If EITHER could not be read, the honest answer is "could not tell",
  // because the caller acts on `false` by ALLOWING a resume — folding an unreadable channel
  // into a negative turns an I/O error into permission to put a second writer on one
  // conversation.
  const blind = []
  if (registered === null) blind.push('the session registry')
  if (beat.live === null) blind.push('the heartbeat store')
  if (blind.length) {
    return { live: null, probes: [], reason: `${blind.join(' and ')} could not be read` }
  }
  return {
    live: false,
    probes: ['registry', 'heartbeat'],
    reason: 'no registry entry against a running pid, and no fresh heartbeat',
  }
}
