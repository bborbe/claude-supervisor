---
description: Read-only fleet roster — every peer Claude Code session, its live status, and its vault task mapping. Zero side effects. Ends with a blocked-by-you jump list (waiting sessions + clickable jump links).
allowed-tools:
  - ListAgents
  - Bash(python3:*)
  - Bash(wezterm cli list:*)
  - Bash(date:*)
  - Read
argument-hint: (no args)
---

Answer one question: **what is everyone else doing right now?**

This is NOT `/supervisor:worker-drive`. `/supervisor:worker-drive` = what should **I** do next in this session; `/fleet-status` = what is **everyone else** doing across the machine. Different question, different scope — `/supervisor:worker-drive` never surveys peers, `/fleet-status` never recommends this session's next action.

## What this command does

Pure snapshot, no mutation, no messages sent. Safe to run as often as you like.

1. `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-board.py` — **the board**. One row per live session, read through the plugin's single liveness reader (`scripts/session-liveness.py` — the registry **and** the heartbeat store, so a headless or cluster worker is a row rather than an omission), each classified into `running` / `idle` / `needs-input` / `problem`. It performs the whole join itself — registry, attention store, transcript ages and the vault task lookup — and **asserts its own coverage**: it exits non-zero rather than printing a table that silently omits a session, because a table that renders correctly and drops a row is the exact failure this board exists to prevent.

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-board.py --json \
     | python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/box-table.py
   ```

   `--json` also emits the counts, the per-row gate detail and the residual list — transcript-fresh sessions the registry does not carry (a headless worker holds no registry entry at all). The extra keys are safe: `box-table.py` reads only `header`, `rows` and `widths`.

2. `ListAgents` — the live roster, for the statuses the board does not carry and for the `waiting` set the blocked-by-you section below is built from. **The name is the task the session is on**; the status is live.

   Never infer a peer's directory from its `ListAgents` name: names are reused across days and `[ref]` is not a session-id prefix.
3. **Render the board** — indented two spaces under the lead line, **exactly per Fleet Manager Session runbook (per-vault) § Sweep output — the fleet table**. That section is the single source for the frame (a timestamped marker line, then a box indented two spaces under it), the columns, the widths and the icons, and this command must never restate them. Never hand-draw the box. `/fleet-loop` reads the same section, so both commands render identically by construction — the same arrangement the worker pair has against the Manager Session runbook § Sweep output.

   **No id column.** Key on the session id internally; the operator sees the name.

   **The join is on the session id, not the name — and it now lives in `fleet-board.py`, not in prose here.** The board keys on the registry's `sessionId`; `fleet-sessions.py` is a *lookup* (session id → task title), never the row set: measured 2026-09-21 it returns 2352 rows, every stamped task ever, so using it as a roster would render the table useless.

   **Why not the name.** The previous join was `ListAgents` name == `fleet-sessions.py` `WORKING ON`, which holds only because `/rename <task title>` happens to make the two strings equal. Rename a session to anything else and the join silently drops it: its open gates stop appearing in the sweep while the session is alive and possibly blocked on an unanswered gate. Measured 2026-09-18 — the same name-match in `/supervisor:open` Step 2C spawned a **second** manager onto a live topic, and neither knew about the other. The session id is stable across `/rename`; verified 2026-09-18 on three sessions carrying `formerNames`, each holding one constant `sessionId` through every rename.

   ⚠️ **`[ref]` is NOT the session-id prefix** — verified 2026-08-21: refs are 6 chars (`3daaa7`, `56cff2`), session ids are 8 (`b7c33528`); no ref is a prefix of any id. Joining on it matches nothing. It is also computed per roster read, so it is **not stable across time** — never persist anything keyed on it.

   **The name is for display, not for joining.** Names are normally unique among live peers (13/13 distinct when checked 2026-08-21), which is why the name reads well in the table — but uniqueness is not stability. Keep the name as the operator-facing label and the session id as the key.

   ⚠️ **Do not resolve names to session ids via `~/.claude/history.jsonl`.** `/rename` values are recoverable there (549 of them), but names ARE reused **across time** — "a dependency task" was set by two different sessions on different days. Latest-wins on history produces confidently wrong ids. The registry is the right source: it holds only live sessions and carries the id directly.

   **Show the operator the name, always.** The name is what they set with `/rename` and what they recognise. Sessions never renamed show their auto-generated name (`personal-76`, `boss-48`) — say "never renamed" rather than presenting it as meaningful.

   A peer may appear in only one source — say so rather than dropping the row (e.g. a session that hasn't stamped a task file yet, or a vault task stamped by a session no longer running).

4. **Fallback: resolve the name across every vault when `fleet-sessions.py` returns nothing for it.** `fleet-sessions.py` only maps sessions that wrote a `claude_session_id:` stamp into a task file — plenty of sessions are working a task they never stamped, and those come back blank. Before writing `—` for a peer, resolve its `ListAgents` name through the sweep reader's own resolver:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/resolve-task-file.py" "$NAME" | cut -f2
   ```

   `scripts/resolve-task-file.py` is the **single home** of the name-based resolution rule — read it there rather than restating it. It searches **every configured vault**, `tasks_dir` before `goals_dir`, and resolves only on exactly one hit in the tier reached; a blank value means no resolution, and the peer renders `—`. ⚠️ **A value of `UNKNOWN` is not a blank and must not render `—`** — it means the vault list itself could not be read, so say that rather than reporting a peer as unowned. It is carried on **stdout** precisely so this pipeline sees it; the same reason also goes to stderr as `DEGRADED`. ⚠️ **A single-`$VAULT` loop used to sit here and carried the same false-*unowned* defect the sweep reader had** — a peer working a task in a sibling vault rendered `—` as though it were doing nothing. That is why this step was repointed rather than left as a second copy of the rule.

   This is a **filename-keyword match in a known flat tree** — the documented narrow exception to "semantic search for discovery", not a discovery sweep. Do not `find`, do not fuzzy-match: exact `<name>.md` only. A near-miss is worse than a blank, because it attributes work to a peer that isn't doing it.

   Measured 2026-08-21: stamp-only resolution covered 4 of 13 peers; adding this fallback took it to **7 of 13** — it recovered two sessions whose task file exists under the exact session name (`a bug report`, `a sample task`) and one that resolves to `23 Goals/`, not `24 Tasks/` (`Optimize Cluster A Resource Usage`) — which is why the goals dir is in the loop at all.

   The rest stay unresolved by design, in two kinds: **never renamed** (`boss-48`, `gaming-f0`) and **name diverges from the vault title** (`github enable automerge agent` vs the task `a sample task`). Report those as `—` and say which kind. Never guess across a divergence.

## Status semantics — do not over-read

From the Claude Code cross-session messaging notes (operator's vault; not shipped with this plugin) § Status semantics. Repeat this caveat in the output, not just internally:

| Status | Meaning | Safe to conclude |
|---|---|---|
| `busy` | actively working | **owns its task — leave it alone** |
| `shell` | running a bash command | actively working |
| `waiting` | transient | ❌ **not** "blocked on a human" — observed flipping to `shell` within 5 min |
| `idle` | finished a turn, nothing queued | may be parked awaiting input, may be done — cannot tell from status alone |
| *(blank)* | no recent activity | nothing |

**Only `busy` and `shell` support a confident conclusion.** Never report a `waiting` peer as stuck or needing the operator. **Statuses go stale in minutes** — a status printed a few minutes ago is not necessarily still true; re-run `ListAgents` before relying on it for anything beyond this report.

## Output shape

Lead with the box rendered per § Sweep output, indented two spaces, under a lead line of its own — `/fleet-status` is a snapshot rather than a tick, so its lead line carries the timestamp but **no `✓` marker**. Then, tersely:

- Count by status (`N busy, N shell, N waiting, N idle, N blank`).
- Flag any row present in `ListAgents` but absent from `fleet-sessions.py` (no vault task stamp) or vice versa (stamped task, no live session) — these are not errors, just note them.
- **No recommendation, no verdict, no `👤 You:` / `⏰ Next:` panel.** This command reports; it does not decide. If the caller wants "what should I do about this," that's a separate judgment call outside this command's scope — say so rather than inventing one.

## Blocked-by-you section — the jump list

After the counts and one-source notes, emit a **blocked-by-you group** so the operator can clear waiting sessions in one pass. Icons per the operator's Icons reference: ⌛ waiting (possibly on you) · ⏸️ blocked · ⚠️ stale.

1. **Collect** — from the step-1 `ListAgents` roster, every session with status `waiting`. These are the "possibly on you" set. Status semantics: `waiting` is transient, **NOT** confirmed blocked-on-human — label the section honestly and re-verify a row before acting on it (task phase `human_review`, or the session's last message).
2. **Resolve each waiting session's pane** — `wezterm cli list` → TITLE (the `-n` session name) == `ListAgents` name → **PANEID** (not TABID). Unresolvable rows print the session name only.
3. **Emit per row + a jump suggestion for the next one** (oldest wait first):

   ```text
   ⌛ Blocked by you (N waiting — waiting ≠ confirmed blocked; verify before acting)
     1. <name> — <age> · jump: <jump-link.py PANEID>
     2. <name> — <age> · jump: <jump-link.py PANEID>
     Next blocker to jump to: <name> → <jump-link.py PANEID --label>
   ```

   **Each `jump:` field is the one-line output of `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py <PANEID>` — never a hand-written URL, and never the token.** With the local fleet-jump server configured that is a clickable `http://127.0.0.1:1337/jump?pane=<N>&t=…` link the operator follows with **SHIFT+CMD+click** (the click is three keys, not one: inside a mouse-reporting TUI like Claude Code the modifier is what bypasses reporting, and WezTerm's own config documents this); without it the script prints the `/supervisor:jump <N>` command, so the row is always usable. **Never print a bare URL you built yourself** — the token lives in a 0600 file outside every repo, so a hand-written link is either broken or leaks the token into the repo. Emitting the link is a read-only act: printing a URL mutates nothing, which is what keeps this command inside its no-mutation contract.

   The link carries a pane id; **hand over a pane id, never a tab id** — a tab that moves windows is renumbered, so a handed-over `--tab-id` goes dead (measured 2026-09-18: three spawned workers routed as tabs 158/159/160 in window 0 became tabs 163/164/165 in window 2, and `activate-tab --tab-id 159` failed outright with *"could not determine which pane should be active"*, while `activate-pane --pane-id 239` worked immediately). Cross-window raise still needs Accessibility — no `activate-window` in wezterm CLI.

   **Names lead, numbers serve the command.** Every row and every mention leads with the session/task name the operator recognizes; the `[ref]` and tab id are secondary, for the command only — never reference a session by bare number in prose.

## Rules

- **Read-only, always.** Never `SendMessage`. Never write files. Never mutate vault state.
- **Never conflate crews.** `ListAgents` covers interactive Claude Code sessions only — Pattern B k8s agents (task files + watchers) are a different crew with a different contract; don't fold them into this table.
- **Don't editorialize on `idle`/`waiting`.** If asked "is session X stuck?", the honest answer from status alone is "can't tell — check its task file or ask it," not a guess.
