---
description: Answer a pending worker permission prompt
argument-hint: "[request_id] [allow|deny] [message]"
allowed-tools: mcp__supervisor__answer_permission
---
Answer the pending permission: $ARGUMENTS

If the argument lacks a request_id or an allow/deny verb, ask for the missing part — never guess.

Call `mcp__supervisor__answer_permission` with that request_id and behavior. For a denial, supply a `message` telling the worker what to do instead. Then confirm which worker was unblocked.
