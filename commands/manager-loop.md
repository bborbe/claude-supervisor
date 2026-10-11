---
# No `name:` field, deliberately — and this comment exists because a review pass read
# one in. A command takes its name from its filename; the frontmatter rule for
# `commands/*.md` (`agent-cmd/command-frontmatter`) requires `description`,
# `allowed-tools` and `argument-hint` and nothing else. `name:` is required for
# `agents/*.md` (`agent-cmd/agent-frontmatter`) and `skills/<name>/SKILL.md`. All 22
# commands in this repo omit it, so adding it to one would be the inconsistency.
description: Run the manager loop for ONE topic OR ONE goal — watch its declared set + task files (default 15 min, `--interval`), detect problems, TTS the human (voice-mode gated), print a status table, recommend new sessions and session closes. Name via $1, or detected from the session when omitted; the branch (topic vs goal) is auto-detected, never guessed. Per the vault's Manager Session runbook.
allowed-tools:
  - Task
  - Read
  - Edit
  - Monitor
  - AskUserQuestion
  - Bash(grep:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - Bash(awk:*)
  - Bash(mkdir:*)
  - Bash(vault-cli:*)
  - Bash(pgrep:*)
  - Bash(ps:*)
  - Bash(stat:*)
  - Bash(find:*)
  - Bash(date:*)
  - Bash(python3:*)
  - Bash(echo:*)
  - Bash(gh:*)
  - Bash(git tag:*)
  - Bash(kubectl*:*)
  - Bash(wezterm cli spawn:*)
  - Bash(wezterm cli list:*)
  - Bash(wezterm cli get-text:*)
  - ListAgents
  - SendMessage
  - ScheduleWakeup
  - CronCreate
  - mcp__tts__say
  - mcp__supervisor__spawn_agent
  - mcp__supervisor__list_agents
  - mcp__supervisor__agent_status
  - mcp__supervisor__answer_permission
  - mcp__supervisor__await_permission
argument-hint: "[goal|topic] [--interval <N>m] (subject detected when omitted; interval defaults to 15m)"
---
⚠️ **A voiced question is also a board card** — any `mcp__tts__say` containing `?` is paired with an `attention-ask.py post` within 60 s. Rule and detector: [`docs/voice-asks.md`](../docs/voice-asks.md).

Manager-loop slash command — the **narrow / deep** layer. Per the vault's Manager Session runbook.

Rationale, measured evidence and incident history for the condensed rules below live in the vault runbook: the Manager Session runbook § Command rationale (moved from /manager-loop). This file carries only what a sweep executes.

One manager per topic: owns ONE topic's declared goal set (e.g. the *Sentry* topic, the *notification* topic), read from that topic's page. The fleet manager (wide layer) may run above. **Enforced, not merely asserted** — Resolution step 6 confirms no manager already owns the name before arming, and stops on a hit.

⚠️ **Never invoke this command from a worker session.** A worker carries a task; a manager carries a topic or goal plus a loop. Starting a manager is a human act, in a session created for it. Rule: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles — who may start what.

## Router — read a step file only when its When holds

| # | Step | When | File |
|---|---|---|---|
| 0 | Arm the tick | manager start, before the first sweep | *inline — the arm step below* |
| 1 | Resolve the subject and the tracked set | manager start; the declaration read re-runs every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-resolve.md` |
| 2 | Take the round's readings | every tick, **before** the sweep dispatch | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-readings.md` |
| 3 | Sweep | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-sweep.md` |
| 4 | Classify the rows | every tick, after the sweep | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-classify.md` |
| 5 | Drive | every tick, after the table renders | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-drive.md` |
| 6 | Spawn on the handover | the drive leg returned `To open` / `To resume` rows | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-spawn.md` |
| 7 | Repair | the `Audit` block carried `repairable` rows | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-repair.md` |
| 8 | Gate relay | the round has `waiting-on-human` or `waiting-approval` rows | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-gates.md` |
| 9 | Under-target offer | live workers below target **and** no ready row **and** the ask not declined | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-under-target.md` |
| 11 | Fleet-manager handoff | a fleet-loop session is on `ListAgents` and the sets changed | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-handoff.md` |
| 12 | Tail — `noop` and re-arm | end of every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-tail.md` |
| 13 | Self-improvement | before the session ends, or a sweep exposes a gap | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-self-improvement.md` |
| 14 | Unowned check | before declaring any member task unowned | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-unowned.md` |

Read a step file **only when its `When` holds** — and if a step's text is not in context (after a compaction, say), re-read it rather than assuming it survived. The rules below are **inline because they must hold whether or not a step file was read**: a rule that lives only in a step file is skipped whenever that file is not read.

⚠️ **Opening a worker is bound by `docs/fleet-surface.md` § Spawn a worker item 6** — the mode is passed in both directions (`mode: headless` → `interactive=false`, `mode: interactive` → `interactive=true`). This file grants the tool but does not open; the binding and the call live in `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-spawn.md`.

## Interval — the tick and its floor

`--interval <N>m` sets the tick. Default **15m**; floor **5m** — the value this command hardcoded before the flag existed, so no previous cadence becomes unreachable. A value below the floor is **clamped up and announced** (`⚠️ --interval 2m is below the 5m floor — clamped to 5m`); never rejected, because a loop that refuses its own interval stops silently — the failure this flag exists to reduce.

Parse it out of `$ARGUMENTS` before resolving the subject: `--interval` is orthogonal to the positional `[goal|topic]`, so a session may carry either, both, or neither.

**The tick is one number, used twice.** Pass the clamped seconds to `ScheduleWakeup`'s `delaySeconds`, and the *same* seconds to `manager-liveness.py --arm --interval` (step 3). ⚠️ **The seconds half assumes the `ScheduleWakeup` spelling** — the floor-5m clamp and the clamp announcement are all expressed in **seconds**, which a `cron` expression cannot carry, so nothing spells what to pass to `--arm` on a bare `CronCreate` arm. The `--arm` record itself is written **unconditionally**, in the same motion after the arm call: a bare `CronCreate` arm forfeits the `noop` placeholder, not the record. ⚠️ That record is the **fallback** basis only — `manager-liveness.py` prefers the observed median arm-to-arm period once ≥4 arms exist (`observed_interval()` → `2 * observed + SLACK`). Never present the declared value as the authoritative period: a tick costs real runtime on top of the delay, and the declared value alone once produced five false lapsed-manager pushes in 32 minutes.

⚠️ **The flag sets no cap of its own — the floor is the only clamp `--interval` applies.**

- **A headless worker's park does not cap the interval — the manager chooses.** A headless permission park auto-denies after 15 minutes (`skills/supervising-workers/SKILL.md`), so a tick longer than that window can leave a worker parked past it and resume it with a denial it did not earn. That is now a **choice the manager accepts, not a cap forced on it**: keep the long tick and accept the auto-deny, or run the wrangler auto-policy (`/supervisor:manager-drain` → the `supervisor:manager-wrangler` agent) so parks are answered without the manager's tick. ⚠️ **The wrangler is a ONE-SHOT loop — it stops as soon as its queue empties**, so a long tick behind a live headless worker needs it **run repeatedly for the life of the tick**, or a standing form; a park raised after it exits auto-denies at the 15-minute mark. Measured 2026-10-05: it answered 10 parks, then stopped while the worker was still mid-flow.
- **The attention watcher is not governed by this flag at all.** The **tick's** `--interval` sets the tick; the watcher `Monitor` stays on its own arm — with its **own** poll period, the **filter's** `--interval` — bounded at 30 min and re-armed on expiry. ⚠️ **At a long interval the watcher still wakes the manager once per watcher event — that is intended, not a leak:** the watcher is the push channel and the tick is the poll, so an hour-long tick behind a live doorbell is exactly the shape this flag exists to make affordable. Never "fix" the mismatch by stretching the watcher. ⚠️ **The arm's rule and its traps live in `65 Runbooks/Manager Session.md` § Cadence mechanics — read them there rather than re-deriving them.** It is scoped **twice**, and neither half is optional: the **section** scope, because the feed's other four sections are not gates, and the **ownership** scope, because the filter's keep-set is wide by design. An unscoped arm wakes the manager on every peer's `Rendered panels` closer — a line the feed itself labels *"a closer line, not a parked gate"*. Measured 2026-10-01 on the Manager Layer arm: **8 wakes in ~55 minutes, 1 actionable**.

## Manager contract — a manager manages, it does not build

The manager **manages; it does not build**. It never performs the topic's *work*: no code edits, no repo/PR/k8s verification, no investigation, no real debugging — not even small ones. (Owner rule, 2026-09-11; boundary restated by the operator 2026-09-18.)

- **The line is work vs management — not read vs write.** Management writes are the manager's own — tasks, task/goal pages, `status`/`phase`, stale stamps, topic-page scope and write-back — via `Edit` / `vault-cli task set`. Code, verification, investigation, debugging and the fix go to a worker. **Delegate the work, not the bookkeeping.**
- ⚠️ **Precedence when a worker already holds the row — this paragraph governs, and it splits by row state.** Three other restatements of the ownership rule give the opposite instruction on a worker-owned row, and until now none of the four named the others: `commands/worker-drive.md` § Peer safety (*"If the task's `claude_session_id` does not name this session, the Progress append is the *only* write permitted — never the title, Success Criteria, Tasks, DoD, or Summary"*) and its § Challenge the acceptance criteria closer (*"Correcting a criterion is a framing change: allowed when `claude_session_id` names this session, otherwise an appended Progress entry plus a `SendMessage` to the owner"*); the vault `CLAUDE.md` § Reconcile On Edit rule (*"Put contradictory evidence in an appended entry plus a `SendMessage` to the owner, never in an edit to their framing"* — whose source is `.claude/claude-md-rules/cr-reconcile-on-task-edit.md`, the file to edit, never the generated `CLAUDE.md`); and `65 Runbooks/Manager Session.md` § Command rationale. **The split, stated here so neither file has to be inferred from the other:** on a row the manager owns, **or** one carrying no live `claude_session_id`, a manager-authored Success Criteria / Tasks / DoD / Summary edit is management and lands directly. On a row a **live worker owns**, the manager **proposes** — an appended `# Progress` entry plus a `SendMessage` to the owner — and the **worker lands the edit on its own anchor**. *Framing* is the axis, not the file: `status` / `phase` / dates / stamps stay the manager's own in both states, which is why clearing a stale `claude_session_id` on another session's task remains a management write.
- ⚠️ **A worker's pane answer and a manager's file answer — the worker reports first, and the operator's answer wins.** A worker answers the operator through `AskUserQuestion` in its own pane while a manager can answer the same questions by editing the file, and from the artifact alone neither party can tell whether the answerer was the operator or the harness — so a manager's edit can land on top of an operator answer with neither side knowing. **The convention (operator decision 2026-10-02 — a convention, deliberately not a mechanism):** a worker that has put a question to the operator **reports the answers back to its manager** — the answers themselves, plus the fact that they were answered — **before** the manager edits the file, and it does so by `SendMessage` rather than by waiting for a reply. **Which actor may override which:** the **operator's answer wins** — a manager that has already edited must not re-edit over an answer the worker reports; it reverts its own edit and re-proposes through the precedence path above. Where two answers genuinely conflict and neither is the operator's, the **row owner's** stands. ⚠️ **This is a convention and nothing enforces it** — a manager that edits before the report arrives can still land an override, which is exactly why the worker reports first rather than waiting. It exists to make the disagreement *visible*, not to make it impossible.
- ⚠️ **An empty `pending_permissions` is not evidence that no gate is outstanding — take the worker's own gate reading before you direct it.** `mcp__supervisor__pending_permissions` carries **this server's headless parks only**, so a row it reports empty can still be an **interactive tab worker** parked on an `AskUserQuestion` in its own pane, with the operator's answer already given. The asymmetry and its two carriers have their single home in `commands/fleet-workers.md` (its tab-worker reading) and `llms.txt` § *How a worker's permission prompt is answered* — read them there and never restate them here. **What this clause adds is the manager-side rule those carriers leave out:** before a `SendMessage` that **directs** a worker — a scope decision, a criterion shape, a subtask instruction — take that worker's **own** gate reading, for every worker, not only the ones you suspect. **Which reading applies turns on who spawned the worker.** For **a worker this session spawned**, it is `mcp__supervisor__agent_status <agent_id>` → **`open_cards`**, the session's open attention-store cards, each carrying the `item_id` to answer — ⚠️ **`null` means the store was unreachable and `[]` means it was read and is empty, and collapsing the two reports a blocked worker as unblocked** (`server/open-cards.mjs`). ⚠️ **That tool resolves only against this session's own agent map**, so a worker another session spawned answers `unknown agent <agent_id>` — which is **not** "no gate" but "not addressable from here", the same empty-read-as-absence shape this clause exists to remove; for that worker take the feed instead: `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/who-needs-me.py --section needs-you` — the convention step 7 already uses — lists every open gate with its **pane id, age, session label and question text**, so the worker's own gate resolves by pane. ⚠️ **Never read a board-wide count as this worker's reading.** `scripts/attention-board.py` prints one line, `Board: N open`, carrying no `item_id` and no per-worker attribution, and its own docstring records that it disagrees with the store API on the count (measured 2026-10-01: API 4, board 2) — it answers *"is anything open anywhere"*, never *"is this worker gated"*, so acting on it is this clause's own failure one level up. ⚠️ **Where neither reading can be taken, state the direction as conditional** — *"conditional on no operator answer already having been given; if one has, the operator's answer governs and you should refuse this"* — so the worker declines cleanly rather than silently overriding an answer the operator already gave. ⚠️ **Never assert an unconditional direction into a row whose gate the operator has already answered.** Measured 2026-09-30, tick 15:37 on `BRO-21389 MDM via REST`: the manager read the pending list, got `[]`, and directed a tab worker whose three gaps the operator had already answered in its pane; the worker refused, correctly, and the manager recorded the miss as its own — *"Two channels, one gate — and the manager's instrument only reads one of them."*
- ⚠️ **This replaces the older "the topic page is the only file the manager may write" rule** (operator override, 2026-09-18). That rule made a one-field status flip a forbidden act and produced a real stall: a task sat `hold` while the manager could only hand the operator a command. Do not reintroduce it as a "fix" — quote this paragraph instead.
- **Never widen past the boundary.** A task-file write is management only while it changes *tracking state* (status, phase, dates, stamps, scope). If it needs the code read, a root cause judged, or a runtime fact verified, it is work — delegate it.
- ⚠️ **Carve-out — a dependency-condition read is management, not work.** The prose-blocker check (clause (2) in `agents/manager-drive.md`, and the pre-spawn bullet below) reads a fact about *whether a dependency the row itself names has shipped* — `gh pr view <n> --json mergedAt,mergeCommit`, `git tag --list <tag>`, or a running artifact's version. That is a **read-only eligibility lookup on the row's own declared condition**, not verification of the topic's work: it judges no code, reproduces no defect, opens no PR and fixes nothing. It is management for the same reason the orphan probe is — without it the manager cannot decide what to start. **Do not read the "no repo/PR/k8s verification" line above as forbidding it**; that line forbids doing the topic's *work*, and a dependency lookup is not work.
- **No worker → spawn one.** Ready-to-start / unowned / orphaned-restart work is started as a new Claude Code session via the Spawn mechanics below — never done in-line by the manager.
- **Task/goal anchored, always.** No work is delegated or spawned without a task (or goal) anchoring it; an unanchored request is not work. Read-only manager business (sweep, table, TTS) needs no anchor; anything else gets a task first (`/vault-cli:create-task "<title>"`).

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **subject** — a goal name matching a page in `24 Goals/`, or a topic name matching a topic page in `23 Topics/`. The branch is detected from whichever page resolves (step 0); this command never assumes one.
  - `/manager-loop "Stop Nil-Slice → Null Crashes on MDM Party Surfaces"` → goal branch
  - `/manager-loop Sentry` → reads `23 Topics/Sentry.md`, tracks its `## Goals` members + their tasks
  - `/manager-loop` (bare) → resolve the subject from § Subject resolution below
  - No page resolves → the manager reports it and stops; it never globs a substitute set
- For one look without a loop, run `/manager-status` (read-only); for one act pass, run `/manager-drive`. There is no one-sweep flag: a single sweep still spawns, resumes and writes back, so it was never a read-only look.

## Subject resolution — when `$1` is omitted

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` — the vault resolution, the four-source chain, the case-insensitive vault test, the "no fallback, ever" clause, the `Subject:` line, the recording contract (which files, on which resolution), and the subject-status reconcile. Read it there; **it is not restated here.**

**Only source 3 stays inline**, because it is the one source no agent can be handed — a subagent runs in a fresh context and cannot see the parent conversation:

3. **Conversation** — the most recent `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` or `/supervisor:manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention).

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /manager-loop "<name>"` and do nothing else.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs.



## Resolution — detect the branch, then read the declared set

A name resolves to **either a topic or a goal**; the branch is **detected, never assumed**. Membership is **declared, never re-derived** — never widened by a glob, a theme match or a content grep. Goals and topics close differently (step G).

**The `goals:` declaration has three shapes — match all three.** Membership is read from frontmatter, so the parser must handle every shape the vault actually writes. Census over `25 Tasks/`, 2026-09-18 (826 task files):

| shape | example | count |
|---|---|---|
| list of `[[wikilinks]]` | `goals:` / `    - '[[X]]'` | 385 |
| plain scalar, no brackets | `goals: X` | 2 |
| nested list-of-list, no brackets | `goals:` / `    - - X` | 1 |

**Normalize every shape to the bare goal name, repeating until stable:** strip `[[` `]]`, strip quote marks, strip every leading `- `; re-run all three while anything changed. One fixed-order pass silently drops list or nested entries.

**A wikilink-only extractor (`\[\[([^\]]+)\]\]`) is non-compliant** — it drops the scalar and nested shapes and the set renders silently smaller.

**An empty declaration is not a shape.** `goals: []`, `goals: null` and a bare `goals:` name no goal and are correctly excluded by every parser — do not fold them into the census. 39 of the 42 non-wikilink declarations in `25 Tasks/` are empty, so counting them as misses overstates the damage roughly thirteenfold.

- **Both match, or neither** → print every candidate path (or `no match`) and **stop**. Never guess between a goal and a topic, and never silently prefer one. A name that matches neither is not tracked — that is the intended pressure.
- **Closure is not yours.** The goal's `# Success Criteria` are its closure contract and they close **mechanically**. Report SC status and hand closure to `/vault-cli:complete-goal` — never tick SC yourself, and never flip the goal's status **in order to close it**. ⚠️ **The rule's scope is closure, and `status` is not otherwise off-limits to the manager** — the resolution-time reconcile that flips a subject reading `todo`/`next` to `in_progress` (`docs/subject-resolution.md` § *Reconcile the subject's status*) is the manager's own write and is **required, not forbidden**. Reading this sentence as a blanket ban on `status` writes is what leaves a worked goal's page reading `todo` while its own tasks run. A goal closing on criteria vs a topic closing on a judged gate is the branch, not a size difference.
- **Skip steps 1–5** — every one of them is topic-only. There is no topic page, no `## Goals` list, no declared-optional set and no `# Status Summary` on a goal, and **step 5's missing-topic-page stop would fire spuriously on a goal branch**. Do not synthesise them.
- Everything else — the sweep, buckets, orphan detection, the drive dispatch, spawn mechanics, the status table, TTS, guardrails — is **identical to the topic branch**, because the loop is level-independent.

5. **Topic branch only — no topic page** → print the missing page name and the fix (`create 23 Topics/<Topic>.md from the Topic Writing Guide template`), then **stop**. No fallback — no filename glob, `goals:` scan, theme match or content grep.

6. **Confirm no manager already owns this name — before arming anything.** `ListAgents` for `<Topic> Manager` (goal branch: the goal's manager). Hit → print its name and age and **stop**. Miss → re-read the roster **once**; still a miss → proceed and arm. Never probe with `pgrep -f`. Rule: runbook § Step 0.

Print the resolved set at manager start (`Tracked (N): <task> · <task> …`) and **re-print it whenever the declaration read changes it** (step G / step 2) — as a delta line, never silently. Three things stay fixed mid-run, and none of them is the declaration read: membership is **never** widened by a glob, a theme match or a content grep (this section's preamble); the subject and branch **detection** (step 0's probe — not step G's declaration read, which refreshes) resolves **once** at manager start, never per wakeup; and **page *declarations* do not refresh** — the declared-optional set (step 3) is a written marking, not a derived read, so it stays frozen for the run. If the human names a set explicitly, track that set for this run — but say so in the header, since it diverges from the page.

## The open-items ledger — the operator's asks

A manager keeps the **same ledger as the fleet manager**, keyed by its own session and scoped to its subject: it owns the stretch from an instruction being said to a task existing, and from a question being asked to its answer.

Read and write it only through the **`supervisor:open-items` skill** — `/supervisor:open-items <subcommand> …`. The skill is the single home of the kinds, the subcommands, the render rule, the act-every-sweep rule, `answer`-vs-`note`-vs-`withdraw`, and `close --evidence`; follow it verbatim, never restate it here. ⚠️ **The subcommand list is deliberately NOT enumerated on this line**, though it used to be: the copy had **already** drifted — it omitted `classify`, which the skill's `argument-hint` has carried all along — so a manager reading only this file was already being told a shorter list than the skill offers. The skill's `argument-hint` is the list. The session id is derived by the script — no `SID` to pass for your own ledger.

## The loop — what runs, and where its rules live

1. Read the vault's `Manager Session` runbook (the canonical procedure — follow it verbatim).

3. **Arm the tick — the FIRST thing it does, ahead of the sweep (gap 5).** `ScheduleWakeup` every `--interval` — default 15 min, floor 5 m, clamped and announced below it (§ Interval) — or a `Monitor`, re-sweeping with the **tick** prompt — `/supervisor:manager-loop-tick "<subject>"` (plus the same `--interval`), **never this command**. ⚠️ **The arm is invoked ONCE and a wakeup must never re-invoke it.** Re-arming with this command's own name is the defect the split exists to remove: the harness re-injects a command's full body on every invocation, so a loop that names itself pays this whole file again on every tick. This file is the **arm** — full rules, read once; `commands/manager-loop-tick.md` is the **tick** — the per-round map, read on every wakeup. ⚠️ **`ScheduleWakeup` is one *spelling* of the cron driver, not a driver of its own** — a `ScheduleWakeup` loop **is** a `CronCreate` job, so `CronList`/`CronDelete` reach it and `/supervisor:stop` disarms it (`docs/fleet-surface.md` § Session end; measured 2026-10-03). A one-shot `CronCreate` call is that same driver's **other** spelling, and it is the right arm when the tick must land on a **wall-clock minute** rather than after a relative delay: `ScheduleWakeup` takes `delaySeconds`, `CronCreate` takes a `cron` expression. ⚠️ **Only the `ScheduleWakeup` spelling carries `noop`** — the `CronCreate` tool has no such field; pass `recurring: false` explicitly for a one-shot arm rather than relying on its default. ⚠️ **So arm with `ScheduleWakeup`, and treat a bare `CronCreate` call as a spelling that forfeits this command's write-back trigger:** such a tick cannot set the `noop: false` placeholder here, and it **must not make step 7's correcting call either** — whether a `ScheduleWakeup` call cancels a `CronCreate`-armed job is **not established**, and this repo's own measurement points away from it (2026-09-30: `ScheduleWakeup {stop:true}` returned *"there was no pending wakeup to cancel"* while `CronList` showed the loop's job), so the correction would leave **both** arms live and double the tick rate. Reach for `CronCreate` bare only where losing `noop` is acceptable. **Record the arm in the same motion, right after the arm call:** `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py --arm --topic "<subject>" --interval <the clamped seconds just armed>`. This is the evidence the host-side watcher (`manager-liveness.py --check`) reads: a manager whose last record is older than 2× its own interval is reported as lapsed, so pass the real delay, including a slowed idle cadence. ⚠️ **It belongs HERE, not at the tick's end, and the failure it removes is the reason.** `ScheduleWakeup` is **one-shot per call**: nothing but the tick's own closing call schedules the next firing, so a tick that dispatches a sweep, then takes an inbound peer question, then answers it and ends leaves the loop **unarmed** — no error, no marker, no stale-loop signal — and the `--arm` record is skipped in the same motion, so the one instrument that could have caught it is fed by the same skipped call. Arming first makes a missed re-arm **impossible** rather than merely unlikely; a tick that later discovers a reason to slow down or stop re-arms again at that point, and the ordinary case is that this first arm simply stands. ⚠️ **It runs here, after step 2, because `--arm --topic` needs the resolved subject** — and it runs **before step 4's first sweep**, which is what makes a long sweep unable to cost the loop its next wakeup. ⚠️ **`noop` here is a placeholder, not a finding** — the tick has not run, so it cannot yet report change: arm with `noop: false`, and correct it at step 7 once the sweep's result is known. (`noop` carries no scheduling semantics at all — it is read from the transcript to fold consecutive idle ticks — but it *is* this command's write-back trigger, which is why it is corrected rather than left.) Rule: `65 Runbooks/Manager Session.md` § Cadence mechanics — the **Personal** vault, and the one home for this arm's rule and its traps.

6. **Guardrails** (non-negotiable, runbook § Guardrails): the manager does not build — every piece of work goes to a task- or goal-anchored worker; never fabricate progress (disk facts, this sweep); **print the artifact in an earlier message than** the one asserting any negative or attribution claim — a check dispatched in the same message block does not back it, and a failed glob is not absence; pane text is multi-author. The one permitted forward-looking form is an expectation stated *before* the check runs, and only when it carries the literal marker **`Expectation (unmeasured):`** — nothing here forbids that form, only stating a **result** you have not yet read. Management writes are yours. Work carve-outs: (a) gated auto-resume (gap 6); (b) **approved** ready-to-start spawns under the standing mandate, capped — the cap's numbers live in `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 5 and are never restated here (gap 7); (c) the topic-page write-back; (d) the manager-side close of a gate-met topic. ⚠️ **The mandate is over APPROVED rows only** — a row at `phase: todo` is not a ready-to-start row, whatever its `status` says. The operator's `todo → planning` flip *is* the approval, and the mandate does not reach past it: `⌛ waiting-approval` rows are reported, never opened. `65 Runbooks/Manager Session.md` § Step 4's *Waiting-approval* bullet is the rule's single home. TTS only when the operator must **do** something — voice is auto-on via `~/.claude/hooks/voice-mode.py`, `off` always wins, `/tts-mcp:on` for narrate; watch only this topic. An operator ask → `/supervisor:open-items add` before replying. **Never decide an operator gate** — which gates you may clear is the Manager Session runbook § Gate triage — who clears what; production-touching / live-trade gates are never relayed. **A headless worker's gate** arrives via `mcp__supervisor__await_permission` and is answered with `mcp__supervisor__answer_permission(request_id, behavior="allow"|"deny")` — the provenance rules still bind. `wezterm cli` relays into a tab worker are the path-B fallback for tab workers only, and are dispatched through the `gate-relay-send` agent — never typed from this session.

   ⚠️ **Answer an `AskUserQuestion` with `deny` + `message`, never `allow`** (`allow` leaves the worker waiting ~11 min, then it exits unanswered). Prefix: `Operator answer, via supervisor:` when the operator answered here, `Manager answer, via supervisor:` for your own scope decision. Neither releases an irreversible or production-touching action. `allow` only for ordinary tool approvals. Spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable. Refused → `accept edits`, retry.

   ⚠️ **A headless worker that has exited is not finished — continue it, do not restart it.** A headless worker ends its turn on a READY panel, or its parked question times out (~11 min) and it exits with the question unanswered. Neither is completion, and neither leaves a trace beyond the roster row disappearing. The continuation is a fresh turn, not a restart:

   ```
   mcp__supervisor__spawn_agent(prompt="<the answer, or the next instruction>", resume="<session-id>", interactive=false, cwd="<explicit>")
   ```

   Plain user turn, no relay prefix. **Pass `cwd` explicitly** — it is not inherited; read it from `~/.claude/sessions/<pid>.json` if unknown. A timed-out worker still holds its question, so the prompt must carry the answer. Spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § A headless worker exits at turn end.

   ⚠️ **A headless worker's prompts park only with the session that spawned it** — spawn your own workers and answer them yourself; a fleet manager cannot see them. Overlap ownership is unresolved.

**Path-B relay rules.** Legitimate only when (a) the operator answered in this session, current exchange; (b) the relay is verbatim with the `Operator answer, relayed verbatim from the manager session (not a peer inference):` prefix; (c) never on a peer's claim of an operator decision. The mechanics live in the **`gate-relay-send` agent** — dispatch it **once per round** carrying every answer from the batch (per pane: pane id, worker label, the operator's answer verbatim, provenance), never once per pane; it re-reads the pane immediately before typing and reports `not sent` with no pane mutation — **naming which cause, because the two are opposite readings**: `gate cleared` (the worker resolved it meanwhile) or **`gate not cleared`** (the composer still holds unsubmitted text, so the operator's own Enter in that pane is what releases it — hand over the pane and never re-dispatch the send leg). ⚠️ **Never read `not sent` as `gate cleared`**: the pair diverges exactly where a worker stays parked, and collapsing them is the silent suppression this relay exists to remove. **Never relay** a production-touching or irreversible approval, nor a selection modal (match `Enter to select` at the **start of a line** — the real render is `Enter to select · ↑/↓ to navigate · Esc to cancel`, so the anchor is the line's opening, never the whole string and never a bare containment match), nor a multi-question wizard: record it and batch the one-line output of `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py <pane-id>` **verb-free**, exactly as `manager-status` / `fleet-status` render their `jump:` rows. **Classify before dispatching — this check runs here, never inside the agent.** A relay its own contract forbids must not be dispatched at all: `gate-relay-send` is dispatched *after* this decision, so a refusal it performs still spends the subagent the refusal was meant to save. A **bare affirmative** (`y` / `yes` / `ok`) against a `👤 You:` line this session emitted is **not an answer** when that line's action is class D or E — and the classification is of the **action the line names, not the line's verb**, so `you run: … and to proceed to the deploy step` is class D however incidental the verb looks. Say so once (*"that is not an answer; the pane needs your own keystroke"*), hand over the one-line `jump-link.py <pane-id>` output verb-free, dispatch nothing, and never report a relay "in flight". It is a link the operator follows, not a command anyone types here, so there is no actor to label: `/supervisor:jump` is the executor and the managers' commands emit links without running it. The gate itself is released only by the operator's **own keystroke in that pane**, never by anything typed here. **One action per line** — never bundle a second action (e.g. `/reload-plugins`) onto the handover, or the operator's reply is ambiguous between them. A multi-question wizard → `/supervisor:jump <pane-id>`, never relayed. A relay the worker refuses → do not retry; hand over `/supervisor:jump <pane-id>`. Emit `ACTION NEEDED` as a numbered pick list, option 1 `(recommended)`. **Verify submission** from the agent's own one-line result: `delivered` requires the composer cleared *and* the worker visibly working — anything else is reported as it came back, never rounded up.
