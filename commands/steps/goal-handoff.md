---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Handoff

When: a fleet-loop session is on `ListAgents` and the sets changed

5. **Fleet-manager handoff (gap 4).** A fleet-loop session on `ListAgents` → `SendMessage` it the **orphaned + ready-to-start** sets after the table — one message per sweep in which either set changed, silent otherwise. Orphaned = the fleet's decision; ready-to-start = a report of what you started. No fleet manager → the table's `ORPHANED` / `READY TO START` lines are the handoff.
