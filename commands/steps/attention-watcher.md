---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Attention watchers

When: every tick — no `Monitor` to arm: the wake arrives as an `attention gate: <item-id>` line typed into this pane by `attention-controller`'s wake job; dispatch the gate answerer on it

  - **The push arrives; there is no arm to place.** `attention-controller`'s wake job watches the store and types `attention gate: <item-id> (<session>): <label>` straight into this pane — one line per newly-gated session, and `attention cleared: <sid8>` when a gate leaves the set. ⚠️ **That line IS the wake: no `Monitor`, no poller, no ownership-filter process.** The ownership filtering it replaced — peer drop, dead-manager keep, the `foreign-goal` and `role-human` exclusions, and the fleet routing of unowned rows — now runs in-process in that job, which is why a manager is woken for its own workers and not for another goal's. ⚠️ **A manager that arms nothing is not deaf:** the line is typed into the pane, so it wakes the model directly; a `Monitor` on top would be a second reader of the same signal.

  - **On every feed line carrying an `hst:<item-id>` token — or a pane-typed `attention gate: <item-id>` line — dispatch the gate answerer IMMEDIATELY — in the turn that line woke, never at the next round:**

    ⚠️ **The wake line and the dispatch must name the same shape, and this file is the only place that contract is written down.** The job types `attention gate: <item-id>` (`managerWakeLinePrefix`, `pkg/manager-wake-job.go`), and `agents/gate-answerer.md` accepts that token as well as `hst:`. A dispatch rule keyed on `hst:` alone — or an answerer that skips anything without that prefix — goes **silently inert**: the wake still arrives and nothing answers it. `hst:` is kept in the rule because a manager may still have an older feed line in flight; it is no longer produced by this plugin.

    ```
    Task tool with:
      subagent_type: 'supervisor:gate-answerer'
      run_in_background: true
      prompt: 'Manager session: <this-session-id>
               Gates: <every hst:<item-id> token on the line, and every item id in an `attention gate: <item-id>` line>'
    ```

    ⚠️ **ONE background dispatch per line, and nothing else for that line in this turn** — a `--digest` line carrying several `hst:` tokens hands them all to the one agent. A re-arm re-emits every still-open gate; dispatch anyway — the check's per-manager ledger (`~/.claude/state/gate-answer/<this-session-id>.json`) makes an item this manager already judged or left a `skip` for its lifetime, so a re-emission costs one cheap agent turn and never a second answer. The agent takes two card kinds. A **permission** card it checks mechanically (`scripts/gate-answer-check.py`: open, no operator hold, a worker this manager spawned or adopted, a parked wrapper question, not `operator` tier, exactly one readable brief), judges on `docs/permission-allowlist.md` § *How a manager applies it*, and answers `allow` — the claude-wrapper daemon delivers it to the headless child (carrier `claude-wrapper:board`). A worker's closer **`pick —`** card is `skip` today: the pick path exists but is disabled until the carriers render a manager's answer in the manager's voice (`docs/permission-allowlist.md` § *A worker's closer pick*). Answers go through `gate-answer-check.py --answer --expect <hash>`, which refuses unless the re-check still reads the call that was judged (the daemon then settles the worker's current park — `docs/permission-allowlist.md` § The instant path names that residual race); anything else is left for the operator. It never answers an `operator`-tier gate, an `mcp__*` / `WebFetch` / `Agent` call, a write outside the worker's cwd and `/tmp`, or unseen code (a script file, an inline program, a launcher), and never denies. Its report arrives as a background notification; act on it only when it names a `recorded, not delivered` answer. A bare pane id (a tab worker's gate) is not dispatched — it keeps the relay path below.

    ⚠️ **The answerer's own contract is in `agents/gate-answerer.md`, not restated here.** It checks each item mechanically (`scripts/gate-answer-check.py`: open, no operator hold, a worker this manager spawned or adopted, not `operator` tier, exactly one readable brief), judges on `docs/permission-allowlist.md` § *How a manager applies it*, answers `allow` through `gate-answer-check.py --answer --expect <hash>`, and never denies. A worker's closer `pick —` card is `skip` today. Anything it will not answer is left for the operator.

    ⚠️ **`attention cleared: <sid8>` is the other line the job types**, and it means a gate LEFT the set — it is not progress until confirmed. The ownership rules that decide which manager a line reaches (peer drop, dead-manager keep, `foreign-goal`, `role-human`, fleet routing) have one home: `pkg/manager-wake-job.go`'s `keeps()`, with `pkg/session-goal-scope.go` supplying the vault's answer. The reconciled rule and the exact line shapes are also in `the Manager Session runbook` § Cadence mechanics.
