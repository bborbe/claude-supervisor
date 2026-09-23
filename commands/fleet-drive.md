---
description: Restart stalled-but-unblocked fleet sessions — compose the fleet sweep read-only, verify each parked session's blocker live, nudge only the verified-unblocked, escalate the rest grouped by cause. One-shot; arms nothing.
allowed-tools:
  - Task
  - SendMessage
  - ListAgents
  - Read
argument-hint: "[--dry-run] — no argument performs the sweep; --dry-run verifies and drafts but sends nothing and writes no ledger"
---

Fleet drive slash command — the fleet layer's **drive** verb, run once, by hand.

The fleet has a show (`/supervisor:fleet-status`) and an act loop (`/supervisor:fleet-loop`), but nothing restarts a session that is idle with open work and nothing blocking it. This command does, for one pass. It is the fleet sibling of `/supervisor:manager-drive`: a thin command dispatching an agent that carries the logic.

⚠️ **A fleet-manager verb — never run it from a worker session.** A worker carries a *task*; a manager carries a topic or goal and a loop. Running a fleet-wide sweep inside a worker collapses the two roles silently: the session keeps its task anchor while its turns sweep the whole fleet (`${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles). A worker that wants a sweep routes it to its manager with `SendMessage` and says so. A change to this command is exercised in a manager session's runtime, never from the session that authored it — `--dry-run` suppresses the sends, not the role collapse.

⚠️ **One-shot.** It arms no cadence and schedules nothing. Wiring it into the `fleet-loop` loop is separate work.

⚠️ **It never classifies and never joins.** The classification and the roster-name → `sessionId` join both belong to `supervisor:fleet-sweep-reader`; this command composes them. A second copy of either drifts silently.

## Arguments

- **`--dry-run` (optional):** verify and draft, print what would be sent, send nothing, write no ledger.

Parse `$ARGUMENTS`: contains `--dry-run` → `dry-run: true` everywhere below; otherwise `dry-run: false`. Any other token → print `❌ Unknown argument: <token>` and stop.

## Procedure

1. **Roster.** Call `ListAgents` once. Note this session's own name — it is never a nudge target.

2. **Compose the sweep, read-only.**

   `Task(subagent_type: "supervisor:fleet-sweep-reader", prompt: <roster verbatim + this session's id + vault path and tasks dir + round timestamp + "persist: false">)`

   `persist: false` is load-bearing: a by-hand drive run between two manager rounds must not advance `stall_count` or consume the snapshot the next manager round diffs against. The plugin prefix is required — a bare name resolves to a personal `~/.claude/agents/` copy.

   **No usable digest** (error, empty, or the header lacks `snapshot written: skipped`) → stop and say so. Never fall back to a hand-rolled classification: every verdict would be UNKNOWN, and an empty fleet and a dead source must never render the same.

3. **Dispatch the drive leg.**

   `Task(subagent_type: "supervisor:fleet-drive", prompt: <digest verbatim + this session's name and id + vault path + round timestamp + "dry-run: true|false">)`

   The agent loads the ledger, suppresses re-nudges, verifies each `parked` session's blocker live, assigns revive / blocked / unverifiable / finished, persists the ledger and returns drafted nudges. Its rules live in `agents/fleet-drive.md` and are not restated here.

4. **Send — from this session only.** For each `NUDGES` line, unless `--dry-run`: re-check the target's roster status first — moved to `busy`/`shell` since step 1 → skip and report the skip (never preempt a busy peer); otherwise `SendMessage(to: <exact roster name>, message: <text>)`. Every send stays in this session because a sub-agent has no cross-session address — a reply to a sub-agent's message lands here after it has returned. Report any failed send; the ledger already counts it as nudged, so the next round suppresses rather than nags.

5. **Print** the agent's report verbatim — table, escalation groups, ledger line — then `Sent: <n>` with one line per recipient, and `Skipped: <n>` with reasons. For any escalated operator gate whose pane is known, hand over `/supervisor:jump <pane-id>`; never a command for the operator to run here.

## What this command must never do

- **Never classify, never join names to ids.** Both are `fleet-sweep-reader`'s.
- **Never send a course correction.** A nudge is read-only context that authorises nothing; anything stronger is drafted for the operator's explicit yes.
- **Never nudge past an operator gate.** A pending pick, `approve:` line or permission prompt is a blocker, never a revive.
- **Never write inside the vault.** The ledger lives at `~/.claude/state/fleet-drive/ledger.json`, written by the agent.
- **Never `AskUserQuestion` mid-run.** Anything needing a decision goes into the escalation batch.
