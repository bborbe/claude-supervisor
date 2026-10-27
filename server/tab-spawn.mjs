// The inner command of a tab spawn — the shape `commands/open.md` § 3.1 and
// `docs/fleet-surface.md` § Spawn a worker both carry, in its `bash -lc` form.
//
// It lives in its own module for one reason: the shape has now been got wrong SIX times,
// and the sixth was found by a review of the change that fixed the other five. A string
// built inline in `supervisor.mjs` cannot be asserted on — importing that module starts an
// MCP server — so the defect could only be caught by reading it. Here it can be tested.
//
// ⚠️ **NO `exec`.** `exec "<launcher>"` replaces the pane's shell with the launcher, so no
// process is left to `waitpid` and the session's exit status stops existing by
// construction — `137` is a value only a *parent* can read. Measured 2026-10-27 on this
// shape: with the launcher as a child behind `trap _sx EXIT`, `kill -9` records `rc=137`;
// behind `exec`, the trap cannot fire at all. A call site that re-adds `exec` silently
// re-opens the gap and nothing reports it — the session simply leaves no record.
//
// ⚠️ **The observer starts BEFORE the trap and BEFORE the launcher.** It detaches into its
// own session and blocks the shell on a pipe handshake until it is there, so a
// process-group kill landing immediately after the spawn cannot take it along. It watches
// `$$` — this shell — because the shell outlives the launcher and dies exactly when the
// group is killed, which is the case the trap cannot record.
//
// ⚠️ **`--session` is deliberately absent.** The claude session id does not exist until the
// tab registers one, and the record reads `-` for an unknown session rather than inventing
// one. The trap, the start marker and the observer all carry `--pid $$`.
//
// What each writer can and cannot name is owned by `docs/session-end-record.md`
// § *Two writers, layered by what each survives* and is not restated here.

import { shellEnvExports, shellQuote } from './spawn-mode.mjs'

// `$?` is captured as the function's FIRST statement because the inner string is parsed by
// the pane's shell: `trap '_sx $?' EXIT` is impossible (it would run a command named after
// the status) and `trap "_sx \$?"` expands `$?` at definition time. A function that reads
// `$?` on entry and a bare `trap _sx EXIT` is the shape that works in both quoting worlds.
const EXIT_TRAP =
  '_sx() { rc=$?; "$SESSION_EXIT_RECORD" append --pid $$ --rc "$rc" || true; }; ' +
  '"$SESSION_EXIT_RECORD" append --kind spawn --pid $$ || true; ' +
  '"$SESSION_EXIT_RECORD" observe --pid $$ || true; ' +
  'trap _sx EXIT; '

/**
 * Build the `bash -lc` script for a tab spawn.
 *
 * `env` is exported before the `cd` so the exports survive the login shell's profile files.
 * The writer path itself does NOT ride here — it is passed by the caller as an `env`
 * assignment before `bash`, because a spawned pane inherits wezterm's mux-server
 * environment, not this process's.
 */
export function tabSpawnInner({ cwd, launcher, tabName, prompt, env }) {
  return (
    `${shellEnvExports(env)}cd ${shellQuote(cwd)} && ` +
    `${EXIT_TRAP}${launcher} -n ${shellQuote(tabName)} ${shellQuote(prompt)}`
  )
}
