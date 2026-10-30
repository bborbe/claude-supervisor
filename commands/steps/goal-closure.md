---
# Not a slash command — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Closure

When: every tick, after the sweep

Applies to the round's own subject and to every page in its tracked set, when every
`# Success Criteria` box **and** every `# Definition of Done` box reads `[x]`.

**A page whose evidence is complete closes itself. Nobody certifies it.**

The rule and its rationale have one home — `commands/goal-loop.md` § Resolution, the bullet
*"You may close a goal; you may never certify one"* (line 136). This step owns only the execution.

## What to do

For the subject, then each page of the tracked set:

1. **Read it with the reader script — never a whole-page count:**
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/goal-closure-read.py --kind goal  "<path to the goal page>"
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/goal-closure-read.py --kind task  "<path to the task page>"
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/goal-closure-read.py --kind topic "<path to the topic page>"
   ```
   ⚠️ **The argument is a page PATH, not a name** — the reader opens it as a file and exits 2
   with `cannot read` on anything else. The resolution step prints the subject's path
   (`Branch: goal (24 Goals/<Goal>.md)`), so take it from there. A name from `Tracked (N):`
   resolves to `<goals-dir>/<name>.md` or `<tasks-dir>/<name>.md` by the same **exact
   basename, case-insensitive** match the resolution step uses — never a substring glob, and
   never a hand-built join that misses a filename typed in a different case.
   ⚠️ **`--kind` is not cosmetic — the tiers close on different evidence.** A goal and a
   task close on `# Success Criteria` + `# Definition of Done`; a **topic closes on
   `# Success Criteria` + `# Completion Gate`** (`docs/session-progress.md:37`,
   `docs/sweep-output.md:95` — a topic is the only level carrying both sets). Measuring a
   topic against a DoD it does not have reads UNMEASURABLE forever; measuring it against the
   SC alone and ignoring the Gate closes it on the wrong evidence. A Gate mixing checkboxes
   with an ordered list is **out of compliance**, and the reader refuses it.

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

   ⚠️ **A grandfathered goal is the one exception, and it is narrow.** The vault requires a
   `# Definition of Done` only on goals created on or after **`DOD_REQUIRED_AS_OF` =
   `2026-06-26`**; a goal created before that date, or carrying no `created` at all, is
   **grandfathered** — an absent DoD is an accepted gap, not a violation. Without the
   exception this rule made every grandfathered goal permanently unclosable: closable by
   every other reader in the vault, unclosable by the one that actually closes it. The
   reader applies it, so an absent DoD on a grandfathered goal reads `CLOSE` and this step
   closes it normally.
   The exception covers **absence only** — a DoD heading that exists but holds no box is
   still `UNMEASURABLE` at every cutoff — and **goals only**: a task's DoD requirement is
   not part of that decision, and a topic closes on its `# Completion Gate`, which has no
   cutoff. The date is the same constant `agents/goal-auditor.md` § 12 carries; it lives in
   two places because a plugin script cannot import from a vault agent.

2. **Run the tier's own verb, `--vault` passed explicitly, and quote it verbatim:**
   ```bash
   vault-cli task  complete "<task>"  --vault "<vault>"
   vault-cli goal  complete "<goal>"  --vault "<vault>"
   vault-cli topic complete "<topic>" --vault "<vault>"
   ```
   ⚠️ **`--vault` is not optional** — without it the write targets the default vault, so a
   subject in any other vault lands on the wrong page or nowhere at all. It is the same
   explicit-`--vault` contract `commands/steps/goal-resolve.md` carries for the manager's write.

   ⚠️ **Double-quote, as the rest of the tree does — and check the name first, with the
   script rather than with your own eye:**
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/closure-name-safe.py "<name>"
   ```
   It exits 0 on a safe name and 1 on one carrying `$`, a backtick, `"` or `\` — the entire
   injection surface inside a double-quoted word. **On exit 1, do not run the verb** — report
   the name and stop. ⚠️ **The check is a script, not a convention:** this line runs unattended
   every tick, and a rule the model has to remember each time is not a gate. It is stricter
   than `commands/open.md:499` states, deliberately — it is the mechanism that mandate leaves
   unnamed.

   The verb re-checks its own gate. **Its verdict is the authority, never this step's read** —
   when the two disagree, the verb wins and the disagreement is reported, not resolved by hand.

3. **Never pass `--force`.** A close that needs it is a close this step does not make.

## Only after the whole set is examined — ending the round

⚠️ **Steps 1–3 run over EVERY page before anything stops.** Ending the tick from inside the
loop would close the subject and quit before the tracked set was read, so the tasks a finished
goal owns would never be closed — and on the topic branch it would also preempt row 12, whose
tail (`commands/steps/round-tail.md`) owns the topic's post-close sequence: reconcile the
Status Summary → `/vault-cli:sync-progress` → stop the loop → `/vault-cli:session-close`.

Once every page in the set has been examined, and **only** if the SUBJECT itself reported
`CLOSE`:

- **Goal branch** — report the verdict, record the stand-down with
  `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manager-liveness.py --stop --subject "<subject>" --branch goal`,
  and end the tick without re-arming. A loop that keeps arming on a completed subject reports
  work where there is none.
- **Topic branch** — report the verdict and **hand off to the tail**: do not stop here, because
  the tail owns the reconcile, the sync and the session close. Let row 12 run its close
  sequence, which stops the loop itself.

⚠️ **A tracked page closing is not this** — a task or a member goal closing just leaves the
tracked set; only the subject ends the round.

## Which pages this covers

| Branch | Examined | Contract |
|---|---|---|
| goal | the subject goal, then each task printed in `Tracked (N):` | SC + DoD |
| topic | the topic page, then each goal in its `## Goals` list | topic page: SC + **Completion Gate**; member goals: SC + DoD |

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
