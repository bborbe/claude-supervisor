---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Act on the open-items ledger

When: the ledger holds ≥1 open entry

**Act on every open ledger entry.** For each `asked-of-me` / `pushed` entry: no task behind it → file one (`/vault-cli:create-task` **passing `--docs-only` or `--code`** — the entry names the ask, so its route is known; passing it is what stops `create-task` asking) and record it on the entry; a task with no worker → spawn via `/supervisor:open "<task>"` (spawn readiness precondition first); a stalled owner → nudge under the two classes above; task reads `status: completed` → verify on disk **this round**, then `close --evidence "<task file> reads status: completed"`. `asked-of-you` entries are re-surfaced until answered; `answer` in the turn the answer arrives **closes** the entry. On the other two kinds `answer` records a note and leaves the entry `open`.
