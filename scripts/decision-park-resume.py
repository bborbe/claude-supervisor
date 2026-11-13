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

Where the four readings live
----------------------------
The four readings are printed, and — with `--persist-beside` — written to
`~/.claude/state/fleet-handback/<round>.decision`, beside the round's `.digest`. Until 2026-11-13
they existed only in the keep-alive leg's ≤ 1 KB hand-back, so a round's decision numbers died
with the message and nothing could audit them afterwards (measured: `grep -rl decision-candidates
~/.claude/state/fleet-handback/` → 0 files). The file carries the SAME string the script prints —
one renderer, `summary_line()` — so the artifact and the hand-back cannot disagree. They are still
**not** `fleet-legs.py` digest keys: that would change the digest contract, a separate decision.

Usage
-----
    decision-park-resume.py --vault <path> --tasks-dir <path> --persist-beside <digest>
        [--now <epoch>] [--json]
    decision-park-resume.py --vault <path> --record <task> --sid <session-id>

`--persist-beside` is **required** on the decide path: the four readings must reach disk, not only
the hand-back, so a round that forgot the flag is refused rather than silently reverting the defect.
`--record` is exempt — it writes the resume store, not a round's readings.

Exit codes: 0 = a verdict was produced (including "nothing to do") or a resume was recorded;
1 = a read failed and the round must report it; 2 = usage error; 3 = the readings were produced
but `--persist-beside` could not write them — the round reports that failure and never renders a
clean round over it.
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
#: Only `completed` MET a dependency. `hold` is an ACTIVE block of unknown length
#: (`docs/worker-session-states.md`) and `aborted` is an abandoned one — neither is a finished
#: dependency, and the fleet's own `blocked_by_verdict` (`manager-predispatch.py:524`) counts only
#: `completed`. Treating `hold` as cleared resumes a worker whose blocker is still parked, and
#: `/supervisor:open`'s resume path does not re-check `blocked_by`.
DEPENDENCY_MET = {"completed"}

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


def blocked_by(fm: str) -> list[str]:
    """The `blocked_by` members, read through the canonical scalar reader.

    ⚠️ `[]` and an empty value are BOTH "no dependencies" — not a list containing one
    unresolvable name, which is how a hand-rolled parser turns every row with an empty set into
    a permanent `unread`. Members arrive either as a YAML block (`- [[Name]]`) or inline.
    """
    raw = keepalive().scalar(fm, "blocked_by").strip()
    if raw in ("", "[]"):
        return []
    names = []
    for chunk in raw.replace("[", "\n").replace("]", "\n").split("\n"):
        name = chunk.strip().lstrip("-").strip().strip("'\"")
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


def approval_cleared(approved: str, turn: float | None) -> bool | None:
    """Kind (a). `approved_at` alone is NOT the read — every candidate carries it.

    The event is ordering: the approval must be later than the session's last turn, which is what
    "the approval landed and nothing resumed it" means. `None` = could not compare.
    """
    if not approved:
        return False
    try:
        stamp = parse_iso(approved)
    except Exception:
        return None
    if turn is None:
        return None
    return stamp > turn


def blocked_by_cleared(names: list[str], tasks_dir: str, turn: float | None) -> bool | None:
    """Kind (d). ⚠️ An EMPTY set is not a cleared wait — "every name is terminal" is vacuously
    true of it, and that would resume every orphaned row on every round.

    ⚠️ **Completion alone is not the event either — ordering is, exactly as for kind (a).** A set
    whose every member is `completed` stays completed forever, so a read of status alone fires on
    every round the worker is closed: resume → idle → reaped → resume again, with nothing ever
    consuming the wait. The event is "the last dependency finished after the worker last spoke":
    the latest `completed_date` must be later than the session's last turn. Once the resumed
    worker writes a turn, that ordering is false and the wait is consumed. `None` = could not
    compare (a missing or unparseable `completed_date`, or no readable transcript).

    Measured 2026-11-11 on the private-personal vault: 5158 of 7275 completed tasks carry
    `completed_date` (2133 of 2388 touched in the last 14 days). An undated dependency is `unread`
    — reported, never resumed — rather than guessed from a file mtime that a checkout resets.
    """
    if not names:
        return False
    k = keepalive()
    latest = None
    for name in names:
        try:
            with open(os.path.join(tasks_dir, name + ".md"), encoding="utf-8") as handle:
                fm = k.frontmatter(handle.read())
        except OSError:
            return None
        if k.scalar(fm, "status") not in DEPENDENCY_MET:
            return False
        done = k.scalar(fm, "completed_date")
        try:
            stamp = parse_iso(done)
        except Exception:
            return None
        latest = stamp if latest is None else max(latest, stamp)
    if turn is None:
        return None
    return latest > turn


def spent(vault: str, task: str, now: float) -> int:
    k = keepalive()
    return len(k.recent(k.load_resumes(store_path(vault, task)), now))


def record(vault: str, task: str, sid: str, now: float) -> None:
    """Append one resume to the counter `spent` reads. One store, one branch, both sides here."""
    k = keepalive()
    path = store_path(vault, task)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    k.record(path, k.load_resumes(path), now, sid)


# --------------------------------------------------------------------------- the readings

#: The sibling artifact this script writes beside the round's `.digest`. Not a digest key: the
#: digest's `LEGS` section is `fleet-legs.py`'s and adding a fifth key there is a separate change.
DECISION_SUFFIX = ".decision"


def summary_line(result: dict) -> str:
    """The four readings as ONE line — printed AND persisted, so the two cannot disagree.

    ⚠️ **One renderer, never two.** The hand-back's values and the `.decision` file's must be
    byte-identical, and a second f-string is exactly the drift this function removes: the file
    would be written from one expression and the message from another, and the first divergence
    would be invisible (both look like readings). Per wait kind, never one total — SC5 grades each
    kind on its own, and a single number cannot say which wait cleared.

    ⚠️ **A failed read keeps its `decision-unread=1` sentinel.** `decide()` returns
    `unread: []` on that path, so a naive `len(result["unread"])` renders **0** — the reading a
    clean empty round produces. That would make "the tasks dir could not be read" indistinguishable
    from "nothing was parked", which is the one distinction this leg exists to preserve; the
    pre-refactor line hardcoded `1` for exactly this reason and the refactor must not lose it.
    """
    if not result["ok"]:
        return (
            "decision-candidates=0 · decision-resumed-a=0 · decision-resumed-d=0 · "
            "decision-unread=1"
        )
    resumed = [r for r in result["rows"] if r["verdict"] == "resume"]
    by_kind = {k: sum(1 for r in resumed if r["kind"] == k) for k in ("a", "d")}
    return (
        f"decision-candidates={result['candidates']} · "
        f"decision-resumed-a={by_kind['a']} · decision-resumed-d={by_kind['d']} · "
        f"decision-unread={len(result['unread'])}"
    )


def status_line(result: dict) -> str:
    """`ok` / `unread` — the field that says whether the counts beside it describe a read at all.

    ⚠️ **Without it an all-zero line is ambiguous**, and ambiguity is the whole failure mode: a
    round that could not read its tasks dir writes zeros, and so does a healthy round with nothing
    parked. The status is what a later auditor reads instead of inferring from the numbers.
    """
    return "ok" if result["ok"] else "unread"


def decision_path(digest: str) -> str:
    """`<dir>/<round>.digest` → `<dir>/<round>.decision` — the round's own filename, reused.

    ⚠️ **The digest's stem, never a re-derived round timestamp.** The sanitization rule has one
    home (`agents/fleet-sweep-reader.md` § `<output_format>`); a second derivation is how the two
    filenames drift apart. A path that is not a round digest is refused rather than turned into a
    sibling of something else — a caller that passes the wrong path must hear it.
    """
    root, ext = os.path.splitext(digest)
    if ext != ".digest" or not os.path.basename(root):
        raise ValueError(f"not a round digest path: {digest!r}")
    return root + DECISION_SUFFIX


def persist(digest: str, result: dict, now: float) -> str:
    """Write the round's four readings beside its digest, atomically. Returns the path.

    ⚠️ **The atomic write is not decoration** — a `.decision` half-written by a crash is read as a
    reading by the next round's auditor, and a truncated line is indistinguishable from a smaller
    fleet. Same rule as the `.digest` / `.keepalive` payloads: `<path>.tmp` then `os.replace`.
    """
    from datetime import datetime, timezone

    path = decision_path(digest)
    round_tag = os.path.basename(path)[: -len(DECISION_SUFFIX)]
    written = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    body = "\n".join(
        [
            f"round={round_tag}",
            f"written_at={written}",
            f"status={status_line(result)}",
            summary_line(result),
        ]
    ) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        # A failed write must not leave a stray `.tmp` behind for the prune to count as a round.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


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
        fm = k.frontmatter(text)
        sid = k.session_id(fm)
        if not sid or k.scalar(fm, "status") not in LIVE_STATUS:
            continue
        verdict = k.session_verdict(sid)
        if verdict == "LIVE":
            continue
        if verdict in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            unread.append({"task": task, "reason": f"session {sid} → {verdict}"})
            continue

        candidates += 1
        turn = last_turn(sid)
        approved = approval_cleared(k.scalar(fm, "approved_at"), turn)
        blocked = blocked_by_cleared(blocked_by(fm), tasks_dir, turn)

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
    parser.add_argument(
        "--persist-beside",
        dest="persist_beside",
        metavar="DIGEST",
        help="this round's .digest path — the four readings are written to its .decision sibling",
    )
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

    # ⚠️ **Required, not optional.** The step file tells the leg that passing it is not optional,
    # and a prose-only rule is exactly what this script exists to replace: a round that forgot the
    # flag would print the four readings into the hand-back and persist nothing — the original
    # defect, restored silently. `--record` is exempt because it writes the resume store, not a
    # round's readings.
    if args.persist_beside is None:
        print(
            "usage: --persist-beside <this round's .digest> is required unless --record is given "
            "— the four readings must reach disk, not only the hand-back",
            file=sys.stderr,
        )
        return 2

    # Refuse a path that is not a round digest BEFORE the read: the derivation is this script's,
    # and a caller that passed the wrong path must hear it rather than get a sibling of something
    # else (a `.decision` named after a directory or an unrelated file is unreadable as a round).
    try:
        decision_path(args.persist_beside)
    except ValueError as error:
        print(f"usage: {error}", file=sys.stderr)
        return 2

    result = decide(args.vault, args.tasks_dir, now)

    # ⚠️ Persist BEFORE the output branch, and report a failure on stderr while still printing the
    # readings. A round that dropped them from the message because the file failed would reproduce
    # the original defect one layer down: the numbers would again live only in a message.
    persisted = None
    try:
        persisted = persist(args.persist_beside, result, now)
    except OSError as error:
        print(
            f"decision-persist: FAILED — {decision_path(args.persist_beside)}: {error}",
            file=sys.stderr,
        )

    if not result["ok"]:
        print(f"unread — {result['reason']}")
        print(summary_line(result))
        return 1
    if args.json:
        print(json.dumps(result))
        return 0 if persisted else 3

    for row in result["rows"]:
        if row["verdict"] == "crash-loop":
            print(f"⚠️ WORKER CRASH-LOOP: {row['task']} — {row['detail']}")
        else:
            print(f"resume kind={row['kind']} {row['task']} {row['sid']} — {row['detail']}")
    for row in result["unread"]:
        print(f"unread {row['task']} — {row['reason']}")
    print(summary_line(result))
    return 0 if persisted else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
