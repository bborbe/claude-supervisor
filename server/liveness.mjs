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
// A same-server headless worker is invisible to this module by construction, and the
// supervisor supplies that half itself: see the in-process `agents` Map check in
// `spawnAgent`, which is the only channel that can see a worker this server spawned.

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { config } from './config.mjs'

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
// The caller supplies the other half of the answer, and must: a headless worker this
// server spawned is invisible here, so `spawnAgent` checks its own in-process `agents`
// Map before trusting a `false`. See the header for why that half cannot live here.
export function checkLiveness(sessionId, opts = {}) {
  const registered = registeredAsLive(sessionId, opts)

  if (registered === null) {
    return { live: null, probes: [], reason: 'the session registry could not be read' }
  }
  if (registered === true) {
    return { live: true, probes: ['registry'], reason: 'the session registry lists it against a running pid' }
  }
  return { live: false, probes: ['registry'], reason: 'no registry entry against a running pid' }
}
