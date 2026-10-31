---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Flagged open

When: every tick

## Step 5c — Open what the operator already marked

`/supervisor:open --flagged` has been specified since before the `flag_set_by` field existed and
had **no caller**: `commands/open.md` defines the batch, `commands/steps/goal-spawn.md`
excluded the very rows it selects, and three rule files said the loop must never invoke it.
This leg is the caller. The operator's mark is their decision; opening it executes that
decision and decides nothing new (`docs/session-tiers.md` § The four tiers, rule 2).

**Invoke the command. Do not re-implement its selector** — `commands/open.md` § the
`--flagged` batch owns the vault scan, the due-date rule, the `OPENING_ACTORS` gate and the
`REFUSED` line, and a second copy here would drift from it:

```
/supervisor:open --flagged
```

**Act on what it prints, and nothing else.** It opens each admitted row and prints the rest as
`REFUSED | <vault> | <name> | flag_set_by=<who> | not the operator — not opened, not approved`.
**Print every `REFUSED` line verbatim and open nothing it names.**

⚠️ **`flag_set_by` is the safety rule, not a detail.** Only `operator` and `legacy` are
admitted; an agent-set value is refused, and so is an **absent** one — after the disposition no
row should carry one, so an absent value means a writer that did not declare itself, which is
the bypass. Never widen the admitted set to make a row openable, and never "fix" a refusal by
writing `flag_set_by` on the row.

⚠️ **This leg removes the human from the loop, and that is a real change in blast radius.**
Before it, the batch ran only when the operator typed the command; now it runs every tick
unattended. `legacy` is a plain frontmatter value with no enforcement behind it, so under the
fleet's cooperative-agent trust model an agent that writes `flag: true` +
`flag_set_by: legacy` can get its own task opened without the operator. The same assumption
underlies `keepalive.md` resuming a page-supplied `claude_session_id`. **That is the accepted
model, not a defect — but it is named here so nobody reads the `flag_set_by` check as an
authentication control.** If the model ever changes, this leg and the keep-alive are the two
places that must change with it.

⚠️ **This leg opens work, never a manager — and not because rule 4 forbids it.** Rule 4
explicitly permits a manager opened *because it was flagged*; what stops this leg is that
`/supervisor:open --flagged` selects **tasks** only (`commands/open.md` § the batch). A flagged
manager is rule 2's subject and opens through `/supervisor:open --goal` or `--topic`. ⚠️ **No
fleet-tier leg opens a flagged goal manager today** — `fleet-drive.md` removed the
manager-proposal row on 2026-10-29, so the tier table's *"a goal manager only when that goal is
flagged"* has no fleet executor; the operator opens one, or the topic tier's ranking does on
their instruction (`commands/topic-drive.md` § Start). Say that plainly rather than implying
this leg covers it.

**Report the count.** Print `unactioned: <n> operator-flagged` with the rows this leg left
unopened — the `# Regression metric` reads it.
