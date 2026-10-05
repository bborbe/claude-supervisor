// Binding a spawned session to the vault task it was opened for.
//
// ⚠️ The field this writes is an OWNERSHIP stamp, not a log line. `claude_session_id` is read
// as "who is working this task" by the manager sweep's orphan predicate, the vault UI's
// session column and the auto-resume gate — so a write that REPLACES it does not add a fact,
// it retracts one, and the row's real owner goes invisible on every consumer at once.
//
// Measured 2026-10-05. A cluster spawn bound to a task whose owner was live displaced that
// owner's id with the probe session's. The probe was a CLUSTER worker, which has no
// `~/.claude/sessions/<pid>.json`, so `session-liveness.py --check` can only ever return
// ABSENT for it: the row was left naming a session that can never read LIVE. It survived only
// because `metrics_sessions` still carried the real owner twice and the liveness rule is
// "alive if ANY id in the whole set returns LIVE". That is luck, not safety — a freshly
// spawned row names its owner in `claude_session_id` alone, and would have been silently
// orphaned by the very spawn that was measuring it.
//
// The rule implemented here is the one session-connect already follows — **write the stamp
// only when the field is empty** — plus the history write that makes the new session visible
// without claiming an ownership it was not given:
//
//   unowned task  →  stamp `claude_session_id`   (the ordinary case; this is what makes the
//                    worker reachable from the vault, and what the goal's SC2 requires)
//   owned task    →  append to `metrics_sessions` and LEAVE the owner intact
//
// `metrics_sessions` is the field for exactly this: it already holds the row's session
// history, and the liveness readers consult the whole set, so the new worker reads as live
// while the owner keeps the stamp.
//
// ⚠️ Refusing a bind onto an owned task was the alternative, and it is worse from this
// position: `startClusterSession` has already run by the time the bind happens, so a refusal
// here leaves a live worker that nothing points at. A refusal worth having has to move ahead
// of the spawn, which is a different change to a different function.

import { spawnSync } from 'child_process'

export const OWNER_KEY = 'claude_session_id'

// The real runner. Defaulted rather than called directly everywhere below, so the tests can
// drive the exact argv without a vault — the assertion that matters is which subcommand is
// chosen, and that has to be readable from the call rather than inferred from a file.
export function runVaultCli(args) {
  const proc = spawnSync('vault-cli', args, { encoding: 'utf8' })
  if (proc.error) return { error: `vault-cli could not be run: ${proc.error.message}` }
  return { status: proc.status, stdout: proc.stdout ?? '', stderr: proc.stderr ?? '' }
}

// Read the task's current ownership stamp. `owner: null` means the field is EMPTY — an
// unowned task, which the binding is free to stamp.
//
// ⚠️ An unreadable answer REFUSES rather than reading as unowned. Defaulting here would turn
// a transient vault-cli failure into precisely the displacement this module exists to
// prevent, and it would do it silently — the caller sees a successful bind.
export function currentOwner({ task, vault, run = runVaultCli }) {
  const args = ['task', 'get', task, OWNER_KEY, '--output', 'json']
  if (vault) args.push('--vault', vault)
  const res = run(args)
  if (res.error) return { error: res.error }
  if (res.status !== 0) {
    return { error: (res.stderr || res.stdout || `vault-cli exited ${res.status}`).trim() }
  }
  let parsed
  try {
    parsed = JSON.parse(res.stdout)
  } catch {
    return { error: `vault-cli task get returned unparseable output: ${res.stdout.trim()}` }
  }
  // ⚠️ The exit code is NOT the whole error signal, and guarding on it alone re-opens the
  // displacement through the error path. Measured against the real CLI: a task that does not
  // exist answers `{"error": "...file not found", "success": false}` with **exit 0** — so the
  // check above never fires, `parsed.value` is absent, and a naive read calls the task
  // UNOWNED and stamps it. `value` is the discriminator: a valid answer always carries it as
  // a string, empty when the key is unset (`{"key": …, "name": …, "value": ""}`, also exit 0).
  if (typeof parsed?.value !== 'string') {
    return { error: parsed?.error ?? `vault-cli task get returned no value: ${res.stdout.trim()}` }
  }
  const value = parsed.value.trim()
  return { owner: value === '' ? null : value }
}

export function bindSessionToTask({ task, vault, sessionId, run = runVaultCli }) {
  const read = currentOwner({ task, vault, run })
  if (read.error) return { error: read.error }

  const stamping = read.owner === null
  const args = stamping
    ? ['task', 'set', task, OWNER_KEY, sessionId]
    : ['task', 'append-metrics-session', task, sessionId]
  if (vault) args.push('--vault', vault)

  const res = run(args)
  if (res.error) return { error: res.error }
  if (res.status !== 0) {
    return { error: (res.stderr || res.stdout || `vault-cli exited ${res.status}`).trim() }
  }
  return { vault: vault ?? null, action: stamping ? 'stamped' : 'appended', owner: read.owner }
}
