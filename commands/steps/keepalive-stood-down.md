---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps/<name>.
user-invocable: false
---

# Close a stood-down manager past its bound

Run by `agents/fleet-keepalive-actor.md` § 4b. `/supervisor:stop` disarms a manager's loop and
**keeps its session** by design, so nothing closes it: it holds a fleet slot, a full context
and a keeper-census row for a manager that no longer runs. This is the reverse direction of the
keep-alive — a session no tier keeps is adopted or closed, never left live and unkept
(`docs/session-tiers.md` § Adopt or close).

## 1. Select, then join — the marker is not the population

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/manager-liveness.py" --stood-down-past 60
```

**60 minutes is 4× the round's own cadence** (15 min default, 15 min floor,
`commands/fleet-loop.md`). It exceeds one cadence so a round that has not yet looked cannot
close anything, and sits far below the smallest observed live dwell.

⚠️ **The marker outlives the session it describes.** Measured 2026-11-09: 83 `<slug>.stopped`
markers, 41 of them on pages still holding a `claude_session_id`, and only **2** whose session
the registry read `LIVE` — at 9.3 h and 15.0 h. So the selector's answer is a candidate list,
not a work list.

For each `\t<slug>` row: split the slug on `--` into `<branch>` and `<base>`, resolve the page,
and keep the row **only** when the page's `claude_session_id` is a session
`session-liveness.py` reads `LIVE`. A dead session needs no close — the marker simply outlived
it, and closing nothing is the correct answer for those rows.

**Step 0's heldness read applies** — a held session is neither closed nor adopted.

## 2. Hand over the page first, and prove it landed

`close-worker.py`'s `page_verdict()` refuses `task-not-terminal` while the page still names the
session as `claude_session_id` on a non-terminal page. It returns `None` only when the session
has **handed the page over** (its id in `metrics_sessions` while `claude_session_id` names
another session), or the page is `completed`/`aborted`, or it carries a future `defer_date`.

The handover is two writes against the page — record the session in `metrics_sessions`, then
clear `claude_session_id`. ⚠️ **The proof is that a fresh `page_verdict` no longer returns
`task-not-terminal`, never that the write commands exited 0.**

## 3. Then close, through the one close path

```bash
python3 "$P/close-worker.py" --session <session-id>
```

Never a bare kill and never a direct process signal. Every refusal token it returns
(`gate-held`, `busy-target`, `dirty-worktree`, `task-not-terminal`, …) is honoured and reported
verbatim.

⚠️ **A refusal is a recorded failure, never an alternative pass.** `task-not-terminal` is
precisely the token a leg that skips the handover earns on every manager, so a run that stops
there is reported as **refused** with its token and its page, never as closed. Report the two
outcomes as distinct lines; a leg whose every row is refused has closed nothing and must read
that way.

## 4. What the close does and does not fix

⚠️ **Closing a stood-down topic manager does not make its goals fleet-kept.** Read
`keeper-census.py:361-397`: the topic page itself becomes `fleet round`, but every goal that
topic declares still reads `unkept` — `"declaring topic(s) have no live armed manager"`. The
close is the *first* step of a chain, not a single-step fix.

⚠️ **And this procedure does not close that chain, so do not report it as closed.** The
handover clears the page's `claude_session_id`, and § 1's population is *every topic page
carrying a session id* (`agents/fleet-keepalive-actor.md:149`) — so the page just handed over
is no longer in § 1's population, and § 1 will not open a replacement manager for it.
Re-opening is a **separate gap**, not a consequence of the close. Report the close, the topic's
keeper move and the goals still unkept; a round that renders them kept has reported something
the census does not say.
