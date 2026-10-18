---
description: DEPRECATED alias for `/supervisor:restart` — restart ONE named session (worker or manager) in a single verb. Kept working so existing callers are not broken; new callers use `/supervisor:restart`.
argument-hint: "<session-id | pid>"
allowed-tools:
  - Read
  - Bash(wezterm cli get-text:*)
  - Bash(wezterm cli send-text:*)
  - Bash(wezterm cli list:*)
  - Bash(python3:*)
  - Bash(ls:*)
  - Bash(sort:*)
  - Bash(tail:*)
  - Bash(~/.claude/plugins/cache/claude-supervisor/supervisor/*/scripts/restart-precheck.py:*)
  - Bash(~/.claude/plugins/cache/claude-supervisor/supervisor/*/scripts/restart-worker.py:*)
---

⚠️ **Deprecated — use `/supervisor:restart`.** This file is an alias, kept so anything already invoking `/supervisor:worker-restart` keeps working. It is not a second implementation and must not become one.

**Do this:** read `${CLAUDE_PLUGIN_ROOT}/commands/restart.md` and follow it, exactly as written, with the same argument. That file is the single home of the procedure — the load path, the pre-check, the script call, the cause-of-death branch, the re-arm note and the report all live there.

Nothing about the behaviour differs. The rename happened because the verb stopped being workers-only: `scripts/restart-worker.py` restarts a manager too, so a `worker-` name on the command (and on the tier prefix that names it) described a target set the verb no longer had.

⚠️ **This alias is the one thing keeping the `worker-` prefix exception in `docs/session-tiers.md` alive.** That exception says the prefix names the TARGET, not the invoker, and it cannot move tiers — so if this file is ever removed, that prose and its table row go with it in the same change.
