---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Act on the open-items ledger

When: the ledger holds ≥1 open entry

⚠️ **This step runs in `supervisor:fleet-round` (router row 8). The delegate holds `Skill` for `/vault-cli:create-task` (granted 2026-11-19), and holds no right to `/supervisor:open` — that is a manager verb, so a row needing a spawn is ROUTED, not run.** ⚠️ **Row 8 has not moved yet**; the four-place move is its own change, landing after this and the other step files are ready.

**Act on every open ledger entry.** For each `asked-of-me` / `pushed` entry: no task behind it → file one (`/vault-cli:create-task` **passing `--docs-only` or `--code`** — the entry names the ask, so its route is known; passing it is what stops `create-task` asking) and record it on the entry; a task with no worker → ⚠️ **ROUTE to the manager, never spawn yourself** — `/supervisor:open` is a **manager-only verb** and a worker must not run it (`docs/session-tiers.md`); render the task and its readiness, and the manager opens it (spawn readiness precondition first, on its side); a stalled owner → nudge under the two classes above; task reads `status: completed` → verify on disk **this round**, then `close --evidence "<task file> reads status: completed"`. `asked-of-you` entries are re-surfaced until answered; `answer` in the turn the answer arrives **closes** the entry. On the other two kinds `answer` records a note and leaves the entry `open`.
