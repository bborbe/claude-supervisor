---
description: Spawn a supervisor worker for a task
argument-hint: "[task brief]"
allowed-tools: mcp__supervisor__spawn_agent
---
Spawn one worker for: $ARGUMENTS

If $ARGUMENTS is empty, ask for the task brief instead of spawning.

Follow the `supervising-workers` skill before spawning: decide whether this work belongs in a worker at all (bounded, verifiable, and not needing this conversation's context). When it does, call `mcp__supervisor__spawn_agent` with a complete brief in `prompt` — the worker cannot see this conversation — plus the right `cwd` and a short `label`. Then report the `agent_id`.
