---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps/<name>.
user-invocable: false
---

# Resume a decision-parked worker whose wait cleared

The clock leg (`agents/fleet-keepalive-actor.md` § 2) reads a wait that wrote itself a
`defer_date`. Three waits write none and no reader resumes them: an **approval** that landed
after its worker was closed, an **answered attention card** whose worker exited, and a
**`blocked_by` set** that has gone terminal. The reap (`scripts/reap-idle-headless.py`) closes
such a worker at 15 minutes idle and deliberately leaves the transcript and the task's
`claude_session_id` intact, so the row is resumable — and until this half runs, nothing resumes
it. The clock leg is not a substitute: it reads `park-event.py due`, which holds only waits that
registered a watch.

## Population — the reap's residue, read from the task files

A row is a candidate when its `claude_session_id` names a session `session-liveness.py` does
**not** report live and its `status` is still live (`next` / `todo` / `in_progress`). Do not
re-derive that index: `approved-not-started.py` already walks it and splits the two classes —
`never-started` (no id; the lever is open, not resume) and **`orphaned`** (id not live; the lever
is resume). This half takes `orphaned` only.

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/approved-not-started.py" --tasks-dir "<vault>/<tasks-dir>" --json
```

⚠️ **`--tasks-dir` is required and the path is the vault's own** — resolve it from `vault-cli
config`, never hardcode a directory name: a renamed tasks directory would glob nothing and this
half would report a clean round, the exact silent failure the topicless-goal leg exists to
remove. ⚠️ **An `unknown` reading is not `0`** — an unreadable registry prints `unknown` and
exits non-zero; report it as it comes back and resume nothing, because "no candidates" and "I
could not ask" must not render the same.

## Read the wait, per kind, and act only when it cleared

A candidate whose wait has not cleared is left alone and is not counted as resumed — the point
of this half is that a closed worker is resumed by an event, never by a clock, so an unchanged
wait produces no act on any round.

| Kind | The read | Resume |
|---|---|---|
| (a) approval | the row carries `approved_at` — the vault's own record, written only by `vault-cli task approve` | plain continue |
| (b) answered card | the attention store holds a card for that session id whose record reads answered | **the card's answer as the prompt** |
| (d) `blocked_by` | every name in `blocked_by` resolves to a task whose status is terminal | plain continue |

⚠️ **Kind (b) cannot resume bare.** An exited worker still holds its unanswered question: a
resume with an empty prompt, or with "continue", leaves the question unanswered and re-parks the
worker at the same gate (`docs/fleet-surface.md` § *A headless worker exits at turn end*). The
answer is the prompt — a resume that restores the process without delivering it produces the
exact loop this half exists to end. ⚠️ **When the card's answer cannot be read, the row is
`unread`, never "no wait"** — the three-valued rule `round-readings.py` applies, and for the
same reason: folding a failed read into a negative resumes a worker whose question nobody
answered, or leaves one waiting on an answer that did land.

Resume each named task through `/supervisor:open --task "<name>"` — the same act as § 2, never a
fresh session and never a bare `claude`, which brings a session back on a reduced MCP surface.

## The hand-back

**Count the rows this half resumed — that is `decision-resumed`**, and the number it *considered*
goes beside it as `decision-candidates`, so a round that saw rows and resumed none is
distinguishable from one that saw none. Neither is a digest key.
