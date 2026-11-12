---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Answer a parked or exited worker

When: a worker is parked on a question or has exited

### Two channels reach a worker, and they are not interchangeable

| The worker is… | Whose answer | Channel | Call |
|---|---|---|---|
| **headless, parked on a question** | the **operator's** | supervisor permission channel — no pane | `mcp__supervisor__answer_permission(agent_id, request_id, behavior="deny", message="Operator answer, via supervisor: <option>")` |
| **headless, parked on a question** | **your own** | supervisor permission channel — no pane | `mcp__supervisor__answer_permission(agent_id, request_id, behavior="deny", message="Manager answer, via supervisor: <option>")` |
| **headless, parked on a permission gate** (server worker) | **yours when `permission_tier` is `allowlist` or `manager`**, the operator's when `operator` | supervisor permission channel — no pane | `mcp__supervisor__answer_permission(agent_id, request_id, behavior="allow"\|"deny")` — decide by the tier and rubric in `${CLAUDE_PLUGIN_ROOT}/docs/permission-allowlist.md` § *How a manager applies it*; cannot verify → escalate |
| **wrapper-backed (`cc-*` child), parked on a permission gate** | same tier rule — a `manager`-tier gate is **held off the operator's board** for you for 10 min, then falls through naming you | the wrapper, never the attention store | `POST localhost:18090/api/1.0/sessions/<session-id>/answer {"request_id", "behavior"}`, `request_id` from `GET /api/1.0/sessions` — see the doc § *Releasing a wrapper-backed gate* |
| **headless, already exited** (turn end, or ~11 min question timeout) | either | a fresh turn | `mcp__supervisor__spawn_agent(prompt="<the answer>", resume="<session-id>", interactive=false, cwd="<explicit>")` — **spawn readiness precondition first** |
| **a tab worker** | either | its pane — `send_agent_message`, or by hand | the relay protocol (`${CLAUDE_PLUGIN_ROOT}/commands/steps/needs-input.md`) |


- **The headless channel is primary.** For an `AskUserQuestion`, answer `deny` + `message`, never `allow` (allow runs the tool tty-less and the worker exits unanswered). On a refusal, switch this session to `accept edits` (Shift+Tab) and retry — the refusal is this session's own `auto`-mode classifier.
- **5d's `routed` count is this step's trigger for a class-C park.** `commands/steps/own-parks.md` reports `routed <routed> (class C → step 7)` on its summary line and one **per-park routed line** beneath it — `<request_id> | <worker label> | AskUserQuestion | route | class C — <the question and its options>`. The summary carries the count only; the ids and questions live on those per-park lines. Answer each routed id here; a round that prints `routed 0`, or a skip line, has nothing for this step from that leg. Take every `request_id` from a per-park routed line: never construct or guess one, and never derive one from the count.
- **The tab relay is the fallback** — only where the headless channel cannot reach the same effect.
- **Answer promptly or not at all.** Parked prompts auto-deny after 15 minutes (headless); a worker left waiting stalls silently. Prefer fewer, longer-lived workers over many short ones. ⚠️ **A long tick no longer caps itself for this — the manager chooses.** A tick longer than the 15-minute park window can leave a worker parked past it and resume it with a denial it did not earn; that is the manager's accepted choice, not a clamp on `--interval` (§ Interval in `goal-loop.md`). The wrangler auto-policy (`/supervisor:goal-drain` → the `supervisor:goal-wrangler` agent) answers parks without the manager's tick — ⚠️ **it is a ONE-SHOT loop — it stops as soon as its queue empties — so run it repeatedly for the life of the tick, or use a standing form**; a park raised after it exits auto-denies at the 15-minute mark.
