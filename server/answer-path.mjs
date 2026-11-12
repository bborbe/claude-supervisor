// answer-path.mjs — the spawn-site refusal: a session with no answer path may not open a
// headless worker.
//
// `docs/fleet-surface.md` § Spawn a worker item 6, constraint (a): a headless worker's
// permission gate parks with the SESSION THAT SPAWNED IT. No other manager can release it,
// and an unanswered park dies after 15 minutes with `Supervisor did not answer within 15
// minutes.` Measured 2026-09-20: two workers stranded, four gates expired in ~90 minutes,
// because the spawners had no answer path and nothing at the spawn site asked.
//
// ⚠️ **ONE PROBE, AND IT IS NOT NEW.** `scripts/answer-path-check.py` resolves this
// session's own subject and branch from `~/.claude/state/worker-manager/<session-id>.json`
// and asks `scripts/manager-liveness.py --state` — the instrument the fleet already runs at
// every manager re-arm. A second lock file written here would be the two-counters defect
// this repo refuses everywhere else: the day one writer changes, the two disagree with no
// error on either. So this module owns the CALL, never a second observable.
//
// ⚠️ **REFUSE, NEVER WARN.** A warning cannot block a spawn, and the caller reads the
// refusal text as the reply. Every branch that is not a proven `OK` refuses — no session
// id, a probe that cannot run, output that does not parse, an unproven state — because a
// check that failed open would open exactly the worker this rule exists to prevent.
//
// ⚠️ **The fleet-tier gap this guard used to name is CLOSED, and the note stays because the
// shape recurs.** A session with no `worker-manager` registration reads as having no answer
// path and is refused — the correct verdict for this probe, and the wrong one for any tier
// that answers prompts but files no record. That was the Fleet Manager until
// `scripts/fleet-register.py` plus `commands/steps/re-arm.md` gave it one (v0.209.0). The
// lesson is the rule: **a tier that answers parks must write its registration**, and the fix
// is always the record, never a bypass at the spawn site — a bypass here would remove the
// check for every session at once.

import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

// Resolved from this module's own location, so the probe is found whatever the server's cwd
// is. `scripts/` sits beside `server/` in the repo and in the installed plugin.
export const PROBE_PATH = fileURLToPath(new URL('../scripts/answer-path-check.py', import.meta.url))

const PROBE_TIMEOUT_MS = 20_000

// The two spellings of the remedy, so the refusal and the docs cannot drift on it.
//
// ⚠️ **The split is by role, and it is Rule 2 that makes the two outcomes opposite.**
// Operator ruling 2026-11-12 (`docs/role-mode-matrix.md` Rule 2): `role: agent` is
// `headless` always, so offering an agent row "open the worker interactive" names the one
// outcome the fleet forbids — and it is the cheaper of the two remedies, so a refused
// spawner takes it. Measured 2026-11-12: 24 of the 90 `role: agent` new-worker spawns
// opened as tabs in 24 h, every one `mode_source=argument`. A `human` / `manager` row is
// the opposite case: a tab is exactly what it is for, so the interactive remedy stays.
//
// ⚠️ **This module is the DRILL surface, and a drill session is an agent session.** The
// live spawn path is the Go `claude-wrapper` daemon, whose spawn handler carries no
// answer-path guard; the live remedy is `commands/open.md` § Step 0.6, which cites
// `docs/role-mode-matrix.md` § Answer-path remedy. So the refusal below always renders the
// agent shape. `ANSWER_PATH_REMEDY` is exported for the doc and for any future caller that
// does resolve a `human` / `manager` row — never wired to the drill, which has no such row.
const ARM_LOOP = "arm this session's manager loop and re-run the spawn"

/** The remedy for a `human` / `manager` row — a tab is the permitted outcome. */
export const ANSWER_PATH_REMEDY = `Open the worker interactive instead, or ${ARM_LOOP}`

/** The remedy for a `role: agent` row — a tab is never the permitted outcome. */
export const ANSWER_PATH_REMEDY_AGENT =
  `Arm this session's manager loop and re-run the spawn; if no loop can be armed, the row stays refused for this tick`

// Run the probe. Never throws: a missing interpreter, a timeout, a non-JSON body and a
// non-zero exit all come back as a refusal with the reason, because the caller must be able
// to quote why it was refused and a thrown error would reach it as a crash instead.
export function runProbe(sessionId, { path = PROBE_PATH, exec = spawnSync } = {}) {
  const done = exec('python3', [path, '--session-id', sessionId, '--json'], {
    encoding: 'utf8',
    timeout: PROBE_TIMEOUT_MS,
  })
  if (done.error) {
    return { ok: false, detail: `the answer-path probe could not run (${done.error.message})` }
  }
  let parsed
  try {
    parsed = JSON.parse(done.stdout ?? '')
  } catch {
    const said = (done.stderr || done.stdout || '').trim().slice(0, 200)
    return { ok: false, detail: `the answer-path probe printed nothing parseable${said ? `: ${said}` : ''}` }
  }
  // `ok === true` rather than truthiness: a probe that printed `"ok": "yes"` is a probe
  // whose contract changed, and reading it as a pass would fail open on the one check whose
  // whole job is to refuse.
  //
  // ⚠️ **The exit status is read too, and only where it can disagree.** `answer-path-check.py`
  // exits 1 on a refusal *and* prints the structured body, so a blanket `status !== 0 → refuse`
  // would throw away the reason the caller is meant to quote. The dangerous case is narrower:
  // a probe that exits non-zero while reporting `ok`, i.e. its two halves contradicting each
  // other. That is a refusal, never a pass — the fail-open direction this guard exists to
  // prevent, and the property would otherwise rest on an unstated contract between two files.
  if (done.status !== 0 && parsed.ok === true) {
    return {
      ok: false,
      detail: `the answer-path probe exited ${done.status} while reporting ok — refusing on the disagreement`,
    }
  }
  return { ok: parsed.ok === true, detail: parsed.detail ?? 'the probe gave no detail' }
}

// The refusal text, or `null` when the spawn may proceed.
//
// `opensInteractive` is the RESOLVED mode (see `resolveSpawnMode`), never the raw argument:
// an absent argument is a real mode, and a caller that omitted it must still be checked.
//
// The remedy is the AGENT shape, always: this module serves the drill surface, whose
// sessions are agent sessions (see the header). A `human` / `manager` row never reaches a
// refusal here — `resolveSpawnMode` forces those roles interactive, so the first line
// returns before the probe is consulted.
export function answerPathRefusal({ opensInteractive, sessionId, probe = runProbe }) {
  if (opensInteractive) return null
  if (!sessionId) {
    return `refusing a headless spawn: this server cannot name the session that asked for it, so it cannot prove an answer path exists for the worker's parked prompts. Probe: scripts/answer-path-check.py (manager-liveness.py --state). ${ANSWER_PATH_REMEDY_AGENT}.`
  }
  const { ok, detail } = probe(sessionId)
  if (ok) return null
  return `refusing a headless spawn: ${detail}. Probe: scripts/answer-path-check.py (manager-liveness.py --state), session ${sessionId}. A headless worker's permission prompts park with this session and die unanswered after 15 minutes. ${ANSWER_PATH_REMEDY_AGENT}.`
}
