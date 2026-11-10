#!/usr/bin/env python3
"""`close-idle-tab.py` — close an idle TAB worker's pane, resumable later.

The fleet round's end-of-life for a tab worker that was nudged and did not move: its pane
is closed, and **the task stays `in_progress` with its `claude_session_id` intact** so
`/supervisor:open --task` resumes the same session in a new tab. The row stops holding a
spawn-cap slot and stops appearing under `FREEABLE TABS` for an operator click.

Why a separate verb rather than `close-worker.py`: that script's task gate refuses
`task-not-terminal`, and it is right to — it closes *finished* sessions, where a close is
the end of the work. This verb has a different contract: the task need not be finished,
only **parked and unchanged**, so it names its own gate instead of weakening that one.
The precedent is `reap-idle-headless.py`, which exists for the same reason on the headless
side and refuses `not-headless` because a tab's pane close is `close-worker.py`'s pane
path. This script is that pane path with this verb's gate; the pane kill and the
pane→session join are imported from `close-worker.py`, never copied.

Refusals, each exit 1 with a stable token as the first line of output:

  unknown-session-id    no registry entry carries that session id, or the registry is
                        unreadable.
  ambiguous-pid         more than one registry entry claims the id, or it carries no
                        integer pid.
  held-session          the operator holds this session (`/supervisor:hold`). Only they
                        release it; a close is exactly the act the hold prevents.
  already-gone          the pid is not alive; nothing to close.
  not-tab               `entrypoint` is not `cli`. A headless worker's process close is
                        `reap-idle-headless.py`'s, and SIGTERMing a tab's pid would leave
                        its pane open behind a dead session.
  busy-target           registry `status` is not `idle` — the worker is mid-turn.
  not-idle-long-enough  idle less than `IDLE_FLOOR` per `statusUpdatedAt`.
  dirty-worktree        the target is the only live session in its worktree and that
                        worktree carries tracked changes. A close strands them.
  gate-held             the attention store could not be read, an open card is parked on
                        the session, or the transcript tail carries an unanswered
                        `👤 You:` gate. Closing discards a question nobody answered.
  watcher-alive         a surviving child of the worker is not one of its MCP/stdio
                        helpers — a backgrounded command would die with the pane.
  task-unresolved       no task page carries this session id as `claude_session_id`.
                        Absent evidence is not evidence of a parked task.
  task-terminal         the task is terminal, handed off, or deferred to the future: that
                        is `close-worker.py`'s population, not this one.

Exit codes: 0 closed, 1 refusal or operational error, 2 usage error.

⚠️ **It closes the pane, never the session.** The transcript and the task's
`claude_session_id` must survive — the resume consumer needs both — and the success path
asserts the transcript still exists before reporting, so a change that turned this into a
session delete fails loudly.
"""
import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_sibling(filename):
    """Import a sibling script by path — the repo's own convention for sharing code."""
    name = os.path.basename(filename).replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


close_worker = load_sibling("close-worker.py")
precheck = load_sibling("restart-precheck.py")
restart_worker = load_sibling("restart-worker.py")
wnm = load_sibling("who-needs-me.py")
jump = load_sibling("jump.py")
reap = load_sibling("reap-idle-headless.py")

#: How long a tab worker must have been idle before its pane may be closed. The round's own
#: cadence: the nudge is drafted in one round and the close acts in the next, so a floor of
#: one round is the interval the rule already assumes. Sharing the headless verb's constant
#: keeps one number for "long enough to be abandoned" rather than two that drift.
IDLE_FLOOR = reap.IDLE_FLOOR

refuse = restart_worker.refuse


def tab_gate(rec, idle, cards, cards_readable, children, held=None):
    """`(token, detail)` for the first failing pure guard, or None when the close may proceed.

    Pure apart from the injected readings, so the whole predicate is testable without a
    live worker. Order matters: the hold outranks everything, then identity and liveness,
    then the cheap state reads, then the attention store, then the watcher tree.
    """
    if held is not None:
        return (
            "held-session",
            f"session {rec.get('sessionId')} carries an operator hold ({held}); only the "
            "operator releases it -- see the /supervisor:hold skill",
        )
    pid = rec.get("pid")
    if not isinstance(pid, int):
        return ("ambiguous-pid", "the registry entry carries no integer pid")
    if not precheck.pid_is_alive(pid):
        return ("already-gone", f"pid {pid} is not alive; nothing to close")
    entrypoint = (rec.get("entrypoint") or "").strip().lower()
    if entrypoint != "cli":
        return (
            "not-tab",
            f"entrypoint is {entrypoint or '(none)'!r}, not 'cli'; a headless close is "
            "reap-idle-headless.py's",
        )
    status = (rec.get("status") or "").strip().lower()
    if status != "idle":
        return ("busy-target", f"registry status is {status or '(none)'!r}, not 'idle'")
    if idle is None:
        return ("not-idle-long-enough", "the entry carries no statusUpdatedAt/updatedAt")
    if idle < IDLE_FLOOR:
        return ("not-idle-long-enough", f"idle {int(idle)}s < {IDLE_FLOOR}s floor")
    if not cards_readable:
        return ("gate-held", "the attention store could not be read; failing closed")
    if cards:
        return (
            "gate-held",
            f"{len(cards)} open attention card(s): {cards[0].get('item_id')}",
        )
    if children is None:
        return (
            "watcher-alive",
            "the process tree could not be read, so a background watcher cannot be ruled out",
        )
    if children:
        return (
            "watcher-alive",
            f"non-helper child(ren) still running: {' '.join(children)}",
        )
    return None


def task_gate(task, today):
    """`(token, detail)` when the task is not this verb's population, else None.

    ⚠️ **The gate is inverted on purpose.** `close-worker.py` refuses a non-terminal task
    and closes a finished one; this verb closes a *parked* one and refuses a finished one.
    `task_verdict` returns None for three different states — terminal, handed off, and
    deferred to the future — and every one of them is `close-worker.py`'s population, so
    the refusal names all three rather than picking one.
    """
    if task is None:
        return (
            "task-unresolved",
            "no task page carries this session id as claude_session_id; absent evidence "
            "is not evidence of a parked task",
        )
    if close_worker.task_verdict(task, today) is None:
        path, fm, handed_off = task
        status = (fm.get("status") or "").strip()
        defer = fm.get("defer_date")
        if handed_off:
            why = "has been handed off to a successor session"
        elif status in close_worker.TERMINAL:
            why = f"is already terminal (status {status})"
        else:
            why = f"is deferred to {defer}"
        return ("task-terminal", f"{os.path.basename(path)} {why}; use close-worker.py")
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Close an idle tab worker's pane, keeping its task and transcript."
    )
    parser.add_argument("session_ids", nargs="+", help="session id(s) to close")
    parser.add_argument(
        "--dry-run", action="store_true", help="check every guard, close nothing"
    )
    parser.add_argument(
        "--sessions-dir", default=None, help="override the session registry directory"
    )
    args = parser.parse_args(argv)

    today = close_worker.defer_date.as_now()
    exit_code = 0

    for session_id in args.session_ids:
        entries = restart_worker.registry_entries(args.sessions_dir)
        if entries is None:
            exit_code = refuse(
                "unknown-session-id",
                f"the registry at {args.sessions_dir} could not be read, so {session_id} "
                "cannot be resolved",
            )
            continue
        path, rec = restart_worker.find_target(entries, session_id)
        if path == "ambiguous":
            exit_code = refuse(
                "ambiguous-pid",
                f"{len(rec)} registry entries claim session {session_id} "
                f"({', '.join(os.path.basename(p) for p, _ in rec)}); the pid must be "
                "unique or nothing is closed",
            )
            continue
        if rec is None:
            exit_code = refuse(
                "unknown-session-id", f"no registry entry carries session id {session_id}"
            )
            continue

        held = restart_worker.held_reason(session_id)
        cards, cards_readable = reap.open_cards(session_id)
        pid = rec.get("pid")
        children = reap.watchers(pid) if isinstance(pid, int) else None
        verdict = tab_gate(
            rec,
            reap.idle_seconds(rec),
            cards,
            cards_readable,
            children,
            held=held,
        )
        if verdict is not None:
            exit_code = refuse(*verdict)
            continue

        cwd = (rec.get("cwd") or "").strip()
        dirty, tracked, _untracked = precheck.worktree_probe(cwd)
        if dirty == "dirty" and precheck.other_occupants(entries, cwd, session_id) == 0:
            exit_code = refuse(
                "dirty-worktree",
                f"session {session_id} is the only live session in {cwd} and that "
                f"worktree has {len(tracked)} tracked change(s); a close strands them "
                "with nothing coming back to finish the work",
            )
            continue

        branch, closer = precheck.classify(rec, wnm)
        if branch == "gate-held":
            exit_code = refuse(
                "gate-held",
                f"session {session_id} holds an unanswered operator gate ({closer}); "
                "closing it discards a question the operator never answered",
            )
            continue

        bad = task_gate(close_worker.find_task(session_id, cwd), today)
        if bad is not None:
            exit_code = refuse(*bad)
            continue

        pane, pmap = close_worker.pane_for_session(jump, session_id)
        if pane is None:
            print(
                f"❌ error: session {session_id} resolved and every guard passed, but no "
                f"single live pane maps to it "
                f"({'wezterm cli list unavailable' if pmap is None else 'no tty join'}) — "
                "an operational failure, not a target refusal"
            )
            exit_code = 1
            continue

        if args.dry_run:
            print(f"✅ would close pane {pane} (session {session_id})")
            continue

        code = close_worker.tab_kill(jump, session_id, pane)
        if code != 0:
            exit_code = 1
            continue

        transcript = reap.resolve_transcript(cwd, session_id)
        if transcript is None:
            print(
                f"❌ error: pane {pane} closed but no transcript was found for "
                f"{session_id} — the resume consumer needs it"
            )
            exit_code = 1
            continue
        print(
            f"✅ closed pane {pane} (session {session_id}); task stays in_progress, "
            f"claude_session_id kept, transcript {os.path.basename(transcript)} intact"
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
