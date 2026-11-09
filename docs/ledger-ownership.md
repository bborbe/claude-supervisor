# Ledger ownership — who writes a session's terminal verdict

Which process owns a spawn-ledger row's `status` / `result` / `ended_at`, and what the
retained Node implementation is allowed to mean.

**The rule: the process that holds the child's stdin is the only writer of that child's
terminal verdict.** A turn ends inside the process reading the child's event stream, so no
other process can observe it without guessing; a second writer would either duplicate the
verdict or overwrite one it never saw. The row is keyed by the child's own `session_id`, so
"one row, one writer" is enforceable by construction rather than by convention.

## Which implementation is live on this fleet

The Go daemon **`bborbe/claude-wrapper`** (`~/.local/bin/claude-wrapper`, `-listen
localhost:18090`). Every vault's MCP entry points the `supervisor` server at its HTTP face —
`{"type": "http", "url": "http://127.0.0.1:18090/supervisor/mcp"}` in
`~/.claude/mcp-<vault>.json` — so the server this fleet talks to is the Go one, not the Bun
server in this repo.

Its writes are the whole lifecycle of a row:

| Moment | Write |
|---|---|
| spawn | `POST /api/1.0/session-ledger` — the spawn row, non-terminal |
| a new turn is written | `PATCH /api/1.0/session-ledger/{session_id}` — `status: running`, `ended_at`/`result` cleared, **before** the turn reaches the child |
| the child's `result` event | `PATCH` — `status: done` (or `error` when `is_error`), the child's own result event verbatim, `ended_at` |
| the child leaves with a turn open | `PATCH` — `status: error` with the cause inside `result` |

## What `server/agent-loop.mjs` still is

**Node-only, and DEAD on this fleet.** `server/agent-loop.mjs` carries the same terminal
patch (`writeLedger(agent, { …terminal, status: 'done', result: agent.result })`, and
`status: 'error'` in the `catch`) for a deployment where *this* server spawns the child
itself. No vault's MCP config launches it: `pgrep -f supervisor.mjs` returns nothing on the
Mac, and the Go daemon owns the port those configs point at.

It is retained rather than deleted because it is the reference implementation the Go port was
measured against, and because a Node-hosted deployment would still be correct — the rule is
"whoever holds the child writes the verdict", not "only Go may write it". Read it as
documentation of the intended semantics, never as a writer on this fleet.

⚠️ **Do not add a second writer here for a Go-spawned worker.** A `writeLedger` call from
this server for a session it did not spawn would race the daemon's `PATCH`, and whichever
landed last would silently be the row's verdict — including turning a finished worker back
into a `running` one.

## Reading the row

⚠️ **`status` is not liveness, in either direction.** `server/ledger.mjs` says so in as many
words — 1480 legacy rows still assert `running` for workers long dead, so a reader filtering
on it resurrects the whole fleet — and the store's own type comment repeats it. Liveness comes
from the heartbeat store, never from this field.

What the daemon writes is a **per-turn** verdict, not a process verdict:

| Value | What it proves |
|---|---|
| `done` / `error` | the last turn closed — **not** that the worker left |
| `running` | a turn was written and has not closed yet |

A headless worker outlives its turn here: the daemon keeps the child, its stdin and its
reachability, so a worker between turns reads `done` and is still perfectly reachable. That is
why the daemon re-opens the row with `running` (clearing `ended_at` and `result`) when the next
turn is written — and why a row left at `done` is not evidence that anything ended.

⚠️ **`ended_at` is the field a reader is most likely to over-read.** On the Node side it is
load-bearing in the opposite direction from what its name suggests: `server/a2a-spawn.mjs`
skips any record carrying it and `server/supervisor.mjs` treats `!record.ended_at` as
still-live, so "a worker with `ended_at` is gone" is the wrong inference for a worker between
turns. Those paths do not run on this fleet (see above), but a new reader should key on the
heartbeat store for liveness rather than on this column.
