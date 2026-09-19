---
description: Answer a pending worker permission prompt
argument-hint: "[request_id] [allow|deny] [message]"
allowed-tools: mcp__supervisor__answer_permission
---
Answer the pending permission: $ARGUMENTS

If the argument lacks a request_id or an allow/deny verb, ask for the missing part — never guess.

Call `mcp__supervisor__answer_permission` with that request_id and behavior. For a denial, supply a `message` telling the worker what to do instead. Then confirm which worker was unblocked.

⚠️ **This call is gated by *your own* session's permission mode, not the worker's.** Under `auto`, the classifier can refuse the outgoing `answer_permission(allow)` before the fleet is involved — measured 2026-09-19; the refusal is the manager's own call being gated, not a block on the worker's side. The fix is one keystroke here: **Shift+Tab → `accept edits`, then retry the same call.** Do **not** respond to a refusal by changing the worker's mode or `defaultMode` — `spawn_agent` has no such argument, and workers inherit it from `~/.claude/settings.json`.

Only a headless worker (`interactive: false`) parks a request. A tab worker answers its own prompts in its tab, so there is nothing here to answer for one — to unblock a tab worker, type into it with `send_agent_message` or drive its tab by hand.
