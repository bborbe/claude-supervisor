---
description: Show all supervisor workers and everything awaiting approval
allowed-tools: mcp__supervisor__list_agents, mcp__supervisor__pending_permissions
---
Call `mcp__supervisor__list_agents` and `mcp__supervisor__pending_permissions`, then print one table: label, agent_id, status, last message, and pending request count.

Below the table, list every pending permission with its prompt sentence and request_id, so each can be answered with `/supervisor:manager-answer`.

Two readings of this output are wrong by construction, so state them rather than letting the table imply otherwise:

- **A tab worker reports `interactive`, not `running`, and never appears in `pending_permissions`** — it answers its own prompts in its tab, so it never parks one. An empty pending list says nothing about whether a tab worker is blocked; read its tab.
- **A headless worker (`interactive: false`) is invisible to the session roster** (`/fleet-status`, the `●` set) — it has no socket — even though it *does* appear in `list_agents` above, reading `running`. A roster-only sweep will read its task as unowned and may spawn a duplicate onto it; this table is the place that shows it.
