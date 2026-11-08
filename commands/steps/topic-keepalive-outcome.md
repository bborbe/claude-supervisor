# Topic round — record the keep-alive outcome

When: every `resume` or `restart` verdict the topic round's keep-alive reads — router row 5 of `commands/topic-drive.md`, § Keep alive.

⚠️ **This rule has exactly one home and it is this file.** `commands/topic-drive.md` passes `--receipt` and points here; it does not restate the outcome protocol.

## Why

Measured 2026-11-08 (topic manager `eab58671`, ticks 15:55 and 16:15): `topic-keepalive.py` read `resume` for three goal managers on two consecutive ticks, the round resumed none, and blamed first `spawn.maxConcurrent`, then N = 3. `manager-receipt.py --check` stayed green — every field it knew resolved, and nothing recorded that a verdict had been read at all.

## The verdict is owed — the probe records it

`topic-keepalive.py ... --receipt "<topic>"` — `<topic>` is the subject the readings step passed to `manager-receipt.py --start` — writes every `resume` / `restart` verdict onto the round's receipt as **owed** the moment it prints it. Nothing for the round to do here; a verdict the round forgets is still on the receipt.

## The outcome — the round records it

After acting on (or declining) the verdict, once per verdict:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-receipt.py \
  --record-outcome --topic "<topic>" --goal "<goal>" --outcome <outcome>
```

| outcome | when | what `--check` needs to clear it |
|---|---|---|
| `executed` | the resume / restart ran | **resume:** the `topic-keepalive.py --record-resume <id>` stamp, written only once the liveness instrument reads the id live, inside the round window. **restart:** the relaunched pane's `kind=spawn` exit record for that session, inside the window |
| `refused:<token>` | the command refused | nothing more — but the token must be one the command emits (below) |
| `deferred:busy-target` | `/supervisor:restart` refused `busy-target` (mid-turn) | nothing more — wait a tick, never kill in-flight work |

`/supervisor:restart` prints its token as its first line (`unknown-session-id`, `ambiguous-pid`, `headless-target`, `held-session`, `role-undetermined`, `stale-load-path`, `environment-fault`). `/supervisor:open` prints a label instead — map it: `⏸️ HELD` → `held`, `❓ SESSION UNREADABLE` → `session-unreadable`, `⏸️ CONCURRENCY LIMIT` → `concurrency-limit`, fleet full (`MANAGER_FLEET_FULL`) → `fleet-full`, `🔒 LIVE ELSEWHERE` → `live-elsewhere`. Any other token is refused at the write, and listed in the refusal.

⚠️ **`executed` is checked against the work's own record, never the word.** Writing `executed` for a resume that never ran still prints `INCOMPLETE ROUND — skipped: keepalive` at the tail.

## What the tail reports

`manager-receipt.py --check` (router row 12) prints one `INCOMPLETE ROUND — skipped: keepalive — <goal>: <why>` line per verdict with no outcome, an unknown token, or an `executed` with no record — and exits 11. A round that read no `resume` / `restart` verdict owes nothing, so a goal round and an all-`jump` topic round stay green. Surface every line in the tick's render, as for every other skipped step.

## N never gates a resume

N = 3 caps **starts** (`scripts/topic-spawn.py` refuses a new goal manager at `running >= N`). A resume or restart continues a manager the share already counts, so neither N nor `spawn.maxConcurrent` (its home: `docs/fleet-surface.md` § Spawn a worker item 5) is ever a `refused:` reason — the vocabulary above carries no token for either, by design.
