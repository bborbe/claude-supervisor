---
description: Get a stuck session working again, or tell the operator exactly how to help. Diagnoses the session (waiting on what, task done, drift), then drives the anchored task until a hard stop. Takes no arguments. Absorbs the former /and. Bans WAIT as an answer after repetition, challenges unfinishable acceptance criteria, and never asks permission for non-forks.
allowed-tools:
  - Read
  - Bash
  - Edit
  - Write
  - Task
  - TodoWrite
  - Monitor
  - AskUserQuestion
  - mcp__tts__say
argument-hint: "(no args — diagnose, then drive until a hard stop)"
---

Finish the anchored task. Not report on it — **finish it**.

The user typed this because the session has been *correct* and *not done* at the same time. That is the failure this command exists for: every individual verdict was right, every wait was live, and after two days the task is still open. This command is both the instrument and the actuator: §0b diagnoses, §1–§6 drive. It replaces the operator's hand-typed restart prompt — *what are we waiting for? is the task completed? how can I help? what do you recommend?* — and every report answers those four questions first (§8).

**No arguments, by design.** One command, one behaviour: diagnose, then drive. There is no diagnose-only mode and no step cap — an option the operator has to remember is a second command in disguise.

**Your product is a state change, not a panel.** A `/supervisor:worker-drive` run that ends having only described the situation has failed, regardless of how accurate the description was.

## The contract

Run in a loop: **inventory everything still open (§1)**, then pick the highest-leverage *actionable* row, do it, observe, re-pick. The inventory is what keeps a single loud blocker from hiding five doable items. Continue until one of exactly **five hard stops**. Nothing else stops you.

| Hard stop | Test |
|---|---|
| **Credential / console / click** | The next action needs something only the user physically has (OAuth click, 2FA, a login, a Shift+Tab). |
| **Irreversible outward action** | Prod mutation, merge, release, delete, send. Needs explicit per-action approval. |
| **Genuine fork** | Two paths with *different objectives or scope* — not two sub-steps of one approved path. |
| **Done** | Every criterion met, or every remaining one provably belongs to someone else (§5). |
| **Other-owned** | Every remaining row belongs to another session or an external system, and none is actionable from here. §0b's owner table already permits this — *"Another session / person … **Nothing.** Preempting it sabotages their run. Say so explicitly."* — but the table below had no row for it, so a session that had driven everything had to improvise a stop and flag its own improvisation. Name the owner and stop. |

⚠️ **When the hard stop is an operator action, hand over a step they would
actually perform.** A scratch fixture built to exercise it is not evidence:
a `/tmp` file is not a clickable surface, and a synthetic table is not the
one they use. Verified 2026-09-24 — three fixtures were built for a "click
the cell" criterion, each unable to exercise the predicate, because the
artifact was a file rather than the rendered thing.

Not hard stops — drive straight through these: a defect discovered mid-work (fix it), a sub-step of approved work (do it), a mechanical write with evidence already in hand (write it), an unread file (read it), an ambiguous detail with a sane default (assume it, state the assumption, proceed).

**There is no action cap — the five hard stops are what end a run.** Measured 2026-09-05 across the first day of real use: reporting runs came in at 0, 2, 4, 8 and ~16 actions, and every one terminated on a hard stop, never on a budget. A fixed cap either does nothing or interrupts good work mid-stride; two of the longest runs that morning were the most productive.

Use ~15 actions as a **signal, not a limit**: passing it without reaching a hard stop is evidence the task is bigger than one drive. Say so in the report — name what is left and why it did not converge — then keep going or stop deliberately. Do not stop merely because a number was reached.

Report once, at the end.

## Step 0 — Anchor and load the drive counter

In order, first hit wins:

1. Most recent `🎯 Goal:` / `📌 Task:` closer-panel lines in this conversation.
2. Most recent `[[Task]]` / `[[Goal]]` wikilink used as a work subject.
3. `vault-cli task list --status in_progress` — the post-compaction fallback. **Use it whenever 1 and 2 are absent**, which is exactly the stuck/compacted session this command is for.
4. Daily note's first `[/]` checkbox.

Resolve names via `vault-cli` (`task get` / `goal get`) — never hand-write a plausible path. If nothing resolves: `📌 No task anchor — <reason>`.

Fallbacks 3–4 only count if the task sits under the same goal as recent conversation context; a daily-note mention of another goal is background, not an anchor.

The anchor scopes everything below.

Then read this session's drive state:

```bash
date -u '+%Y-%m-%dT%H:%M:%SZ'
DRIVE=~/.claude/state/drive-<session-uuid>.json
cat "$DRIVE" 2>/dev/null || echo '{"runs":0,"blockers":{}}'
```

Schema — `~/.claude/state/drive-<session-uuid>.json`:

```json
{
  "session": "<uuid>",
  "task": "<task file name>",
  "runs": 3,
  "last_run": "<ISO8601 UTC>",
  "blockers": {"<blocker name>": {"count": 4, "first_seen": "<ISO8601 UTC>", "levers_tried": ["force-trigger", "rollback"]}}
}
```

Per-session file, keyed on the session uuid from the scratchpad path — never a shared name. A sibling session clobbering the counter silently disables the whole escalation ladder.

## Step 0b — Diagnose the session

Verdict words: **ACT** — something is mine to do now; **WAIT** — a verified-alive, progressing wait with a watcher armed; **DRIFTING** — work left the anchor; **DONE** — the anchor reads terminal on disk. A WAIT then enters §2's repetition gate.

### Measure the window first — from disk, never from your own account

Your sense of what you achieved is a claim, not an input; `docs/session-progress.md` owns the grades, verdicts and claim classes.

1. Dispatch §1's coverage mode (once); write it verbatim to `COV=~/.claude/state/worker-drive-handback/<SID>.coverage`.
2. If a closure box moved since the last drive, file **one** claim — `{"box":"SC<n>|DoD<n>","kind":"tick|commit|progress-entry","ref":"<sha | quote-free Progress-line substring>"}`, never prose; else none.
3. Run the script before Step 8 rewrites `last_run` (`--since` defaults to it):

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
WIN=~/.claude/state/worker-drive-handback/<SID>.window.json; mkdir -p "${WIN%/*}"
python3 "$P/progress-window.py" --session <SID> --anchor "<task file>" --coverage "$COV" [--claim '<json>'] --json > "$WIN"; echo "exit=$?"
```

Exit 2 is `UNKNOWN`: name the stderr source, drive on. **`short_path: true` is the one live lever** — skip the reader and §1's inventory and drive the next open box; any other value only adds the ⏱ block (`block` field) to §8.

### Dispatch the read half

One dispatch covers §0b and §1:

```
Task tool with:
  subagent_type: 'supervisor:worker-drive-reader'
  prompt: |
    anchor: <task name> · <absolute task file path> · parent goal <goal>
    SID: <session uuid> · snapshot: ~/.claude/state/diagnose-snapshot-<uuid>.json
    prior_snapshot: <contents, or "no previous snapshot">
    date: <Step 0's `date -u`>
    window: <$WIN path> — the progress-window.py --json output, verbatim
    drift_note: <your observation of work that left the anchor, or "none">
    waits: <armed monitors · background shells · pending subagent notifications · daemons/containers/queues>
    vault: <vault root path> · tasks_dir: <tasks dir>
    coverage: <the §1 coverage dispatch output, verbatim>
```

**`window`, `drift_note` and `waits` must be passed explicitly — the reader cannot see this conversation**, and a fact you do not pass comes back `UNKNOWN`, which reads as a dead wait.

It returns the diagnosis, the verdict word, the wait table, the drive table, the proposed snapshot JSON and a ≤ 1 KB `key=value` hand-back; its full output is the payload at `~/.claude/state/worker-drive-handback/<SID>.payload`.

**It never writes. Three writes stay here:** the snapshot below, §1's `TodoWrite`, §1's peer-safety-gated `# Progress` append — a subagent's `TodoWrite` does not render in the operator's UI, and the reader holds neither this session's `claude_session_id` nor its UI.

Write the reader's proposed JSON to `SNAP=~/.claude/state/diagnose-snapshot-<session-uuid>.json` — this session's file only, keyed on the scratchpad uuid, never a shared name and never another session's file.

## Step 1 — Inventory the remaining work (the drive table)

**The coverage mode is dispatched once, in §0b's window step — it is the reader's input and the script's, never the reader's job** (the reader has no `Task` grant by design):

```
Task tool with:
  subagent_type: 'vault-cli:task-manager-agent'
  prompt: 'Action: coverage. TASK_PATH: <absolute path to the anchored task file>. MODE: tool.'
```

One row per closure box — `<box> | <driving subtask or none> | <evidence: measured(<source>) | asserted | none> | <proposal>` — plus a reconciliation line. ⚠️ **Read-only, and the only coverage implementation:** copy its ratings verbatim, never re-rate by eye, and never carry a local copy of this map into a manager command.

**Then the read half** (§0b above) takes this output as its `coverage:` input. It builds the drive table — one row per open item: Open item · Evidence · Blocked by · Unlock lever · Owner · Now? — reading the Progress log before rating any row, and sorting `✅` first, cheapest-first. **The `Evidence` cell is read, never judged:** copied, never decided.

**Zero `✅` rows is normally the strongest signal in this command** — a blocker is misdiagnosed (§3) or a criterion is unfinishable by construction (§5). Go there; do not park.

### Persist the table — TodoWrite live, Progress durable

Two writes, different lifetimes, **both here, from the reader's returned payload** — never in the reader (§0b).

**Actionable rows → `TodoWrite`, immediately.** One todo per `✅` row, phrased as the *action*, not the criterion; tick each as you drive, and drop any that turns out blocked mid-run. It is the operator's live progress view and it replaces any interim status report. No `TodoWrite` in the session → say so in your first turn and fall back to one short progress line every ~5 actions.

**Blocked rows → a dated Progress entry, gated on change.** Append, never rewrite:

```
- <date>: /supervisor:worker-drive run #<n> — blocked: <item> · blocker: <specific, verified this run> · lever: <what would clear it> · owner: <who>
```

Only when the blocker, lever or owner differs from the last such entry. Unchanged → write nothing and say so in the report.

**Peer safety.** If the task's `claude_session_id` does not name this session, the Progress append is the *only* write permitted — never the title, Success Criteria, Tasks, DoD or Summary; contradictory evidence goes in the entry plus a `SendMessage` to the owner. ⚠️ Precedence against the manager contract: `commands/goal-loop.md` § Manager contract.

## Step 2 — The repetition gate (the core mechanism)

For each blocker still standing, increment its `count`. Then apply the ladder. **The rung is not a suggestion — it is what this run does.**

| count | Rung | Behaviour |
|---|---|---|
| 1 | **Wait is legitimate** | Verify liveness + progress (§0b wait rules). Arm a watcher. Drive parallel work under §6 in the meantime. |
| 2 | **Wait is suspect** | Re-verify from scratch, distrusting the prior diagnosis. Name one lever you have not tried and try it. |
| 3 | **Wait is banned** | You may not return WAIT on this blocker. Produce a bypass, a scope cut, or a handoff with a deadline. Say which. |
| 4+ | **The plan is wrong** | Stop treating the blocker as a blocker and treat the *approach* as the defect. §4 and §5 are now mandatory, not optional. |

Observed basis: `eed76c7d` returned WAITING on the same fleet queue 5 times in 3.5h on 2026-09-04, then again the next morning. Each verdict was individually correct. The ladder is what nobody was allowed to climb.

**Record every lever in `levers_tried`.** A lever proposed and not executed does not count. Re-proposing a lever already in the list is a no-op turn — the thing this command exists to prevent.

## Step 3 — Hunt the lever before accepting the wait

§0b's owner table says an external blocker usually means "do nothing." **That default is wrong once driving.** Before any WAIT survives count ≥ 2, enumerate levers explicitly and say which you tried:

- **A blocker that is a decision, not a diagnosis** — before treating a blocker as misdiagnosed, ask *who set it*. A hold the operator put in place **after hearing your argument** is a ruling, and re-defeating its stated premise does not reach it: the ruling rests on the basis they chose, not the one you can disprove. The lever is approval, not re-argument — surface the new evidence and ask. `§5 Axis A` already carries this for splits (*"ownership grants write access, not authority to reverse a stated decision"*); it applies to every blocker, not just splits.
- **Force / trigger** — a poll interval you can skip (`/github-*-trigger`, an admin endpoint, a `--force` flag). **Only on an artifact this session owns.** A PR another session opened and is actively working is theirs to push, re-trigger and merge; a trigger you fire at it is a duplicate at best. When the lever you need belongs to a peer, that is not a lever — it is an Other-owned stop, and the finding goes to the manager.
- **Bypass** — reach the same end state by another path (do by hand what the automation would do; admin-merge; direct patch; **replay the trigger through the service's own smoke/seed tooling** — a seed/override flag drives the real pipeline with a controlled input, proving routing/delivery/dedup now).
- **Unstick the blocker itself** — the queue isn't slow, something upstream is broken. In `eed76c7d` the "opaque fleet queue" was an executor OOM-crashloop; a `rollout undo` cleared 10 hours of "waiting."
- **Check whether the answer already exists** — before waiting on a job to *produce* evidence, search for evidence already on disk: a prior run of the same config, a sibling artifact, a cached report. A comparator does not have to be freshly generated to be valid, only *comparable*. Ask what would make an existing artifact non-comparable, and whether that difference is actually load-bearing. **Search the whole subject, not the artifact in front of you** — sibling tasks tracking the same incident are the likeliest home, and one window can hold only half the discriminator: a `200` is ambiguous under quota, the `429` beside it is not. Observed 2026-10-08: a run read the prod proxy log, saw only `200`s, published *"inbound filters, not quota"* — while the owning task held the same `200` **and the `429`s**, and had read it right. Observed 2026-09-14: a session waited ~40 min on a US100 baseline backtest while a completed run with the same config and the same end date sat one API call away, differing only by a 14-day start offset on an 8.7-year window. Two diagnose-only runs and one drive run all rated the wait healthy — the lever was found only when the operator said "we are stuck".
- **Measure the base rate of the awaited event** — before accepting a clock-bound wait, ask how often the thing you are waiting for actually happens, and answer it from the record, not from the design. A criterion gated on "real traffic will populate this" is a foregone conclusion when that traffic has never occurred: measure it, and either the wait is real or the criterion is the bug (§5 Axis B). Observed 2026-09-24: a task waited 24 h for a window of goal-resolved items, and one query showed the answer path had been exercised **4 times in the store's entire history — all fixtures, 0 organic** — so the wait bought a number that could already be stated, and the criterion was split out instead.
- **A blocker that asserts an ABSENCE** — "no X exists", "the set is empty", "nothing is eligible". Verify it with the **full** test the domain defines, never one clause of it: a single-clause check returns the same empty result on a working system and a broken one, so it cannot tell a real absence from an unasked question. Observed 2026-09-24: the blocker was "no confirmed orphan exists", a registry-absence scan alone agreed, and only applying all **eleven** gate clauses surfaced **33** passing candidates — the absence was real *for the caller's tracked set*, but the reason (a documented structural blindness in how that set is built — `23 Topics/Manager Layer.md:65`) was invisible to the one-clause test. One clause of a ten-clause gate is not a check.
- **Narrow the criterion** — §5.
- **Ask** — last, not first, and only for a real hard stop.

Both real levers in that session were found *after* the user pushed, never by the session's own verdict. Hunt before you park.

## Step 4 — Never ask for a non-fork

Ban list. If the next action is one of these, **do it and report it** — a question here is the failure mode, not politeness:

- Ticking checkboxes whose evidence is already recorded in the file. *(Live example: a session reporting `0/22 (0%)` while its own Progress log records five merged milestones, ending "Want me to do that?" — the drift then makes the completion gate unreachable, so the task can never close.)*
- Syncing progress, updating a task/goal page, writing a Progress entry.
- Fixing a defect found while doing approved work.
- Running the next sub-step of an already-approved action.
- Reading a file, re-verifying a claim, re-running a check.
- Any read-only investigation, ever.

If you catch yourself writing "Want me to…", "Should I…", or "Let me know if…" for something on this list — delete it and perform the action.

A fork means **different objective or different scope**. If the user would plausibly answer "1 + 2 + 3", it was never a fork.

## Step 5 — Challenge the acceptance criteria

At blocker count ≥ 3, audit the *remaining* criteria — a diagnosis alone never does this, and it is frequently the actual cause.

Two axes. Both are cases where **the criterion is the bug**, not the session's speed.

### Axis A — unfinishable by construction

Flag any criterion that is **not completable by this session by construction**:

- Requires an external system to act *on its own schedule* ("reaches X through the normal watcher path, with **no operator action**").
- Forbids the session from satisfying it ("closed by the run that proves it, **not by hand**").
- Depends on another team, another session, or a human's queue.

These are standing watches wearing a task's clothes. A session anchored on one cannot finish, no matter how well it works. When found, say so plainly and propose the split:

```
Unfinishable criterion: SC4 — "no operator action" is a property of the fleet, not of this session.
Split: this task completes on what shipped (root cause + fix merged + gate signal deployed).
       The watch moves to a new task with a watcher and no session anchored to it.
```

**First check whether the task was recently and explicitly resolved by someone else** — a status/phase change or Progress entry dated today by another actor, or a resolution the operator picked off `session-close`'s menu (complete / defer / hold / leave-open). If one exists, STOP: name the split, its reason, and the decision it would override, and let the operator choose. Ownership in `claude_session_id` grants write access, not authority to reverse a stated decision — a recorded ruling that is not restated here.

Observed 2026-09-11: a split was executed on a task the operator had resolved minutes earlier as "leave it open until Friday, no status change", chosen off a menu that offers no split option. The split was the better shape and was ratified when a peer session escalated — but "unless the user objects" is not consent when nobody was told, and in an unattended run nobody ever is.

Absent such a resolution, **execute the split** — creating the follow-up task, moving the criterion, and completing the original is mechanical work under §4, not a fork. The split must be visible in both files.

### Never tick a moved criterion

A relocated criterion is **not a met criterion**. Ticking it `[x]` with a "moved to follow-up" note makes the completion gate pass by relabeling: the prose is honest, the checkbox is not, and every reader and rollup script sees `4/4 met` when 3 were met and 1 was relocated. Observed on this command's own first run, 2026-09-05.

**Move the line out of the section entirely.** Do not leave a ticked stub behind:

```
# Success Criteria            ← now contains ONLY genuinely-met criteria
- [x] Root cause named: …
- [x] Gate reports the underlying signal — v0.17.12 deployed

# Moved to [[<follow-up task>]]
- `bborbe/lockbox` go.mod reaches 1.27.0 — unfinishable here: "no operator action" is a fleet property
```

No checkbox on a moved line — it is not this task's to check. The follow-up task carries it as an open `[ ]`, and the Progress entry records the move with its reason and date.

**A partially-met criterion splits into two lines, never one ticked line.** DoD-2 on that same run read "verified by unit test + deployed; real-run verification moved" — ticked. Correct form: `- [x] classifier shipped and deployed (v0.17.12)` stays, and `real-run verification on a genuine failing gate` moves out under `# Moved`. One line per owner.

### Axis B — faulty premise: satisfiable, but proves nothing

The mirror failure. A criterion that this session *can* satisfy, whose evidence would not establish the claim it makes. It passes, the box gets ticked honestly, and nothing was proven.

Observed 2026-09-05: a soak criterion read *"no `ImagePullBackOff` across a deploy cycle **and a node restart**."* A node reboot does not clear containerd's image cache — images live on disk and survive it — so an `IfNotPresent` pod reuses its cached layer exactly as before. The criterion would have passed without ever exercising a pull.

Test each remaining criterion: **if this passed tomorrow, what would I actually know?** If the answer is "nothing the previous criterion didn't already tell me", or the mechanism it assumes doesn't work the way the wording assumes, say so and propose the corrected criterion. Do not silently satisfy it — a criterion met on a false premise is worse than an open one, because it closes the question.

**Apply the test to your own replacement, before writing it.** The correction is a new criterion and fails the same way — ask "if my replacement passed tomorrow, what would I know?" Observed 2026-09-09: a 7-day Sentry-silence window was correctly rejected as unfalsifiable (the issue fires ~0.22/day, so 7 days expects ~1.5 events), and the 48h steady-state window written to replace it had the identical defect — it measured steady state for a defect that only fires on shutdown, against pods with 0 restarts. Both would have been ticked honestly. The second was caught an hour later, by which point it had been written into the task file and was one `/supervisor:worker-drive` run away from costing 27h of wall-clock wait.

Correcting a criterion is a framing change: allowed when `claude_session_id` names this session, otherwise an appended Progress entry plus a `SendMessage` to the owner. Same rule and same precedence as § Peer safety above — the manager-side statement of both lives in `commands/goal-loop.md` § Manager contract.

### The follow-up task must be well-formed

When Axis A produces a split, the new task is a real task and gets the full template — **`# Success Criteria`, `# Tasks`, `# Definition of Done`**, plus a Progress entry naming the split and its reason. Carry the investigation findings across; do not leave them only in the parent.

Observed the same day: a split emitted Success Criteria and no `# Tasks`, so the next session that picked it up was told `Phase: plan-unvalidated · Plan: not started (missing SC/Tasks)` before it could do anything. A split that hands the next session a planning chore has moved the work without moving the readiness.

**The gate is then honest, and must still be checked.** After the split, re-run the vault's Auto-Complete Task Gate against the *reduced* section: zero `[ ]`/`[/]` anywhere, evidence present, no blockers voiced. A split that has to tick a box to pass is not a split — it is the thing this rule forbids. If the reduced section still has an unmet box, the task is not complete: say so and keep driving.

## Step 6 — Parallel work is mandatory during a legitimate wait

At count 1 a wait can be real. It is never a reason to idle. Do, in order:

1. Remaining work under the anchor that does not depend on the blocker clearing.
2. Prepare the post-blocker step so it is ready the instant the watcher fires.
3. Record evidence and findings while fresh.

Never surface work belonging to another goal. If genuinely nothing exists, say so in one line — do not manufacture work to look busy.

## Step 7 — Permission-mode precheck, once, up front

Before the loop starts, classify the actions you expect to run. If any will hit the auto-mode classifier (`ssh … --yes`, prod `make apply`, `kubectl exec`, bulk `rollout restart`, chained deploys), **surface it now, in the first turn**, not on the third denial:

```
👤 You: you run: Shift+Tab → accept edits, then reply `y` — blast radius: <exact scope>
```

`accept edits` does not reliably survive between turns; one session hit **three denials on one task in one day**, re-parking each time. Ask once, at the top, with the full blast radius named. Never decompose an action to slip past a denial — that evades the user's decision rather than respecting it.

On an auto-mode denial of an inert doc edit, retry the identical `Edit` once before escalating.

## Step 8 — Report once, at the end

One block. No per-step narration, no interim panels.

**The ⏱ block (`$WIN` `block`, §0b) is the report's first lines, verbatim** — never retyped.

**The two anchor links are mandatory and come first after the ⏱ block — always.** Not "when convenient", not "when the report is long", not "unless the verdict is Done". A `/supervisor:worker-drive` report without them is incomplete, because the operator's first question on reading any report is *which* task this was, and a bare title is not clickable.

Resolve them for real — `vault-cli task get` / `goal get` for the actual filenames, never a hand-written plausible path. Percent-encode spaces as `%20` and slashes as `%2F`, drop the `.md`. Drop only the `🎯 Goal:` line when the task genuinely has no parent goal (never write "none"); if there is no task anchor at all, write `📌 No task anchor — <reason>` rather than omitting the line.

Observed 2026-09-05 on run #2 of this command's first deployment: a complete, correct report shipped with both lines missing — the shorter the report, the likelier they get trimmed.

Two further shapes seen the same day, both wrong:

- **A theme is not a goal.** One report emitted `🎯 Goal: [Leverage Autonomous Agents](…/21 Themes/…)`. A theme is the goal's ancestor, not a substitute for it. Never promote a theme, objective, or vision into the `🎯 Goal:` slot to avoid an empty line.
- **Never write "none".** Another emitted `🎯 Goal: none linked — themes: a theme wikilink only`. When the task has no parent goal, **omit the line entirely** — an explicit "none" is the failure the rule names, not a compliant substitute for it. (`📌 Task:` is the opposite: it is never omitted; with no anchor it becomes `📌 No task anchor — <reason>`.)

Measured across the 93 `/supervisor:worker-drive` runs that loaded this file after the 2026-09-05 fix (sampled 2026-09-07, chronological, no skipping): naming the shapes cut them but did not eliminate them. Both goal-slot shapes recurred — 2 literal `none`, 2 theme-promotions, 4/93 total — and the original whole-panel omission is now the dominant residual at 8/93, concentrated in short runs and mid-drive turns that ended without a closer.

So treat the anchor block as the report's first composed lines — directly under the pasted ⏱ block — written before the body exists — not as a header appended once the report is finished. Every shape above is a variant of the same failure: the panel was composed last, and last is when it gets dropped or improvised. If a `/supervisor:worker-drive` turn ends at all — including one that stops early, hands back a wait, or reports no change — it ends with `📌`.

```
⏱ Window … (the script's block, verbatim)

🎯 Goal: [<name>](obsidian://open?vault=<V>&file=23%20Goals%2F<file>) — <n>/<m> SC · <n>/<m> subtasks · <binding constraint>
📌 Task: [<name>](obsidian://open?vault=<V>&file=24%20Tasks%2F<file>) — <phase>, session <id-prefix>

State:            <working 🟢 | external 🟡 | decision 🔵 | closed ⚪ | unclassified — reason> · <signal>
Waiting on:       <named wait — ALIVE · progressing | stalled | DEAD — or `nothing`>
Task done?:       <status + phase read from disk this run · <n>/<m> boxes open>
How you can help: <the one thing only the operator can do — or `nothing, I'm driving`>
Recommend:        <one action>

PROBLEM: <one line — the outcome this task exists to produce, in plain terms, no identifiers>

OPEN (<n> items, <n> actionable):
| # | Open item | Blocked by | Unlock lever | Owner | Now? |
|---|---|---|---|---|---|
(only rows that still matter after this run)

DROVE (<n> actions, run #<runs> on this task):
- <action> → <observed result>
- <action> → <observed result>

STOPPED: <one of the five hard stops, named>
Blocker:  <name> — count <n>, rung <n>  (or: none)
Levers tried this run: <lever> · <lever>
Criteria audit: <n> unfinishable-by-construction found — <verdict>  (omit if not run)

Remaining:  <n steps, ~n turns> — <step · step · step>
ETA:        <duration + projected clock time + basis | n/a — Claude-side | unknown — why>

👤 You: <one of the six verbs — see the global closer rule>
⏰ Next:  <concrete trigger: actor/mechanism, never a bare id, never "soon">
```

Short path (§0b): no `OPEN` table — `Next open box: <box>` instead.

**The five header lines are mandatory, in this order, on every report.** Each value comes from this run — the status from disk, the wait from §0b's evidence — never from memory. `State:` is one of `working` 🟢 / `external` 🟡 / `decision` 🔵 / `closed` ⚪ (else `unclassified — <reason>`), names the signal read this run, and the closer icon follows it (`docs/worker-session-states.md`). `How you can help` names a real hard stop (§ The contract) or says `nothing`; never invent a chore.

`PROBLEM:` is not decoration. A run can be accurate in every line and still leave the operator unable to say what the task is *for* — every other field in the shape reports state, and none restates purpose. Observed 2026-09-14: after hours of correct reports on one task, the operator asked *"what problem we try to solve"* and then *"u lost me"*. Write it in plain terms — no identifiers, no paths, no acceptance-criteria vocabulary: the sentence you would say out loud to someone who had never seen the task.

**Arm the answered-card watcher before you end the turn.** A `👤 You:` line is a real question: the `Stop` hook mints a board card for it — waiting verbs (`pick —`, `you run:`, `review:`) mint without `📣 Board:`, rule home `~/.claude/hooks/attention-log.py` — `producer_id` *this* session. The operator may answer from the board; nothing wakes this session when they do, so arm:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/answered-watch.py
```

It emits `ANSWERED <item-id>` when the operator answers a card **this session posted** (`producer_id`), so a peer's card never fires it. **On `ANSWERED`, run `attention-ask.py poll <item-id>` and act immediately.** Re-arm on expiry as §0b does.

Do **not** arm it when `👤 You:` reads `nothing` or `later (on …)`: the hook records those as `idle`, so there is no card. A manager's feed watcher is **not a second doorbell** — a raise vs an answer. Reading the answer — `ANSWERED:` releases; `NOT_OPERATOR_ANSWERED:`, `DECLINED:` and `HIDDEN` do not — has one home, `scripts/answered-attribution.py`.

Then write the updated drive state. If a watcher was armed, name it in `⏰ Next:`.

### Speak the outcome

Headline only: what changed and what stopped you.

**Gate on voice mode, not on tool presence.** `/tts-mcp:voice` is the sole authority on spoken-output volume, and voice is off until someone invokes it:

| Voice mode | What this command does |
|---|---|
| `on` / `narrate` | **Speak the verdict.** A verdict is an attention signal, spoken in every mode except `off` and `interview`. |
| `interview` | **Speak questions, not answers — the verdict stays on screen.** The tts-mcp playbook is explicit — *"⚠️ `interview` speaks questions, not answers"* — and its own enablement table marks the **Answers** column ❌ for this mode. ⚠️ **This row listed `interview` as a speak-the-verdict mode until 2026-09-24**, contradicting the artifact that owns the question; a session following it spoke verdicts in a mode whose contract says answers are screen-only. |
| `off`, or never invoked this session | Say nothing. Print one line under the panel: `🔇 voice off — /tts-mcp:voice narrate to hear verdicts` |
| `mcp__tts__say` tool absent entirely | Skip silently, no hint. |

**Detect the mode — do not assume it.** No tool exposes `/tts-mcp:voice`'s session state, so a command that reads "was voice invoked this session?" from memory will answer *off* and stay silent in exactly the sessions voice was turned on for. The TTS server's state endpoint carries the answer — every entry in `recent[]` is tagged with the `sender` that produced it:

```bash
# ⚠️ A spoken entry is tagged with the RESOLVED SESSION NAME, not the UUID, so the
# gate must grep the name — grepping $CLAUDE_CODE_SESSION_ID matched nothing in ANY
# session, and the gate silently read "voice off" in exactly the sessions voice was
# on for. Measured 2026-10-07: a call passing `sender: "<uuid>"` was stored under
# the session title, and of the 9 distinct senders in the buffer not one was a UUID.
# ⚠️ The UUID is matched too: the HTTP fallback below carries no
# X-Claude-Code-Session-Id header, and the server resolves a name ONLY from that
# header — so a verdict spoken through the fallback lands under the raw UUID.
# ⚠️ That second pattern is added ONLY when the id is non-empty. A spawned child
# is stripped of CLAUDE_CODE_SESSION_ID (`scripts/attention-ask.py:61`; `open.md`
# unsets it before spawning a manager), and the fallback interpolates the same
# empty variable into its body — so one child's fallback utterance lands as
# "sender":"" and a bare second pattern would then read non-zero in EVERY child,
# speaking in sessions that have never spoken.
# The name is matched verbatim: /state carries the registry name byte-for-byte,
# a leading "⚙ " included. Match literally (-F) — names carry regex metacharacters.
SESSFILE="${HOME}/.claude/sessions/${CLAUDE_PID}.json"
NAME="$( [ -n "$CLAUDE_PID" ] && [ -f "$SESSFILE" ] \
  && python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("name") or "")' "$SESSFILE" 2>/dev/null )"
if [ -z "$NAME" ]; then
  echo "🔇 voice unknown — no session name for CLAUDE_PID=${CLAUDE_PID:-unset} ($SESSFILE); not speaking"
else
  PATTERNS=(-e "\"sender\":\"$NAME\"")
  if [ -n "$CLAUDE_CODE_SESSION_ID" ]; then
    PATTERNS+=(-e "\"sender\":\"$CLAUDE_CODE_SESSION_ID\"")
  fi
  curl -s --max-time 5 http://127.0.0.1:12000/state | grep -cF "${PATTERNS[@]}"
fi
```

Non-zero → this session has spoken before, so voice is on → speak. Zero or unreachable → treat as off and print the hint line. `🔇 voice unknown` is a **third** outcome, not a flavour of zero: the session's own name could not be read, so the gate cannot decide either way and says so — an unreadable registry that still printed a bare `🔇 voice off` would be the original bug wearing a new hat. Observed 2026-09-16: the diagnose pass (then `/and`) ran twice in a session where the operator had voice on and stayed silent both times, because the gate had no way to decide; the operator had to ask for the verdict to be spoken, which is the failure the command exists to prevent.

Never speak in a session that has never spoken — that is the exact noise the skill exists to prevent. The hint line makes it discoverable without being noisy.

**What to speak** — the headline, never the panel verbatim:

- The verdict in words ("Verdict: wait" / "Nothing blocked, I can continue").
- WAIT → what fires, which watcher catches it, roughly when.
- ACT / WORKING → the single next action.
- HAND OFF → the exact command and its blast radius.
- DRIFTING → what drifted, and what the anchor was.
- A `👤 You:` fork → spell it out: "option one … option two … say one or two."

⚠️ **A voiced question is also a board card** — any `mcp__tts__say` containing `?` is paired with an `attention-ask.py post` within 60 s. Rule and detector: [`docs/voice-asks.md`](../docs/voice-asks.md).


**How to speak** — `/tts-mcp:voice` § Speaking playbook is authoritative; the minimum reproduced here because the skill may not be loaded when this command runs:

- Voice `ryan`, unless `/tts-mcp:engine` selected a non-qwen3 engine (voice and engine must match or the server 400s).
- **English always**, even when the user writes German.
- **Throwaway lead word** — `"Okay."` / `"So,"`. CoreAudio clips the first word; never let a content word lead.
- **Then a 2–4 word tag**, after the lead word, never first. Source in order: the Step 0 `📌 Task:` anchor → its parent goal → the repo or service. **A run frequently has no anchor** — then use a short description of the work (`"harness config"`, `"inbox triage"`). Never skip the tag; one server serves every session and an untagged utterance is noise.
- Terse, one idea per sentence. No markdown, URLs, paths, code, or hashes — describe them in words.
- Lead with the recommendation and say the word "recommended".
- Fire-and-forget: one `mcp__tts__say` call. Never poll `get_status`, never block on it.
- **Always pass `sender`** — `$CLAUDE_CODE_SESSION_ID`. Which value the next run's gate actually finds depends on the path, and it matches **either**: the `mcp__tts__say` tool replaces the value with this session's **name** (resolved from the session-id header the tool carries), while the HTTP fallback below sends no such header and so stores the raw UUID. A spoken verdict with no `sender` at all may not be attributable, so pass it on every call.

**If `mcp__tts__say` errors with `No such tool available`**, the session's MCP binding dropped — the server is fine and restarting it will not help. Fall through to HTTP so the verdict is still heard:

```bash
curl -s -X POST http://127.0.0.1:12000/say \
  -H 'Content-Type: application/json' \
  -d '{"text":"Okay. <tag> — <verdict headline>.","voice":"ryan","sender":"'"$CLAUDE_CODE_SESSION_ID"'"}'
```

Every rule above still applies to the fallback text.

**Never describe speech you did not send.** Writing "spoken now" or narrating the utterance in the reply is not the channel — if the tool was not called, the user hears nothing and the text is simply false. This command is the likeliest place for that failure: the spoken verdict is owed on every run, and the intent to speak gets written down instead of executed. Call the tool, or say nothing about speaking.

### Close with one modal — this run's only ask

**The run ends with exactly ONE `AskUserQuestion`, and it is the run's only one.** Everything this run wants a decision on goes into it: at most **4 options**, each **reversible**, each carrying the pick you recommend, and **every pick is executed in the same turn** the answer arrives. A run that ends on a report with its decisions scattered through the prose has handed the operator a reading task and called it a decision.

- **Production-touching, irreversible and criterion-rewording items never enter this modal.** They go as separate `approve:` lines, on a card this session posted itself (`attention-ask.py post`), with the consent token named on the line — a modal is a multi-select over cheap reversible work, and a blast radius is not a checkbox.
- ⚠️ **Never a second modal, and never one earlier in the run.** A question that arises mid-turn waits for the close; two modals in one run is the shape this command exists to remove.
- **Nothing to ask → no modal.** A run whose rows are all blocked, or all done, prints its report and stops. An empty modal is an interruption, not a closer.
- **One card per ask.** The modal mints its own; a waiting-verb closer mints one too. Never add a `📣 Board:` line or `attention-ask.py post` on top — arm `answered-watch.py` only after `attention-ask.py poll` confirms the card.
- ⚠️ **Why a modal here and a card on the manager tier is not an inconsistency.** Measured 2026-10-15: a one-shot cron due while a modal was open did **not** fire — it was deferred until the turn ended, and the control leg showed the same job firing the instant the session went idle. A *worker* ends its turn on the answer, so a modal costs it nothing. A *manager* round sits inside a loop whose next tick is a scheduled wakeup, so a modal there stalls its own schedule — which is why that tier posts to the attention board instead (`commands/steps/manager-gates.md`) and why this is the one place the modal shape is correct.

## Rules

- **Ship state changes, not descriptions.** A run that only reports has failed — an inventory is not an outcome.
- **Always open the report with the `🎯 Goal:` / `📌 Task:` links**, resolved via `vault-cli` and percent-encoded. Short reports and ⚪ DONE verdicts are exactly when they get dropped, and exactly when the operator needs them.
- **Inventory before acting.** Every run starts with the drive table; every open item gets a blocker and a lever, or it is `nothing` and gets driven now.
- **Push actionable rows to `TodoWrite`; append blocked rows to Progress only when the analysis changed.** The todo list is the live progress view — never write an interim checkpoint panel on top of it.
- **Never return WAIT on a blocker at count ≥ 3.** Bypass, scope cut, or handoff with a deadline — pick one and execute it.
- **Hunt levers before parking.** External ownership is a reason to be careful, not a reason to idle.
- **Never ask on a non-fork.** §4's ban list is absolute.
- **Challenge criteria on both axes.** Unfinishable-by-construction *and* faulty-premise (satisfiable but proves nothing). The criterion is the bug more often than the session is slow.
- **A split emits a well-formed task** — SC + Tasks + DoD + a Progress entry naming the split. Moving work without moving readiness is not a split.
- **Never tick a moved criterion.** Relocated ≠ met. Move the line out under `# Moved to [[<task>]]`, unchecked, or the completion gate passes on a false count.
- **Verify, never recall.** Every claim of done needs output from this run. §0b's liveness rules apply unchanged.
- **Never touch another session's work** — containers, branches, worktrees, task framing, prompts, and the PR levers on a PR they own (push, re-trigger, retry, merge, close). Report the finding to the manager instead.
- **Never end the session.** No wind-down, no `/vault-cli:session-close` unless the anchor is **terminal** on disk, verified this run — either `status: completed` with zero `[ ]`/`[/]` boxes, **or** `status: aborted`. An aborted anchor is equally terminal, and its open boxes are moot by construction, so requiring `completed` alone left a session with no closer at all; when the anchor is `aborted` rather than `completed`, say so plainly in the panel rather than presenting it as a clean completion. `hold` does **not** qualify — it is parked, not finished. Handed-off, parked, blocked and tidied all look done and are not — a `/supervisor:worker-drive` run that ends by offering session-close on an unfinished task has inverted its own purpose.
- **One goal per session.** Drive the anchor. Never drift to another goal's tasks, even if the daily note lists them.
- **Irreversible actions are never inside a menu.** Present alone, with blast radius, for explicit approval.
