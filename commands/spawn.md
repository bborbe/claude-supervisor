---
description: Spawn a supervisor worker for a task
argument-hint: "[task brief]"
allowed-tools: mcp__supervisor__spawn_agent
---
Spawn one worker for: $ARGUMENTS

If $ARGUMENTS is empty, ask for the task brief instead of spawning.

Follow the `supervising-workers` skill before spawning: decide whether this work belongs in a worker at all (bounded, verifiable, and not needing this conversation's context), then pick the mode deliberately.

Call `mcp__supervisor__spawn_agent` with a complete brief in `prompt` — the worker cannot see this conversation — plus the right `cwd` and a short `label`.

**The default is a wezterm tab, and a tab worker answers its own permission prompts — you will not see them.** Pass `interactive: false` when you intend to answer its prompts yourself through `await_permission`; that is the only mode the approval loop serves. Say which mode you spawned in the report, because the two behave differently from that point on.

Then report the `agent_id`, the mode, and — for a tab worker — the `pane_id`.
