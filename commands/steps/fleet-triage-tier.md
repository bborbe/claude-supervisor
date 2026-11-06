---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 2 — Derive the tier

When: the sweep's JSON parsed

| tier | rule |
|---|---|
| `fleet-manager` | `role: manager` and `label` is `Fleet Manager` |
| `goal-manager` | `role: manager` and `<goals_dir>/<subject>.md` exists — **either registered name**, `24 Goals` or `23 Goals` (`scripts/close-worker.py:104` `GOAL_DIRS`, `scripts/fleet-board.py:104` `GOAL_SUBDIRS`; the `24 Tasks` vaults use `23 Goals`) |
| `topic-manager` | `role: manager` and `23 Topics/<subject>.md` exists |
| `manager` (unresolved) | `role: manager`, neither page found |
| `worker` | `role: worker` |
| `unmanaged` | `role: unmanaged` |

**The tier decides what the run may do:**

- `worker` → closable once its task is terminal; drivable via its owning manager.
- manager tiers → may be nudged, **never closed**; a manager's own loop is a fleet finding.
- `unmanaged` → never closed (no task proves it finished); report only.
