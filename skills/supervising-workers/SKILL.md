---
name: supervising-workers
description: Decide when work belongs in a supervisor worker rather than in this session, and how to unblock that worker. Use when the supervisor MCP server is loaded in this session (mcp__supervisor__* tools are available) and work could run unattended.
---

If the `mcp__supervisor__*` tools are not in this session, ignore this skill — you are not a manager session. The word "worker" is overloaded (dark-factory workers, Task sub-agents), so activate only on the MCP tools being present.

Work that can run unattended belongs in a worker, not in this session.

## Prerequisites

- The supervisor MCP server is loaded and its tools are visible.
- The worker gets its own `cwd` and cannot see this conversation — the brief must be self-contained.
- A task anchors the work. Anchor it before spawning, or the worker becomes untracked work.

## When to spawn

Spawn when the task is bounded, verifiable, and does not need this conversation's context: a fix in one repo, a file set to produce, a check to run. Do it yourself when the work is a decision, a quick read, or needs what was just discussed.

## The operator surface

The tool sequence is `spawn_agent` → `await_permission` → `answer_permission` → `list_agents`. The slash commands wrap the same calls: `/supervisor:spawn`, `/supervisor:workers`, `/supervisor:answer`, and `/supervisor:drain` for bulk approvals. The approval *policy* lives in the `worker-wrangler` agent — do not restate it here.

`spawn_agent` takes `prompt` (required) plus optional `cwd` and `label`. `await_permission(timeout_ms)` blocks until a worker asks; it returns `null` on timeout, so treat a null as "nothing to do", never as an approval.

## After a worker stops

Read its result and report a one-line outcome to the human. If it failed or was denied, say why before spawning a replacement. Immediately after an allow, status can still read `running` for a few seconds — never treat one check as final.

## Escalation

Routine approvals are yours. Escalate to the human when the request is outside the task's stated scope, touches production or credentials, or repeats an approval already denied once.

## Success Criteria

- Every worker reached a terminal status, or is reported with the reason it did not.
- Nothing is left pending at the end except requests deliberately escalated.
- Every denial carried a reason the worker could act on.

## Honest limits

- Cost figures the server reports are priced from Anthropic's table and mean nothing when traffic is routed elsewhere — never quote them.
- An unanswered permission auto-denies after 15 minutes.
- A worker session is single-shot: one prompt, one conversation.
