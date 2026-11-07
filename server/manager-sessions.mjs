// Live **manager sessions** — the unit the fleet-wide manager cap counts.
//
// ⚠️ **NOT the spawn ledger, and that is the whole reason this module exists.** Managers are
// normally started by hand, in a plain wezterm tab, which writes no ledger record — so a count
// built on the ledger (the shape `worker-sessions.mjs` uses) would read the common case as
// absent and the cap would never bind. `scripts/gate-owner-filter.py`'s `is_manager()` reads a
// session with no ledger record AS a manager for the same reason.
//
// **The producer is `scripts/fleet-board.py --json`**, which already classifies every
// registry session as manager / worker / unmanaged (rule 1: a name resolving to a
// `23 Topics` / `24 Goals` page, OR an orange tab colour). Reusing it keeps ONE definition of
// "manager" for the fleet board and for this cap; a second classifier here would drift from
// the one the operator reads. Chosen 2026-10-10 over a Managers-window tab count (a second,
// wezterm-dependent classifier) and an `/supervisor:open` ledger record (blind to the
// hand-started managers this exists for).
//
// The Fleet Manager — the board's root, `ROOT_NAME` — is EXCLUDED: the operator's design puts
// it outside the cap, and counting it would spend one of the soft slots on the session that
// supervises the rest.
//
// ⚠️ Read per call, never cached: a spawn decided against a count taken minutes earlier is a
// decision against a fleet that no longer exists.
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { defaultRun } from './cluster-heartbeat.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
export const BOARD = join(HERE, '..', 'scripts', 'fleet-board.py')

// The root's registry name. Mirrors `ROOT_NAME` in `scripts/fleet-board.py`, which owns it.
export const ROOT_NAME = 'Fleet Manager'

// ⚠️ NOT `RUN_TIMEOUT_MS`'s 20 s, and the difference is the whole of one defect. The board walks
// the vault to classify every registry session as manager / worker / unmanaged, so its wall time
// grows with the fleet: measured 2026-10-14 at 14–73 s under load (7.1 s on a quiet fleet), against
// a 20 s bound inherited from the cluster reader. A count killed at 20 s is indistinguishable from
// a fleet that cannot be counted, and the refusal that follows points the operator at the LIMIT —
// the one thing that was not wrong. Observed: `spawn.maxManagers` raised to 10, refused as 5.
//
// ⚠️ A bound, not a fix. The board's own cost is tracked separately (its vault-wide walk: ~106 k
// file opens and ~224 k `lstat`s per run); this exists so a slow board yields a COUNT rather than
// a refusal, and so a genuine hang still ends. Raise it only against a measured wall time.
export const COUNT_TIMEOUT_MS = 120_000

// Why the count could not be taken, in the operator's terms — the cause, never the limit.
//
// ⚠️ `managerSessions` used to return a bare `null` and discard `stderr`, so every failure —
// a timeout, a missing interpreter, a Python traceback — reached the operator as the same
// "the count could not be taken", beside a limit that was usually the stale one. The stderr is
// the only place the reason lives; dropping it is what made this misdiagnose for hours.
export function describeCountFailure(proc) {
  if (!proc) return 'scripts/fleet-board.py could not be started'
  if (proc.error) {
    // `defaultRun` reports a kill as `status: null` + SIGTERM, so the signal is what tells a
    // timeout apart from a reader that died on its own.
    if (proc.status === null && proc.signal) {
      return `scripts/fleet-board.py timed out after ${COUNT_TIMEOUT_MS} ms (signal: ${proc.signal})`
    }
    return `scripts/fleet-board.py failed: ${proc.error.message}`
  }
  if (proc.status !== 0) {
    const stderr = (proc.stderr || '').trim()
    return (
      `scripts/fleet-board.py exited with code ${proc.status}` +
      (proc.signal ? ` (signal: ${proc.signal})` : '') +
      (stderr ? `: ${stderr}` : ' — it wrote nothing to stderr')
    )
  }
  return 'scripts/fleet-board.py ran but produced no session list'
}

// The live managers' session ids, Fleet Manager excluded — `{ ids, error }`, where `ids: null`
// means the count could not be taken and `error` says why.
//
// ⚠️ `ids: null` is "unknown", never "none": the refusal turns it into an error rather than a
// zero, because an empty fleet is the answer that opens past a limit. The `error` travels WITH
// it — a failure the caller cannot describe is one it can only misreport, which is how a 20 s
// timeout reached the operator as a limit problem.
export async function managerSessions({ board = BOARD, run = defaultRun } = {}) {
  const proc = await run(['python3', board, '--json'], { timeoutMs: COUNT_TIMEOUT_MS })
  if (!proc || proc.status !== 0) return { ids: null, error: describeCountFailure(proc) }

  let parsed
  try {
    parsed = JSON.parse(proc.stdout || '')
  } catch {
    return { ids: null, error: describeCountFailure(proc) }
  }
  if (!parsed || !Array.isArray(parsed.sessions)) return { ids: null, error: describeCountFailure(proc) }

  return {
    ids: parsed.sessions
      .filter((s) => s && s.role === 'manager' && (s.label || '').trim() !== ROOT_NAME)
      .map((s) => s.session_id),
    error: null,
  }
}
