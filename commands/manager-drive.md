---
description: Drive ONE subject by hand — reap the finished, nudge stuck or error-marked workers, auto-resume confirmed orphans. The act leg of the show/check/act triad, runnable without arming a manager loop; reap runs before drive, always. The subject is detected when omitted, and the classification is composed from the existing sweep, never rebuilt.
allowed-tools:
  - Task
  - Read
  - Bash(grep:*)
  - Bash(vault-cli:*)
  - Bash(pgrep:*)
  - Bash(ps:*)
  - Bash(find:*)
  - Bash(stat:*)
  - Bash(python3:*)
  - Bash(mkdir:*)
  - Bash(date:*)
  - ListAgents
argument-hint: "[goal|topic] (detected when omitted)"
---

Manager drive slash command — the **act leg**, run once, by hand, against one subject.

This is the third leg of the triad `manager-status` (show) · `manager-verify` (check) · **`manager-drive` (act)**, and it is the same act leg `/manager-loop` composes on every tick. Running it by hand is what makes the act leg testable on its own: you see what drive would do to a subject without arming a loop over it.

⚠️ **"Testable" here means operator-runnable, not unit-tested — and that is this repo's existing shape, not a gap this change introduces.** Commands and agents in this plugin are markdown prompts: there is no harness that executes them, and no test file exists for any of the eleven commands or four agents already shipped. What this repo *does* test is the server (`server/*.test.mjs`) and the render scripts (`scripts/tests/`), and this change touches neither. The verification this artifact actually carries is the marketplace-clone e2e run recorded in its PR — the command appears in the loaded `slash_commands`, the agent registers and dispatches, and the loaded `manager-loop.md` is byte-identical to the worktree. A test asserting that a markdown prompt contains certain sentences would restate the file rather than exercise it.

⚠️ **A manager-tier verb — never run it from a worker session.** A worker carries a *task*; a manager carries a topic or goal and a loop. Driving a subject's tracked set from inside a worker collapses the two roles silently: the session keeps its task anchor while its turns reap, nudge and auto-resume another set's sessions (`${CLAUDE_PLUGIN_ROOT}/docs/session-tiers.md`; `docs/fleet-surface.md` § Session roles). A worker that needs a subject driven routes it to its manager with `SendMessage` and says so. Exercised in a manager session's runtime, never from the session that authored it — being one pass with no cadence limits the cadence, not the blast radius.

⚠️ **One-shot.** This command arms nothing and schedules nothing — it runs once and exits. Arming a *loop* remains a human act performed in a session created for it.

⚠️ **Reap runs before drive, always.** A completed task with zero open boxes is also idle, so a drive pass that runs first nudges a finished session to continue — and a session with nothing left to do that is told to continue will invent work. The sequence is **sweep → reap → drive → escalate**.

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **subject** — a goal name matching a page in `24 Goals/`, or a topic name matching a topic page in `23 Topics/`. The branch is detected from whichever page resolves; this command never assumes one.
  - `/manager-drive "MDM Bugs"` → explicit subject
  - `/manager-drive` (bare) → resolve the subject from § Subject resolution below — the same rule `/manager-loop` and `/manager-status` use, so a manager session's bare drive acts on the subject it is already watching

## Subject resolution — when `$1` is omitted

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` — the vault resolution, the four-source chain, the case-insensitive vault test, the "no fallback, ever" clause, the `Subject:` line, and the recording contract (which files, on which resolution). Read it there; **it is not restated here.**

**Only source 3 stays inline**, because it is the one source no agent can be handed — a subagent runs in a fresh context and cannot see the parent conversation:

3. **Conversation** — the most recent `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` or `/supervisor:manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention).

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /manager-drive "<name>"` and do nothing else.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs.

⚠️ **Resolve the vault first** — this command carries no `## Resolution` section of its own, so it takes the vault paragraph from the shared doc (`cwd → vault-cli config path`) instead of deriving the vault there.

## Procedure

1. **Resolve the subject (§ Subject resolution) and read its declared set.** Dispatch `Task(subagent_type: "vault-cli:work-on-goal-assistant")` is **not** the path here — read the page directly and take the declared set the way `/manager-loop` § Resolution does: a topic's `## Goals` members plus every task whose `goals:` names one (all three declaration shapes), or a goal's own tasks. Print `Tracked (N): <task> · <task> …`. **Never widen it** — no glob, no theme match, no content grep.

2. **Compose the sweep — do not rebuild it.** This command owns no classification. Dispatch the same agent `/manager-loop` does:

   `Task(subagent_type: "supervisor:manager-sweep-reader", prompt: <tracked set + declared-optional set + this run's ListAgents roster verbatim + vault + mode: snapshot + timestamp>)`

   The plugin prefix is required — a bare `manager-sweep-reader` resolves to a personal `~/.claude/agents/` copy, never the plugin agent. The agent owns the task-file read, the bucket classification **the vault's Manager Session runbook** declares, the id-set extraction and the orphan **candidates**. Bucket rules: runbook § Step 4.

   **If the delegation returns no usable table** — it errored, came back empty, or resolved to something that did not return the report — **stop and say so**. Do not fall back to a hand-rolled classification: `manager-loop` has a `box-table.py` renderer fallback, but that is a second *renderer*, not a second *classifier*, and this command has no renderer to fall back to.

3. **Confirm the orphan verdicts — this part is yours.** The sweep-reader returns **candidates** and stops by design; it cannot probe liveness without inheriting the caller's own ancestor-chain blind spot. For each candidate, probe every id in its set against the **session registry** first — `grep -l "<id>" ~/.claude/sessions/*.json`, then `ps -p <pid>` on the file's pid — and treat the task as alive if **any** id holds an entry against a running pid. Then run `pgrep -f "<id>"` and `ps -eo pid,args | grep -F "<id>"`: a hit on either also means alive, but an empty argv read is **indeterminate, never dead** — a live session usually carries its id in no argv (measured 2026-09-22/23: three live sessions read 0 under both). Death needs registry absence plus the drive leg's transcript-staleness clause. Also collect the id sets across the whole tracked set first and flag any id on **two or more non-terminal** tasks as a shared collision — resume **neither**.

4. **Dispatch the act leg.**

   `Task(subagent_type: "supervisor:manager-drive", prompt: <subject + tracked set + classification + confirmed orphan verdicts + roster + vault + timestamp>)`

   It reaps, then nudges, then runs the auto-resume gate, and returns the action lines. Its rules — the reap test, the ten gate clauses, the re-probe-at-spawn-site rule, the crash-loop cap — live in `agents/manager-drive.md` and are not restated here.

5. **Print what came back, voice the nudges, and escalate.** Reproduce the agent's action lines verbatim, including its `Not resumed` and `Escalated` sections — a near-miss clause is the most useful line in the report. **Voice the `Nudged` lines** with `mcp__tts__say` (voice-mode gated): the agent owns the message, you own the voice, because a subagent has no TTS. Then the operator-facing tail: any gate that needs their decision goes out as **`/supervisor:jump <pane-id>`**, never as a command for them to run here (the approval belongs to the session that raised it).

## What this command must never do

- **Never classify.** The sweep is `supervisor:manager-sweep-reader`'s, and a second classification here would drift from the one `/manager-loop` acts on.
- **Never decide an operator gate.** You surface it; the operator answers it in the session that raised it.
- **Never close a worker's session.** `/vault-cli:sync-progress` and `/vault-cli:session-close` read the parent conversation, and every route into a worker's pane is refused. Reaping means *sending the evidence*, nothing more.
- **Never claim an act the agent reported it could not perform.** If a tool failed to bind, the agent says so; carry that through rather than smoothing it.
