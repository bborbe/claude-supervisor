---
description: Close ONE finished session's pane — a worker whose task is terminal or deferred, idle, and holding no gate. Refuses in seven named cases and reuses the restart scripts' guards rather than adding a second kill mechanism. Takes a session id (or a pid).
argument-hint: "<session-id | pid>"
allowed-tools:
  - Read
  - Bash(~/.claude/plugins/cache/claude-supervisor/supervisor/*/scripts/close-worker.py:*)
  - Bash(wezterm cli list:*)
  - Bash(python3:*)
---

Close a pane whose work is finished. The fleet manager could open and restart a session
but not close one, so every finished tab waited for the operator — nine of them on
2026-10-19. This verb is the third arm of the same family, and it is deliberately the
smallest: it tears down a pane and resumes nothing.

**This command is the orchestrator. It is not a second kill mechanism.** The registry
read, the hold store and the refusal contract belong to `scripts/restart-worker.py`; the
worktree probe, the occupancy count and the cause-of-death classifier belong to
`scripts/restart-precheck.py`; the pane→session join belongs to `scripts/jump.py`.
`scripts/close-worker.py` imports all three and adds only what none of them answers —
*is the target's task finished, and where is its pane.*

⚠️ **Tier: fleet manager and manager. Never from a worker.** Closing a session is
dispatch's mirror image, and the tier table (`docs/session-tiers.md`) states it in the
same row: `fleet manager, manager — never worker`.

## Argument

A session id, or a pid. A pid resolves through `~/.claude/sessions/<pid>.json` — the
registry is authoritative because Claude Code deletes the entry on exit. **Never resolve
a target from a process listing**: `pgrep -fl` and `ps` print command lines that carry
MCP `Authorization:` headers, and `ps -o comm=` prints the full argv on macOS.

## Step 0 — Resolve the load path

```bash
LOAD_PATH=$(ls -d ~/.claude/plugins/cache/claude-supervisor/supervisor/[0-9]*/ | sort -V | tail -1)
```

The **cache** path, never the marketplace clone: the clone updates independently of what
a running session loaded, and the `settings.json` allow rule is scoped to the cache.
`[0-9]*` is load-bearing — the sibling `builtin/` directory holds no `scripts/`, and
`sort -V` orders it after every version.

## Step 1 — Check, then close, via the script

```bash
"$LOAD_PATH"scripts/close-worker.py <session-id>
```

**Invoke it directly — the script path is the first token.** It carries a
`#!/usr/bin/env python3` shebang and is executable, and the `settings.json` allow rule is
shaped `Bash(<script path>:*)`, which matches a command whose first token is that path.
`python3 <path>` does not match it and would fall back to a broad interpreter grant.

`--dry-run` runs every guard and closes nothing — use it when the operator wants the
verdict without the act.

**The refusal token is the first line of stdout** — branch on it, never on the prose
below it. Seven tokens, each terminal for this run:

| Token | Then |
|---|---|
| `unknown-session-id` | **Stop.** No entry carries it, or the registry could not be read. |
| `ambiguous-pid` | **Stop.** More than one pid claims that id; closing one is a coin flip. |
| `held-session` | **Stop.** The operator holds it. Only they release it (`/supervisor:hold`). |
| `busy-target` | **Stop.** The session is mid-turn. A close loses the work with no resume behind it. |
| `headless-target` | **Stop.** Not `interactive`; there is no pane. |
| `dirty-worktree` | **Stop.** The target is the only live session in that worktree and it carries tracked changes. Hand the operator the choice: commit, stash, or another target. |
| `gate-held` | **Stop.** Its transcript tail carries an unanswered `👤 You:` gate. Closing discards a question the operator never answered — hand them the jump link instead. |
| `task-unresolved` | **Stop.** No task file carries this session id as `claude_session_id`. Fail closed: absent evidence is not evidence of a finished task. |
| `task-not-terminal` | **Stop.** The task is neither terminal nor deferred to a future `defer_date`. |

⚠️ **A refusal is terminal — never fall back to another teardown route.** `wezterm cli
kill-pane` by hand, or closing the tab, is the same act with none of the guards. Falling
back is not a retry; it is a close the gate never authorised.

⚠️ **`gate-held` is not `held-session`.** A hold is the operator's explicit
`/supervisor:hold`; a gate is a question the session is parked on. Both refuse, and
neither is the other's remedy.

## Step 2 — Report

The script prints its own report on success: the session, its pid, the task it closed
against, the worktree verdict, the classification and the pane. Repeat it — do not
paraphrase the task status, which is the evidence the close was permitted.

`✅ closed pane <N> · pid <pid> dead · registry entry removed` is the success line. A
`⚠️` line means the pane went but the pid or its registry entry survived; report it as
written and do not retry.

**No jump link on success** — the pane is gone, and a link to a closed pane is a dead
coordinate. On a `gate-held` refusal, hand over the jump link instead:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py <PANE-ID>
```

## Rules

- **One target per run, named by the operator or the manager's own sweep.** This command
  never hunts for which session to close.
- **Never reimplement the registry read, the guards, or the pane join.** One mechanism,
  called.
- **A close is not a `session-close`.** `/vault-cli:sync-progress` and
  `/vault-cli:session-close` read the worker's own conversation and remain the worker's
  act; this verb only removes a pane whose task is already terminal or deferred.
- **Never close a pane holding a gate.** That is what `gate-held` is for; the operator's
  answer is the only way past it.
