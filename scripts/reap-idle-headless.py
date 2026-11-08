#!/usr/bin/env python3
"""`reap-idle-headless.py` — close the PROCESS of an idle headless worker, resumable later.

The fleet's chosen end-of-life for a parked headless worker: a worker idle for at least
`IDLE_FLOOR` is closed — **process only, transcript and task anchor untouched** — and the
manager later resumes the same session id with `--resume <sid>` when the recorded wait
clears. The reap-and-resume loop is the fix; this script is only its close half. It does
not decide *when* to run (the trigger) and it does not resume anything.

Why a separate verb rather than `close-worker.py --force`: `close-worker.py` documents that
SIGTERM is not a substitute for its task gate, and a force flag there would relax that
refusal for every caller. This verb has a different contract — the task need not be
finished, only parked — so it names its own gate instead of weakening that one. A
terminal-task worker is **refused** here and stays with `close-worker.py`.

⚠️ **It closes the process, never the session.** The transcript
(`~/.claude/projects/<escaped-cwd>/<sid>.jsonl`) and the task's `claude_session_id` must
survive: the resume consumer needs both. The success path asserts the transcript still
exists before reporting, so a change that turned this into a session delete fails loudly.

Nine refusals, each exit 1 with a stable token as the first line of output:

  unknown-session-id    no registry entry carries that session id.
  ambiguous-pid         more than one entry claims the id, or none carries an integer pid.
  already-gone          the pid is not alive; nothing to reap (a stale entry is removed).
  busy-target           registry `status` is not `idle` — the worker is mid-turn.
  not-idle-long-enough  idle less than `IDLE_FLOOR` (15 min) per `statusUpdatedAt`.
  wrapper-busy          the wrapper record is unreadable, or reads a non-idle state, a
                        `pending_turns` that is not 0, or a parked `question`.
  gate-held             the session has an open attention card; closing it orphans a
                        question a person is about to answer.
  watcher-alive         a surviving child of the worker is not one of its MCP/stdio
                        helpers — a backgrounded command would die with the process.
  task-terminal         the task is terminal (`completed`/`aborted`/`hold`) or deferred to
                        the future: that is `close-worker.py`'s population, not this one.

⚠️ **The watcher read has a stated hole.** It inspects children of the claude pid; a
watcher detached with `setsid`/`nohup` leaves that tree and escapes it. The read guards the
common case, it is not a proof.

Exit codes: 0 reaped, 1 refusal or operational error, 2 usage error.
"""
import argparse
import glob
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

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

SESSIONS_DIR = close_worker.SESSIONS_DIR

#: How long a worker must have been idle before its process may be closed. The operator's
#: ruling, 2026-11-07: idle >= 15 minutes -> close the process, keep the transcript, resume
#: on the recorded event. A scheduling policy, not an inference about abandonment.
IDLE_FLOOR = 15 * 60

#: How long to wait for the pid and its registry entry to disappear after SIGTERM.
EXIT_DEADLINE = 8.0

WRAPPER_URL = os.environ.get("SUPERVISOR_WRAPPER_URL") or "http://localhost:18090"
ATTENTION_URL = os.environ.get("SUPERVISOR_ATTENTION_URL") or "http://localhost:18080"

#: Where Claude Code writes transcripts, `<projects>/<escaped-cwd>/<sid>.jsonl`.
PROJECTS_DIR = os.environ.get("SUPERVISOR_PROJECTS_DIR") or os.path.expanduser(
    "~/.claude/projects"
)

#: Argument substrings that identify a child as one of the session's own MCP / stdio
#: helpers rather than a command the worker backgrounded. Measured 2026-11-07: every idle
#: worker has children and every child seen was one of these — so "has a child" is not a
#: watcher signal, and this filter is what makes the read mean something.
HELPER_MARKERS = (
    "--mcp-stdio",
    "mcp-",
    "playwright",
    "bun run --cwd",
    "uvx",
    "npm exec",
)


def refuse(token, detail):
    """Print the token on its own first line, then the human report.

    The caller branches on that first line, so a refusal that printed its report first
    would read as success — the same contract `close-worker.py` keeps.
    """
    print(token)
    print(f"❌ {token}: {detail}")
    return 1


def registry_entries(session_id, sessions_dir=None):
    """Every registry record claiming `session_id`."""
    out = []
    for path in sorted(glob.glob(os.path.join(sessions_dir or SESSIONS_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                rec = json.load(handle)
        except (OSError, ValueError):
            continue
        if rec.get("sessionId") == session_id:
            out.append((path, rec))
    return out


def idle_seconds(rec, now=None):
    """Seconds since the registry last moved this session to `idle`, or None.

    `statusUpdatedAt` is the moment the status became `idle`; `updatedAt` is the fallback
    for a record written before that field existed. Both are epoch milliseconds.
    """
    stamp = rec.get("statusUpdatedAt") or rec.get("updatedAt")
    if not isinstance(stamp, (int, float)):
        return None
    return max(0.0, (now if now is not None else time.time()) - stamp / 1000.0)


def is_helper(args):
    """True when a child's command line is one of the session's MCP / stdio helpers."""
    return any(marker in args for marker in HELPER_MARKERS)


def watchers(pid):
    """Child pids of `pid` that are not MCP/stdio helpers — possible background work."""
    try:
        listed = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        # Cannot read the tree: fail closed, the caller refuses.
        return ["unreadable"]
    out = []
    for child in listed:
        try:
            args = subprocess.run(
                ["ps", "-o", "args=", "-p", child],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ["unreadable"]
        if args and not is_helper(args):
            out.append(child)
    return out


def fetch_json(url, timeout=3):
    """The parsed JSON at `url`, or None when it cannot be read."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None


def wrapper_record(session_id, wrapper_url=None):
    return fetch_json(f"{wrapper_url or WRAPPER_URL}/api/1.0/sessions/{session_id}")


def open_cards(session_id, attention_url=None):
    """`(cards, readable)` — the session's open attention cards, and whether the store answered."""
    data = fetch_json(f"{attention_url or ATTENTION_URL}/api/1.0/attention")
    if data is None:
        return None, False
    items = data if isinstance(data, list) else (data.get("items") or data.get("data") or [])
    if not isinstance(items, list):
        return None, False
    cards = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("producer_id") == session_id
        and item.get("state") == "open"
    ]
    return cards, True


def transcript_path(cwd, session_id, projects_dir=None):
    """`~/.claude/projects/<escaped-cwd>/<sid>.jsonl` — the artifact a resume reads."""
    escaped = (cwd or "").replace("/", "-")
    return os.path.join(projects_dir or PROJECTS_DIR, escaped, f"{session_id}.jsonl")


def guard(rec, now=None, wrapper=None, cards=None, cards_readable=True, children=None):
    """`(token, detail)` for the first failing guard, or None when the close may proceed.

    Pure apart from the injected readings, so the whole predicate is testable without a
    live worker. Order matters: identity, liveness, cheap state reads, then the watcher.
    """
    pid = rec.get("pid")
    if not isinstance(pid, int):
        return ("ambiguous-pid", "the registry entry carries no integer pid")
    if not precheck.pid_is_alive(pid):
        return ("already-gone", f"pid {pid} is not alive; nothing to reap")
    status = (rec.get("status") or "").strip()
    if status != "idle":
        return ("busy-target", f"registry status is {status or '(none)'!r}, not 'idle'")
    idle = idle_seconds(rec, now)
    if idle is None:
        return ("not-idle-long-enough", "the entry carries no statusUpdatedAt/updatedAt")
    if idle < IDLE_FLOOR:
        return ("not-idle-long-enough", f"idle {int(idle)}s < {IDLE_FLOOR}s floor")
    if wrapper is None:
        return ("wrapper-busy", "the wrapper record could not be read")
    if wrapper.get("state") != "idle":
        return ("wrapper-busy", f"wrapper state {wrapper.get('state')!r}, not 'idle'")
    if wrapper.get("pending_turns") != 0:
        return (
            "wrapper-busy",
            f"wrapper pending_turns {wrapper.get('pending_turns')!r}, expected 0",
        )
    if wrapper.get("question"):
        return ("wrapper-busy", "a question is parked on the wrapper record")
    if not cards_readable:
        return ("gate-held", "the attention store could not be read; failing closed")
    if cards:
        return (
            "gate-held",
            f"{len(cards)} open attention card(s): {cards[0].get('item_id')}",
        )
    if children is None:
        children = watchers(pid)
    if children:
        return (
            "watcher-alive",
            f"non-helper child(ren) still running: {' '.join(children)}",
        )
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Close the process of an idle headless worker, keeping its transcript."
    )
    parser.add_argument("session_ids", nargs="+", help="session id(s) to reap")
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
        entries = registry_entries(session_id, args.sessions_dir)
        if not entries:
            exit_code = refuse(
                "unknown-session-id", f"no registry entry carries {session_id}"
            )
            continue
        if len(entries) > 1:
            exit_code = refuse(
                "ambiguous-pid",
                f"{len(entries)} registry entries claim {session_id}: "
                f"{', '.join(os.path.basename(p) for p, _ in entries)}",
            )
            continue

        path, rec = entries[0]
        pid = rec.get("pid")
        wrapper = wrapper_record(session_id)
        cards, cards_readable = open_cards(session_id)
        verdict = guard(rec, wrapper=wrapper, cards=cards, cards_readable=cards_readable)

        if verdict is None:
            # The task gate is inverted here on purpose: `task-not-terminal` is this verb's
            # population, and a resolved *terminal* task belongs to close-worker.py.
            task = close_worker.find_task(session_id, rec.get("cwd"))
            bad = close_worker.task_verdict(task, today)
            if bad is None and task is not None:
                verdict = (
                    "task-terminal",
                    f"{os.path.basename(task[0])} is already terminal; use close-worker.py",
                )
            elif bad is not None and bad[0] != "task-not-terminal":
                # `task-unresolved` is not a finished task, but this verb needs a resolvable
                # anchor to know what the worker is waiting on. Fail closed and say why.
                verdict = ("task-unresolved", bad[1])

        if verdict is not None:
            exit_code = refuse(verdict[0], verdict[1])
            continue

        transcript = transcript_path(rec.get("cwd"), session_id)
        print(
            f"🧹 reap-idle-headless · session {session_id[:8]} · pid {pid} · "
            f"{rec.get('name')!r}"
        )
        print(f"   idle {int(idle_seconds(rec) or 0)}s · transcript {transcript}")
        if args.dry_run:
            print(f"   ✅ would close pid {pid} (transcript kept)")
            continue

        # Re-read the entry immediately before the signal: the guards take seconds, and a
        # session that exited in that window can have its pid recycled onto another process.
        try:
            with open(path, encoding="utf-8") as handle:
                fresh = json.load(handle)
        except (OSError, ValueError):
            fresh = {}
        if fresh.get("pid") != pid or fresh.get("sessionId") != session_id:
            exit_code = refuse(
                "ambiguous-pid",
                f"the registry entry changed during the guards (pid {pid} → "
                f"{fresh.get('pid')}); nothing was signalled",
            )
            continue

        if precheck.pid_is_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                print(f"❌ error: cannot signal pid {pid}: {error}")
                exit_code = 1
                continue

        deadline = time.time() + EXIT_DEADLINE
        while time.time() < deadline and precheck.pid_is_alive(pid):
            time.sleep(0.2)
        while time.time() < deadline and os.path.exists(path):
            time.sleep(0.2)

        alive = precheck.pid_is_alive(pid)
        entry_present = os.path.exists(path)
        if not alive and entry_present:
            try:
                os.unlink(path)
                entry_present = False
                print(f"   ℹ️ removed the stale registry entry for dead pid {pid}")
            except OSError as error:
                print(f"   ⚠️ registry entry {path} survived pid {pid}: {error}")

        # The whole point of this verb over close-worker: the resume consumer needs both.
        transcript_kept = os.path.exists(transcript)
        ok = not alive and not entry_present and transcript_kept
        print(
            f"   {'✅' if ok else '⚠️'} reaped pid {pid} · "
            f"process {'dead' if not alive else 'STILL ALIVE'} · registry entry "
            f"{'removed' if not entry_present else 'STILL PRESENT'} · transcript "
            f"{'kept' if transcript_kept else 'MISSING'}"
        )
        if not ok:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
