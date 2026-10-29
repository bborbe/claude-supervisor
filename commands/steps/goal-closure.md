---
# Not a slash command — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Closure

When: every tick, after the classify

Applies to every subject whose every `# Success Criteria` box **and** every `# Definition of
Done` box reads `[x]` — that is the only qualifying state, and the sections below define it.

**A page whose evidence is complete closes itself. Nobody certifies it.**

The rule and its rationale have one home — `commands/goal-loop.md` § Manager contract
(*"You may close a goal; you may never certify one"*). This step owns only the execution.

## What to do

For each tracked goal (and, when the topic branch is running, each declared topic):

1. **Read both checkbox sections from the file**, never from a status field:
   ```bash
   grep -cE '^[[:space:]]*-[[:space:]]*\[( |/)\]' "<page>"
   ```
   Count across **both** `# Success Criteria` and `# Definition of Done`. **Zero open boxes in
   both sections is the only qualifying state.** A page with open boxes is not a closure
   candidate — report its count and move on.

2. **Run the tier's own verb and quote it verbatim:**
   ```bash
   vault-cli goal complete "<goal>"      # goal tier
   vault-cli topic complete "<topic>"    # topic tier
   ```
   The verb re-checks its own gate. **Its verdict is the authority, never this step's read** —
   when the two disagree, the verb wins and the disagreement is reported, not resolved by hand.

3. **Never pass `--force`.** A close that needs it is a close this step does not make.

## What this step must never do

- **Never tick a box** in either section, for any reason. That is certification and it is
  forbidden at every tier.
- **Never set `status: completed` directly.** The verb writes it; nothing else may.
- **Never close on a partial read** — a page whose DoD section is missing is *not* complete,
  it is unmeasurable. Report it as a gap rather than treating an absent section as empty.
- **Never close a page whose open boxes are another task's work.** Report which boxes are open
  and whose they are; closure is not a way to dispose of them.

## Reporting

One line per page examined, and one for the round:

```
closure: <page> — <n> open (SC <a>, DoD <b>)            → not a candidate
closure: <page> — 0 open → goal complete → <verdict>    → closed
closure: <page> — 0 open → goal complete → REFUSED: <the verb's own line>
```

⚠️ **A refusal is reported verbatim and leaves the page open.** It is not smoothed into
"closed", and it is not retried with `--force`.
