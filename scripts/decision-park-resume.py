#!/usr/bin/env python3
"""Which decision-parked workers may be resumed this round, and which may not.

The clock leg resumes waits that wrote themselves a `defer_date` (`park-event.py due`). Two waits
write none and nothing resumed them: an **approval** that landed after the worker was closed, and
a **`blocked_by` set** that has gone terminal. This script is that decision, so it is testable
rather than prose a sub-agent interprets at round time.

It decides. It does not resume: the caller performs the resume a verdict names, then records it
with `--record`.

⚠️ **Kind (b) — an answered attention card — is deliberately absent.** It ships:
[[An Answered Card Resumes the Session Waiting On It]] resumes a dead session on its answered card
today. A second implementation would be a second owner of one wait.

Why a script and not a step file
--------------------------------
The first version was prose, and six review rounds found six different real defects in it. Every
one was a runtime contract. The seventh round found the tests themselves were green against fakes
— so these tests drive the **real** parsers and the **real** store, never a stand-in for them.

The consumption bound is this script's own, and it is written here
--------------------------------------------------------------
`worker-keepalive.py`'s cap cannot bound this population: its verdict returns `jump` for exactly
these rows (a parked `phase`, a future `defer_date`, no open boxes, a live goal-manager owner) and
its `crash-loop` branch sits after all of them. So this script keeps its own counter — and
`--record` is what writes it, through the same `topic-keepalive.py` store helpers every other leg
uses. A counter nothing writes is not a bound; that was round seven.

Usage
-----
    decision-park-resume.py --vault <path> --tasks-dir <path> [--now <epoch>] [--json]
    decision-park-resume.py --vault <path> --record <task> --sid <session-id>

Exit codes: 0 = a verdict was produced (including "nothing to do") or a resume was recorded;
1 = a read failed and the round must report it; 2 = usage error.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

RESUME_CAP = 3
RESUME_WINDOW = 30 * 60

#: A row is a candidate while its task is still live. A terminal task is finished, not parked.
LIVE_STATUS = {"next", "todo", "in_progress", ""}
TERMINAL = {"completed", "aborted", "hold"}

PROJECTS_DIR = os.path.expanduser(os.environ.get("PROJECTS_DIR") or "~/.claude/projects")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_keepalive = None


def keepalive():
    """`scripts/topic-keepalive.py` — liveness and the resume store.

    Cached because a test patches its seams; a fresh module per call would ignore the patch.
    """
    global _keepalive
    if _keepalive is None:
        _keepalive = _load("topic_keepalive", "topic-keepalive.py")
    return _keepalive


def store_path(vault: str, task: str) -> str:
    """The one path this script both reads and writes. Same store, same branch, no second home."""
    return keepalive().state_path(vault, task, "decision-park")


# --------------------------------------------------------------------------- frontmatter


def frontmatter(text: str) -> dict[str, str]:
    """The leading `---` block as a flat map. Bodies quote these keys in prose, so a whole-file
    scan reads a task as approved on the strength of a sentence about approval."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith((" ", "\t", "-")) and key:
            if line.strip().startswith("- "):
                out[key] = (out[key] + "\n" + line.strip()[2:].strip().strip("'\"")).strip()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            out[key] = value.strip().strip("'\"")
    return out


def blocked_by(fm: dict[str, str]) -> list[str]:
    names = []
    for line in (fm.get("blocked_by") or "").split("\n"):
        name = line.strip().strip("'\"")
        if name.startswith("[[") and name.endswith("]]"):
            name = name[2:-2]
        if name:
            names.append(name)
    return names


# --------------------------------------------------------------------------- the reads


def now_epoch() -> float:
    import time
    return time.time()


def parse_iso(value: str) -> float:
    """ISO-8601 → epoch seconds. Raises on anything else, so the caller can answer `unread`."""
    from datetime import datetime, timezone
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def last_turn(sid: str) -> float | None:
    """The session's last transcript message — the timestamp `LAST-ACTIVE` renders.

    Never the file mtime: `who-needs-me.py:session_transcript_age` reads mtime and says the two
    answer different questions on purpose. `None` when no transcript can be read.
    """
    import glob
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", sid + ".jsonl")):
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 8192))
                tail = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return None
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                record_line = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record_line, dict):
                continue
            stamp = record_line.get("timestamp")
            if stamp:
                try:
                    return parse_iso(stamp)
                except Exception:
                    continue
    return None


def approval_cleared(fm: dict[str, str], turn: float | None) -> bool | None:
    """Kind (a). `approved_at` alone is NOT the read — every candidate carries it.

    The event is ordering: the approval must be later than the session's last turn, which is what
    "the approval landed and nothing resumed it" means. `None` = could not compare.
    """
    approved = fm.get("approved_at") or ""
    if not approved:
        return False
    try:
        stamp = parse_iso(approved)
    except Exception:
        return None
    if turn is None:
        return None
    return stamp > turn


def blocked_by_cleared(names: list[str], tasks_dir: str) -> bool | None:
    """Kind (d). ⚠️ An EMPTY set is not a cleared wait — "every name is terminal" is vacuously
    true of it, and that would resume every orphaned row on every round."""
    if not names:
        return False
    for name in names:
        try:
            with open(os.path.join(tasks_dir, name + ".md"), encoding="utf-8") as handle:
                status = frontmatter(handle.read()).get("status", "")
        except OSError:
            return None
        if status not in TERMINAL:
            return False
    return True


def spent(vault: str, task: str, now: float) -> int:
    k = keepalive()
    return len(k.recent(k.load_resumes(store_path(vault, task)), now))


def record(vault: str, task: str, sid: str, now: float) -> None:
    """Append one resume to the counter `spent` reads. One store, one branch, both sides here."""
    k = keepalive()
    path = store_path(vault, task)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    k.record(path, k.load_resumes(path), now, sid)


# --------------------------------------------------------------------------- decide


def decide(vault: str, tasks_dir: str, now: float) -> dict:
    k = keepalive()
    rows: list[dict] = []
    unread: list[dict] = []
    candidates = 0

    try:
        names = sorted(os.listdir(tasks_dir))
    except OSError as error:
        return {"ok": False, "reason": f"tasks dir unreadable: {error}", "rows": [],
                "unread": [], "candidates": 0}

    for name in names:
        if not name.endswith(".md"):
            continue
        task = name[:-3]
        try:
            with open(os.path.join(tasks_dir, name), encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            unread.append({"task": task, "reason": "task file unreadable"})
            continue
        fm = frontmatter(text)
        sid = fm.get("claude_session_id", "")
        if not sid or fm.get("status", "") not in LIVE_STATUS:
            continue
        verdict = k.session_verdict(sid)
        if verdict == "LIVE":
            continue
        if verdict in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            unread.append({"task": task, "reason": f"session {sid} → {verdict}"})
            continue

        candidates += 1
        approved = approval_cleared(fm, last_turn(sid))
        blocked = blocked_by_cleared(blocked_by(fm), tasks_dir)

        # ⚠️ Per-wait, never all-or-nothing: one unreadable wait must not strand a row whose
        # other wait DID clear — that turns a partial read into a silent no-op.
        if approved is True:
            kind = "a"
        elif blocked is True:
            kind = "d"
        elif approved is None or blocked is None:
            reason = "approved_at unreadable" if approved is None else "blocked_by unresolvable"
            unread.append({"task": task, "reason": reason})
            continue
        else:
            continue

        count = spent(vault, task, now)
        if count >= RESUME_CAP:
            rows.append({"task": task, "sid": sid, "verdict": "crash-loop",
                         "detail": f"{count} resumes in 30 min; stop resuming"})
            continue
        rows.append({"task": task, "sid": sid, "verdict": "resume", "kind": kind,
                     "detail": f"attempt {count + 1} of {RESUME_CAP}"})

    return {"ok": True, "rows": rows, "unread": unread, "candidates": candidates}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Decide which decision-parked workers to resume.")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--tasks-dir")
    parser.add_argument("--record", metavar="TASK")
    parser.add_argument("--sid")
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = args.now if args.now is not None else now_epoch()

    if args.record:
        if not args.sid:
            print("usage: --record requires --sid", file=sys.stderr)
            return 2
        record(args.vault, args.record, args.sid, now)
        print(f"recorded {args.record} {args.sid}")
        return 0

    if not args.tasks_dir:
        print("usage: --tasks-dir is required unless --record is given", file=sys.stderr)
        return 2

    result = decide(args.vault, args.tasks_dir, now)
    if not result["ok"]:
        print(f"unread — {result['reason']}")
        return 1
    if args.json:
        print(json.dumps(result))
        return 0

    resumed = [r for r in result["rows"] if r["verdict"] == "resume"]
    for row in result["rows"]:
        if row["verdict"] == "crash-loop":
            print(f"⚠️ WORKER CRASH-LOOP: {row['task']} — {row['detail']}")
        else:
            print(f"resume kind={row['kind']} {row['task']} {row['sid']} — {row['detail']}")
    for row in result["unread"]:
        print(f"unread {row['task']} — {row['reason']}")
    print(f"decision-candidates={result['candidates']} · "
          f"decision-resumed={len(resumed)} · decision-unread={len(result['unread'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
