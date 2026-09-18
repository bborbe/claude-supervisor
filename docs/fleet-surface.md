# Fleet surface — spawn shape and table render spec

The canonical, self-contained home for the two operational specs the fleet commands
(`/supervisor:worker-manager`, `/supervisor:fleet-manager`, `/supervisor:fleet-status`,
`/supervisor:worker-status`) depend on.

These specs previously lived in an Obsidian vault runbook and were referenced by absolute
path, because Obsidian wikilinks do not resolve across vaults. A plugin cannot depend on a
particular person's vault to describe its own mechanics, so the operational content lives
here and the vault runbooks keep only the per-vault operating procedure.

Change the shape here, then update every call site, then re-run the grep that proves no
copy diverged.

## Spawn a worker

**A — `spawn_agent` (preferred).** The prompt is a spawn *argument*, so the task never goes
over keystrokes:

```
mcp__supervisor__spawn_agent(prompt="...", cwd="/path", label="alpha")                     # TAB worker
mcp__supervisor__spawn_agent(prompt="...", cwd="/path", label="alpha", interactive=false)  # headless
```

⚠️ **`interactive` defaults to `true`** (`server/supervisor.mjs`, `spawnAgent`). The first
call is a real `claude` in a wezterm tab and **its prompts are answered in that tab** — the
manager is not supervising it. Only the second, with `interactive:false` passed explicitly,
is headless and parks prompts for the manager.

`policy` is headless-only, which is why a tab-producing call site never carries it.

The launcher is resolved from `vault-cli config` (`claude_script`), falling back to the
`personal` entry. Never invoke the bare `claude` binary — that routes around the router, the
MCP config and the model selection. Override with `SUPERVISOR_CLAUDE_CMD`.

**Precondition, and it fails silently:** `supervisor` must be in the launcher's MCP config
allowlist. Launchers that pass `--strict-mcp-config` exclude plugin-provided MCP servers —
the plugin reads enabled and `claude mcp list` reports healthy while the tools are absent
from every session. If `mcp__supervisor__*` is unavailable, use path B.

**B — raw `wezterm cli spawn` (fallback, and the path for resuming a live session).**

```bash
wezterm cli spawn -- bash -lc 'unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; exec "<claude_script>" --resume <session_id> -n "<title>" "/color pink"'
```

Two load-bearing details:

- **Keep the `unset`; its reason is NOT established.** The peer-registration explanation was
  refuted by experiment. It is harmless and cheap, so it stays — do not restate the socket
  story as its justification.
- **The colour goes in the spawn; the work command is typed afterwards.** Claude Code parses
  one submitted message as one command, and `/color` takes the *entire* trimmed argument, so
  seeding it inside a larger prompt yields `Invalid color`. Type the work command after:
  `wezterm cli send-text --pane-id <N> --no-paste $'<work command>\r'`. The trailing `\r` is
  load-bearing; a bare `\n` leaves the text unsubmitted.

⚠️ **Typing is legitimate only into a pane this recipe just spawned, while it is still idle.**
Answering another session's operator gate by `send-text` is forbidden. Typing into a pane
showing `Enter to select` turns any keystroke into a menu selection.

**Why A is preferred over B:** A removes the typed *task*; B only hardens the typist.
`send_agent_message` is not an alternative channel — it types and steals focus. Two limits on
A: it can only supervise sessions **it created**, and `spawn_agent({resume})` refuses a
session that is still live. Measured 2026-09-18: a call site that omitted the follow-up
`send-text` left a worker (BRO-21462, spawned 09:03) idle for 16 min while its task file
stayed untouched.

⚠️ **The `resume` refusal is a property of `mcp__supervisor__spawn_agent`, not of the
platform.** `resumeSupportError` (`server/tab.mjs`) rejects `resume` alongside
`interactive:true` because the *tool's* tab path launches the `cc-*` launcher without handing
it `--resume` — the flag would be dropped and you would get a fresh conversation while
believing you were continuing one. Path B hands `--resume` to the launcher directly, so it is
**interactive and resuming at once**. Read the refusal as *"this tool's tab path cannot carry
a resume"*, never as *"a resume cannot be interactive"*.

**Resume takes two independent decisions — first the path, then the drive.** Liveness picks
the path; *why the session died* picks whether anything drives the resumed pane. They do not
substitute for one another.

**1 — Path, by liveness:**

| Resuming… | Path | Why |
|---|---|---|
| a session **proven dead** | **A**, `interactive=false, resume="<id>"` — or **B** where headless is not permitted (below) | A is headless, so prompts park for the manager. On A, `resume` **requires** `interactive:false` — the pair is refused there, not silently downgraded |
| a session whose liveness **cannot be determined** | **B**, `wezterm cli spawn --resume` | A refuses an unverifiable resume by design |

⚠️ **Where headless is not permitted, the proven-dead row moves to B.** Some phases forbid
headless fleet-wide — the operator's standing constraint during manager-system development:
*"we are currently not running headless ever because we are in the development of the manager
system."* That constraint is **phase-scoped, not permanent**; do not delete path A on account
of it. Under it a proven-dead session resumes via **B**, exactly as an indeterminate one does.
The row's preference for A is a preference, not a requirement, and it is the only row affected.

**2 — Drive, by cause of death:**

| The session died… | Then |
|---|---|
| **mid-work** | the resumed pane lands at an empty composer with nothing driving it — **deliver the work**: as the `prompt` argument on A, or typed after the colour on B |
| **holding a human gate** | **drive nothing.** The pane comes up holding its own unanswered gate; idling there is the *correct* state |

⚠️ **A resume onto an unanswered operator gate must never be driven.** The two cases are
indistinguishable from the session id alone — read the transcript tail before choosing. The
error is not symmetric: an undriven mid-work resume wastes a session, a driven gate-death
resume **answers a question on the operator's behalf**.

## Sweep output — the fleet table

**This section is the single source for the fleet table.** `/supervisor:fleet-manager` and
`/supervisor:fleet-status` both render it and neither carries its own spec.

```
14:30 ✓ Fleet — 27 sessions · 6 busy · 3 shell · 4 waiting · 12 idle · 2 orphaned
  ┌────────────────────────────┬─────────────────────┬────────────────────────────────────┬─────────────┬───────────┐
  │ Session                    │ Status              │ Vault task                         │ Project     │ Last      │
  ├────────────────────────────┼─────────────────────┼────────────────────────────────────┼─────────────┼───────────┤
  │ Dark-Factory Refuses …     │ 🔄 progressing      │ Dark-Factory Refuses to Start …    │ personal    │ 2m ago    │
  │ Sentry Manager             │ ⏸️ parked           │ Map Sentry Projects to the Repo …  │ personal    │ 14m ago   │
  │ PR Review - 2026W38-tue    │ ⌛ waiting-on-human │ PR Review - 2026W38-tue            │ personal    │ 9m ago    │
  │ Complete Kafka Restore     │ ✅ done             │ Complete Kafka Restore             │ brogrammers │ 5h ago    │
  └────────────────────────────┴─────────────────────┴────────────────────────────────────┴─────────────┴───────────┘
```

- **Columns and widths:** Session 26 · Status 19 · Vault task 34 · Project 11 · Last 9 —
  **115 rendered characters**, the ceiling for a 119-column terminal. A box that wraps is
  worse than a truncated cell. **`Project` is the column to drop** if task titles need more
  room; cutting it buys the task column 10 characters.
- **Status** carries the bucket icon: 🔄 progressing · ⚠️ stalled · ⏸️ parked · ✅ done.
  **Orphaned is not a Status cell** — it is an action line *below* the box, because it
  describes the absence of a session rather than a live one's state.
- **Render with `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude}/scripts/box-table.py`** —
  stdin `{"header": [...], "rows": [[...]], "widths": [...]}`. **Never hand-draw the box.**
- **The marker line is timestamped and is always the first line of the tick's output:**
  `HH:MM ✓ Fleet — N sessions · <count by status> · <what changed or "no change">`. Silence
  is ambiguous — a quiet loop and a dead loop look identical from the outside.
  `/supervisor:fleet-status` is a one-shot snapshot and carries **no** marker, but indents
  its box the same two spaces under its own lead line.
- ⚠️ **Do not type a leading glyph.** The harness already bullets assistant output with `⏺`;
  a literal copy renders doubled.
- Below the box, only the non-empty action lines: the **blocked-by-you jump list**
  (`⌛ Blocked by you (N waiting …)` with `wezterm cli activate-tab` per row),
  `⚠️ ORPHANED: <task> — <why>`, and `⚠️ ACTION NEEDED: <the human decision>`. **Names lead**;
  the `[ref]` and tab id are secondary.

The table is the dashboard; TTS stays problem-only and voice-mode gated; the action lines
appear only when non-empty.

## Referencing vault notes

Several fleet commands cite the operator's Obsidian runbooks. The rule is a **wikilink by
title** (`[[Worker Manager Session]]`) — never a filesystem path, never an `obsidian://` URL.

Vaults number their folders differently — one vault's `65 Runbooks/` is another's
`70 Runbooks/`, one's `50 Knowledge Base/` is another's `50 Knowledge/` — so any path form is
wrong in some vault by construction, while the note's *filename* is stable. A title therefore
resolves wherever the note exists.

Where a note exists in only one vault, a wikilink would dangle everywhere else, so it is
written as plain prose marked *(operator's vault; not shipped with this plugin)*. Operational
content the commands genuinely need in order to run was moved into this file instead, rather
than left behind in a single vault's runbook — a plugin should not need a particular person's
vault to describe its own mechanics.
