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
| `executed` | the resume / restart ran | **resume:** the `topic-keepalive.py --record-resume <id>` stamp **for the verdict's own session id**, written only once the liveness instrument reads that id live, inside the round window. **restart:** the relaunched pane's `kind=spawn` exit record for that session, inside the window |
| `refused:<token>` | the command refused | nothing more — but the token must be one **that verdict's** command emits (below) |
| `deferred:busy-target` | **restart only:** `/supervisor:restart` refused `busy-target` (mid-turn) | nothing more — wait a tick, never kill in-flight work |

**Tokens, per verdict** — any other is refused at the write, which lists the accepted set:

- **`resume`** — `/supervisor:open --goal`, whose resume path (Step 2B item 3 → Step 3) prints labels, not tokens: `⌛ waiting-approval` → `waiting-approval`; `--list --json` exited non-zero ("cannot tell") → `cannot-tell`; `start-rate.mjs` exit 1 → `start-rate`; `resolve-launcher.py` non-zero → `launcher-refused`.
- **`restart`** — `/supervisor:restart` prints its token as the first line: `unknown-session-id`, `ambiguous-pid`, `headless-target`, `held-session`, `role-undetermined`, `stale-load-path`, `environment-fault`, and `restart-precheck.py`'s `dirty-worktree`.

⚠️ **`executed` is checked against the work's own record, never the word.** Writing `executed` for a resume that never ran still prints `INCOMPLETE ROUND — skipped: keepalive` at the tail. A resume issued but not yet live at the tail has no stamp and reads incomplete too — wait for the liveness instrument before recording, as the keep-alive table already says.

⚠️ **The honest bound.** A `refused:` token is taken on the round's word — no artifact proves the refusal happened. Named rather than closed: the check makes a skipped verdict a visible act, it cannot make a fabricated one impossible — the general bound `scripts/manager-receipt.py`'s module docstring states under *The honest bound*.

## What the tail reports

`manager-receipt.py --check` (router row 12) prints one `INCOMPLETE ROUND — skipped: keepalive — <goal>: <why>` line per verdict with no outcome, an unknown token, or an `executed` with no record — and exits 11. A round that read no `resume` / `restart` verdict owes nothing, so a goal round and an all-`jump` topic round stay green. Surface every line in the tick's render, as for every other skipped step.

## N never gates a resume

N = 3 caps **starts** (`scripts/topic-spawn.py` refuses a new goal manager at `running >= N`). A resume or restart continues a manager the share already counts, and the resume path never reaches `/supervisor:open`'s Gate 3 — so neither N nor `spawn.maxConcurrent` (its home: `docs/fleet-surface.md` § Spawn a worker item 5) is ever a `refused:` reason. The vocabulary above carries no token for either, by design: those are the excuses the measured round gave.
