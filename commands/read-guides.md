---
description: Read all supervisor guides for context before working — session tiers (which commands each session may run) plus the fleet-surface spec
allowed-tools: [Read, Glob]
argument-hint: "(no argument)"
---

Read all claude-supervisor documentation to build full context. Use this before running any `/supervisor:*` command, before spawning or driving workers, or whenever you are unsure which session tier you are in and what it may do.

## Step 1: Read supervisor docs

Glob `${CLAUDE_PLUGIN_ROOT}/docs/**/*.md` (recursive) and Read every file returned. These include:

- `session-tiers.md` — the three tiers (fleet manager / manager / worker), the command → tier table, the `/supervisor:open`-is-manager-only rule, and the worker-routes-to-manager rule
- `fleet-surface.md` — how commands are addressed, session roles, the disarm contract, the spawn shape, the session roster, and the fleet table render spec

## Step 2: Index supervisor commands (don't read all)

Glob `${CLAUDE_PLUGIN_ROOT}/commands/*.md` and list filenames only — do NOT read every file. Read one on demand when you are about to run it.

## Step 3: Summarize

Report:
- **Your tier** — name the three session types (**fleet manager**, **topic/goal manager**, **worker** — headless or interactive) and state the `/open` rule: **managers open workers with `/supervisor:open`; a worker never calls `/open`.** Then point the user at `session-tiers.md` for the authoritative wording; do NOT restate the command → tier table here. It is the single source, and a restated copy is exactly what drifts.
- **Supervisor rules learned** — key rules from `fleet-surface.md` (addressing, session roles, disarm contract)
- **Available commands** — filenames grouped by tier prefix (`fleet-*` / `manager-*` / `worker-*` / bare)
- **Confirm readiness to work**
