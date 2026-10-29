---
# Not a slash command — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Closure

When: every tick, after the classify

Applies to the round's own subject and to every page in its tracked set, when every
`# Success Criteria` box **and** every `# Definition of Done` box reads `[x]`.

**A page whose evidence is complete closes itself. Nobody certifies it.**

The rule and its rationale have one home — `commands/goal-loop.md` § Manager contract
(*"You may close a goal; you may never certify one"*). This step owns only the execution.

## What to do

For the subject, then each page of the tracked set:

1. **Read it with the reader script — never a whole-page count:**
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/goal-closure-read.py "<page>"
   ```
   It prints the frontmatter `status`, each section's done/open split, and a verdict, and
   exits 0 only on `CLOSE`:

   | Verdict | Meaning | Action |
   |---|---|---|
   | `TERMINAL` | the page already reads `completed`/`aborted` | **skip it** — nothing to close |
   | `UNMEASURABLE` | a section is absent, or carries no recognised box | report the gap, close nothing |
   | `OPEN` | at least one box is open | report which, close nothing |
   | `CLOSE` | both sections present, every box `[x]` | step 2 |

   ⚠️ **`TERMINAL` is what stops a page being re-closed every tick.** The verb writes
   `status: completed` and leaves every box at `[x]`, so a page closed last tick satisfies the
   same predicate this tick — without this guard the round reports a close attempt forever.

   ⚠️ **`UNMEASURABLE` is not complete.** The predicate is *every box reads `[x]`*, which a
   section holding no box satisfies vacuously — that is how an empty `# Definition of Done`
   reached the verb. An absent section and an empty one are the same answer: refuse.

2. **Run the tier's own verb, `--vault` passed explicitly, and quote it verbatim:**
   ```bash
   vault-cli task  complete "<task>"  --vault "<vault>"
   vault-cli goal  complete "<goal>"  --vault "<vault>"
   vault-cli topic complete "<topic>" --vault "<vault>"
   ```
   ⚠️ **`--vault` is not optional** — without it the write targets the default vault, so a
   subject in any other vault lands on the wrong page or nowhere at all. It is the same
   explicit-`--vault` contract `commands/steps/goal-resolve.md` carries for the manager's write.

   The verb re-checks its own gate. **Its verdict is the authority, never this step's read** —
   when the two disagree, the verb wins and the disagreement is reported, not resolved by hand.

3. **Stop the loop when the SUBJECT closes** — report the verdict, record the stand-down with
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manager-liveness.py --stop --subject "<subject>" --branch <goal|topic>`,
   and end the tick without re-arming. A loop that keeps arming on a completed subject reports
   work where there is none. ⚠️ **A tracked page closing is not this** — only the subject ends
   the loop; a task or member goal closing just leaves the tracked set.

4. **Never pass `--force`.** A close that needs it is a close this step does not make.

## Which pages this covers

| Branch | Examined |
|---|---|
| goal | the subject goal, then each task printed in `Tracked (N):` |
| topic | the topic page, then each goal in its `## Goals` list |

⚠️ **Both tiers reach this step through a router row, and that row is what makes the claim
true.** `commands/goal-drive.md` row 4c runs it on the goal branch; `commands/topic-drive.md`
row 4c runs it on the topic branch. A tier with no router row has no execution whatever its
arm says — which is why the topic row exists rather than the rule being restated in prose.

## What this step must never do

- **Never tick a box** in either section, for any reason. That is certification, and it is
  forbidden at every tier.
- **Never set `status: completed` directly.** The verb writes it; nothing else may.
- **Never close on a partial read.** A page whose DoD section is missing or empty is
  *unmeasurable*, not complete — report it as a gap.
- **Never close a page whose open boxes are another task's work.** Report which boxes are open
  and whose they are; closure is not a way to dispose of them.

## Reporting

One line per page examined, and one for the round:

```
closure: <page> — <n> open (SC <a>, DoD <b>)            → not a candidate
closure: <page> — TERMINAL (already <status>)           → skipped
closure: <page> — UNMEASURABLE (<which section>)        → gap reported
closure: <page> — 0 open → <tier> complete → <verdict>  → closed
closure: <page> — 0 open → <tier> complete → REFUSED: <the verb's own line>
```

⚠️ **A refusal is reported verbatim and leaves the page open.** It is not smoothed into
"closed", and it is not retried with `--force`.
