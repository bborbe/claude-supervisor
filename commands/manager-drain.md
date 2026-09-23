---
description: Answer every pending permission by policy and escalate the rest
allowed-tools: Task
---
Run the approval loop for the currently running workers using the Task tool with the `supervisor:manager-wrangler` agent. The decision policy lives in that agent, not here — do not restate it.

The loop serves **headless workers only**: it waits on `await_permission`, and a tab worker answers its own prompts, so it never parks one. If every worker was spawned in the default tab mode, the loop has nothing to drain and will exit on its idle timeouts — report that plainly rather than as a clean sweep.

When the agent returns, report its summary: how many prompts were answered, what was denied, and what it escalated to you still unanswered.
