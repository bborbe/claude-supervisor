# Answer a parked or exited worker

When: a worker is parked on a question or has exited

### Two channels reach a worker, and they are not interchangeable

| The worker is… | Whose answer | Channel | Call |
|---|---|---|---|
| **headless, parked on a question** | the **operator's** | supervisor permission channel — no pane | `mcp__supervisor__answer_permission(request_id, behavior="deny", message="Operator answer, via supervisor: <option>")` |
| **headless, parked on a question** | **your own** | supervisor permission channel — no pane | `mcp__supervisor__answer_permission(request_id, behavior="deny", message="Manager answer, via supervisor: <option>")` |
| **headless, already exited** (turn end, or ~11 min question timeout) | either | a fresh turn | `mcp__supervisor__spawn_agent(prompt="<the answer>", resume="<session-id>", interactive=false, cwd="<explicit>")` — **spawn readiness precondition first** |
| **a tab worker** | either | its pane — `send_agent_message`, or by hand | the relay protocol (`${CLAUDE_PLUGIN_ROOT}/commands/steps/needs-input.md`) |


- **The headless channel is primary.** For an `AskUserQuestion`, answer `deny` + `message`, never `allow` (allow runs the tool tty-less and the worker exits unanswered). On a refusal, switch this session to `accept edits` (Shift+Tab) and retry — the refusal is this session's own `auto`-mode classifier.
- **The tab relay is the fallback** — only where the headless channel cannot reach the same effect.
- **Answer promptly or not at all.** Parked prompts auto-deny after 15 minutes (headless); a worker left waiting stalls silently. Prefer fewer, longer-lived workers over many short ones. ⚠️ **A long tick no longer caps itself for this — the manager chooses.** A tick longer than the 15-minute park window can leave a worker parked past it and resume it with a denial it did not earn; that is the manager's accepted choice, not a clamp on `--interval` (§ Interval in `manager-loop.md`). The wrangler auto-policy (`/supervisor:manager-drain` → the `supervisor:manager-wrangler` agent) answers parks without the manager's tick — ⚠️ **it is a ONE-SHOT loop — it stops as soon as its queue empties — so run it repeatedly for the life of the tick, or use a standing form**; a park raised after it exits auto-denies at the 15-minute mark.
