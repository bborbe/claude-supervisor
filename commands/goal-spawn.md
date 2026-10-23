---
description: Spawn a supervisor worker for a task
argument-hint: "[task brief]"
allowed-tools:
  - mcp__supervisor__spawn_agent
  - Bash(python3:*)
---
Spawn one worker for: $ARGUMENTS

If $ARGUMENTS is empty, ask for the task brief instead of spawning.

Follow the `supervising-workers` skill before spawning: decide whether this work belongs in a worker at all (bounded, verifiable, and not needing this conversation's context), then pick the mode deliberately.

Call `mcp__supervisor__spawn_agent` with a complete brief in `prompt` — the worker cannot see this conversation — plus the right `cwd` and a short `label`.

**The mode follows the shared classifier, not this command — a brief carrying none of item 6's five watch-need signals spawns `headless`, so know which one you are opening.** A tab worker answers its own permission prompts and you will not see them; pass `interactive=false` when you intend to answer its prompts yourself through `await_permission`, which is the only mode the approval loop serves. ⚠️ **The mode is decided by the shared rule, never ad hoc — a `role: agent` task with none of item 6's five watch-need signals is `headless`.** The classifier, the `mode:` storage and both headless constraints have their single home in `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 6 — read them there and never restate them here. ⚠️ **A headless worker's gates park with *this* session**, so do not open one whose prompts you cannot answer — and this is **checked, not advised**: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/answer-path-check.py" --session-id "$CLAUDE_CODE_SESSION_ID"` before a headless spawn. Exit 0 opens; exit 1 **refuses the spawn** — print the probe's own line and open nothing. The same probe is enforced in `server/supervisor.mjs`'s `spawnAgent`, so a spawn that skips this step is still refused. For work anchored to a task file, the mode comes from that task's own `mode:` field and is **honoured in both directions** — pass `interactive=false` for `headless` and `interactive=true` for `interactive`, omitting the argument only when the field is absent, so an explicit `mode: interactive` is never silently overridden by the fleet default. `/supervisor:open` is the path that reads the field and writes it back — reach for this bare command only when the brief is not task-anchored. ⚠️ **On that non-task-anchored path there is no `mode:` field to read or write, so classify the brief itself and pass the argument explicitly** — `interactive=false` when the brief carries none of item 6's five watch-need signals, `interactive=true` when it carries one. **Never omit it here.** Omission is reserved for a *task* whose `mode:` is absent, and that case is resolved upstream: Step 0.6 writes the field before the spawn. A bare brief has no such step, so omitting would mean the spawn carried **no decision at all** and reported `mode_source=config` — indistinguishable in the ledger from a site that never decided, which is the defect this rule exists to remove. Say which mode you spawned in the report, because the two behave differently from that point on.

Then report the `agent_id`, the mode, and — for a tab worker — the `pane_id`.
