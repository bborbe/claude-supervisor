// Is a session id still running?
//
// Two probes, because neither is sufficient alone, and the gap between them is not
// theoretical: `pgrep` was the only probe for one release, and it cannot see an
// ordinary running session at all.
//
// 1. The session registry. Claude Code writes `~/.claude/sessions/<pid>.json` for an
//    interactive session, carrying the session id it belongs to. This is the only
//    probe that finds a session started FRESH — its id appears nowhere in any
//    process's argv, so `pgrep` has nothing to match. Verified 2026-09-14 against a
//    live session whose transcript was being written seconds earlier: `pgrep -fl`
//    found nothing, the registry listed it as `busy`.
// 2. `pgrep -fl <id>`. A session launched as `claude --resume <id>` carries its own
//    id in argv, so this covers the processes a registry might not list — headless
//    SDK workers, which hold no socket. It is the same probe `/open` uses to find a
//    live turn.
//
// The registry is keyed by pid, so a file left behind by a crashed session would read
// as live forever; every hit is therefore confirmed against the pid, never the JSON
// alone. And "no probe could be read" is reported as its own answer rather than
// folded into "not live" — an unguarded resume corrupts a conversation, so a caller
// must be able to tell "confirmed closed" from "could not tell".

import { spawnSync } from 'node:child_process'
import { readdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const SESSIONS_DIR =
  process.env.SUPERVISOR_SESSIONS_DIR ||
  join(process.env.CLAUDE_CONFIG_DIR || join(homedir(), '.claude'), 'sessions')

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

export function defaultPgrep(sessionId) {
  try {
    const res = spawnSync('pgrep', ['-fl', sessionId], { encoding: 'utf8' })
    if (res.error || res.status === 127) return null
    return res.status === 0 && Boolean((res.stdout || '').trim())
  } catch {
    return null
  }
}

// `live: true` is the only answer that forbids a resume; `live: null` is "could not
// tell" and belongs to the caller to resolve, never to be read as false.
export function checkLiveness(sessionId, opts = {}) {
  const { pgrep = defaultPgrep } = opts
  const registered = registeredAsLive(sessionId, opts)
  const matched = pgrep(sessionId)

  const probes = []
  if (registered !== null) probes.push('registry')
  if (matched !== null) probes.push('pgrep')

  if (probes.length === 0) {
    return { live: null, probes, reason: 'neither the session registry nor pgrep could be read' }
  }
  if (registered === true) {
    return { live: true, probes, reason: 'the session registry lists it against a running pid' }
  }
  if (matched === true) {
    return { live: true, probes, reason: 'a running process matches its id' }
  }
  return { live: false, probes, reason: 'no registry entry against a running pid, and no matching process' }
}
