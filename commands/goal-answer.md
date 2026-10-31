---
description: Answer a pending worker permission prompt
argument-hint: "[request_id] [allow|deny] [message]"
allowed-tools: mcp__supervisor__answer_permission, mcp__supervisor__list_agents, Read
---
Answer the pending permission: $ARGUMENTS

If the argument lacks a request_id or an allow/deny verb, ask for the missing part — never guess.

⚠️ **Resolve the request to its session first, and refuse when that session carries an operator hold — for a *manager*-voiced answer only.** A hold is operator *policy* about a **session** (*do not work on this*), and answering a prompt unblocks that session to keep working. `mcp__supervisor__list_agents` returns each row with **both** `pending_permissions` and `session_id` side by side (`server/supervisor.mjs:1054,1068`), so the join is direct and needs no guessing:

1. `mcp__supervisor__list_agents` → the row whose `pending_permissions` contains this `request_id` → take its **`session_id`**.
2. `Read` `~/.claude/state/session-holds.json` and take the entry keyed on that **`session_id`** — never the row's `agent_id`, which is a supervisor handle and not a session id.
3. Held **and** the answer is your own → **do not call `answer_permission`.** Report the hold and its reason, and name the release path (`/supervisor:hold release <target>`) — the operator can release and re-answer in one step.
4. No row carries the request id → say so and stop. **Never fall through to answering**: an unresolvable request is not an absent hold, and the two are indistinguishable downstream.

⚠️ **The split in step 3 is provenance, not mechanism, and it is the whole point.** An answer you give under `Manager answer, via supervisor:` is **your own decision** — and deciding to unblock a parked session is exactly what a hold forbids. An answer you relay under `Operator answer, via supervisor:` is **the operator's own**, given in this session — deliver it. *The hold constrains agents, not the operator*; refusing their own words would drop a decision they actually made, which is the same non-site reading `agents/gate-relay-send.md` carries. The canonical rule — the store, the CLI, and why the row must stay visible — lives in `skills/hold/SKILL.md`; read it there rather than restating it here.

Call `mcp__supervisor__answer_permission` with that request_id and behavior. For a denial, supply a `message` telling the worker what to do instead. Then confirm which worker was unblocked.

⚠️ **For an `AskUserQuestion`, the answer is `deny` + a `message` — never `allow`.** `allow` lets the tool run in a tty-less session, where it waits ~11 minutes and the worker **exits with the question unanswered**; the gate then reads as a stall with no error recorded anywhere. Put the answer in the `message`, in the form the question expects (the option label, or the free text), and **prefix it with the voice that is actually answering**: `Operator answer, via supervisor:` when the operator answered this question in your session, `Manager answer, via supervisor:` when the decision is your own. Using the operator prefix for your own inference forges a provenance claim the worker is written to reject — measured 2026-09-20: the worker received it, agreed with it, and still did not act. ⚠️ **Neither prefix releases an irreversible or production-touching action** — those need the operator's own confirmation, and a relay launders exactly the wording that makes it worth having. Full spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable. Reserve `allow` for an ordinary tool-approval request, where the tool can actually run. This is the primary answer path for a headless worker — it needs no pane, and the whole tab-relay apparatus below has no target for one. Full spec, including the continuation that replaces this call once a worker has exited: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § A headless worker exits at turn end.

⚠️ **This call is gated by *your own* session's permission mode, not the worker's.** Under `auto`, the classifier can refuse the outgoing `answer_permission(allow)` before the fleet is involved — measured 2026-09-19; the refusal is the manager's own call being gated, not a block on the worker's side. The fix is one keystroke here: **Shift+Tab → `accept edits`, then retry the same call.** Do **not** respond to a refusal by changing the worker's mode or `defaultMode` — `spawn_agent` has no such argument, and workers inherit it from `~/.claude/settings.json`.

Only a headless worker (`interactive: false`) parks a request. A tab worker answers its own prompts in its tab, so there is nothing here to answer for one — to unblock a tab worker, type into it with `send_agent_message` or drive its tab by hand.
