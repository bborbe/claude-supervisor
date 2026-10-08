#!/usr/bin/env python3
"""Materialise the fleet-park fixture into an isolated store, then check the reader's answer.

**Why a harness and not a unit test.** The classification under test lives in
`agents/fleet-sweep-reader.md` — a markdown agent, not a script. There is nothing to
import, so the replay has to hand the **released** agent a real store to read with its
own tools and take back the classes it rendered. A harness that printed the expected
classes and called it a pass would be the stub `SC3`'s falsifier names; this one builds
the inputs and the check, and the agent supplies the middle.

⚠️ **The fixture is synthetic and must stay so.** `bborbe/claude-supervisor` is a PUBLIC
repo, and `scripts/tests/test_stuck.py`'s header already records what committing a real
session costs. See `fixtures/fleet-park-signals.json`'s own `_note`.

Usage
-----
    python3 scripts/tests/fleet-park-replay.py --materialise [--root DIR]
        Build a fresh isolated tree and print the fleet-sweep-reader inputs, the
        expected class per row, and the `export` lines that point a shell at it.

    python3 scripts/tests/fleet-park-replay.py --check <digest-file> [--root DIR]
        Read a returned digest and print, per row, expected vs rendered. Exit 1 when
        any row disagrees, or when a row is missing from the digest entirely — a
        silently dropped row is the failure this whole change exists to remove.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "fleet-park-signals.json")

# The isolated roots, named for the env vars the subject scripts read. Kept here rather
# than in each test module for the reason `fleet_sessions_isolation.py` gives: several
# import-time assignments of one key leave only the last in force.
ROOTS = ("SESSIONS_DIR", "PROJECTS_DIR", "ATTENTION_STATE_DIR")

# The pid every synthetic registry record names. launchd on macOS — always occupied, and
# stable, which is what the reader needs to see the rows as LIVE (see `materialise`).
LIVE_PID = 1


def live_proc_start(pid):
    """`pid`'s real start time as the registry writes `procStart` — ctime, UTC.

    ⚠️ **A planted registry record without this field is UNKNOWN by design, not live.**
    `session-liveness.py` proves the pid belongs to the recorded session by comparing
    `procStart` against the live holder's `ps -o lstart=`, so a fixture that omits it
    exercises the missing-field path — the rows would be dropped as not-live and the
    replay would report a clean digest for a fleet it never classified. Same helper,
    and same reason, as `test_manager_predispatch.py`'s.
    """
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    if not out:
        return None
    return (
        datetime.strptime(out, "%a %b %d %H:%M:%S %Y")
        .astimezone(timezone.utc)
        .strftime("%a %b %d %H:%M:%S %Y")
    )


def load_fixture(path=FIXTURE):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def materialise(root):
    """Build the isolated tree under `root` and return {row key: session id}."""
    for sub in ("attention", "sessions", "projects", "vault/25 Tasks"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)

    rows = load_fixture()["rows"]
    now = time.time()
    for i, row in enumerate(rows, start=1):
        sid = row["session_id"]

        # The attention store: one append-only log holding the session's Stop record,
        # which is where the `⏰ Ends:` slot lives. Written even when the detail carries
        # no slot — "no declaration" is a shape the reader must also get right.
        log = os.path.join(root, "attention", f"{sid}.events.jsonl")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "open", "event": "Stop", "item_id": "stop",
                "session_id": sid, "detail": row["stop_detail"], "ts": now - 60,
            }) + "\n")
            if row.get("gate"):
                fh.write(json.dumps(dict(
                    {"type": "open", "item_id": "gate", "session_id": sid,
                     "ts": now - 120}, **row["gate"])) + "\n")

        # The older snapshot form, which `who-needs-me.py` still folds in.
        needs = os.path.join(root, "attention", f"{sid}.needs.json")
        with open(needs, "w", encoding="utf-8") as fh:
            json.dump({"session_id": sid, "state": "open",
                       "kind": "idle", "ts": now - 60}, fh)

        # The registry, pid-keyed. ⚠️ **The pid is NOT this process's.** The reader runs
        # in a later process, after this harness has exited, so a record naming this pid
        # would be occupied for a moment and then read ABSENT for the whole replay — the
        # rows would be dropped as dead and the digest would come back clean for a fleet
        # it never classified. `LIVE_PID` is launchd: always occupied, and its `procStart`
        # is stable, so the records read LIVE for as long as the materialised tree lasts.
        with open(os.path.join(root, "sessions", f"{i}.json"), "w", encoding="utf-8") as fh:
            json.dump({"sessionId": sid, "pid": LIVE_PID,
                       "procStart": live_proc_start(LIVE_PID),
                       "status": row["status"], "name": row["name"]}, fh)

        # The transcript, aged to the fixture's own reading. `who-needs-me.py` globs
        # `PROJECTS_DIR/*/<sid>.jsonl`, so the intermediate directory is arbitrary.
        tdir = os.path.join(root, "projects", "synthetic")
        os.makedirs(tdir, exist_ok=True)
        tpath = os.path.join(tdir, f"{sid}.jsonl")
        with open(tpath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "synthetic"}}) + "\n")
        stamp = now - float(row["transcript_age_seconds"])
        os.utime(tpath, (stamp, stamp))

        # The anchored task, so the reader's step 4 resolves a file and an open-box count.
        boxes = "".join("- [ ] step\n" for _ in range(row["task_open_boxes"]))
        with open(os.path.join(root, "vault", "25 Tasks", row["task_file"]), "w",
                  encoding="utf-8") as fh:
            fh.write(f"---\nclaude_session_id: {sid}\nstatus: in_progress\n"
                     f"phase: {row.get('phase', 'execution')}\n---\n\n# Tasks\n\n{boxes}")

        # The task file's mtime is read from the fixture, not hardcoded: the `stalled`
        # rule keys on "mtime unchanged for >=2 sweeps", so a fixture that pinned every
        # row to one value could not express a row whose task HAD just moved. Every
        # shipped row is hours old, which is the unchanged case the rule wants.
        stamp = datetime.strptime(row["task_mtime"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
        os.utime(os.path.join(root, "vault", "25 Tasks", row["task_file"]),
                 (stamp, stamp))

    return {row["key"]: row["session_id"] for row in rows}


def reader_inputs(root):
    """The block a caller hands the released fleet-sweep-reader, verbatim."""
    rows = load_fixture()["rows"]
    lines = [
        "FLEET-SWEEP-READER INPUTS (synthetic replay — no live session is involved)",
        "",
        "P (scripts dir):  %s" % os.path.dirname(HERE),
        "VAULT:            %s" % os.path.join(root, "vault"),
        "TASKS DIR:        25 Tasks",
        "SID (caller):     %s" % "00000000-0000-4000-8000-000000000000",
        "ROUND TIMESTAMP:  %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "",
        "ListAgents roster (verbatim):",
    ]
    for row in rows:
        lines.append("  ⚙ %s  ·  interactive  ·  %s"
                     % (row["name"], row["status"]))
    lines += ["", "Isolated roots — export these before dispatching the reader:"]
    for var, sub in (("SESSIONS_DIR", "sessions"), ("PROJECTS_DIR", "projects"),
                     ("ATTENTION_STATE_DIR", "attention")):
        lines.append("  export %s=%s" % (var, os.path.join(root, sub)))
    lines += [
        "  # ⚠️ REQUIRED, not optional. `who-needs-me.py` tries the HTTP store FIRST",
        "  # (`$ATTENTION_STORE_URL`, default localhost:18080) and falls back to the event",
        "  # log only when it is unreachable. Without this line the feed half of the replay",
        "  # reads the LIVE store, the fixture's gates are never seen, and the run looks",
        "  # isolated without being isolated — measured 2026-10-08 while building this.",
        "  export ATTENTION_STORE_URL=http://127.0.0.1:1",
        "  export OBSIDIAN_DIR=%s" % os.path.join(root, "vaults-parent"),
        "  export SUPERVISOR_PROJECTS_DIR=%s" % os.path.join(root, "projects"),
        "",
        "⚠️ BOUNDARY — the FEED half is SUPPLIED, not read.",
        "  `who-needs-me.py` refuses outright when the WezTerm pane list is unreadable",
        "  (*\"Refusing to print a feed that would read as an empty queue\"*), and the",
        "  synthetic sessions own no pane — so its live output cannot be isolated here",
        "  without borrowing real pane ids off this machine, which would make the replay",
        "  depend on whichever panes happen to be open. The BLOCKED / CLOSERS rows below",
        "  are rendered by the harness and handed in as the reader's step-1 output.",
        "  **Everything else is read live:** the `⏰ Ends:` slot and the gate records come",
        "  from the materialised attention store, the roster and task files from the",
        "  isolated roots, and the reader must derive every class itself.",
        "",
        "BLOCKED (feed, raised — not verified open):",
    ]
    for row in rows:
        if row.get("gate"):
            lines.append("  %s · pane — · %s" % (row["name"], row["gate"]["detail"]))
    lines += ["", "CLOSERS (rendered panels — last-turn closer line):"]
    for row in rows:
        lines.append("  %s · pane — · %s"
                     % (row["name"], row["stop_detail"].replace("\n", " | ")[:90]))
    lines += ["", "Expected class per row (the CHECK, never an input to the reader):"]
    for row in rows:
        lines.append("  %-24s %s" % (row["key"], row["expected_class"]))
    return "\n".join(lines)


def check(digest_path, root):
    """Compare a returned digest's classes against the fixture. Exit 1 on any mismatch."""
    rows = load_fixture()["rows"]
    with open(digest_path, encoding="utf-8") as fh:
        digest = fh.read()
    bad = []
    for row in rows:
        sid8 = row["session_id"][:8]
        hit = [ln for ln in digest.splitlines() if sid8 in ln]
        if not hit:
            bad.append((row["key"], row["expected_class"], "MISSING from the digest"))
            continue
        line = hit[0]
        if row["expected_class"] not in line:
            bad.append((row["key"], row["expected_class"], line.strip()[:100]))
    for key, want, got in bad:
        print(f"MISMATCH {key}: want {want} — {got}")
    if bad:
        print(f"\n{len(bad)} of {len(rows)} rows disagreed", file=sys.stderr)
        return 1
    print(f"all {len(rows)} rows match the fixture")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--materialise", action="store_true")
    ap.add_argument("--check", metavar="DIGEST")
    ap.add_argument("--root")
    a = ap.parse_args(argv)

    root = a.root or tempfile.mkdtemp(prefix="fleet-park-replay-")
    if a.check:
        return check(a.check, root)
    materialise(root)
    print(reader_inputs(root))
    print(f"\nmaterialised at: {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
