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

⚠️ **This leg opens work, never a manager.** A flagged *manager* is rule 2's subject and still
opens through `/supervisor:open`; a flagged **goal with no manager** does not get a manager
from this leg — that is rule 4, and `fleet-drive.md` removed the manager-proposal row for it.

**Report the count.** Print `unactioned: <n> operator-flagged` with the rows this leg left
unopened — the `# Regression metric` reads it.
