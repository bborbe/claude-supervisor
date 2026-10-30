---
# No `name:` field, deliberately — a command takes its name from its filename. The
# frontmatter rule for `commands/*.md` (`agent-cmd/command-frontmatter`) requires
# `description`, `allowed-tools` and `argument-hint` and nothing else.
description: Arm the TOPIC manager loop — watch the goals this topic declares and the goal managers running on them (default 15 min, `--interval`), keep the live ones alive, start only goals the operator flagged, print the goal table. The arm is invoked ONCE; every wakeup runs /supervisor:topic-drive. Sweeps goal managers, never workers.
allowed-tools:
  - Task
  - Read
  - Edit
  - Monitor
  - Skill
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
  - Bash(wezterm cli list:*)
  - Bash(wezterm cli get-text:*)
  - ListAgents
  - SendMessage
  - ScheduleWakeup
  - CronCreate
  - mcp__tts__say
  - mcp__supervisor__list_agents
  - mcp__supervisor__agent_status
argument-hint: "[topic] [--interval <N>m] (topic detected when omitted)"
---
⚠️ **A voiced question is also a board card** — any `mcp__tts__say` containing `?` is paired with an `attention-ask.py post` within 60 s. Rule and detector: [`docs/voice-asks.md`](../docs/voice-asks.md).

Topic-loop — the **topic tier's arm**. It owns ONE topic's declared goal set and the goal
managers running on it. It manages **goal managers, and the workers it opens**.

⚠️ **The loop sweeps goal rows; opening a worker is a separate right.** The operator's
hierarchy (2026-10-23) puts the fleet manager at the top of everything, has a topic manager
managing goal managers **and** workers, and a goal manager managing workers
(`docs/session-tiers.md` § The four tiers). A worker is opened on demand with
`/supervisor:open --task`, never swept here. A worker row reaching this tier is not work for
the sweep: route it down to the goal manager that owns it, or up to the fleet manager when no
goal owns it.

⚠️ **This file is the ARM.** It resolves, confirms no manager already owns the subject,
arms the wakeup and exits. Every wakeup runs **`/supervisor:topic-drive "<topic>"`** — the
**round** — which owns the step list and the router table. ⚠️ **The step list is
deliberately NOT copied here**: a list with two homes drifts.

⚠️ **Never invoke this command from a worker session.** Starting a *topic* manager is the
operator's decision (the fleet manager performs the open); starting a *goal* manager is this
tier's. Rule: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles.

## Interval — the tick and its floor

`--interval <N>m` sets the tick. Default **15m**; floor **5m**. A value below the floor is
**clamped up and announced** (`⚠️ --interval 2m is below the 5m floor — clamped to 5m`);
never rejected. Parse it out of `$ARGUMENTS` before resolving the subject.

**The tick is one number, used twice.** Pass the clamped seconds to `ScheduleWakeup`'s
`delaySeconds`, and the *same* seconds to `manager-liveness.py --arm --interval`.

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **topic name**, matching
  a topic page in the vault's topics folder *(operator's vault; not shipped with this
  plugin)*. A name that resolves to a **goal** page is the wrong tier: print
  `❌ "<name>" is a goal, not a topic — use /supervisor:goal-loop.` and stop. Never silently
  take the goal branch; that is the collapse this split exists to end.
  - `/supervisor:topic-loop Sentry` → reads the Sentry topic page and tracks its declared goals
  - `/supervisor:topic-loop` (bare) → resolve from § Subject resolution below
  - No page resolves → report it and stop; never glob a substitute set

## Subject resolution — when `$1` is omitted

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` — the vault
resolution, the four-source chain, the recording contract, and the subject-status reconcile.
Read it there; **it is not restated here.**

**Only source 3 stays inline** — it is the one source no agent can be handed:

3. **Conversation** — the most recent `/supervisor:topic-loop`, `/supervisor:topic-status`,
   `/supervisor:topic-drive` or `/supervisor:topic-verify` argument in this conversation,
   then the most recent **topic** page referenced as a subject (a wikilink or a read/edited
   path — not a prose mention).

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a topic name: /supervisor:topic-loop "<name>"` and do nothing else.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`.

## Resolution — the topic page and its declared goals

1. **Resolve the topics folder from config, then find the topic page.** The folder is
   declared per vault: `vault-cli config list --output json` → the entry whose `path` is this
   vault → its `topics_dir`, defaulting to `23 Topics`. Then `find "$TOPICS_DIR" -maxdepth 1
   -iname "$SUBJECT.md"` and confirm `page_type: topic` **inside the frontmatter block**.
   ⚠️ **`-iname`, not a shell glob** — a plain `ls` glob is case-sensitive under zsh.
   Never accept a writing guide that merely describes topics; those live outside the folder.

2. **Read the `## Goals` list — the topic's declared membership.** ⚠️ **The heading is
   `## Goals`, a sub-heading under `# Scope`, not `# Goals`** — a literal `^# Goals` match
   returns nothing, and with no fallback that reads as an empty set rather than a miss.

   Membership is **declared, never re-derived**. An entry may be a goal or a task: a goal
   admits the tasks that name it, and a task entry admits that task directly. Never widen
   the list by a filename glob, a theme match or a content grep.

3. **The topic's `# Status Summary`** — hand-written prose, context only.

4. **No topic page** → print the missing page name and the fix (`create 23 Topics/<Topic>.md
   from the Topic Writing Guide template` *(operator's vault; not shipped with this
   plugin)*), then **stop**. No fallback — no filename glob, no membership scan, no theme
   match, no content grep.

Print `Tracked (N): <goal> · <goal> …` at manager start, and **re-print it whenever the
declaration read changes it**. Membership is never widened mid-run; the subject and branch
detection resolve **once** at manager start, never per wakeup.

4b. **⚠️ The phase precondition — a loop arms only a subject at `phase: execution`.** Having found the page, run `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` § *The phase precondition* **before arming anything**: read the page's `phase:` and, unless it reads `execution`, print the refusal naming `/vault-cli:execute-topic "<subject>"` and **STOP** — arm no cadence, record no arm, write no `status`, and **record the stand-down with `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manager-liveness.py --stop --subject "<subject>" --branch topic`**. ⚠️ **`planning`, `todo` and an absent `phase:` are all refusals.** ⚠️ **Say plainly that the operator re-runs this command after the flip** — the refusal ends the loop, so no cadence is left running to carry the retry. The round carries the same rule: a refused loop stops rather than re-arming, because a re-arming refusal keeps recording liveness and would hide the subject from the armed-set check. ⚠️ **The loop never runs the gate and never writes `phase:`** — the operator runs it. ⚠️ **The rule, the refusal shape, the version-drift rationale and the eight-command scope table live in the doc — read them there; they are not restated here.**

5. **Confirm no manager already owns this subject — before arming anything.** `ListAgents`
   for `<Topic> Manager`. Hit → print its name and age and **stop**. Miss → re-read the
   roster **once**; still a miss → proceed and arm. Never probe with `pgrep -f`.

## The loop — what runs, and where its rules live

1. Read the plugin's canonical procedure — `agents/topic-sweep-reader.md` — and follow it
   verbatim. The vault's Manager Session runbook is the operator's parallel copy and is
   **absent on a clean install**, so it is deeper reading, never the authority.

2. **Arm the round — the FIRST thing it does, ahead of the sweep.** `ScheduleWakeup` every
   `--interval`, with the **round** prompt `/supervisor:topic-drive "<topic>"` (plus the
   same `--interval`), **never this command**. ⚠️ **The arm is invoked ONCE; a wakeup must
   never re-invoke it.** ⚠️ **Arm with `ScheduleWakeup`** — a bare `CronCreate` forfeits the
   `noop` write-back and must not make step 7's correcting call either. **Record the arm in
   the same motion, right after the arm call:**

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py \
     --arm --subject "<topic>" --branch topic --interval <the clamped seconds just armed>
   ```

   ⚠️ **`--branch topic` is not optional** — the state slug carries the tier
   (`topic--<base>`), so a topic and a goal that share a name never overwrite each other's
   cadence record.

   ⚠️ **The wakeup target carries a hard ceiling — ≤10 KB — and `scripts/check-budget.py`
   enforces it.** `commands/topic-drive.md` is in that checker's explicit `WAKEUP_TARGETS`
   list. Keep the round under the ceiling by moving rationale to the runbook, never by
   deleting a rule.

## Manager contract — a manager manages, it does not build

The topic manager **manages; it does not build**. It never performs the topic's *work*: no
code edits, no repo/PR/k8s verification, no investigation, no real debugging.

- **The line is work vs management — not read vs write.** Management writes are the
  manager's own — the topic page, a goal's `status`/`phase`, stale stamps — via `Edit` /
  `vault-cli goal set`. Code, verification, investigation and the fix go to a worker.
- **A topic manager never sweeps a worker.** It starts, keeps alive and reaps **goal
  managers**, and it may open a worker on demand with `/supervisor:open --task` — but a
  worker row is reported downward to the goal manager that owns it, never acted on here.
  This is the boundary the split exists to draw.
- **Never present a manager-owned action as an `AskUserQuestion` option.** Opening or
  resuming an approved goal manager, readying a row, repairing a page box — execute them,
  then report them as a table line. The modal is reserved for operator-only decisions
  (`phase: todo` approval, a production-touching or irreversible action, a TDR), and even
  those reach the operator as an attention-board card carrying a `--dedup-key`.
- **No goal manager → start one, under the ranking, and only for a goal the operator
  FLAGGED.** `/supervisor:topic-spawn` is the only route; it holds no spawn tool and
  delegates to `/supervisor:open --goal`. ⚠️ **Rule 4 — restart is not open:** a goal
  carrying no operator flag **and no recorded `manager_session_id`** is **reported and
  held**, never opened, however free the slots are. `commands/topic-drive.md` § Start states
  the same rule and this line must not drift from it; the executable gate is
  `scripts/topic-spawn.py`'s `goal-unflagged` refusal.
  ⚠️ **That gate is the OPEN's and must never be applied to a resume.** A goal with a
  recorded id whose session is `ABSENT`, or whose loop is `STALE`, is a **restart** — rule 1
  restarts it automatically and the flag is not consulted. Holding those as "unflagged"
  makes the goal tier's automatic restart inert for every unflagged goal.
- **Anchored, always.** No work is delegated or spawned without a goal anchoring it.
  Read-only manager business (sweep, table, TTS) needs no anchor.

## Guardrails (non-negotiable)

- **Never fabricate progress** — disk facts, this sweep.
- **Print the artifact in an earlier message than** the one asserting any negative or
  attribution claim; a failed glob is not absence; pane text is multi-author.
- **The mandate is over approved rows only** — a `phase: todo` goal is not startable. The
  operator's `todo → planning` flip *is* the approval; `⌛ waiting-approval` rows are
  reported, never started.
- **TTS only when the operator must do something** — voice auto-on via
  `~/.claude/hooks/voice-mode.py`, `off` always wins.
- **Never decide an operator gate.** You surface it; the operator answers it in the session
  that raised it.
- **Answer an `AskUserQuestion` with `deny` + `message`, never `allow`.** Prefix:
  `Operator answer, via supervisor:` when the operator answered here, `Manager answer, via
  supervisor:` for your own scope decision. Neither releases an irreversible or
  production-touching action.
- **Close on complete evidence; never certify one.** A member goal closes on its `# Success
  Criteria` + `# Definition of Done`; **this topic closes on its `# Success Criteria` + its
  `# Completion Gate`** — a topic is the only level carrying both sets
  (`docs/session-progress.md:37`). When every box in both of a page's own sets reads `[x]`, the
  round runs the tier's verb — `vault-cli goal complete "<goal>" --vault "<vault>"` or
  `vault-cli topic complete "<topic>" --vault "<vault>"` — and quotes its verdict;
  the execution, the terminal guard and the `--vault` contract live in
  `${CLAUDE_PLUGIN_ROOT}/commands/steps/goal-closure.md` and are not restated here. ⚠️ **Never
  tick SC and never flip a page's status in order to close it** — a close that needs `--force`
  is one you do not make. ⚠️ **Tasks are not this tier's** — a task closes under its goal's
  manager, and this tier never reads a task file (`agents/topic-sweep-reader.md`).
  ⚠️ **Scoped to closure** — the resolution-time reconcile that flips a subject reading
  `todo`/`next` to `in_progress` is the manager's own write and is required, not forbidden.
