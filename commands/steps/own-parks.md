---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Answer your own workers' routine parks

When: every tick

⚠️ **This leg acts, and a read-only execution is not a run of it.** It calls `mcp__supervisor__list_agents` and `mcp__supervisor__answer_permission`, which a read-only sub-agent does not hold — so a delegate that runs this file as written reaches precondition 1 and prints `own-parks: skipped — answer_permission unavailable` while the parks stay parked. Run it in the round session, or in a sub-agent granted write tools. See `commands/fleet-drive.md` § the delegate rule.

A headless worker's permission prompt parks with the session that **spawned** it, and no other session can answer it (`scripts/answer-path-check.py`; `commands/open.md` § the answer-path probe). When this session is the spawner, every park it does not answer goes to the operator's board or auto-denies at 15 minutes. Goal managers already answer theirs through `supervisor:goal-wrangler` (`/supervisor:goal-drain`). Before this step the fleet tier held `answer_permission` but no step called it.

This step answers **only** routine parks — those the written allowlist already decides — and **only** for workers the spawn ledger names this session as spawner of. It is class B of the runbook's Gate triage (*"already determined by a written source"*), where the written source is `${CLAUDE_PLUGIN_ROOT}/docs/permission-allowlist.md`. It adds no class-A mandate.

## Preconditions — fail closed

Check all three, in order. If any one fails, **do nothing**: answer no park, dispatch no wrangler, and print the skip line. A skipped step leaves every park pending exactly as it was, which is the state the operator already handles. Answering something this session cannot verify is the failure this step must never cause.

1. **`answer_permission` is unavailable** → skip with `own-parks: skipped — answer_permission unavailable`. Unavailable means the `mcp__supervisor__answer_permission` or `mcp__supervisor__list_agents` tool is absent from this session, or a `list_agents` call returns an error or a row carrying **neither** a `pending_question` nor a `pending_permissions` field. Do not retry through another channel: the attention board, a pane keystroke or `SendMessage` cannot release a harness permission gate.
2. **No parks** → skip with `own-parks: nothing parked` only when **both** park fields are empty on every row: `pending_question` (any entry carrying a `request_id`) **and** `pending_permissions`.

   ⚠️ **`pending_permissions` alone is the wrong field, and reading it alone is the defect this paragraph removes.** A headless worker's park appears as **`pending_question`** — `{request_id, kind, tool_name, tool_use_id, input}` — and `pending_permissions` is `[]` for exactly those parks. ⚠️ **Do not filter on `kind`.** The server does not: `pendingQuestionOf` returns the wrapper's own `question` object unchanged (`server/wrapper-stream.mjs:281`), so any kind label a reader invents here is narrower than the predicate that produced the field. The key that matters is `request_id` — it is what `answer_permission` takes, and the class triage, not the label, decides what may be answered. Measured 2026-11-08: one `list_agents` roster of **101 rows** carried **0** non-empty `pending_permissions` and **36** `pending_question` entries with a `request_id`.

   ⚠️ **`{unreadable: true}` is a third state, not "nothing parked".** When the wrapper session list cannot be read, `pendingQuestionOf` returns `{unreadable: true, error: …}` rather than `null`. Treat it as **unknown**, never as absence: do not print `nothing parked`, and do not treat the row as park-free. Skip with `own-parks: skipped — wrapper session list unreadable`, because a roster that cannot say whether a worker is parked is exactly the state this step must not answer from. A step reading only the first field concluded "nothing parked" and answered none of them, while every one of those workers went on to auto-deny. The previous wording here quoted rules.md — *"An empty `pending_permissions` is not absence of a gate"* — and then treated it as absence. That contradiction was the bug.
3. **Ownership.** Each `list_agents` row **carrying a park** — a `pending_question` with a `request_id`, or a non-empty `pending_permissions` — carries its own `agent_id` and `session_id`. Run:

   ```bash
   python3 $P/own-park-check.py --self "$CLAUDE_CODE_SESSION_ID" --session <worker-session-id> [--session …]
   ```

   Only workers printed as `OWN <sid>` are in scope. `REFUSE` lines are out of scope, and so is any row whose `session_id` is empty. **Any non-zero exit** (`2` = unreadable ledger or no session id of your own; anything else = the check itself failed) → skip with `own-parks: skipped — ownership unverifiable`. This server only holds its own workers' parks (a `rehydrated` row's `pending_permissions` is always `[]` — its `pending_question` is **not**, which is exactly why the ownership check rather than the field is what bounds this step). The check is still needed: a worker whose ledger row is missing, or names another spawner, is not provably this session's.

## Dispatch

With ≥ 1 `OWN` worker, split the parks by **whether they carry an `allowlist_entry`**, because that field — not the tool name — is what the decision policy reads.

- **`pending_permissions` rows carry one** (`server/supervisor.mjs` sets `allowlist_entry: p.allowlistEntry?.prefix ?? null`). Collect their request ids and dispatch the wrangler in **scoped mode**; the `session_id` is what its hold check keys on.
- **`pending_question` rows do not.** `pendingQuestionOf` returns the wrapper's raw `question` — `{request_id, kind, tool_name, tool_use_id, input}` — with no `allowlist_entry`, so no policy can classify it. ⚠️ **These are surfaced and escalated, never dispatched for an answer.** An id handed to the wrangler without that field is escalated by it anyway; dispatching it only spends a sub-agent turn to reach the same conclusion. Count each one as an escalation in the report.

That split is the whole design: an allowlisted wrapper park is already answered upstream by `server/wrapper-policy.mjs`, which polls the wrapper's parks and runs the same `decide()` — so what reaches this step is what `decide()` deliberately left. **This step's job for wrapper parks is to make them visible and route them, not to answer them.**

Dispatch the wrangler in **scoped mode** with the `pending_permissions` pairs; it owns the decision policy, so do not restate it:

```
Task tool with:
  subagent_type: 'supervisor:goal-wrangler'
  prompt: 'Answer only these request_ids: <request_id>=<session_id>, … (OWN rows only). Escalate every other request.'
```

Scoped mode reads the parks once and never waits on `await_permission`, so it costs the tick one bounded read rather than idle timeouts. The wrangler answers `allow` only when the request's `allowlist_entry` is non-null and no deny class matches — which is why only the `pending_permissions` ids are dispatched. Everything it does not answer stays pending, as an escalation.

⚠️ **Latency limit.** The step sees only what is parked when it runs. A park raised just after it waits for the next tick, and at a 15-minute interval it auto-denies first (`worker-channels.md`: *"Answer promptly or not at all"*). This step shortens the wait for routine parks; it does not make it zero.

## What this step never answers

The class boundary is a closed list. A park in any row below stays pending for the operator, and the wrangler escalating it is the correct result, not a failure.

| # | Class | Example | Who decides |
|---|---|---|---|
| **C** | **Not covered by the allowlist** — `allowlist_entry` is `null`; priority, scope or a choice between approaches; a worker's question (`AskUserQuestion`) | `gh -R bborbe/claude-supervisor pr ready 645` (no allowlist entry) | The operator |
| **D** | **Production-touching or irreversible** — the allowlist's prod, irreversible, scope-changing and credentials deny classes, even when an allowlist entry also matches; `make apply`, deploy, merge, release, tag push, force-push, a delete, spawning or stopping a session (`docs/permission-allowlist.md` § The deny list) | `BRANCH=prod make apply` | The operator, in the pane |
| **E** | **Live-trade / TDR** — any `40 Decisions/TDR *` field, trade-create or trade-close on a funded account | `trade-create` on a live account | The operator, via `vault-cli decision ack` |

Also never answered, whatever the class: a request for a worker printed `REFUSE`, and a request for a worker whose session carries an operator hold (the wrangler's hold check).

## Report

One line in the round's output, always, even when skipped. It is built from the wrangler's scoped-mode report, whose allow reasons name the allowlist entry and whose escalations start with their class. Nothing in this repo parses it; it exists to be quoted when counting answered against escalated parks:

```
own-parks: answered <n> (class B) · escalated <m> (C <c> · D <d> · E <e> · hold <h> · unverified <u>) · refused-not-own <r>
```

For each answered park, list `<request_id> | <worker label> | <tool> | allow | class B — allowlist_entry <prefix>`. An answered line that names no allowlist entry is a defect: report it as one rather than counting it.
