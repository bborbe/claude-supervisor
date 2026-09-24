---
description: Spawn a supervisor worker for a task
argument-hint: "[task brief]"
allowed-tools: mcp__supervisor__spawn_agent
---
Spawn one worker for: $ARGUMENTS

If $ARGUMENTS is empty, ask for the task brief instead of spawning.

Follow the `supervising-workers` skill before spawning: decide whether this work belongs in a worker at all (bounded, verifiable, and not needing this conversation's context), then pick the mode deliberately.

Call `mcp__supervisor__spawn_agent` with a complete brief in `prompt` — the worker cannot see this conversation — plus the right `cwd` and a short `label`.

**The default is a wezterm tab, and a tab worker answers its own permission prompts — you will not see them.** Pass `interactive=false` when you intend to answer its prompts yourself through `await_permission`; that is the only mode the approval loop serves. ⚠️ **The mode is decided by the shared rule, never ad hoc — `mode: headless` only on positive evidence the work never needs a human mid-flight, and `interactive` whenever it is unclear.** The classifier, the `mode:` storage and both headless constraints have their single home in `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 6 — read them there and never restate them here. ⚠️ **A headless worker's gates park with *this* session**, so do not open one whose prompts you cannot answer. For work anchored to a task file, the mode comes from that task's own `mode:` field, and `/supervisor:open` is the path that reads it and writes it back — reach for this bare command only when the brief is not task-anchored. Say which mode you spawned in the report, because the two behave differently from that point on.

Then report the `agent_id`, the mode, and — for a tab worker — the `pane_id`.
