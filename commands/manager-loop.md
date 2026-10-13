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

Manager-loop — the **narrow / deep** layer.

Rationale and incident history: the Manager Session runbook § Command rationale. This file carries only what a sweep executes.

One manager per topic: owns ONE topic's declared goal set (e.g. the *billing* topic, the *notification* topic), read from that topic's page; the fleet manager (wide layer) may run above. Resolution step 6 confirms no manager already owns the name before arming, and stops on a hit.

⚠️ **Never invoke this command from a worker session.** Starting a manager is a human act. Rule: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles — who may start what.

## The round — the arm owns no step list

This file is the **arm**: it resolves, confirms no manager already owns the name, arms the wakeup and exits. Every wakeup runs **`/supervisor:manager-drive "<subject>"`** — the **round** — and the round owns the step list and the router table (`commands/manager-drive.md` § Router): which step files exist, and the condition under which each is read.

⚠️ **The table is deliberately NOT copied here.** It had two homes while the arm and the round each carried one, and a step list with two homes drifts — one copy is amended and the other silently keeps the old steps. Read it from the round.

The rules below are **inline because they must hold whether or not a step file was read**.

⚠️ **Opening a worker is bound by `docs/fleet-surface.md` § Spawn a worker item 6** — the mode is passed in both directions (`mode: headless` → `interactive=false`, `mode: interactive` → `interactive=true`). This file grants the tool but does not open; the call lives in `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-spawn.md`.

## Interval — the tick and its floor

`--interval <N>m` sets the tick. Default **15m**; floor **5m**. A value below the floor is **clamped up and announced** (`⚠️ --interval 2m is below the 5m floor — clamped to 5m`); never rejected.

Parse it out of `$ARGUMENTS` before resolving the subject: `--interval` is orthogonal to the positional `[goal|topic]`, so a session may carry either, both, or neither.

**The tick is one number, used twice.** Pass the clamped seconds to `ScheduleWakeup`'s `delaySeconds`, and the *same* seconds to `manager-liveness.py --arm --interval` (step 3). ⚠️ **The seconds half assumes the `ScheduleWakeup` spelling** — the clamp is expressed in **seconds**, which a `cron` expression cannot carry; nothing spells what to pass to `--arm` on a bare `CronCreate` arm. The `--arm` record is written **unconditionally**, in the same motion after the arm call: a bare `CronCreate` arm forfeits the `noop` placeholder, not the record. ⚠️ The record is the **fallback** basis only — `manager-liveness.py` prefers the observed median arm-to-arm period once ≥4 arms exist (`observed_interval()` → `2 * observed + SLACK`). Never present the declared value as the authoritative period.

⚠️ **The flag sets no cap of its own — the floor is the only clamp `--interval` applies.**

- **A headless worker's park does not cap the interval — the manager chooses.** A headless permission park auto-denies after 15 minutes (`skills/supervising-workers/SKILL.md`). The manager **chooses**: keep the long tick and accept the auto-deny, or run the wrangler auto-policy (`/supervisor:manager-drain` → the `supervisor:manager-wrangler` agent) so parks are answered without the manager's tick. ⚠️ **The wrangler is a ONE-SHOT loop — it stops as soon as its queue empties**, so a long tick behind a live headless worker needs it **run repeatedly for the life of the tick**, or a standing form; a park raised after it exits auto-denies at the 15-minute mark.
- **The attention watcher is not governed by this flag at all.** The **tick's** `--interval` sets the tick; the watcher `Monitor` stays on its own arm — with its **own** poll period, the **filter's** `--interval` — bounded at 30 min and re-armed on expiry. ⚠️ **At a long interval the watcher still wakes the manager once per watcher event — intended, not a leak**. Never "fix" the mismatch by stretching the watcher. ⚠️ **The arm's rule and its traps live in `the Manager Session runbook` § Cadence mechanics — read them there.** It is scoped **twice**, neither half optional: the **section** scope (the feed's other four sections are not gates) and the **ownership** scope (the filter's keep-set is wide by design).

## Manager contract — a manager manages, it does not build

The manager **manages; it does not build**. It never performs the topic's *work*: no code edits, no repo/PR/k8s verification, no investigation, no real debugging — not even small ones.

- **The line is work vs management — not read vs write.** Management writes are the manager's own — tasks, task/goal pages, `status`/`phase`, stale stamps, topic-page scope and write-back — via `Edit` / `vault-cli task set`. Code, verification, investigation, debugging and the fix go to a worker.
- ⚠️ **Precedence when a worker already holds the row — this paragraph governs, and it splits by row state** (three other restatements give the opposite instruction on a worker-owned row: `commands/worker-drive.md` § Peer safety; the vault `CLAUDE.md` § Reconcile On Edit; `the Manager Session runbook` § Command rationale). **The split:** on a row the manager owns, **or** one carrying no live `claude_session_id`, a manager-authored Success Criteria / Tasks / DoD / Summary edit is management and lands directly. On a row a **live worker owns**, the manager **proposes** — an appended `# Progress` entry plus a `SendMessage` to the owner — and the **worker lands the edit on its own anchor**. `status` / `phase` / dates / stamps stay the manager's own in both states.
- ⚠️ **A worker's pane answer and a manager's file answer — the worker reports first, and the operator's answer wins.** A worker answers through `AskUserQuestion` in its own pane; a manager can answer the same questions by editing the file, so a manager's edit can land on top of an operator answer. **The convention (not a mechanism):** a worker that has put a question to the operator **reports the answers back to its manager** (the answers, and that they were answered) **before** the manager edits the file, by `SendMessage`, not by waiting for a reply. **The operator's answer wins** — a manager that has already edited reverts its edit and re-proposes through the precedence path above. Where two answers conflict and neither is the operator's, the **row owner's** stands. ⚠️ **Nothing enforces this.**
- ⚠️ **An empty `pending_permissions` is not evidence that no gate is outstanding — take the worker's own gate reading before you direct it.** `mcp__supervisor__pending_permissions` carries **this server's headless parks only**, so a row it reports empty can still be an **interactive tab worker** parked on an `AskUserQuestion` in its own pane. The asymmetry and its carriers live in `commands/fleet-workers.md` and `llms.txt` § *How a worker's permission prompt is answered*. **What this clause adds:** before a `SendMessage` that **directs** a worker, take that worker's **own** gate reading, for every worker. **Which reading applies turns on who spawned the worker.** For **a worker this session spawned**: `mcp__supervisor__agent_status <agent_id>` → **`open_cards`**, each card carrying the `item_id` to answer — ⚠️ **`null` means the store was unreachable, `[]` means read-and-empty**; it resolves only against this session's own agent map, so a worker another session spawned answers `unknown agent <agent_id>` — **not** "no gate" but "not addressable from here". For that worker take the feed: `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/who-needs-me.py --section needs-you` — every open gate with its **pane id, age, session label and question text**. ⚠️ **Never read a board-wide count as this worker's reading** (`scripts/attention-board.py` prints `Board: N open`, no `item_id`). ⚠️ **Where neither reading can be taken, state the direction as conditional** — *"conditional on no operator answer already having been given; if one has, the operator's answer governs"* — and **never assert an unconditional direction into a row whose gate the operator has already answered.**
- ⚠️ The older "the topic page is the only file the manager may write" rule is **superseded** (see above); do not reintroduce it as a "fix".
- **Never widen past the boundary.** A task-file write is management only while it changes *tracking state* (status, phase, dates, stamps, scope). If it needs the code read, a root cause judged, or a runtime fact verified, it is work — delegate it.
- ⚠️ **Carve-out — a dependency-condition read is management, not work.** The prose-blocker check (clause (2) in `agents/manager-drive.md`) reads whether *a dependency the row itself names has shipped* — `gh pr view <n> --json mergedAt,mergeCommit`, `git tag --list <tag>`, or a running artifact's version. That is a **read-only eligibility lookup on the row's own declared condition**: it judges no code, reproduces no defect, opens no PR and fixes nothing. **Do not read the "no repo/PR/k8s verification" line above as forbidding it.**
- **No worker → spawn one.** Ready-to-start / unowned / orphaned-restart work is started as a new Claude Code session via the Spawn mechanics below — never done in-line by the manager.
- **Task/goal anchored, always.** No work is delegated or spawned without a task (or goal) anchoring it; an unanchored request is not work. Read-only manager business (sweep, table, TTS) needs no anchor; anything else gets a task first (`/vault-cli:create-task "<title>"`).

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **subject** — a goal name matching a page in `24 Goals/`, or a topic name matching a topic page in `23 Topics/` *(operator's vault; not shipped with this plugin)*. The branch is detected from whichever page resolves (step 0); this command never assumes one.
  - `/manager-loop "Stop Nil-Slice → Null Crashes on MDM Party Surfaces"` → goal branch
  - `/manager-loop Sentry` → reads `23 Topics/Sentry.md` *(operator's vault; not shipped with this plugin)*, tracks its `## Goals` members + their tasks
  - `/manager-loop` (bare) → resolve the subject from § Subject resolution below
  - No page resolves → the manager reports it and stops; it never globs a substitute set
- For one look without a loop, run `/manager-status` (read-only); for one act pass, run `/manager-drive`. There is no one-sweep flag: a single sweep still spawns, resumes and writes back.

## Subject resolution — when `$1` is omitted

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` — the vault resolution, the four-source chain, the recording contract, and the subject-status reconcile. Read it there; **it is not restated here.**

**Only source 3 stays inline** — it is the one source no agent can be handed:

3. **Conversation** — the most recent `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` or `/supervisor:manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention).

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /manager-loop "<name>"` and do nothing else.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`.



## Resolution — detect the branch, then read the declared set

A name resolves to **either a topic or a goal**; the branch is **detected, never assumed**. Membership is **declared, never re-derived** — never widened by a glob, a theme match or a content grep. Goals and topics close differently (step G).

**The `goals:` declaration has three shapes — match all three.** Membership is read from frontmatter, so the parser must handle every shape the vault writes:

| shape | example | count |
|---|---|---|
| list of `[[wikilinks]]` | `goals:` / `    - '[[X]]'` | 385 |
| plain scalar, no brackets | `goals: X` | 2 |
| nested list-of-list, no brackets | `goals:` / `    - - X` | 1 |

**Normalize every shape to the bare goal name, repeating until stable:** strip `[[` `]]`, strip quote marks, strip every leading `- `; re-run all three while anything changed.

**A wikilink-only extractor (`\[\[([^\]]+)\]\]`) is non-compliant** — it drops the scalar and nested shapes.

**An empty declaration is not a shape.** `goals: []`, `goals: null` and a bare `goals:` name no goal and are correctly excluded by every parser — do not fold them into the census.

**Both match, or neither** → print every candidate path (or `no match`) and **stop**. Never guess between a goal and a topic, and never silently prefer one. A name that matches neither is not tracked.
- **Closure is not yours.** The goal's `# Success Criteria` are its closure contract and they close **mechanically**. Report SC status and hand closure to `/vault-cli:complete-goal` — never tick SC yourself, and never flip the goal's status **in order to close it**. ⚠️ **The rule's scope is closure; `status` is not otherwise off-limits to the manager** — the resolution-time reconcile that flips a subject reading `todo`/`next` to `in_progress` (`docs/subject-resolution.md` § *Reconcile the subject's status*) is the manager's own write and is **required, not forbidden**.
- **Skip steps 1–5** — every one of them is topic-only. There is no topic page, no `## Goals` list, no declared-optional set and no `# Status Summary` on a goal. Do not synthesise them.
- Everything else — the sweep, buckets, orphan detection, the drive dispatch, spawn mechanics, the status table, TTS, guardrails — is **identical to the topic branch**.

5. **Topic branch only — no topic page** → print the missing page name and the fix (`create 23 Topics/<Topic>.md from the Topic Writing Guide template` *(operator's vault; not shipped with this plugin)*), then **stop**. No fallback — no filename glob, `goals:` scan, theme match or content grep.

6. **Confirm no manager already owns this name — before arming anything.** `ListAgents` for `<Topic> Manager` (goal branch: the goal's manager). Hit → print its name and age and **stop**. Miss → re-read the roster **once**; still a miss → proceed and arm. Never probe with `pgrep -f`. Rule: runbook § Step 0.

Print the resolved set at manager start (`Tracked (N): <task> · <task> …`) and **re-print it whenever the declaration read changes it** (step G / step 2). Three things stay fixed mid-run: membership is **never** widened by a glob, a theme match or a content grep; the subject and branch **detection** (step 0's probe) resolves **once** at manager start, never per wakeup; and **page *declarations* do not refresh** — the declared-optional set (step 3) is a written marking, not a derived read. If the human names a set explicitly, track it for this run and say so in the header.

## The open-items ledger — the operator's asks

A manager keeps the **same ledger as the fleet manager**, keyed by its own session and scoped to its subject: it owns the stretch from an instruction being said to a task existing, and from a question being asked to its answer.

Read and write it only through the **`supervisor:open-items` skill** — `/supervisor:open-items <subcommand> …`. The skill is the single home of the kinds, subcommands, render rule, act-every-sweep rule, `answer`-vs-`note`-vs-`withdraw`, and `close --evidence`; follow it verbatim. ⚠️ **The subcommand list is deliberately NOT enumerated on this line** — the skill's `argument-hint` is the list. The session id is derived by the script — no `SID` to pass.

## The loop — what runs, and where its rules live

1. Read the vault's `Manager Session` runbook (the canonical procedure — follow it verbatim).

3. **Arm the round — the FIRST thing it does, ahead of the sweep (gap 5).** `ScheduleWakeup` every `--interval` — default 15 min, floor 5 m, clamped and announced below it (§ Interval) — or a `Monitor`, re-sweeping with the **round** prompt — `/supervisor:manager-drive "<subject>"` (plus the same `--interval`), **never this command**. ⚠️ **The arm is invoked ONCE; a wakeup must never re-invoke it.** This file is the **arm** (read once); `commands/manager-drive.md` is the **round** (read on every wakeup). ⚠️ **`ScheduleWakeup` is one *spelling* of the cron driver** — a `ScheduleWakeup` loop **is** a `CronCreate` job, so `CronList`/`CronDelete` reach it and `/supervisor:stop` disarms it (`docs/fleet-surface.md` § Session end). A one-shot `CronCreate` is that driver's **other** spelling, right when the tick must land on a **wall-clock minute**: `ScheduleWakeup` takes `delaySeconds`, `CronCreate` takes a `cron` expression. ⚠️ **Only `ScheduleWakeup` carries `noop`** — (`CronCreate` has no such field; pass `recurring: false` for a one-shot arm). ⚠️ **Arm with `ScheduleWakeup`; a bare `CronCreate` forfeits this command's write-back trigger** — it cannot set the `noop: false` placeholder here, and **must not make step 7's correcting call either**. **Record the arm in the same motion, right after the arm call:** `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py --arm --topic "<subject>" --interval <the clamped seconds just armed>` — the evidence the host-side watcher (`manager-liveness.py --check`) reads; a last record older than 2× the interval is reported as lapsed. ⚠️ **It belongs HERE, not at the tick's end:** `ScheduleWakeup` is **one-shot per call**, so a tick that ends before re-arming leaves the loop **unarmed**; arming first makes a missed re-arm **impossible**. ⚠️ **It runs after step 2** (`--arm --topic` needs the resolved subject) **and before step 4's first sweep**. ⚠️ **`noop` here is a placeholder, not a finding** — arm with `noop: false`, correct at step 7. Rule: `the Manager Session runbook` § Cadence mechanics.

⚠️ **The wakeup target carries a hard ceiling — ≤10 KB (10,240 B) — and `scripts/check-budget.py` is the check that enforces it.** The budget follows **what the wakeup names**, never the filename: whatever command that is, the harness re-injects its whole body on every wakeup, so a round that grows past the cap pays its full size on every tick. `commands/manager-drive.md` is the target and is held there by the ratchet's explicit `WAKEUP_TARGETS` list — **so a target renamed without re-pointing that list leaves the cap applying to nothing while the check still reports green.** Keep the round under the ceiling by moving rationale to the runbook, never by deleting a rule. Rationale and the measurements behind it: `the Manager Session runbook` § Command rationale — maintained.

6. **Guardrails** (non-negotiable, runbook § Guardrails): the manager does not build — every piece of work goes to a task- or goal-anchored worker; never fabricate progress (disk facts, this sweep); **print the artifact in an earlier message than** the one asserting any negative or attribution claim — a check dispatched in the same message block does not back it, and a failed glob is not absence; pane text is multi-author. The one permitted forward-looking form is an expectation stated *before* the check runs, carrying the literal marker **`Expectation (unmeasured):`**. Management writes are yours. Work carve-outs: (a) gated auto-resume (gap 6); (b) **approved** ready-to-start spawns under the standing mandate, capped — cap numbers in `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 5 (gap 7); (c) the topic-page write-back; (d) the manager-side close of a gate-met topic. ⚠️ **The mandate is over APPROVED rows only** — a `phase: todo` row is not ready-to-start. The operator's `todo → planning` flip *is* the approval; `⌛ waiting-approval` rows are reported, never opened (runbook § Step 4's *Waiting-approval* bullet). TTS only when the operator must **do** something — voice auto-on via `~/.claude/hooks/voice-mode.py`, `off` always wins, `/tts-mcp:on` for narrate; watch only this topic. An operator ask → `/supervisor:open-items add` before replying. **Never decide an operator gate** — which gates you may clear is the runbook § Gate triage; production-touching / live-trade gates are never relayed. **A headless worker's gate** arrives via `mcp__supervisor__await_permission` and is answered with `mcp__supervisor__answer_permission(request_id, behavior="allow"|"deny")` — the provenance rules still bind. `wezterm cli` relays into a tab worker are the path-B fallback for tab workers only, via the `gate-relay-send` agent — never typed from this session.

   ⚠️ **Answer an `AskUserQuestion` with `deny` + `message`, never `allow`** (`allow` leaves the worker waiting ~11 min, then it exits unanswered). Prefix: `Operator answer, via supervisor:` when the operator answered here, `Manager answer, via supervisor:` for your own scope decision. Neither releases an irreversible or production-touching action. `allow` only for ordinary tool approvals. Spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable. Refused → `accept edits`, retry.

   ⚠️ **A headless worker that has exited is not finished — continue it, do not restart it.** A headless worker ends its turn on a READY panel, or its parked question times out (~11 min) and it exits with the question unanswered. The continuation is a fresh turn, not a restart:

   ```
   mcp__supervisor__spawn_agent(prompt="<the answer, or the next instruction>", resume="<session-id>", interactive=false, cwd="<explicit>")
   ```

   Plain user turn, no relay prefix. **Pass `cwd` explicitly** — it is not inherited; read it from `~/.claude/sessions/<pid>.json` if unknown. A timed-out worker still holds its question, so the prompt must carry the answer. Spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § A headless worker exits at turn end.

   ⚠️ **A headless worker's prompts park only with the session that spawned it** — spawn your own workers and answer them yourself; a fleet manager cannot see them.

**Path-B relay rules.** Legitimate only when (a) the operator answered in this session, current exchange; (b) the relay is verbatim with the `Operator answer, relayed verbatim from the manager session (not a peer inference):` prefix; (c) never on a peer's claim of an operator approval. Mechanics: the **`gate-relay-send` agent** — dispatch it **once per round** carrying every answer from the batch (per pane: pane id, worker label, the operator's answer verbatim, provenance), never once per pane; it re-reads the pane immediately before typing and reports `not sent` with no pane mutation, **naming which cause**: `gate cleared` or **`gate not cleared`** (the composer still holds unsubmitted text — hand over the pane and never re-dispatch the send leg). ⚠️ **Never read `not sent` as `gate cleared`.** **Never relay** a production-touching or irreversible approval, nor a selection modal (match `Enter to select` at the **start of a line**), nor a multi-question wizard: record it and batch the one-line output of `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py <pane-id>` **verb-free**. **Classify before dispatching — this check runs here, never inside the agent**; a relay its own contract forbids must not be dispatched at all. A **bare affirmative** (`y` / `yes` / `ok`) against a `👤 You:` line this session emitted is **not an answer** when that line's action is class D or E — the classification is of the **action the line names, not the line's verb**. Say so once, hand over the one-line `jump-link.py <pane-id>` output verb-free, dispatch nothing, and never report a relay "in flight". The gate is released only by the operator's **own keystroke in that pane**. **One action per line.** A multi-question wizard → `/supervisor:jump <pane-id>`, never relayed. A relay the worker refuses → do not retry; hand over `/supervisor:jump <pane-id>`. Emit `ACTION NEEDED` as a numbered pick list, option 1 `(recommended)`. **Verify submission** from the agent's own one-line result: `delivered` requires the composer cleared *and* the worker visibly working.
