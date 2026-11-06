---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Answer your own workers' routine parks

When: every tick

A headless worker's permission prompt parks with the session that **spawned** it, and no other session can answer it (`scripts/answer-path-check.py`; `commands/open.md` § the answer-path probe). When this session is the spawner, every park it does not answer goes to the operator's board or auto-denies at 15 minutes. Goal managers already answer theirs through `supervisor:goal-wrangler` (`/supervisor:goal-drain`). Before this step the fleet tier held `answer_permission` but no step called it.

This step answers **only** routine parks — those the written allowlist already decides — and **only** for workers the spawn ledger names this session as spawner of. It is class B of the runbook's Gate triage (*"already determined by a written source"*), where the written source is `${CLAUDE_PLUGIN_ROOT}/docs/permission-allowlist.md`. It adds no class-A mandate.

## Preconditions — fail closed

Check all three, in order. If any one fails, **do nothing**: answer no park, dispatch no wrangler, and print the skip line. A skipped step leaves every park pending exactly as it was, which is the state the operator already handles. Answering something this session cannot verify is the failure this step must never cause.

1. **`answer_permission` is unavailable** → skip with `own-parks: skipped — answer_permission unavailable`. Unavailable means the `mcp__supervisor__answer_permission` or `mcp__supervisor__list_agents` tool is absent from this session, or a `list_agents` call returns an error or a row with no `pending_permissions` field. Do not retry through another channel: the attention board, a pane keystroke or `SendMessage` cannot release a harness permission gate.
2. **No parks** → every `mcp__supervisor__list_agents` row's `pending_permissions` is `[]`, so skip with `own-parks: nothing parked`. An empty list is not proof that no worker is gated (rules.md: *"An empty `pending_permissions` is not absence of a gate"*). It only means this server holds nothing to answer.
3. **Ownership.** Each `list_agents` row with a non-empty `pending_permissions` carries its own `agent_id` and `session_id`. Run:

   ```bash
   python3 $P/own-park-check.py --self "$CLAUDE_CODE_SESSION_ID" --session <worker-session-id> [--session …]
   ```

   Only workers printed as `OWN <sid>` are in scope. `REFUSE` lines are out of scope, and so is any row whose `session_id` is empty. Exit `2` (unreadable ledger, or no session id of your own) → skip with `own-parks: skipped — ownership unverifiable`. A `rehydrated: true` row is another session's worker adopted into this roster; the ledger check refuses it unless this session spawned it.

## Dispatch

With ≥ 1 `OWN` worker, dispatch the wrangler. It owns the decision policy, so do not restate it:

```
Task tool with:
  subagent_type: 'supervisor:goal-wrangler'
  prompt: 'Answer only for agent_ids: <comma-separated OWN agent_ids>. Escalate every other request.'
```

The wrangler answers `allow` only when the request's `allowlist_entry` is non-null and no deny class matches. Everything it does not answer stays pending, as an escalation.

## What this step never answers

The class boundary is a closed list. A park in any row below stays pending for the operator, and the wrangler escalating it is the correct result, not a failure.

| # | Class | Example | Who decides |
|---|---|---|---|
| **C** | **Not covered by the allowlist** — `allowlist_entry` is `null`; priority, scope or a choice between approaches; a worker's question (`AskUserQuestion`) | `gh -R bborbe/claude-supervisor pr ready 645` (no allowlist entry) | The operator |
| **D** | **Production-touching or irreversible** — the allowlist's prod, irreversible, scope-changing and credentials deny classes, even when an allowlist entry also matches; `make apply`, deploy, merge, release, tag push, force-push, a delete, spawning or stopping a session (`docs/permission-allowlist.md` § The deny list) | `BRANCH=prod make apply` | The operator, in the pane |
| **E** | **Live-trade / TDR** — any `40 Decisions/TDR *` field, trade-create or trade-close on a funded account | `trade-create` on a live account | The operator, via `vault-cli decision ack` |

Also never answered, whatever the class: a request for a worker printed `REFUSE`, and a request for a worker whose session carries an operator hold (the wrangler's hold check).

## Report

One line in the round's output, always, even when skipped. It is the before/after count the fleet's metric reads:

```
own-parks: answered <n> (class B) · escalated <m> (C <c> · D <d> · E <e>) · refused-not-own <r>
```

For each answered park, list `<request_id> | <worker label> | <tool> | allow | class B — allowlist_entry <prefix>`. An answered line that names no allowlist entry is a defect: report it as one rather than counting it.
