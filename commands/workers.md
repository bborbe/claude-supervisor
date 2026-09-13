---
description: Show all supervisor workers and everything awaiting approval
allowed-tools: mcp__supervisor__list_agents, mcp__supervisor__pending_permissions
---
Call `mcp__supervisor__list_agents` and `mcp__supervisor__pending_permissions`, then print one table: label, agent_id, status, last message, and pending request count.

Below the table, list every pending permission with its prompt sentence and request_id, so each can be answered with `/supervisor:answer`.
