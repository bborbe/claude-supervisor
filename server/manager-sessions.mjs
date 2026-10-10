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

// The live managers' session ids, Fleet Manager excluded — or `null` when the board could not
// be read. `null` is "unknown", never "none": the refusal turns it into an error rather than a
// zero, because an empty fleet is the answer that opens past a limit.
export async function managerSessions({ board = BOARD, run = defaultRun } = {}) {
  const proc = await run(['python3', board, '--json'])
  if (!proc || proc.status !== 0) return null
  let parsed
  try {
    parsed = JSON.parse(proc.stdout || '')
  } catch {
    return null
  }
  if (!parsed || !Array.isArray(parsed.sessions)) return null
  return parsed.sessions
    .filter((s) => s && s.role === 'manager' && (s.label || '').trim() !== ROOT_NAME)
    .map((s) => s.session_id)
}
