---
description: Sweep every live session, say what is blocking each one, hand the operator-bound gates over with jump links, and root-cause the blockers that keep recurring. Report-only on gates — the only outward action is one message per live manager.
allowed-tools:
  - Bash(python3:*)
  - SendMessage
# No `argument-hint`: the sweep takes no tunable. `--stuck-min` is accepted by the
# script for parity with the sibling and ignored by it, so advertising it in the UI
# would promise a threshold this command does not honour.
---

Two steps. Do both, in order, and add nothing else.

**1. Sweep.** Run exactly:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/who-needs-my-help.py $ARGUMENTS
```

Print its output verbatim. Do not add analysis, do not answer a gate, do not drive a modal, do not type into any pane.

**2. Ask the managers.** The sweep ends with a `Managers to query` block naming each live manager session and the question to put to it. Send **exactly one** `SendMessage` to each name listed, carrying that question. Never `wezterm cli send-text` / `send-keys` — typing into another session's pane is forbidden, and it is what would turn a question into a menu selection. If the block is absent, send nothing.

## The four sections

- `Sweep` — every live session and why it is parked. Each row carries the runbook's gate class (**A–E**, *who may clear*) **and** the cause class (`awaiting-operator`, `waiting-on-task`, `waiting-on-clock`, `cap-blocked`, `unknown`, *why it is parked*), plus any board flag. A session parked on a watcher or a clock is listed with the watcher and its ETA; it is not a gate, so it carries no gate class.
- `Handover` — the operator-bound rows only — gate classes **C, D and E** — ordered prod first, then closest-to-SC, each with a jump link and a recommended answer. Classes A and B are the manager's and never appear here.
- `Root cause` — cause classes seen on **two or more distinct calendar days**, each with the mechanism that explains it. Read from `~/.claude/state/who-needs-me/causes.json`; a class seen twice in one run, or twice on one date, is one occurrence.
- `Managers to query` — the live manager sessions and the question to put to each.

⚠️ **What this command does not do.** It answers no gate, drives no modal, relays no approval, and types into no pane. Its single outward action is the `SendMessage` step above.

⚠️ **A `wezterm cli get-text` read detects modals, not gates.** A pane parked on `approve:` / `pick —` / `you run:` renders that line as ordinary text with no footer, so the pane cannot answer the question for that shape. The instruments for a closer-line gate are the attention store's open items and the closer's own verb — never the modal scan alone.
