#!/usr/bin/env python3
"""Decide the heal for a STUCK headless worker — and never perform it itself.

A headless worker whose permission channel dies ("Stream closed") cannot be
restarted the way a tab worker can. `scripts/restart-worker.py` refuses a headless
target by design — *"a headless worker has no reliable resume guard, so its
liveness stays undetermined"* — and the resume it *does* perform is a
`wezterm cli spawn`, which is a tab. So the heal runs through the supervisor's
`spawn_agent` MCP tool, and **only a Claude session can call that**.

That constraint is the whole design. `heal()` takes its effects as ARGUMENTS
rather than performing them, so the decision is testable without a supervisor, a
vault or a pane, and `main()` prints the action for a manager to execute. A script
that tried to resume a worker itself would have to reimplement an MCP client to do
it, and would then be a second resume mechanism beside the one that already
refuses this case.

The ladder, and why it stops:

  not yet resumed → resume headless once. A first death is a transient; the
                    channel restarts and the worker carries on in the same id.
  resumed already → reopen as an INTERACTIVE tab. The first resume did not hold,
                    so the cause is the channel, not the blip.

⚠️ There is no third rung, and that is structural rather than an omission. The
spawn ledger is keyed by session id and a resume keeps the SAME id, so a resume
OVERWRITES its record in place: the ledger can say "resumed at least once" and
never "resumed twice". The bound therefore holds through the second rung itself —
the interactive reopen is a new TAB session, which the watcher reads as
`stuck-tab:` and never feeds back here, and the dead headless session has no
heartbeat left to raise another gate with. A failed reopen is reported and not
retried: the operator decides, the ladder does not loop.

A parked JUDGEMENT gate is not a death and is never healed: it is reported, and
`answer_permission` is never called. Auto-answering a judgement gate is out of
scope for this task and would be the worst possible direction — it settles a
question the operator was asked, on their behalf, silently.
"""
import argparse
import json
import os
import sys

ACTION_RESUME_HEADLESS = "resume-headless"
ACTION_REOPEN_INTERACTIVE = "reopen-interactive"
ACTION_REPORT_ONLY = "report-only"

# The only death this heals. A parked gate is not a death — see the module
# docstring — and an unrecognised kind is reported rather than guessed at.
KIND_STREAM_CLOSED = "stream-closed"

# Resolved as `server/config.mjs:167` does.
LEDGER_DIR = os.environ.get("SUPERVISOR_LEDGER_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "claude-supervisor", "sessions")


def decide(kind, deaths):
    """The action for a death of `kind`, given `deaths` prior headless resumes.

    ⚠️ `deaths` counts HEADLESS RESUMES, not deaths, and from the real ledger it is
    only ever 0 or 1 (see the module docstring). Any non-zero value is the
    interactive rung — never a second headless resume.

    ⚠️ A kind that is not `stream-closed` is REPORT-ONLY, never a resume and never
    an answer. A parked judgement gate reaching a resume would restart a worker
    that is working correctly, and reaching an answer would settle the operator's
    question for them.
    """
    if kind != KIND_STREAM_CLOSED:
        return ACTION_REPORT_ONLY
    if deaths == 0:
        return ACTION_RESUME_HEADLESS
    return ACTION_REOPEN_INTERACTIVE


def deaths_from_ledger(session_id, ledger_dir=LEDGER_DIR):
    """How many times `session_id` has already been resumed.

    Read from the spawn ledger, which is the only store that keeps this: the
    session registry forgets a session exactly when it exits, and the transcript
    records the conversation rather than the spawns. A resume keeps the SAME
    session id, so the chain is counted by `resumed_from`, not by distinct ids.

    ⚠️ A missing or unreadable ledger returns 0 rather than raising. Zero means
    "resume headless", which is the first rung — the recoverable direction. The
    opposite default would skip straight to reporting a worker that has never been
    resumed.
    """
    try:
        names = os.listdir(ledger_dir)
    except OSError:
        return 0
    n = 0
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(ledger_dir, name)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            # Well-formed JSON that is not a record — skip it, never crash on it.
            continue
        # ⚠️ A PREFIX is a legal lookup and is the form a manager actually holds:
        # the watcher prints 8 chars, so an exact match would silently count 0 and
        # resume a session that had already been resumed — the loop the bound
        # exists to stop, reached through the join rather than the ladder.
        rf = str(rec.get("resumed_from") or "")
        if rf and rf.startswith(session_id):
            n += 1
    return n


def heal(session_id, task, kind, deaths, *, spawn_headless, spawn_interactive,
         set_task_mode, post_card, jump_link):
    """Run the action `decide()` names, through the injected effects.

    Every effect is a callable the caller supplies, because every one of them is a
    tool only a manager session holds: the two spawns and `set_task_mode` are the
    manager's own, and `post_card` / `jump_link` are shell helpers. Injecting them
    is what lets a test assert "exactly one headless resume, and the session id
    unchanged" with no supervisor in the loop.

    Returns the action taken, so a caller can log it without re-deriving it.
    """
    action = decide(kind, deaths)
    if action == ACTION_RESUME_HEADLESS:
        # Same session id — the resume continues the conversation rather than
        # starting a new one, which is what keeps the task's claude_session_id
        # pointing at the work.
        spawn_headless(session_id)
        return action
    if action == ACTION_REOPEN_INTERACTIVE:
        # Mode first, then the tab: a tab opened before the task says
        # `interactive` is a tab the next reader will treat as headless.
        set_task_mode(task, "interactive")
        try:
            pane = spawn_interactive(session_id)
        except Exception:
            # Never leave the task saying `interactive` with no tab behind it: put
            # the mode back and tell the operator, then let the failure surface.
            set_task_mode(task, "headless")
            try:
                post_card(f"{task} died twice with a closed permission channel — "
                          f"the interactive reopen FAILED; mode restored to "
                          f"headless", None)
            except Exception:
                pass  # the spawn failure below is the one the operator needs
            raise
        post_card(f"{task} died twice with a closed permission channel — reopened "
                  f"as an interactive tab", jump_link(pane))
        return action
    post_card(f"{task} is STUCK ({kind}) and past the heal ladder — no resume "
              f"attempted", None)
    return action


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", required=True, help="the stuck session id")
    ap.add_argument("--task", required=True, help="its task name")
    # Required, not defaulted: a default of `stream-closed` would turn every
    # caller that forgot it into a headless resume — the one rung that acts.
    ap.add_argument("--kind", required=True,
                    help="the stuck cause from the gate reason, e.g. stream-closed")
    ap.add_argument("--ledger-dir", default=LEDGER_DIR)
    args = ap.parse_args(argv)
    if len(args.session.strip()) < 8:
        # A short or empty id prefix-matches every ledger record.
        ap.error("--session must be at least 8 characters of the session id")

    deaths = deaths_from_ledger(args.session, args.ledger_dir)
    action = decide(args.kind, deaths)
    # The decision only. The manager holds the tools that perform it; printing the
    # action is how the two halves meet, and it keeps this script runnable on a
    # host with no supervisor at all.
    print(f"HEAL {args.session} [{args.kind}] resumes={deaths} -> {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
