---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Act on the open-items ledger

When: the ledger holds ≥1 open entry

⚠️ **This step runs in `supervisor:fleet-round` (router row 8), so it is written for a headless delegate.** `/vault-cli:create-task` is legal here — the delegate holds `Skill` (granted 2026-11-19) — but **`/supervisor:open` is not**: it is a manager-only verb (`docs/session-tiers.md`), so the spawn branch below is a **route**, never a spawn.

**Act on every open ledger entry.** For each `asked-of-me` / `pushed` entry: no task behind it → file one (`/vault-cli:create-task` **passing `--docs-only` or `--code`** — the entry names the ask, so its route is known; passing it is what stops `create-task` asking) and record it on the entry; a task with no worker → ⚠️ **ROUTE it, never spawn** — write the task and its readiness (spawn readiness precondition included) into the render's Needs-input item, at `~/.claude/state/fleet-handback/<round>.render`, and let the manager's row 11 run `/supervisor:open` from there, because `/supervisor:open` is a manager-only verb and a worker must not run it; a stalled owner → nudge under the two classes above; task reads `status: completed` → verify on disk **this round**, then `close --evidence "<task file> reads status: completed"`. `asked-of-you` entries are re-surfaced until answered; `answer` in the turn the answer arrives **closes** the entry. On the other two kinds `answer` records a note and leaves the entry `open`.
