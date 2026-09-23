---
description: Drive ONE subject by hand — reap the finished, nudge stuck or error-marked workers, auto-resume confirmed orphans. The act leg of the show/check/act triad, runnable without arming a manager loop; reap runs before drive, always. The subject is required and the classification is composed from the existing sweep, never rebuilt.
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
  - Bash(date:*)
  - ListAgents
argument-hint: "<goal|topic>"
---

Worker drive slash command — the **act leg**, run once, by hand, against one subject.

This is the third leg of the triad `worker-status` (show) · `worker-verify` (check) · **`manager-drive` (act)**, and it is the same act leg `/worker-manager` composes on every tick. Running it by hand is what makes the act leg testable on its own: you see what drive would do to a subject without arming a loop over it.

⚠️ **"Testable" here means operator-runnable, not unit-tested — and that is this repo's existing shape, not a gap this change introduces.** Commands and agents in this plugin are markdown prompts: there is no harness that executes them, and no test file exists for any of the eleven commands or four agents already shipped. What this repo *does* test is the server (`server/*.test.mjs`) and the render scripts (`scripts/tests/`), and this change touches neither. The verification this artifact actually carries is the marketplace-clone e2e run recorded in its PR — the command appears in the loaded `slash_commands`, the agent registers and dispatches, and the loaded `worker-manager.md` is byte-identical to the worktree. A test asserting that a markdown prompt contains certain sentences would restate the file rather than exercise it.

⚠️ **One-shot, and not a manager.** This command arms nothing and schedules nothing — it runs once and exits. Arming a *loop* remains a human act performed in a session created for it (see `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles). Running this command from an ordinary session is fine precisely because it is one pass with no cadence.

⚠️ **Reap runs before drive, always.** A completed task with zero open boxes is also idle, so a drive pass that runs first nudges a finished session to continue — and a session with nothing left to do that is told to continue will invent work. The sequence is **sweep → reap → drive → escalate**.

## Arguments

- **`$1` (required):** the **subject** — a goal name matching a page in `24 Goals/`, or a topic name matching a topic page in `23 Topics/`. Unlike `/worker-manager` and `/worker-status`, this command does **not** detect the subject: it is the by-hand entry point, and a silent guess at scope is the failure both of those commands exist to prevent. No `$1` → print `❌ Pass a goal or topic name: /manager-drive "<name>"` and stop.
- The subject-resolution rule itself (session state, session name, conversation, vault's last) lives once, in `/worker-status` § Subject resolution. This command does not restate it and does not carry a copy — if you want detection, run `/worker-status` and pass what it resolved.

## Procedure

1. **Resolve the subject and read its declared set.** Dispatch `Task(subagent_type: "vault-cli:work-on-goal-assistant")` is **not** the path here — read the page directly and take the declared set the way `/worker-manager` § Resolution does: a topic's `## Goals` members plus every task whose `goals:` names one (all three declaration shapes), or a goal's own tasks. Print `Tracked (N): <task> · <task> …`. **Never widen it** — no glob, no theme match, no content grep.

2. **Compose the sweep — do not rebuild it.** This command owns no classification. Dispatch the same agent `/worker-manager` does:

   `Task(subagent_type: "supervisor:worker-sweep-reader", prompt: <tracked set + declared-optional set + this run's ListAgents roster verbatim + vault + mode: snapshot + timestamp>)`

   The plugin prefix is required — a bare `worker-sweep-reader` resolves to a personal `~/.claude/agents/` copy, never the plugin agent. The agent owns the task-file read, the seven-bucket classification, the id-set extraction and the orphan **candidates**.

   **If the delegation returns no usable table** — it errored, came back empty, or resolved to something that did not return the report — **stop and say so**. Do not fall back to a hand-rolled classification: `worker-manager` has a `box-table.py` renderer fallback, but that is a second *renderer*, not a second *classifier*, and this command has no renderer to fall back to.

3. **Confirm the orphan verdicts — this part is yours.** The sweep-reader returns **candidates** and stops by design; it cannot probe liveness without inheriting the caller's own ancestor-chain blind spot. For each candidate, probe every id in its set with **both** `pgrep -f "<id>"` and `ps -eo pid,args | grep -F "<id>"`, and treat the task as alive if **either** returns a hit. Also collect the id sets across the whole tracked set first and flag any id on **two or more non-terminal** tasks as a shared collision — resume **neither**.

4. **Dispatch the act leg.**

   `Task(subagent_type: "supervisor:manager-drive", prompt: <subject + tracked set + classification + confirmed orphan verdicts + roster + vault + timestamp>)`

   It reaps, then nudges, then runs the auto-resume gate, and returns the action lines. Its rules — the reap test, the eight gate clauses, the re-probe-at-spawn-site rule, the crash-loop cap — live in `agents/manager-drive.md` and are not restated here.

5. **Print what came back, voice the nudges, and escalate.** Reproduce the agent's action lines verbatim, including its `Not resumed` and `Escalated` sections — a near-miss clause is the most useful line in the report. **Voice the `Nudged` lines** with `mcp__tts__say` (voice-mode gated): the agent owns the message, you own the voice, because a subagent has no TTS. Then the operator-facing tail: any gate that needs their decision goes out as **`/supervisor:jump <pane-id>`**, never as a command for them to run here (the approval belongs to the session that raised it).

## What this command must never do

- **Never classify.** The sweep is `supervisor:worker-sweep-reader`'s, and a second classification here would drift from the one `/worker-manager` acts on.
- **Never decide an operator gate.** You surface it; the operator answers it in the session that raised it.
- **Never close a worker's session.** `/vault-cli:sync-progress` and `/vault-cli:session-close` read the parent conversation, and every route into a worker's pane is refused. Reaping means *sending the evidence*, nothing more.
- **Never claim an act the agent reported it could not perform.** If a tool failed to bind, the agent says so; carry that through rather than smoothing it.
