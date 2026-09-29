---
description: Jump to a Claude Code session's WezTerm tab. Bare = newest session needing attention; or by tab id, tab:<N>, pane:<N>, or a title substring. The executor for the `/supervisor:jump <pane-id>` lines that the fleet-status, fleet-loop, manager-status and manager-loop commands print but never run — those commands are read-only by contract and must never mutate. **Managers always hand over `/supervisor:jump <pane-id>`, never a raw `wezterm cli activate-tab` line.**
allowed-tools:
  - Bash(python3:*)
argument-hint: "[<tab-id> | tab:<N> | pane:<N> | <title substring>] [--list] [--dry-run]"
---

Run exactly:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump.py $ARGUMENTS
```

Print the output verbatim. Do not add analysis, do not run `ListAgents`, do not message peers.

## Modes

| Invocation | Effect |
|---|---|
| `/supervisor:jump` | jump to the **newest** session needing attention (permission prompt or open question) |
| `/supervisor:jump --list` | print the attention queue, jump nothing — the `→` marks what bare `/supervisor:jump` would pick |
| `/supervisor:jump 5` | activate 5 — the tab if tab 5 exists, else pane 5 |
| `/supervisor:jump tab:5` | force tab 5 past the ambiguity check |
| `/supervisor:jump pane:5` | activate pane 5 (more precise — a tab can hold split panes) |
| `/supervisor:jump "Complete Kafka Restore"` | case-insensitive substring match on tab titles |
| `/supervisor:jump --dry-run …` | print the resolved target, activate nothing |

## Why newest-first

The attention feed is hook-written and goes stale: a gate the worker has already cleared still sits in the file until its next tool call. The **newest** entry is therefore the most likely still open, and the oldest the most likely already cleared. That is why bare `/supervisor:jump` picks newest-first.

## The ambiguity rule

A bare number resolves to **whichever namespace holds it** — the tab if tab N exists, else pane N. Tab ids and pane ids are different namespaces and do collide: on 2026-09-16, tab 5 was `Kafka Topic Restore from S3 Dumps` while pane 5 was `PR Review - 2026W38-wed`. Only when a number is live as **both** and they disagree does the command **refuse and name both** rather than guess — a sole match is not ambiguous, and it jumps. `tab:` and `pane:` are the escapes. **This flipped 2026-09-18.** The rule read *"a bare number means a tab id"* and refused a pane-only match — which is exactly the case the fleet and topic lists now produce, because those lists print **pane** ids. Refusing a number that could only mean one thing made the operator retype a command the tool had just named for them.

## Cross-window

`activate-tab` cannot cross WezTerm windows: called from another window it succeeds and nothing visibly moves. The command prints the window id always, and adds an explicit warning when the target window differs from the caller's. That warning is the difference between a silent no-op and a legible one — when it fires, switch windows yourself.

## Related — do not rebuild these

- `/supervisor:open <name>` — resolves a task/goal/topic and *then* jumps, resumes, or spawns. Heavyweight resolution; this command is the bare executor.
- `/who-needs-me` — lists who needs you, vault-local to the primary vault. This command reuses the same attention state (`who-needs-me.py`'s parsing, imported not copied) but is global and jumps.
- `/fleet-status`, `/manager-status` — read-only by contract; they print jump lines and must never mutate. This command is what executes them.
