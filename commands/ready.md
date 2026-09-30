---
description: Ready ONE named task and open it — run the audit → repair → re-audit loop in a sub-agent until the audit returns zero hard-gate failures, then act on the Gate 2 verdict: open via `/supervisor:open`, or escalate naming the gap. Cross-tier — runnable by the fleet manager and by a goal/topic manager. It readies a task; it never approves one.
allowed-tools:
  - Bash(vault-cli:*)
  - Bash(grep:*)
  - Task
argument-hint: "<task name> [--vault <vault>]"
---

Ready ONE named task to the spawn bar, then open it — so the calling session stops repeating the
audit → read gaps → edit the task file → re-audit → open sequence by hand. The calling session
prints a verdict line and nothing else; every gap and every fix diff stays in the sub-agent.

## What this is not

- **Not a second readiness loop.** The bounded repair-then-open loop already lives in
  `agents/manager-drive.md` clause (1). This verb re-surfaces it onto **one named task**. Read the
  loop there — its round cap, its stop condition and its early-stop rule are owned by that clause
  and are never restated here.
- **Not a second readiness bar.** The bar and its single home are `docs/fleet-surface.md`
  § Spawn a worker item 2. Reference that home; never restate the number.
- **Not a batch path.** `/supervisor:open --flagged` stays the batch path; this is the
  single-task path.
- **Not an approval.** It readies a task; the operator approves one. See step 2.

## Process

### 1. Resolve the task

`vault-cli --vault "<vault>" task search "<name>"`. Exactly one match → continue. More than one →
print each and STOP; task titles are not namespaced, so never guess. None → print the closest
candidates and STOP; never create a task from an unmatched name.

### 2. Gate 1 — approval, before any audit

`vault-cli --vault "<vault>" task get "<task>" phase`.

`phase: todo` means the operator has not approved the row. Print the `⛔ NOT APPROVED` block
verbatim from `commands/open.md` § Step 1.5 Gate 1 and **STOP** — spawn nothing, do not run
Gate 2, do not write to the task, and never add the approval yourself. That block owns the refusal
text and its reason; do not restate them here.

### 3. Gate 2 — readiness, then repair

Dispatch the `vault-cli:task-auditor` **agent** via `Task`, using the readiness prompt whose single
home is `commands/open.md` § Step 1.5 Gate 2. `Task` is the only tool that addresses a
`subagent_type` — `Skill("vault-cli:task-auditor")` answers `Unknown skill` and scores nothing.

Read the `READINESS:` line. **Parse the score; never trust the label.** A missing or unparseable
line is not-ready.

Then run `agents/manager-drive.md` clause (1)'s loop against this one row: repair, re-audit, stop on
the conditions that clause owns — including its **step 0 `UNFIXABLE:` branch**, which is that
token's only reader. Do not re-implement the branch here: a caller-side reader leaves the loop's
own stop rule unenforced for every other entry into it.

### 4. Act on the verdict

Reuse `commands/open.md` § Step 1.5 Gate 2 for the decision. Never re-implement score parsing and
never soften the bar.

- **Clears the bar** → hand to `/supervisor:open "<task>"` for this single named task.
- **Below the bar, or an `UNFIXABLE:` verdict** → escalate naming the gap. A row below the bar is
  not held in silence: **post the attention-board card that names the score against the bar, then
  hold the row** — per `docs/fleet-surface.md` § Spawn a worker item 2, with the card's fields and
  the hold line as `agents/manager-drive.md` clause (1)'s `<error_handling>` entry writes them.
  The `UNFIXABLE:` verdict arrives already read by the loop — this step relays it, and never
  re-implements the branch.

### 5. Print the verdict — and only the verdict

The calling session prints **one line**, carrying all three fields:

```
READY: <task> — <before>/10 → <after>/10 — opened | escalated: <gap> | refused: not approved
```

Nothing else reaches the manager's context, and the whole response stays within the line budget
the task's own Success Criterion sets. Audit gap bullets and fix diffs stay in the sub-agent's
transcript: a sub-agent that returns its gap list into the caller's context has broken this
contract even if the caller then summarises it.

## Why the loop is not re-implemented

A second loop is a second counter: the day one is edited and the other is not, nothing errors and
no diff catches it. `agents/manager-drive.md` clause (1) already runs this loop for the
ready-to-start bucket of a swept set; this verb runs it for one named row and hands the result to
`open`. Same loop, same bar, one home each.

## Related

- `/supervisor:open <name>` — resolves a name and jumps, resumes or spawns. This verb is the
  readiness prelude; it does not replace `open`.
- `/supervisor:manager-drive` — the act leg for a whole swept set, by hand.
- `/supervisor:fleet-drive` — the same act leg for the fleet.
