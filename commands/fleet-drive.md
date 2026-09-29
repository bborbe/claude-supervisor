---
description: Restart stalled-but-unblocked fleet sessions — compose the fleet sweep read-only, verify each parked session's blocker live, nudge only the verified-unblocked, escalate the rest grouped by cause. Takes no arguments. One-shot; arms nothing.
allowed-tools:
  - Task
  - SendMessage
  - ListAgents
  - Read
  - Bash(python3:*)
argument-hint: "(no args — one drive pass: verify, draft, send)"
---

Fleet drive slash command — the fleet layer's **drive** verb, run once, by hand.

The fleet has a show (`/supervisor:fleet-status`) and an act loop (`/supervisor:fleet-loop`), which dispatches this leg on every round. This command is that same leg, run once, by hand — for a pass between rounds, or when the loop is not armed. It is the fleet sibling of `/supervisor:manager-drive`: a thin command dispatching an agent that carries the logic.

⚠️ **A fleet-loop verb — never run it from a worker session.** A worker carries a *task*; a manager carries a topic or goal and a loop. Running a fleet-wide sweep inside a worker collapses the two roles silently: the session keeps its task anchor while its turns sweep the whole fleet (`${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles). A worker that wants a sweep routes it to its manager with `SendMessage` and says so. A change to this command is exercised in a manager session's runtime, never from the session that authored it.

⚠️ **One-shot.** It arms no cadence and schedules nothing. `/supervisor:fleet-loop` dispatches the same agent on every round; this command is the by-hand single pass, mirroring `/manager-drive`.

⚠️ **It never classifies and never joins.** The classification and the roster-name → `sessionId` join both belong to `supervisor:fleet-sweep-reader`; this command composes them. A second copy of either drifts silently.

## Arguments

**No arguments, by design.** One command, one behaviour: compose the sweep, verify each parked session, draft the nudges, send them. There is no verify-only mode — a pass that drafts but sends nothing is a second command in disguise, and `/supervisor:fleet-status` already covers the read-only case.

## Procedure

1. **Roster.** Call `ListAgents` once. Note this session's own name — it is never a nudge target.

2. **Compose the sweep, read-only.**

   `Task(subagent_type: "supervisor:fleet-sweep-reader", prompt: <roster verbatim + this session's id + vault path and tasks dir + round timestamp + "persist: false">)`

   `persist: false` is load-bearing: a by-hand drive run between two manager rounds must not advance `stall_count` or consume the snapshot the next manager round diffs against. The plugin prefix is required — a bare name resolves to a personal `~/.claude/agents/` copy.

   **No usable digest** (error, empty, or the header lacks `snapshot written: skipped`) → stop and say so. Never fall back to a hand-rolled classification: every verdict would be UNKNOWN, and an empty fleet and a dead source must never render the same.

3. **Dispatch the drive leg.**

   `Task(subagent_type: "supervisor:fleet-drive", prompt: <digest verbatim + this session's name and id + vault path + round timestamp>)`

   The agent loads the ledger, suppresses re-nudges, verifies each `parked` session's blocker live, assigns revive / blocked / unverifiable / finished, persists the ledger and returns drafted nudges. Its rules live in `agents/fleet-drive.md` and are not restated here.

4. **Send — from this session only.** Send every `REAPS` line first, then every `NUDGES` line — reap before drive, so a finished session is never told to continue. For each line, re-check the target's roster status first — moved to `busy`/`shell` since step 1 → skip and report the skip (never preempt a busy peer); otherwise `SendMessage(to: <exact roster name>, message: <text>)`. Every send stays in this session because a sub-agent has no cross-session address — a reply to a sub-agent's message lands here after it has returned. Report any failed send; the ledger already counts it as nudged, so the next round suppresses rather than nags.

5. **Print** the agent's report verbatim — table, escalation groups, the `Waiting on your keystroke` list, ledger line — with the one addition below, then `Sent: <n>` with one line per recipient, and `Skipped: <n>` with reasons.

   **The report carries a `Waiting on your keystroke` list** — the round's operator to-do list, built by the drive agent from the digest's `CLOSERS` lines and its own `blocked` verdicts. Print it as it arrives; its rules live in `agents/fleet-drive.md` and are not restated here.

   **Every `ESCALATION` row carries its jump link — and this session renders none of them.** The drive agent hands each row its already-rendered link, produced by `fleet-sweep-reader` from the pane it resolved. Print the rows as they arrive. **Run no per-row pane read here**: no `jump-link.py`, no `who-needs-me.py --pane-for` — the sweep owns that lookup, and it already holds the session-registry join.

   The resolution the sweep performs prefers the session's recorded pane, and falls back to the **registry's current name** against the pane titles when that pane is gone — a pane id is a lease, WezTerm renumbers and reuses them, so a live session's record can name a pane that no longer exists. The fallback's name is read from the registry at call time, which is what makes `/rename` tracked rather than broken; a name carried in from anywhere else is the stale join that fails silently. An ambiguous match refuses rather than picks. A row the sweep still could not resolve arrives as `no pane — <reason>`; print it verbatim — never a blank, never a bare `—`.

   Print the link the agent carries, never a bare `/supervisor:jump <N>` and never a hand-built URL — `${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/docs/session-tiers.md` § *Handing the operator a pane* (keep-in-sync).

## What this command must never do

- **Never classify, never join names to ids.** Both are `fleet-sweep-reader`'s.
- **Never send a course correction.** A nudge is read-only context that authorises nothing; anything stronger is drafted for the operator's explicit yes.
- **Never nudge past an operator gate.** A pending pick, `review:` / `you run:` line, permission prompt, or an `approve:` line that fails the agent's routine-continue table (`agents/fleet-drive.md` step 4a) is a blocker, never a revive. The table is the agent's alone — not restated here.
- **Never close a session.** A reap is the disk-evidence message; closing stays the worker's own call.
- **Never write inside the vault.** The ledger lives at `~/.claude/state/fleet-drive/ledger.json`, written by the agent.
- **Never `AskUserQuestion` mid-run.** Anything needing a decision goes into the escalation batch.
