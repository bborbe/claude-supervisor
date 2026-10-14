---
description: Which Claude Code sessions need me right now — permission prompts, open questions, and sessions stuck in one tool call too long — each with a WezTerm pane jump. Read-only on the fleet; writes its own cause tally under ~/.claude/state/who-needs-me/.
allowed-tools:
  - Bash(python3:*)
  - Bash(wezterm cli activate-pane:*)
argument-hint: "[--stuck-min N]"
---

Run exactly:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/who-needs-me.py $ARGUMENTS
```

Print the output verbatim. Do not add analysis, do not run `ListAgents`, do not message peers.

- `Needs you` — sessions blocked on a permission prompt or an `AskUserQuestion`. Oldest first. Each row leads with its cause class (`awaiting-operator`, `waiting-on-task`, `waiting-on-clock`, `cap-blocked`, `unknown`) and any board flag, so the reason a pane is parked is on the row rather than inferred.
- `Board card missing` — parked panes the attention board carries no card for, flagged `board-card-missing` or `board-card-expired`. Its own section, never counted under `Needs you`: these rows have not had their liveness decided by the store, so counting them would put a re-judged row into the number a manager triages by.
- `Probably stuck` — sessions inside a single tool call longer than the threshold (default 20 min). Usually a PR watch or a build that overran.
- `Idle` — turn ended, waiting for a prompt. Counted on the last line, never listed individually.

A row ends with the `wezterm cli activate-pane --pane-id N` line **when that pane is provably the session's own**. When it is not — a headless worker inherits its spawner's pane — the row reads `unroutable` and then carries an `answer:` line naming the `attention-answer.py answer <item_id> --decision allow|deny` command that clears the gate instead; there is no pane to jump to, so that line is the handover. If the user says "jump N" / "go to N", run `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/who-needs-me.py --jump N`.

Data source: `~/.claude/state/attention/`, written by `~/.claude/hooks/attention-log.py` on `PermissionRequest`, `PreToolUse`, `PostToolUse`, `Notification`, `Stop`, `UserPromptSubmit`, `SessionEnd`. Sessions whose pane is gone are dropped. Sessions started before the hook was wired have no state until their next tool call.

A recurring-cause tally is kept at `~/.claude/state/who-needs-me/causes.json` — one run is one invocation, deduped per calendar day — and a cause class seen on 3 or more days with no owning task is flagged at the foot of the output. `--resolve-owners` fills that file's `owners` map from `vault-cli task search`; it is slow (the search loads a model) and is never on the default path.
