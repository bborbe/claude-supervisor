---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Round dispatch

When: every tick — after the re-arm (row 1), before the read half

⚠️ **The round's machine half runs in ONE delegate.** Dispatch `supervisor:fleet-round`
**once**, passing the round id, this session's id, the vault and the absolute tasks dir. The
delegate reads the step files, runs rows 0, 2–10, 12, 15, 16, 16b, 18 and 20 — dispatching the
existing sweep, keep-alive, readings and open legs, **answering the parked workers, acting on the
open-items ledger, verifying the cause it names and rendering the output shape** — persists the
round, and returns the ≤ 1 KB verdict plus the artifact paths. **You keep rows 1, 11, 13, 14, 17
and 19, and nothing else:** the re-arm, the needs-input question, the attention watchers, the
compaction, the voice and the round tail. The split, and why the manager no longer reads the step
files, is `agents/fleet-round.md`.

⚠️ **Why exactly those six stay — each is structural, not preference.** Row **1** arms the wakeup.
Row **11** needs an `AskUserQuestion`, and a headless delegate that asked would park with nobody
to answer. Row **13** arms a `Monitor` whose lifetime is *your* session — an arm written inside
the delegate expires with its turn. Row **14** compacts *your* session. Row **17** speaks as the
manager. Row **19** judges **your** `re-arm` epoch and the receipt **you** wrote, which the
delegate cannot see. Every other row only decides or renders, so it belongs in the delegate.

⚠️ **This is the change that moves the number.** Measured 2026-11-19 over eight rounds: the
manager's own thinking plus the tool results it read were **65–70%** of a round, with a **19×**
spread (11.7 → 228.4 KB, median 105.9 KB against a 70 KB bound). Trimming hand-backs cannot close
a spread that wide; moving the decisions and the render out of the manager is the lever. Probe: a
round whose manager-side thinking plus tool-result share falls below **30%**.

⚠️ **The verdict is CHECKED, not trusted — run the gate before you read a single count.**
`agents/fleet-round.md` already says *emit the fence and NOTHING else*, and the delegates
ignore it: measured 2026-11-18 across eight live `supervisor:fleet-round` runs, **seven emitted
no fence at all** — a 54–111 B closing remark — and the eighth emitted prose at **1 510 B**
against the 1 024 B cap. A rule the delegate can re-read and override is not a bound, so the
check is what holds it:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/check-handback-size.py \
  --transcript ~/.claude/projects/*/<this session id>/subagents/agent-<agentId>.jsonl
```

⚠️ **The `*` is the project slug and it is load-bearing** — every session lives at
`~/.claude/projects/<project-slug>/<session-id>/…`, never directly under `projects/`, so a path
without that segment resolves to nothing. It is deliberately unquoted so the shell expands it,
the same form `docs/fleet-surface.md` and `commands/steps/fleet-triage-state.md` use.
`<agentId>` is the id the Task tool returned.

**Exit 0** → read the counts. **Exit 1** → read the script's **stderr first**, then act on what it
names: *no fence* or *over the cap* is the delegate's defect → **re-ask it once for the counts
only**, in one line, and read nothing from its prose; *not a file* or *unreadable* is **your**
path or a broken transcript, never a delegate defect — fix the path and re-run, do not re-ask.
⚠️ **The re-ask is the mechanism, not politeness** — it makes an overrun cost the *delegate* a
turn instead of loading the manager's context with a narrative that restates what the payload
already holds. A delegate that overruns twice is a defect to report, not a third ask.

⚠️ **Row 19 (the tail) runs after the delegate returns, never before.** It judges the `re-arm`
epoch and the receipt **you** wrote — an arm that has not landed yet reads `skipped: re-arm` on
a round that armed perfectly — and it must run before the render, or a skip it finds cannot
reach the block the operator reads.

⚠️ **Row 7 reads the delegate's `parks=` file.** The delegate runs row 5d and hands back
`parks-answered` / `parks-routed` / `parks-escalated` plus the `parks=` path; the routed parks
there are yours to answer, and nothing else carries them. A round that skips that read leaves a
class-C question to the 15-minute timeout.

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
