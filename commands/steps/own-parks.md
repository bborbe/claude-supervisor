---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Answer your own workers' routine parks

When: every tick

⚠️ **This leg acts, and a read-only execution is not a run of it.** It calls `mcp__supervisor__list_agents` and `mcp__supervisor__answer_permission`, which a read-only sub-agent does not hold — so a delegate that runs this file as written reaches precondition 1 and prints `own-parks: skipped — answer_permission unavailable` while the parks stay parked. Run it in the round session, or in a sub-agent granted write tools. See `commands/fleet-drive.md` § the delegate rule.

A headless worker's permission prompt parks with the session that **spawned** it, and no other session can answer it (`scripts/answer-path-check.py`; `commands/open.md` § the answer-path probe). When this session is the spawner, every park it does not answer goes to the operator's board or auto-denies at 15 minutes. Goal managers already answer theirs through `supervisor:goal-wrangler` (`/supervisor:goal-drain`). Before this step the fleet tier held `answer_permission` but no step called it.

This step answers routine parks — those the written allowlist already decides — and **only** for workers the spawn ledger names this session as spawner of. Routine answering is class B of the runbook's Gate triage (*"already determined by a written source"*), where the written source is `${CLAUDE_PLUGIN_ROOT}/docs/permission-allowlist.md`. It adds no class-A mandate.

⚠️ **A worker's question is not left to time out.** An `AskUserQuestion` park carries no `allowlist_entry`, so the wrangler escalates it — and until 2026-11-07 an escalation was the end of the road: the park sat until the 15-minute timeout auto-denied it and the worker exited having written nothing. Measured that day: a fleet manager spawned a worker whose plan gate asked a modal, nobody answered, and the task file was found untouched twenty minutes later. This step now **names** every class-C park in its report, and step 7 (`commands/steps/worker-channels.md`) answers it in the manager's own voice with `Manager answer, via supervisor:`. Detection is this step's; the decision is the round's — it spawned the worker and holds the task context the question is about, which a scoped wrangler reading one request payload does not.

## Preconditions — fail closed

Check all three, in order. If any one fails, **do nothing**: answer no park, dispatch no wrangler, and print the skip line. A skipped step leaves every park pending exactly as it was, which is the state the operator already handles. Answering something this session cannot verify is the failure this step must never cause.

1. **`answer_permission` is unavailable** → skip with `own-parks: skipped — answer_permission unavailable`. Unavailable means the `mcp__supervisor__answer_permission` or `mcp__supervisor__list_agents` tool is absent from this session, or a `list_agents` call returns an error or a row with no `pending_permissions` field. Do not retry through another channel: the attention board, a pane keystroke or `SendMessage` cannot release a harness permission gate.
2. **No parks** → every `mcp__supervisor__list_agents` row's `pending_permissions` is `[]`, so skip with `own-parks: nothing parked`. An empty list is not proof that no worker is gated (rules.md: *"An empty `pending_permissions` is not absence of a gate"*). It only means this server holds nothing to answer.
3. **Ownership.** Each `list_agents` row with a non-empty `pending_permissions` carries its own `agent_id` and `session_id`. Run:

   ```bash
   python3 $P/own-park-check.py --self "$CLAUDE_CODE_SESSION_ID" --session <worker-session-id> [--session …]
   ```

   Only workers printed as `OWN <sid>` are in scope. `REFUSE` lines are out of scope, and so is any row whose `session_id` is empty. **Any non-zero exit** (`2` = unreadable ledger or no session id of your own; anything else = the check itself failed) → skip with `own-parks: skipped — ownership unverifiable`. This server only holds its own workers' parks (a `rehydrated` row's `pending_permissions` is always `[]`). The check is still needed: a worker whose ledger row is missing, or names another spawner, is not provably this session's.

## Dispatch

With ≥ 1 `OWN` worker, collect each request id from those rows' `pending_permissions`, paired with that row's `session_id`. Dispatch the wrangler in **scoped mode** with those pairs; the `session_id` is what its hold check keys on. It owns the decision policy, so do not restate it:

```
Task tool with:
  subagent_type: 'supervisor:goal-wrangler'
  prompt: 'Answer only these request_ids: <request_id>=<session_id>, … (OWN rows only). Escalate every other request.'
```

Scoped mode reads `pending_permissions` once and never waits on `await_permission`, so it costs the tick one bounded read rather than idle timeouts. The wrangler answers `allow` only when the request's `allowlist_entry` is non-null and no deny class matches. Everything it does not answer stays pending, as an escalation.

An escalation of an ordinary tool request stays pending for the operator — that is the correct result. An escalation of a class-C `AskUserQuestion` is instead **routed** to step 7, because leaving it pending is what the 15-minute timeout then answers with a denial.

⚠️ **Latency limit.** The step sees only what is parked when it runs. A park raised just after it waits for the next tick, and at a 15-minute interval it auto-denies first (`worker-channels.md`: *"Answer promptly or not at all"*). This step shortens the wait for routine parks; it does not make it zero.

## What this step never answers

The class boundary is a closed list. Class B is the only class this step **answers**; a class-C park the manager owns is **routed** to step 7 rather than left pending; every other row below stays pending for the operator, and the wrangler escalating it is the correct result, not a failure.

| # | Class | Example | Who decides |
|---|---|---|---|
| **C** | **Not covered by the allowlist** — `allowlist_entry` is `null`; priority, scope or a choice between approaches; a worker's question (`AskUserQuestion`) | `gh -R bborbe/claude-supervisor pr ready 645` (no allowlist entry) | The manager when it owns the question, the operator when it does not — see below |
| **D** | **Production-touching or irreversible** — the allowlist's prod, irreversible, scope-changing and credentials deny classes, even when an allowlist entry also matches; `make apply`, deploy, merge, release, tag push, force-push, a delete, spawning or stopping a session (`docs/permission-allowlist.md` § The deny list) | `BRANCH=prod make apply` | The operator, in the pane |
| **E** | **Live-trade / TDR** — any `40 Decisions/TDR *` field, trade-create or trade-close on a funded account | `trade-create` on a live account | The operator, via `vault-cli decision ack` |

Also never answered, whatever the class: a request for a worker printed `REFUSE`, and a request for a worker whose session carries an operator hold (the wrangler's hold check).

### Is the class-C question the manager's?

Two readings, and only the first is routed:

- **The manager's** — the question asks which of two approaches the task file already permits, or what to do next inside the task's declared scope. Step 7 answers it with `Manager answer, via supervisor: <option label>`, which makes no provenance claim about the operator.
- **The operator's** — the question asks to widen or change the task's declared scope, chooses between outcomes the task file does not decide, or names a production-touching or irreversible action. Leave it pending: the prefix releases no such gate (`${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable).

⚠️ **When the answer is not derivable from the task file and the worker's own question, it is the operator's.** A wrong manager answer is worse than a timeout, because the worker acts on it and the round reports it as answered. Escalating costs one card; a wrong answer costs a worker building on a plan nobody reviewed.

## Report

One line in the round's output, always, even when skipped. It is built from the wrangler's scoped-mode report, whose allow reasons name the allowlist entry and whose escalations start with their class. Step 7 reads the `routed` half of it — that count is its trigger (`commands/steps/worker-channels.md`) — and nothing else parses it; it exists to be quoted when counting answered against escalated parks:

```
own-parks: answered <n> · routed <c> (class C → step 7) · escalated <m> (C <co> · D <d> · E <e> · hold <h> · unverified <u>) · refused-not-own <r>
```

Every park lands in exactly one of answered, routed, escalated or refused-not-own, so the line accounts for everything the step saw. ⚠️ **`routed` and the escalated `C <co>` are the two halves of one class, not two classes.** The wrangler escalates every class-C park; the step routes the ones the manager owns and leaves the rest pending, so `c + co` is the class-C population — `routed 3 · escalated 2 (C 1 …)` means three answered by the manager and one still the operator's. For each answered park, list `<request_id> | <worker label> | <tool> | allow | class B — allowlist_entry <prefix>`. For each routed park, list `<request_id> | <worker label> | AskUserQuestion | route | class C — <the question, one line>`. An answered line that names no allowlist entry is a defect: report it as one rather than counting it.
