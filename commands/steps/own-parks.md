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

⚠️ **A worker's question is not left to time out.** An `AskUserQuestion` park carries no `allowlist_entry`, so the wrangler escalates it — and until 2026-11-07 an escalation was the end of the road: the park sat until the 15-minute timeout auto-denied it and the worker exited having written nothing. Measured that day: a fleet manager spawned a worker whose plan gate asked a modal, nobody answered, and the task file was found untouched twenty minutes later. This step now **names** every class-C park in its report, and step 7 (`commands/steps/worker-channels.md`) answers the **routed** ones — those the manager owns — in the manager's own voice with `Manager answer, via supervisor:`. Detection is this step's; the decision is the round's — it spawned the worker and holds the task context the question is about, which a scoped wrangler reading one request payload does not.

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

An escalation of an ordinary tool request stays pending for the operator — that is the correct result. A **`pending_question`** park is never dispatched to the wrangler (it carries no `allowlist_entry`, per the split above); a class-C `AskUserQuestion` among them is instead **routed** to step 7, because leaving it pending is what the 15-minute timeout then answers with a denial.

⚠️ **Latency limit.** The step sees only what is parked when it runs. A park raised just after it waits for the next tick, and at a 15-minute interval it auto-denies first (`worker-channels.md`: *"Answer promptly or not at all"*). This step shortens the wait for routine parks; it does not make it zero.

## What this step never answers

The class boundary is a closed list. Class B is the only class this step **answers**; a class-C **`AskUserQuestion`** the manager owns is **routed** to step 7 rather than left pending; every other park below stays pending for the operator, and the wrangler escalating it is the correct result, not a failure.

⚠️ **Routing is scoped to `AskUserQuestion`, not to class C as a whole.** The `Manager answer, via supervisor:` prefix is honoured on an `AskUserQuestion` only — *"a denial on `Bash`, `Edit` or `Write` stays a denial whatever prefix it carries"* (`${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable). A class-C park that is not a question, such as the `gh pr ready` in the table below, has no channel that can carry an answer and is never routed.

| # | Class | Example | Who decides |
|---|---|---|---|
| **C** | **Not covered by the allowlist** — `allowlist_entry` is `null`; priority, scope or a choice between approaches; a worker's question (`AskUserQuestion`) | `gh -R bborbe/claude-supervisor pr ready 645` (no allowlist entry) | The manager when it owns the `AskUserQuestion`; the operator for every other class-C park — see below |
| **D** | **Production-touching or irreversible** — the allowlist's prod, irreversible, scope-changing and credentials deny classes, even when an allowlist entry also matches; `make apply`, deploy, merge, release, tag push, force-push, a delete, spawning or stopping a session (`docs/permission-allowlist.md` § The deny list) | `BRANCH=prod make apply` | The operator, in the pane |
| **E** | **Live-trade / TDR** — any `40 Decisions/TDR *` field, trade-create or trade-close on a funded account | `trade-create` on a live account | The operator, via `vault-cli decision ack` |

Also never answered, whatever the class: a request for a worker printed `REFUSE`, and a request for a worker whose session carries an operator hold (the wrangler's hold check).

### Is the class-C question the manager's?

Two readings, and only the first is routed:

- **The manager's** — the question asks which of two approaches the task file already permits, or what to do next inside the task's declared scope. Step 7 answers it with `Manager answer, via supervisor: <option label>`, which makes no provenance claim about the operator.
- **The operator's** — the question asks to widen or change the task's declared scope, chooses between outcomes the task file does not decide, or names a production-touching or irreversible action. Leave it pending: the prefix releases no such gate (`${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable).

⚠️ **When the answer is not derivable from the task file and the worker's own question, it is the operator's.** A wrong manager answer is worse than a timeout, because the worker acts on it and the round reports it as answered. Escalating costs one card; a wrong answer costs a worker building on a plan nobody reviewed.

⚠️ **A routed park that comes back is escalated, not routed again.** If the same `request_id` appears as a class-C escalation on **two consecutive ticks**, step 7's answer did not land — the worker is still parked. Route it once more is pointless: report it in the escalated `C` count and name it for the operator on that second tick. This is the fleet tier's only fallback for a routed park that no one answered, and without it a broken answer path looks exactly like a worker that is simply slow.

⚠️ **The question you judge is the park's own text, never a guess from the tool name.** Two park sources carry it: a **`pending_question`** row holds `{request_id, kind, tool_name, tool_use_id, input}` — the raw wrapper question and its options — and a **`pending_permissions`** `AskUserQuestion` reaches you through the wrangler's escalation line, which `agents/goal-wrangler.md` § `<output_format>` requires to carry the question and its option labels. A park whose text names neither cannot be classified: leave it pending and report it as such.

## Report

One line in the round's output on every tick that reaches the dispatch. It is built from the wrangler's scoped-mode report, whose allow reasons name the allowlist entry and whose escalations start with their class. Step 7 reads the `routed` half of it — that count is its trigger (`commands/steps/worker-channels.md`) — and nothing else parses it; it exists to be quoted when counting answered against escalated parks:

```
own-parks: answered <answered> · routed <routed> (class C → step 7) · escalated <escalated> (C <c-operator> · D <d> · E <e> · hold <h> · unverified <u>) · refused-not-own <refused>
```

A **skip** line is the one exception, and it means the same as `routed 0`: preconditions 1–3 print `own-parks: skipped — …` or `own-parks: nothing parked` and nothing was read, so step 7 has nothing to answer from that leg. Read a skip as zero routed, never as an unknown.

Every park lands in exactly one of answered, routed, escalated or refused-not-own, so the line accounts for everything the step saw. ⚠️ **`routed` and the escalated `C <c-operator>` are the two halves of one class, not two classes.** The wrangler escalates every class-C park; the step routes the `AskUserQuestion` ones the manager owns and leaves the rest pending, so `routed + c-operator` is the class-C population — `routed 3 · escalated 2 (C 1 …)` means three answered by the manager and one still the operator's.

For each answered park, list `<request_id> | <worker label> | <tool> | allow | class B — allowlist_entry <prefix>`. For each routed park, list `<request_id> | <worker label> | AskUserQuestion | route | class C — <the question and its options, one line>`.

The routed line's question and options come from the wrangler's escalation text — § *Is the class-C question the manager's?* names why that is the only source and what to do when it is absent.

An answered line that names no allowlist entry is a defect: report it as one rather than counting it.
