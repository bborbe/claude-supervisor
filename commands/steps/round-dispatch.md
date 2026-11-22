---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Round dispatch

When: every tick — after the re-arm (row 1), before the read half

⚠️ **The round's machine half runs in ONE delegate.** Dispatch `supervisor:fleet-round`
**once**, passing the round id, this session's id, the vault and the absolute tasks dir. The
delegate reads the step files, runs rows 0, 2–3, 5, 7–10, 12, 15, 18 and 20 — dispatching the
existing sweep, keep-alive and readings legs, taking the provenance acts, the cause check
and the escalation, and writing the render to `<round>.render` — persists the round, and returns
the ≤ 1 KB verdict plus the artifact paths. You keep rows 1, 11, 13, 14, 17 and 19: the re-arm,
the operator questions, the watchers, the voice and the round tail, and you print the render
file **after** that tail. The split, and why the manager no longer reads the step files, is
`agents/fleet-round.md`.

⚠️ **The round id is the `round=` token `manager-receipt.py --start` printed — verbatim.**
Pass that token to the delegate and use it for every `<round>.*` path in the tail. Never
re-read the receipt's `round_started_at` (its repr can carry a seventh decimal) and never pass
a timestamp: the artifacts are named by the six-decimal stem, so any other spelling names files
that do not exist. Measured 2026-11-21: one round passed an ISO stamp, another
`1795255389.1027682` against artifacts under `1795255389.102768`, and the tail's
`fleet-legs.py --status` read `UNKNOWN`.

⚠️ **The verdict is CHECKED, not trusted — run the gate before you read a single count.**
`agents/fleet-round.md` already says *emit the fence and NOTHING else*, and the delegates
ignore it: measured 2026-11-18 across eight live `supervisor:fleet-round` runs, **seven emitted
no fence at all** — a 54–111 B closing remark — and the eighth emitted prose at **1 510 B**
against the 1 024 B cap. A rule the delegate can re-read and override is not a bound, so the
check is what holds it:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/check-handback-size.py \
  --transcript ~/.claude/projects/*/<this session id>/subagents/agent-<agentId>.jsonl
gate_rc=$?
```

⚠️ **Capture `$?` on the very next line, and NEVER pipe this invocation.** `python3 … | tail -5;
echo "rc=$?"` reports the *pipe's last stage*, not the script — so a `handback-size FAILED` line can
sit beside `rc=0` and read as a pass. Measured 2026-11-20: that exact piped form was the **only**
live invocation in a manager session's whole transcript, and it logged `size_exit=0` over a real
exit 1 while the gate never ran again. `scripts/check-round-gate-exit-code.py`, wired into
`precommit`, now fails the build when this block is piped before `gate_rc=$?`.

⚠️ **The `*` is the project slug and it is load-bearing** — every session lives at
`~/.claude/projects/<project-slug>/<session-id>/…`, never directly under `projects/`, so a path
without that segment resolves to nothing. It is deliberately unquoted so the shell expands it,
the same form `docs/fleet-surface.md` and `commands/steps/fleet-triage-state.md` use.
`<agentId>` is the id the Task tool returned.

**`gate_rc` 0** → read the counts. **`gate_rc` 1** → read the script's **stderr first**, then act on what it
names: *no fence* or *over the cap* is the delegate's defect → **re-ask it once for the counts
only**, in one line, and read nothing from its prose; *not a file* or *unreadable* is **your**
path or a broken transcript, never a delegate defect — fix the path and re-run, do not re-ask.
⚠️ **The re-ask is the mechanism, not politeness** — it makes an overrun cost the *delegate* a
turn instead of loading the manager's context with a narrative that restates what the payload
already holds. A delegate that overruns twice is a defect to report, not a third ask.

⚠️ **Row 19 (the tail) runs after the delegate returns, never before.** It judges the `re-arm`
epoch and the receipt **you** wrote — an arm that has not landed yet reads `skipped: re-arm` on
a round that armed perfectly. ⚠️ **The render file is already written by then** (row 20 is the
delegate's), so the tail cannot reach inside it: a skip it finds must be stated **in the tail's
own output, printed after the render file**, never assumed to appear in the block. That is the
order this line enforces — tail, then print.

⚠️ **Row 7 is the delegate's now, and it reads its own `parks=` file.** The delegate runs
row 5d and hands back `parks-answered` / `parks-routed` / `parks-escalated` plus the `parks=`
path; row 7 routes what it finds there and **you** answer it at row 11, because a headless
delegate must not speak in the operator's voice. ⚠️ **The routed parks and a routed row-8 spawn
are the two material sources row 11 batches besides the attention feed's `BLOCKED` set** —
`commands/steps/needs-input.md` names them, and the render's Needs-input item carries them, so
nothing routes to a row that never reads it. Nothing else carries those parks, so a round that
skips the read leaves a class-C question to the 15-minute timeout.

⚠️ **The digest path is the delegate's to derive, never yours to pass.** It is
`~/.claude/state/fleet-handback/<round timestamp>.digest`, written by the sweep reader the
delegate dispatches — so a path handed from here would name a file that does not exist yet and
the delegate would either invent one or fail a read it cannot control.

⚠️ **Arming stays here and is unchanged.** A delegate's lifetime ends with its turn, so an arm
written inside it would expire with it.

⚠️ **Arming without dispatching loses the round.** A wakeup that re-arms and returns has lost
it: the predecessor `fbad5082` did exactly that for two hours from 10:13 on 2026-11-17,
writing "out of context" in each closer while the keeper logged `lapse=none`. Every wakeup
ends with a dispatched round or a named reason it could not run.

⚠️ **A compaction never leaves the loop armed-but-not-driven.** After a compaction the first
turn re-arms (row 1) **and** dispatches the round; the re-arm alone is not a round.
