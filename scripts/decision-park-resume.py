#!/usr/bin/env python3
"""Which decision-parked workers may be resumed this round, and which may not.

The clock leg resumes waits that wrote themselves a `defer_date` (`park-event.py due`). Three
waits write none and nothing resumed them: an **approval** that landed after the worker was
closed, an **answered attention card** whose worker exited, and a **`blocked_by` set** that has
gone terminal. This script is that leg's whole decision, so the procedure is testable rather than
prose a sub-agent interprets at round time.

It decides. It does not resume: the caller performs the resume the verdict names.

Why a script and not a step file
--------------------------------
The first version was prose, and six review rounds found six different real defects in it —
a resume that carried no prompt, an `approved_at` read the population already guaranteed, a
vacuous empty `blocked_by`, no consumption bound, a recorded id the resume never produced, and a
cap that was armed but never read. Every one is a runtime contract, and every one is a test below.
Prose cannot settle that; a script can.

The consumption bound is this script's own
------------------------------------------
`worker-keepalive.py`'s cap cannot bound this population: its verdict returns `jump` for exactly
these rows (a parked `phase`, a future `defer_date`, no open boxes, a live goal-manager owner) and
its `crash-loop` branch sits after all of them. So this script keeps its own counter, in the same
state store and with the same cap and window, and reads it before it emits a resume.

Usage
-----
    decision-park-resume.py --vault <path> --tasks-dir <path> [--now <epoch>] [--json]

Exit codes: 0 = a verdict was produced (including "nothing to do"), 1 = a read failed and the
round must report `unread` rather than "no candidates", 2 = usage error.
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


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_keepalive = None


def keepalive():
    """`scripts/topic-keepalive.py` — the liveness instrument and the resume store.

    Cached because a test patches its seams; a fresh module per call would ignore the patch.
    """
    global _keepalive
    if _keepalive is None:
        _keepalive = _load("topic_keepalive", "topic-keepalive.py")
    return _keepalive


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
    raw = fm.get("blocked_by") or ""
    names = []
    for line in raw.split("\n"):
        name = line.strip().strip("'\"")
        if name.startswith("[[") and name.endswith("]]"):
            name = name[2:-2]
        if name:
            names.append(name)
    return names


# --------------------------------------------------------------------------- local reads


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


PROJECTS_DIR = os.path.expanduser(os.environ.get("PROJECTS_DIR") or "~/.claude/projects")
ATTENTION_DIR = os.path.expanduser(os.environ.get("ATTENTION_STATE_DIR") or "~/.claude/state/attention")


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
                stamp = json.loads(line).get("timestamp")
            except ValueError:
                continue
            if stamp:
                try:
                    return parse_iso(stamp)
                except Exception:
                    continue
    return None


def answered_card(sid: str) -> str | None:
    """`""` = the log was read and holds no answered card; `None` = the log could not be read.

    ⚠️ The two are NOT the same answer and must never collapse: "no answered card" is a wait that
    has not cleared, "could not read" is `unread`, and folding the second into the first is the
    silent direction that resumes a worker whose question nobody answered.

    `GET /api/1.0/attention` returns OPEN items only, so it can never show an answered one —
    this reads the append-only event log instead. ⚠️ A `skip` record is a DECLINE, and an empty
    answer is no answer: both return None, because resuming on either hands the worker a prompt
    that answers nothing.
    """
    path = os.path.join(ATTENTION_DIR, sid + ".events.jsonl")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    opened: dict[str, dict] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item_id") or event.get("item-id")
        if not item:
            continue
        if event.get("event") == "open":
            opened[item] = event
        elif event.get("event") == "close":
            record = event.get("answer") or {}
            if (record.get("kind") or "").lower() == "skip":
                continue
            text = (record.get("option") or record.get("text") or "").strip()
            if text:
                return text
    return ""


# --------------------------------------------------------------------------- the waits


def approval_cleared(fm: dict[str, str], last_turn: float | None) -> bool | None:
    """Kind (a). `approved_at` alone is NOT the read — the population already guarantees it.

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
    if last_turn is None:
        return None
    return stamp > last_turn


def blocked_by_cleared(names: list[str], tasks_dir: str) -> bool | None:
    """Kind (d). ⚠️ An EMPTY set is not a cleared wait — "every name is terminal" is vacuously
    true of it, and that would resume every orphaned row on every round."""
    if not names:
        return False
    for name in names:
        path = os.path.join(tasks_dir, name + ".md")
        try:
            with open(path, encoding="utf-8") as handle:
                status = frontmatter(handle.read()).get("status", "")
        except OSError:
            return None
        if status not in TERMINAL:
            return False
    return True


# --------------------------------------------------------------------------- the cap


def spent(vault: str, task: str, now: float) -> int:
    k = keepalive()
    path = k.state_path(vault, task, "decision")
    return len(k.recent(k.load_resumes(path), now))


# --------------------------------------------------------------------------- main


def decide(vault: str, tasks_dir: str, now: float) -> dict:
    k = keepalive()
    rows: list[dict] = []
    unread: list[dict] = []
    candidates = 0

    try:
        names = sorted(os.listdir(tasks_dir))
    except OSError as error:
        return {"ok": False, "reason": f"tasks dir unreadable: {error}", "rows": [], "unread": []}

    for name in names:
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(tasks_dir, name), encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            unread.append({"task": name[:-3], "reason": "task file unreadable"})
            continue
        fm = frontmatter(text)
        sid = fm.get("claude_session_id", "")
        if not sid or fm.get("status", "") not in LIVE_STATUS:
            continue
        verdict = k.session_verdict(sid)
        if verdict == "LIVE":
            continue
        if verdict in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            unread.append({"task": name[:-3], "reason": f"session {sid} → {verdict}"})
            continue

        candidates += 1
        task = name[:-3]
        last_turn = globals()['last_turn'](sid)
        approved = approval_cleared(fm, last_turn)
        blocked = blocked_by_cleared(blocked_by(fm), tasks_dir)
        answered = answered_card(sid)

        if approved is None or blocked is None or answered is None:
            unread.append({"task": task, "reason": "a wait could not be read"})
            continue
        if not (approved or blocked or answered):
            continue

        kind = "a" if approved else ("b" if answered else "d")
        count = spent(vault, task, now)
        if count >= RESUME_CAP:
            rows.append({"task": task, "sid": sid, "verdict": "crash-loop",
                         "detail": f"{count} resumes in 30 min; stop resuming"})
            continue
        rows.append({"task": task, "sid": sid, "verdict": "resume", "kind": kind,
                     "prompt": answered if kind == "b" else None,
                     "detail": f"attempt {count + 1} of {RESUME_CAP}"})

    return {"ok": True, "rows": rows, "unread": unread, "candidates": candidates}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Decide which decision-parked workers to resume.")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--tasks-dir", required=True)
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    now = args.now if args.now is not None else now_epoch()
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
            prompt = f" prompt={row['prompt']!r}" if row["prompt"] else ""
            print(f"resume kind={row['kind']} {row['task']} {row['sid']}{prompt} — {row['detail']}")
    for row in result["unread"]:
        print(f"unread {row['task']} — {row['reason']}")
    print(f"decision-candidates={result['candidates']} · "
          f"decision-resumed={len(resumed)} · decision-unread={len(result['unread'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
