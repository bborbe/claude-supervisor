# Worker Session States

A worker session is in exactly one of four states when it ends a turn. The state is a fact about what the session is doing *now*, not a summary of what it achieved, and `/supervisor:worker-drive` reports it as the first line of the §8 report so the operator can read a session's state without opening the pane.

## The four states

| State | Meaning | Signal read this run | Closer icon |
|---|---|---|---|
| `working` | Generating now | A tool call in flight, or the run is mid-drive with an actionable row | 🟢 |
| `external` | Waiting on a machine | A bot, CI run, deploy, watcher or clock; a watcher is armed and named in `⏰ Ends:` / `⏰ Next:` | 🟡 |
| `decision` | Only the operator can unblock | A `👤 You:` line of `pick` / `approve:` / `review:` / `you run:`, with its card | 🔵 |
| `closed` | The anchor is terminal on disk | Task `completed` / `aborted` / `hold`, zero open boxes | ⚪ |

**Unclassified fallback.** A session that fits none of the four prints `unclassified — <reason>` and is treated as `decision` until a pane read says otherwise. It is never forced into a bucket.

## External — arm the watcher yourself

A wait on a bot, CI, deploy or clock is `external`, not `decision`. The run arms the watcher itself (`/github-pr-watch`, `ScheduleWakeup`, `Monitor`, `CronCreate`, a `defer_date`) and names it in `⏰ Ends:`. It never asks the operator to "reply watch": an external wait dressed as a decision blocks the operator on a machine event.

⚠️ **`external` is a state for a wait, not a licence to hold a slot for hours.** A wait expected to run longer than the park threshold — `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/park-event.py threshold`, which is the only place the number is written — is not lived through resident. It **parks**: `defer_date` at the re-check instant (the readers honour the time of day), the event key registered through `park-event.py register` (which joins a watch a peer already holds for the same event rather than arming a second), the trigger recorded as `later (on <trigger>)`, and the slot released. The rule's home is `commands/worker-drive.md` § *Park a clock-bound session*; this paragraph names only what it means for the state vocabulary, so a session that reports `🟡 WAITING` with an unbounded watcher is reporting the wrong state rather than a legal one.

## Decision — exactly one card

Every `decision` run produces exactly one board card for its ask:

- the closing `AskUserQuestion` — the `Stop` hook mints its card;
- or, when there is no modal, the closer's own waiting verb (`pick —`, `you run:`, `review:`), which mints without a `📣 Board:` line. The mint rule's home is the `Stop` hook's `board_opt_in or approve_closer or waiting_closer` condition in `attention-log.py`; do not restate it here.

Never add a second card for the same ask: no `📣 Board: yes` line and no manual `attention-ask.py post` on top of a waiting-verb closer. Arm `answered-watch.py` only after the card is confirmed present (`attention-ask.py poll <item-id>` returns the open item) — a stale local gate slot can suppress the mint until `bborbe/claude#126` lands.

## Closed — session-close only on a terminal anchor

`closed` covers `completed`, `aborted` and `hold`, but only `completed` and `aborted` are terminal: run `/vault-cli:sync-progress`, then `/vault-cli:session-close`, and stop. `hold` is parked, not finished — sync and report it, never end the session on it. This is the existing Never-end-the-session rule, not a new one.

## Provenance

The vocabulary and its gaps were first recorded in the operator's vault note `Worker Session States`; that note is provenance, not a dependency — the rule above is the copy this plugin runs on.
