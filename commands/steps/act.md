---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Act on the open-items ledger

When: the ledger holds ≥1 open entry

⚠️ **Reader matters here, because row 8 has NOT moved yet.** Today this step is still the **fleet manager's** (router row 8; `agents/fleet-round.md` §2), so every branch below is written for it. When the row moves to `supervisor:fleet-round`, two things change: `/vault-cli:create-task` stays legal (the delegate holds `Skill`, granted 2026-11-19), and the **spawn branch becomes a route** — `/supervisor:open` is a manager-only verb, so a worker must not run it. The move is its own four-place change, landing last.

**Act on every open ledger entry.** For each `asked-of-me` / `pushed` entry: no task behind it → file one (`/vault-cli:create-task` **passing `--docs-only` or `--code`** — the entry names the ask, so its route is known; passing it is what stops `create-task` asking) and record it on the entry; a task with no worker → spawn via `/supervisor:open "<task>"` (spawn readiness precondition first) — ⚠️ **when the reader is the manager, which it still is: row 8 has not moved.** Once it moves to the delegate, this branch becomes a **route** instead, because `/supervisor:open` is a manager-only verb (`docs/session-tiers.md`) and a worker must not run it; a stalled owner → nudge under the two classes above; task reads `status: completed` → verify on disk **this round**, then `close --evidence "<task file> reads status: completed"`. `asked-of-you` entries are re-surfaced until answered; `answer` in the turn the answer arrives **closes** the entry. On the other two kinds `answer` records a note and leaves the entry `open`.
