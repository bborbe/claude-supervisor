---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Unowned

When: before declaring any member task unowned

9. **Before declaring a task unowned, check the registry, not only the roster** — headless workers appear in `ListAgents`, but its mode column cannot tell them apart and rows vanish at turn end; read `~/.claude/sessions/<pid>.json`.

   ⚠️ **Only when the `supervisor` tools are bound in THIS session** (fixed at session start; depends on the launch PATH and on when the vault's MCP entry landed).

   - Tools bound → call `mcp__supervisor__list_agents` before concluding a member task has no owner. Interactive workers also appear in the roster (prefixed `⚙`); headless ones never do.
   - Tools **not** bound → say so in the verdict, explicitly: *"roster-only — the supervisor check could not be run in this session."* Never let it read as though the check ran.

   Advisory, never a gate on dispatch. Install and traps: the supervisor install guide.
