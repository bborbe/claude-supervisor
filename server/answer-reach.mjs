// answer-reach.mjs — did this session's answering commands actually REACH their routes?
//
// `scripts/answer-path-check.py` used to decide that a spawner could serve a headless
// worker's parks by reading only whether the spawner's manager loop was armed. An armed loop
// is not an answer channel: measured 2026-11-09, spawner `0110c4a3` read `{"ok": true}` at
// exit 0 while its 27 workers' parks were released 56 times by the attention store and
// **never once by it**. The worker is opened against a spawner that will strand every park,
// and the failure is silent and 15 minutes delayed.
//
// So the probe reads a second fact: a **reach** — evidence that this session's answering
// command actually arrived at the route and was refused there harmlessly. Two legs, because
// a manager can answer on two routes and they are gated differently:
//
//   · `mcp`     — the `answer_permission` MCP tool. Its handler already consults the wrapper
//                 and then replies with `foreignParkMessage` for an id nobody holds; that
//                 reply IS the reach, and it settles nothing.
//   · `wrapper` — a Bash `curl` POST to `/api/1.0/sessions/<sid>/answer`. The permission
//                 classifier gates the COMMAND, so a denied command never reaches the
//                 wrapper and produces no status at all — which is exactly the origin case.
//                 `scripts/answer-reach.py` writes this leg only on a 409/404.
//
// ⚠️ **One file per (session, leg), never one file per session.** The two legs are written by
// two different processes — the server and a shell command — and a single `<session>.json`
// would be a lost-update race: whichever wrote second would silently erase the other's leg,
// and the probe would then refuse a manager that had in fact reached both routes. The cost is
// a suffix on the filename; the alternative is a false refusal.
//
// ⚠️ **A reach is not a delivery.** It says the command shape reached the route and was
// refused there, not that a real answer was allowed — a permission rule can treat a sentinel
// `request_id` differently from a live one. The real-delivery half of the criterion reads
// `permissions.jsonl` and the session ledger instead; see the task file.
//
// ⚠️ **This is not a second counter of the loop's liveness.** `manager-liveness.py` answers
// "is this subject's manager loop alive?" from the marker it writes at every re-arm. This
// answers a different question — "did an answer command reach a route?" — and the probe
// requires BOTH, additively: a session whose loop reads STOPPED, STALE or UNARMED is refused
// even when both legs are fresh. Writing a reach is also not a heartbeat: nothing refreshes
// it on a timer, so it ages out on its own.

import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

/** The two answer routes a manager can take. Order is the probe's report order. */
export const LEGS = ['mcp', 'wrapper']

/**
 * The file a (session, leg) reach lives in. Exported so the probe's tests and any reader
 * resolve the same name rather than re-deriving the suffix.
 */
export function reachPath(dir, sessionId, leg) {
  return join(dir, `${sessionId}.${leg}.json`)
}

/**
 * Write one reach record. Returns `{ written, reason }` and never throws: every caller is on
 * a path that must answer a request, and a state-write failure must not turn a refusal
 * message into a crash.
 *
 * ⚠️ A null `sessionId` writes nothing, and that is correct rather than a bug to work around.
 * `parentSessionId()` returns null when no ancestor of this server is a registered session
 * (`server/ledger.mjs`), and the spawn gate already refuses a null session id because it
 * cannot name the session. A reach keyed on a null id could never be read back, so fabricating
 * one would only manufacture evidence for a check that has already refused.
 */
export async function recordReach({
  dir,
  sessionId,
  leg,
  outcome,
  now = () => new Date(),
  write = writeFile,
  mkdirp = mkdir,
}) {
  if (typeof sessionId !== 'string' || !sessionId) {
    return { written: false, reason: 'no session id — a reach keyed on nothing could never be read back' }
  }
  if (!LEGS.includes(leg)) {
    return { written: false, reason: `unknown leg ${JSON.stringify(leg)} — expected one of ${LEGS.join(', ')}` }
  }
  const record = { session_id: sessionId, leg, ts: now().toISOString(), outcome: outcome ?? null }
  try {
    await mkdirp(dir, { recursive: true })
    await write(reachPath(dir, sessionId, leg), `${JSON.stringify(record)}\n`, 'utf8')
  } catch (err) {
    return { written: false, reason: `could not write ${leg} reach: ${err.message}` }
  }
  return { written: true, record }
}

/**
 * Read this session's reaches. Returns `{ mcp, wrapper }` where each value is the record or
 * `null`, plus the reason a leg is unreadable when it is present but broken.
 *
 * A malformed file is reported as `broken`, never as absent: "no record" and "a record I
 * could not parse" are different facts, and the probe's `detail` has to say which one it saw.
 */
export async function readReach({ dir, sessionId, read = readFile }) {
  const out = {}
  for (const leg of LEGS) {
    if (typeof sessionId !== 'string' || !sessionId) {
      out[leg] = { state: 'absent', reason: 'no session id' }
      continue
    }
    try {
      const raw = await read(reachPath(dir, sessionId, leg), 'utf8')
      const parsed = JSON.parse(raw)
      if (!parsed || typeof parsed !== 'object' || typeof parsed.ts !== 'string') {
        out[leg] = { state: 'broken', reason: 'record has no string ts' }
        continue
      }
      out[leg] = { state: 'present', record: parsed }
    } catch (err) {
      if (err && err.code === 'ENOENT') out[leg] = { state: 'absent', reason: 'no record' }
      else out[leg] = { state: 'broken', reason: err.message }
    }
  }
  return out
}
