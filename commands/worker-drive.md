---
description: Get a stuck session working again, or tell the operator exactly how to help. Diagnoses the session (waiting on what, task done, drift), then drives the anchored task until a hard stop. Takes no arguments. Absorbs the former /and. Bans WAIT as an answer after repetition, challenges unfinishable acceptance criteria, and never asks permission for non-forks.
allowed-tools:
  - Read
  - Grep
  - Glob
  - Bash
  - Edit
  - Write
  - Task
  - TodoWrite
  - Monitor
  - AskUserQuestion
  - mcp__tts__say
argument-hint: (no args — diagnose, then drive until a hard stop)
---

Finish the anchored task. Not report on it — **finish it**.

The user typed this because the session has been *correct* and *not done* at the same time. That is the failure this command exists for: every individual verdict was right, every wait was live, and after two days the task is still open. This command is both the instrument and the actuator: §0b diagnoses, §1–§6 drive. It replaces the operator's hand-typed restart prompt — *what are we waiting for? is the task completed? how can I help? what do you recommend?* — and every report answers those four questions first (§8).

**No arguments, by design.** One command, one behaviour: diagnose, then drive. There is no diagnose-only mode and no step cap — an option the operator has to remember is a second command in disguise.

**Your product is a state change, not a panel.** A `/supervisor:worker-drive` run that ends having only described the situation has failed, regardless of how accurate the description was.

## The contract

Run in a loop: **inventory everything still open (§1)**, then pick the highest-leverage *actionable* row, do it, observe, re-pick. The inventory is what keeps a single loud blocker from hiding five doable items. Continue until one of exactly **four hard stops**. Nothing else stops you.

| Hard stop | Test |
|---|---|
| **Credential / console / click** | The next action needs something only the user physically has (OAuth click, 2FA, a login, a Shift+Tab). |
| **Irreversible outward action** | Prod mutation, merge, release, delete, send. Needs explicit per-action approval. |
| **Genuine fork** | Two paths with *different objectives or scope* — not two sub-steps of one approved path. |
| **Done** | Every criterion met, or every remaining one provably belongs to someone else (§5). |

Not hard stops — drive straight through these: a defect discovered mid-work (fix it), a sub-step of approved work (do it), a mechanical write with evidence already in hand (write it), an unread file (read it), an ambiguous detail with a sane default (assume it, state the assumption, proceed).

**There is no action cap — the four hard stops are what end a run.** Measured 2026-09-05 across the first day of real use: reporting runs came in at 0, 2, 4, 8 and ~16 actions, and every one terminated on a hard stop, never on a budget. A fixed cap either does nothing or interrupts good work mid-stride; two of the longest runs that morning were the most productive.

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

Before inventorying, find out whether the session is actually stuck and on what. Three failures this catches, all observed: a **dead wait** (daemon exited, monitor timed out — reported 🟡 WAITING when the answer was act now), a **stalled wait** (alive, zero forward progress past its cycle), and **drift** (productive work on something that is no longer the anchor).

Verdict words used below: **ACT** — something is mine to do now; **WAIT** — a verified-alive, progressing wait with a watcher armed; **DRIFTING** — work left the anchor; **DONE** — the anchor reads terminal on disk. A WAIT then enters §2's repetition gate.

### Last progress, loop check, drift check

Before inspecting external waits, inspect **the session itself**. This is the trigger the user actually reacted to.

**Last progress** — name the last *meaningful* thing accomplished or established: a file changed, a test flipped, a root cause found, a decision made. Then:

```
Last progress:  <concrete thing> (<age, computed from the Step 0 `date -u`>)
```

Re-asking a question already answered, re-running an identical command, or re-reading the same file is **not** progress.

**Loop check** — if the last N turns produced no state change, say so plainly. A session repeating tool calls with no new information is stuck even when every wait is healthy.

**Anchor re-verify** — re-read the anchor's live status; never carry the panel's earlier line forward:

```bash
vault-cli task get "<anchor task>" status --output json   # + goal get for its parent
```

Terminal (`completed` / `aborted`) → the session's work is over: report ⚪ DONE, not another "nothing outstanding". Changed since the previous run → say so; a verdict repeated on a *changed* anchor is stale, not correct.

**Drift check** — compare what the last few turns actually did against the Step 0 anchor. If the work has moved off the anchored goal, that is `DRIFTING`: nothing is broken, nothing is blocked, and the session is still failing. Name what drifted and what the anchor was.

**Approach check** — recommend abandoning the current approach **only with evidence it is wrong** (a disproven assumption, a repeated failure with the same tell, a constraint discovered mid-work). Never speculatively. Absent evidence, the approach stands.


### Enumerate every open wait, verify liveness AND progress

List every wait this session believes is outstanding: `Monitor` watchers, `run_in_background` shells, subagents whose completion notification has not arrived, dark-factory daemons and exec containers, remote queues (k8s jobs, CI, PR review bots), and any "I applied X, waiting for it to take effect."

**Verify mechanically — command output from this run, not recollection:**

```bash
pgrep -fl 'dark-factory daemon' || echo "no daemon"
docker ps --format '{{.Names}}\t{{.Status}}' || echo "no containers"
dark-factory status 2>/dev/null | grep -E 'Daemon|Current|Queue|Containers' || true
kubectl<wrapper> -n <ns> get jobs 2>/dev/null | tail -5
gh pr checks <n> --json name,bucket 2>/dev/null || true
```

**`-f` matches the whole command line and `-l` prints it.** MCP processes on this machine carry `Authorization:` headers in their args, so widening the prescribed pattern to cover your own work (`pgrep -fl 'sentry-watcher|nuke'`) copies credentials into the transcript — observed 2026-09-13. Use PIDs only (`pgrep -f`) unless the pattern is narrow enough that nothing else can match; same caution for `ps … -o command=` in `session-close` § Phase 5. Print PIDs, then inspect one by PID if you need its identity.

**Axis 1 — liveness:**

- **ALIVE** — verified running; name the evidence (pid, container status, job `Running`, monitor task-id armed) and its **age**.
- **DEAD** — believed running, is not. The high-value finding. Treat as an action, not a wait.
- **UNVERIFIABLE** — no way to check from here. Say so explicitly rather than assuming ALIVE. A wait that cannot be proven alive is not a wait: verdict is ACT (verify it, or re-arm a watcher), never WAIT.

**Axis 2 — progress (only meaningful when ALIVE).** Name each wait's **terminal-state signal** — the thing that proves forward motion (a queue's `Completed` count, a monitor's fire, a review landing) — and confirm it fired within the expected cycle. For a wait on an **agent task**, progress is not enough — confirm the task is *eligible*: a non-empty `assignee`, a `phase` in the executor's trigger set, and budget left in the current `<phase>:<ref[:8]>` scope. A task at its trigger cap in an unchanged scope is skipped silently, and an empty `assignee` excludes it outright.

Single-shot delta is impossible, so **the diagnosis is stateful**. Read the previous run's snapshot, diff, then overwrite.

**The snapshot is per-session, never global.** This user runs a fleet of concurrent Claude sessions; a single shared file means every session clobbers the others' baseline, and the stall diff silently never works. Observed 2026-08-23 on this command's own first smoke test: a sibling session had overwritten the file 2.5 minutes earlier with its own unrelated wait.

Derive the session id from the scratchpad path already present in this session's environment (`.../<session-uuid>/scratchpad`), and key the file on it:

```bash
SNAP=~/.claude/state/diagnose-snapshot-<session-uuid>.json
cat "$SNAP" 2>/dev/null || echo "no previous snapshot"
```

If the session id cannot be determined, fall back to a slug of `pwd` — never to the bare unsuffixed name.

Schema — `~/.claude/state/diagnose-snapshot-<session-uuid>.json`:

```json
{
  "session": "<session uuid>",
  "checked_at": "<ISO8601 UTC>",
  "anchor_task": "<task file name>",
  "verdict": "<the verdict this run returned>",
  "verdict_streak": "<consecutive runs returning that same verdict on an unchanged blocker>",
  "waits": [
    {"name": "<wait>", "signal": "<terminal-state signal>", "value": "<count/oid/mtime>", "since": "<ISO8601 UTC>"}
  ]
}
```

Classify:

- **progressing** — the signal's `value` moved since the snapshot, or moved within the expected cycle.
- **stalled** — ALIVE with an **unchanged `value` for longer than one expected cycle**. A queue with a Running job and no `Completed` growth past ~2× nominal job duration is stalled, not busy.
- **first-seen** — no prior snapshot for this wait. Not classifiable as stalled yet; record it and say so.

Write the new snapshot at the end of the run (overwrite this session's file only; the old one has been consumed). Never read or write another session's snapshot.

Traps to check every time:

- A **timed-out monitor** is not a wait. Its window closed; nothing will fire. Re-arm or act.
- An **edit to a file another system owns** may have been reverted. Verify it is still on disk.
- **Liveness ≠ progress.** A crash-looping job is ALIVE.

### Who owns each live blocker

| Owner | How to recognise it | What is allowed |
|---|---|---|
| **Mine** | a process I started, a file I control, a command I can re-run | **Act now.** Never report this as waiting. |
| **Another session / person** | containers or branches named for other projects; a repo someone else is mid-change in | **Nothing.** Preempting it sabotages their run. Say so explicitly. |
| **External system** | queue at a concurrency cap, CI, review bot, rate limit, scheduled poll | Usually nothing. If a lever exists, name it *with its cost* (§3). |

Check before asserting — `docker ps` names carry their originating project prefix.


## Step 1 — Inventory the remaining work (the drive table)

**Before any action, enumerate everything still open.** This is the first move of every run, and it is what stops the session collapsing onto one loud blocker while five doable items sit unexamined — the failure that burned two days in `eed76c7d`, where the whole session funnelled onto a single fleet queue.

Derive the inventory from disk and this conversation, never from memory:

```bash
# `vault-cli task get` takes <task-name> <key> and returns ONE frontmatter field —
# it cannot dump sections. Called with a name alone it fails: "accepts 2 arg(s), received 1".
vault-cli task get "<task>" status --output json          # frontmatter, one key at a time
grep -nE '^\s*- \[[ /]\]' "<vault>/<tasks_dir>/<task>.md" # every open row, verbatim
sed -n '/^# Progress/,$p' "<vault>/<tasks_dir>/<task>.md" | tail -40  # what ALREADY happened
```

**Read the Progress log before driving any row — it is not optional and it is not the checkboxes.** Boxes say what is unticked; Progress says what was already *done*, and on a resumed, compacted, or peer-shared task those two disagree routinely. Acting on the boxes alone is how a finished step gets repeated. Observed 2026-09-11: a run read 14 open boxes and re-ran a `make apply` **against the prod cluster** that the Progress log recorded as already done an hour earlier by the same task's owner — then reported the duplicate to the operator as a reconstruction of "this session's own earlier work from a part of the conversation I no longer have in context," which was invented, not verified. The boxes were accurate; they simply are not a record of what happened.

Every unchecked `[ ]` / `[/]` in Success Criteria, Tasks, and Definition of Done is a row. Add rows for work established in conversation but not yet written to the file (that gap is itself a row, and it is almost always doable now).

Then build the triad — **one row per open item, three questions each**:

| # | Open item | Blocked by | Unlock lever | Owner | Now? |
|---|---|---|---|---|---|
| 1 | <the unchecked item, verbatim> | <specific blocker, or `nothing`> | <the exact action that clears it> | mine / other session / external / user | ✅ / ⛔ |

Rules for the table — each exists because its absence produced a stuck session:

- **`Blocked by: nothing` is the expected answer for most rows.** If every row shows a blocker, you are pattern-matching the loudest one onto the rest. Re-derive. A row is only blocked if you can name the specific thing and say how you verified it *this run*.
- **A blocker must be named concretely.** "the fleet", "the pipeline", "CI" are not blockers — `lockbox task 4fb20d55 has no current_job, executor OOMKilled 71×` is.
- **A blocker must be the task's to own.** Before recording one, point at the Success Criterion, Definition-of-Done item, or Task line that requires it. If no line names it, it is a **finding, not a blocker** — file it as a follow-up and drive on. Check also whether the operator already ruled on it: a standing decision is not a defect to re-argue (§3's first lever). Note that a blocker can pass both rules above and still fail this one — specific, verified, concrete, and simply not this task's. Observed 2026-09-18: a spec parked at `verifying` was declared a completion blocker and carried through four commands; no criterion named it (the SC/DoD text mentioned that spec only as *approved*), and the operator had already ruled three days earlier to leave 114 such specs parked. Both disproofs sat in files already read.
- **Every blocked row needs a lever, even a bad one.** "wait" is not a lever. If the honest answer is that no lever exists, the row is a §5 candidate — an item this session cannot finish by construction.
- **Owner is per row, not per session.** One external-owned row does not make the task blocked. This is the single most valuable output of the table.
- **Checkbox drift is a row.** Items whose evidence is already in Progress but whose box is unticked go in the table and get ticked this run under §4 — no question asked.

Then **sort by `Now? = ✅` and drive those first**, cheapest-first within that set. Blocked rows go to §2's ladder; they never gate the actionable ones.

### Persist the table — TodoWrite live, Progress durable

Two writes, different lifetimes. Do both as the last step of §1, before driving anything.

**Actionable rows → `TodoWrite`, immediately.** One todo per `✅` row, phrased as the *action*, not the criterion. Tick each as you drive; drop any that turns out blocked mid-run rather than leaving it pending.

This is the operator's live progress view — it renders continuously in their UI, one line per item, updated as you work. **It is therefore the answer to "the operator can't see what I'm doing", and it replaces any interim status report.** Do not invent a checkpoint panel every N actions: the todo list already is one, it costs nothing, and it does not interrupt the run. The `no interim panels` rule below stands unchanged.

**If `TodoWrite` is not present in the session, say so in your first turn and fall back to one short progress line every ~5 actions.** The no-interim-panels rule assumes the todo list is carrying that load; with no list *and* no panel, the operator sees nothing between the run starting and the final report. Observed 2026-09-06: two `/supervisor:worker-drive` runs totalling ~32 actions produced no live view at all, and the gap was visible only because the run happened to mention it. Silence is the failure this section exists to prevent — the fallback is cheap, the omission is not.

**Blocked rows → a dated Progress entry on the task file, gated on change.** The blocker/lever/owner triad is the one thing the table produces that the task file has no home for today — and it is exactly what gets re-derived from scratch every session (`eed76c7d` re-diagnosed the same fleet queue five separate times across two days). Append, never rewrite:

```
- <date>: /supervisor:worker-drive run #<n> — blocked: <item> · blocker: <specific, verified this run> · lever: <what would clear it> · owner: <who>
```

**Gate the write on change.** Only append when the blocker, lever, or owner differs from the last such entry. Four runs in forty minutes is a real observed rate, and four identical Progress entries are noise that buries the one that mattered. Unchanged → write nothing and say so in the report.

**Peer safety.** If the task's `claude_session_id` does not name this session, the Progress append is the *only* write permitted — never the title, Success Criteria, Tasks, DoD, or Summary. Contradictory evidence goes in the appended entry plus a `SendMessage` to the owner.

**Guard against the table becoming the deliverable.** Build it once per run, cap it at the genuinely open items, and spend at most one turn on it. A `/supervisor:worker-drive` run that produces a beautiful inventory and zero state changes has failed exactly as hard as one that produced a status panel. Print the table only in the final report (§8), and only the rows that still matter after you drove.

If the table comes back with **zero** `✅` rows, that is normally the strongest signal in this command: either a blocker is misdiagnosed (§3) or a criterion is unfinishable by construction (§5). Go there — do not park.

**One carve-out: a watch task.** A task whose *entire purpose* is to observe an external event — created by a §5 split, or whose criteria are all externally-owned by construction — legitimately reports zero actionable rows every single run. Its criteria were deliberately put there by an earlier split precisely because nothing local can satisfy them. Re-litigating them is the split creating work for itself: run N moves a criterion out, run N+1 challenges it again, forever.

Recognise it by the task's origin (a `# Moved to` section pointed here, or a Progress entry naming the split) plus every row owned `external` / `other session`. Then the correct run is short and ends the same way each time:

1. Verify the watcher is actually armed and alive — a watch task with no running watcher is 🔵 READY with `approve: <re-arm cmd>`, never 🟡 WAITING.
2. Confirm the observable has not already fired (check it, don't assume).
3. **Verify the producer can still fire it.** The watcher proves *you* are listening; it says nothing about whether the thing you are waiting for is still coming. Check the upstream that must act — the queued job, the emitted task, the pipeline stage — and confirm it is progressing, not parked. A watch whose producer is stuck is not a wait; it is a silent failure wearing a wait's clothes. Then confirm the task is **eligible**, not merely present: a non-empty `assignee`, a `phase` in the executor's trigger set, and budget left in the current `<phase>:<ref[:8]>` scope. A task at its trigger cap in an unchanged scope is skipped silently — it is not parked, it is excluded.
4. Report and stop. **No criteria challenge, no §5 audit, no split.**

`eed76c7d` ran four times in forty minutes; at that rate an un-carved-out watch task churns its own file. Nothing to drive is the correct answer here, and saying so costs one turn.

**The carve-out expires at run 3.** It suppresses §2 for the *criteria*, never for the producer. Increment a blocker named for the awaited event on every carved-out run; once its `count` reaches 3 the §2 rung applies in full — WAIT is banned, hunt the lever (§3), treat the pipeline itself as the defect. "Nothing to drive" is allowed indefinitely only while the producer is provably alive.

Observed 2026-09-05: a lockbox Go-bump watch ran 10 times over ~8h, each run correctly reporting zero rows by the carve-out as written. The producer had been dead since the previous day — a dropped Kafka event the watcher's sha-dedup would never re-emit. Both real levers were found within one turn of the user asking "why does it take sooo long? stuck?", never by the command's own verdict, because the carve-out is the one path in `/supervisor:worker-drive` that never reaches §3. The counter proves it: `runs: 10`, yet the earliest blocker `first_seen` postdates session start by an hour — the first five runs recorded no blocker at all, so the ladder had nothing to climb.

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
- **Force / trigger** — a poll interval you can skip (`/github-*-trigger`, an admin endpoint, a `--force` flag).
- **Bypass** — reach the same end state by another path (do by hand what the automation would do; admin-merge; direct patch; **replay the trigger through the service's own smoke/seed tooling** — a seed/override flag drives the real pipeline with a controlled input, proving routing/delivery/dedup now; see [[Prove an External-Event Pipeline With a Controlled Replay]]).
- **Unstick the blocker itself** — the queue isn't slow, something upstream is broken. In `eed76c7d` the "opaque fleet queue" was an executor OOM-crashloop; a `rollout undo` cleared 10 hours of "waiting."
- **Check whether the answer already exists** — before waiting on a job to *produce* evidence, search for evidence already on disk: a prior run of the same config, a sibling artifact, a cached report. A comparator does not have to be freshly generated to be valid, only *comparable*. Ask what would make an existing artifact non-comparable, and whether that difference is actually load-bearing. Observed 2026-09-14: a session waited ~40 min on a US100 baseline backtest while a completed run with the same config and the same end date sat one API call away, differing only by a 14-day start offset on an 8.7-year window. Two diagnose-only runs and one drive run all rated the wait healthy — the lever was found only when the operator said "we are stuck".
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

**First check whether the task was recently and explicitly resolved by someone else** — a status/phase change or Progress entry dated today by another actor, or a resolution the operator picked off `session-close`'s menu (complete / defer / hold / leave-open). If one exists, STOP: name the split, its reason, and the decision it would override, and let the operator choose. Ownership in `claude_session_id` grants write access, not authority to reverse a stated decision — see [[Write Access Is Not Decision Authority]].

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

Correcting a criterion is a framing change: allowed when `claude_session_id` names this session, otherwise an appended Progress entry plus a `SendMessage` to the owner.

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

**The two anchor links are mandatory and come first — always.** Not "when convenient", not "when the report is long", not "unless the verdict is Done". A `/supervisor:worker-drive` report without them is incomplete, because the operator's first question on reading any report is *which* task this was, and a bare title is not clickable.

Resolve them for real — `vault-cli task get` / `goal get` for the actual filenames, never a hand-written plausible path. Percent-encode spaces as `%20` and slashes as `%2F`, drop the `.md`. Drop only the `🎯 Goal:` line when the task genuinely has no parent goal (never write "none"); if there is no task anchor at all, write `📌 No task anchor — <reason>` rather than omitting the line.

Observed 2026-09-05 on run #2 of this command's first deployment: a complete, correct report shipped with both lines missing — the shorter the report, the likelier they get trimmed.

Two further shapes seen the same day, both wrong:

- **A theme is not a goal.** One report emitted `🎯 Goal: [Leverage Autonomous Agents](…/21 Themes/…)`. A theme is the goal's ancestor, not a substitute for it. Never promote a theme, objective, or vision into the `🎯 Goal:` slot to avoid an empty line.
- **Never write "none".** Another emitted `🎯 Goal: none linked — themes: [[Administration]] only`. When the task has no parent goal, **omit the line entirely** — an explicit "none" is the failure the rule names, not a compliant substitute for it. (`📌 Task:` is the opposite: it is never omitted; with no anchor it becomes `📌 No task anchor — <reason>`.)

Measured across the 93 `/supervisor:worker-drive` runs that loaded this file after the 2026-09-05 fix (sampled 2026-09-07, chronological, no skipping): naming the shapes cut them but did not eliminate them. Both goal-slot shapes recurred — 2 literal `none`, 2 theme-promotions, 4/93 total — and the original whole-panel omission is now the dominant residual at 8/93, concentrated in short runs and mid-drive turns that ended without a closer.

So treat the anchor block as the report's *first* emitted lines, written before the body exists — not as a header appended once the report is finished. Every shape above is a variant of the same failure: the panel was composed last, and last is when it gets dropped or improvised. If a `/supervisor:worker-drive` turn ends at all — including one that stops early, hands back a wait, or reports no change — it ends with `📌`.

```
🎯 Goal: [<name>](obsidian://open?vault=<V>&file=23%20Goals%2F<file>) — <n>/<m> SC · <n>/<m> subtasks · <binding constraint>
📌 Task: [<name>](obsidian://open?vault=<V>&file=24%20Tasks%2F<file>) — <phase>, session <id-prefix>

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

STOPPED: <one of the four hard stops, named>
Blocker:  <name> — count <n>, rung <n>  (or: none)
Levers tried this run: <lever> · <lever>
Criteria audit: <n> unfinishable-by-construction found — <verdict>  (omit if not run)

Remaining:  <n steps, ~n turns> — <step · step · step>
ETA:        <duration + projected clock time + basis | n/a — Claude-side | unknown — why>

👤 You: <one of the six verbs — see the global closer rule>
⏰ Next:  <concrete trigger: actor/mechanism, never a bare id, never "soon">
```

**The four header lines are mandatory, in this order, on every report.** They are the operator's four restart questions; answering them first means the operator never has to type them. Each value comes from this run — the status from disk, the wait from §0b's evidence — never from memory. `How you can help` names a real hard stop (§ The contract) or says `nothing`; it never invents a chore.

`PROBLEM:` is not decoration. A run can be accurate in every line and still leave the operator unable to say what the task is *for* — every other field in the shape reports state, and none restates purpose. Observed 2026-09-14: after hours of correct reports on one task, the operator asked *"what problem we try to solve"* and then *"u lost me"*. Write it in plain terms — no identifiers, no paths, no acceptance-criteria vocabulary: the sentence you would say out loud to someone who had never seen the task.

Then write the updated drive state. If a watcher was armed, name it in `⏰ Next:`.

### Speak the outcome

Headline only: what changed and what stopped you.

**Gate on voice mode, not on tool presence.** `/tts-mcp:voice` is the sole authority on spoken-output volume, and voice is off until someone invokes it:

| Voice mode | What this command does |
|---|---|
| `on` / `narrate` / `interview` | **Speak the verdict.** A verdict is an attention signal, spoken in every mode except off. |
| `off`, or never invoked this session | Say nothing. Print one line under the panel: `🔇 voice off — /tts-mcp:voice narrate to hear verdicts` |
| `mcp__tts__say` tool absent entirely | Skip silently, no hint. |

**Detect the mode — do not assume it.** No tool exposes `/tts-mcp:voice`'s session state, so a command that reads "was voice invoked this session?" from memory will answer *off* and stay silent in exactly the sessions voice was turned on for. The TTS server's state endpoint carries the answer — every entry in `recent[]` is tagged with the `sender` that produced it:

```bash
curl -s --max-time 5 http://127.0.0.1:12000/state | grep -c "\"sender\":\"$CLAUDE_CODE_SESSION_ID\""
```

Non-zero → this session has spoken before, so voice is on → speak. Zero or unreachable → treat as off and print the hint line. Observed 2026-09-16: the diagnose pass (then `/and`) ran twice in a session where the operator had voice on and stayed silent both times, because the gate had no way to decide; the operator had to ask for the verdict to be spoken, which is the failure the command exists to prevent.

Never speak in a session that has never spoken — that is the exact noise the skill exists to prevent. The hint line makes it discoverable without being noisy.

**What to speak** — the headline, never the panel verbatim:

- The verdict in words ("Verdict: wait" / "Nothing blocked, I can continue").
- WAIT → what fires, which watcher catches it, roughly when.
- ACT / WORKING → the single next action.
- HAND OFF → the exact command and its blast radius.
- DRIFTING → what drifted, and what the anchor was.
- A `👤 You:` fork → spell it out: "option one … option two … say one or two."

**How to speak** — `/tts-mcp:voice` § Speaking playbook is authoritative; the minimum reproduced here because the skill may not be loaded when this command runs:

- Voice `ryan`, unless `/tts-mcp:engine` selected a non-qwen3 engine (voice and engine must match or the server 400s).
- **English always**, even when the user writes German.
- **Throwaway lead word** — `"Okay."` / `"So,"`. CoreAudio clips the first word; never let a content word lead.
- **Then a 2–4 word tag**, after the lead word, never first. Source in order: the Step 0 `📌 Task:` anchor → its parent goal → the repo or service. **A run frequently has no anchor** — then use a short description of the work (`"harness config"`, `"inbox triage"`). Never skip the tag; one server serves every session and an untagged utterance is noise.
- Terse, one idea per sentence. No markdown, URLs, paths, code, or hashes — describe them in words.
- Lead with the recommendation and say the word "recommended".
- Fire-and-forget: one `mcp__tts__say` call. Never poll `get_status`, never block on it.
- **Always pass `sender`** — `$CLAUDE_CODE_SESSION_ID`, the same value the detection step above greps for. It is what the next run's detection finds; a spoken verdict without it is invisible to the gate that decides whether to speak at all, so the session goes quiet after its first utterance.

**If `mcp__tts__say` errors with `No such tool available`**, the session's MCP binding dropped — the server is fine and restarting it will not help. Fall through to HTTP so the verdict is still heard:

```bash
curl -s -X POST http://127.0.0.1:12000/say \
  -H 'Content-Type: application/json' \
  -d '{"text":"Okay. <tag> — <verdict headline>.","voice":"ryan","sender":"'"$CLAUDE_CODE_SESSION_ID"'"}'
```

Every rule above still applies to the fallback text.

**Never describe speech you did not send.** Writing "spoken now" or narrating the utterance in the reply is not the channel — if the tool was not called, the user hears nothing and the text is simply false. This command is the likeliest place for that failure: the spoken verdict is owed on every run, and the intent to speak gets written down instead of executed. Call the tool, or say nothing about speaking.


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
- **Never touch another session's work** — containers, branches, worktrees, task framing, prompts.
- **Never end the session.** No wind-down, no `/vault-cli:session-close` unless the anchor reads `status: completed` on disk, verified this run, with zero `[ ]`/`[/]` boxes. Handed-off, parked, blocked and tidied all look done and are not — a `/supervisor:worker-drive` run that ends by offering session-close on an unfinished task has inverted its own purpose.
- **One goal per session.** Drive the anchor. Never drift to another goal's tasks, even if the daily note lists them.
- **Irreversible actions are never inside a menu.** Present alone, with blast radius, for explicit approval.
