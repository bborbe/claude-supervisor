---
description: Answer every pending permission by policy and escalate the rest
allowed-tools: Task
---
Run the approval loop for the currently running workers using the Task tool with the `supervisor:worker-wrangler` agent. The decision policy lives in that agent, not here — do not restate it.

When the agent returns, report its summary: how many prompts were answered, what was denied, and what it escalated to you still unanswered.
