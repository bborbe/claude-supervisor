---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Rules

When: not already in this session's context

- **One pass, then one verification sweep.** No polling loop.
- **Never type into another session's pane.**
- **Never run `/vault-cli:sync-progress` or `/vault-cli:session-close` for another session.**
  Both are session-scoped. A task file reading `completed` with zero open boxes is already
  the synced state.
- **Never relay an approval** for a production-touching or irreversible action, and never
  drive a selection modal.
- **A peer's claim is testimony; the task file and a live read are the evidence.**
- **A measured state beats a reported one** — the board, `details`, a task page's Status
  Summary and a closer panel are all asserted.
- **Report the refusals.** A close that refused and was not reported reads as a close that
  happened.
- **A headless worker is closed by `close-worker.py`, not by a hand-rolled `kill`.** The
  script runs the same guards a tab close runs and signals the registry pid itself; a refusal
  from it is terminal for that row, and the pid is reported either way.
- **A permission park is not a stuck session.** Registry `status: waiting`, or a non-empty
  `pending_permissions` / `awaiting_input: true`, means the turn is suspended mid-tool-call.
  Route the gate (3.2b) — never drive it, never close it, and never call it STUCK on
  transcript recency alone.
- **A signal is the last resort of the close path, never of the drive path.** The close path's
  signal is used only for a session whose CLOSED evidence holds, whose transcript is provably
  static, and whose pid carries `--print` — and it is `close-worker.py` that emits it, never a
  hand-run `kill` (see `commands/steps/fleet-triage-act.md` § CLOSED). Killing a worker to
  unstick it is the opposite of this command's job.
- **Every question ends the run carded.** A session classified OPERATOR with no open card is
  the run's failure, named in Step 6's `uncarded` count — never a note in passing.
- **A card is not a ping.** It carries the question verbatim and a recommended answer; if you
  cannot state both, the row is not yet understood well enough to card, and the honest output
  is a STUCK row naming what is missing.
