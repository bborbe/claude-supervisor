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
    python3 scripts/tests/fleet-park-replay.py --materialise [--root DIR] [--keep]
        Build a fresh isolated tree and print the fleet-sweep-reader inputs, the
        expected class per row, and the `export` lines that point a shell at it.
        A tree under a temp dir is removed on exit unless --keep is passed; a tree
        built under an explicit --root is always left in place.

    python3 scripts/tests/fleet-park-replay.py --check <digest-file>
        Read a returned digest and print, per row, expected vs rendered. Exit 1 when
        any row disagrees, or when a row is missing from the digest entirely — a
        silently dropped row is the failure this whole change exists to remove.

Exactly one of --materialise / --check is required.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "fleet-park-signals.json")

# The isolated roots: (env var, the subtree it points at). Kept here rather than in each
# test module for the reason `fleet_sessions_isolation.py` gives: several import-time
# assignments of one key leave only the last in force. ⚠️ `reader_inputs` derives its
# export block from THIS tuple — the two used to be separate lists, so a root added here
# but not there would point a reader at live state while the constant said otherwise.
ROOTS = (
    ("SESSIONS_DIR", "sessions"),
    ("PROJECTS_DIR", "projects"),
    ("ATTENTION_STATE_DIR", "attention"),
)

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
    """Build the isolated tree under `root` and return {row key: session id}.

    ⚠️ **Refuses rather than degrading when `procStart` cannot be read.** `ps -o lstart=`
    returning nothing would write a `null` procStart, `session-liveness.py` would read
    every row UNKNOWN, and the digest would come back clean for a fleet the replay never
    classified — the failure this file's own docstring names as the one that matters.
    """
    proc_start = live_proc_start(LIVE_PID)
    if proc_start is None:
        raise SystemExit(
            "refusing to materialise: `ps -o lstart= -p %d` returned nothing, so every "
            "registry record would carry a null procStart and read UNKNOWN rather than "
            "LIVE — the digest would then be graded against rows the reader never "
            "classified." % LIVE_PID
        )
    for sub in [sub for _, sub in ROOTS] + ["vault/25 Tasks"]:
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
                       "procStart": proc_start,
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


def reader_inputs(root, sids):
    """The block a caller hands the released fleet-sweep-reader, verbatim.

    `sids` is `materialise`'s {row key: session id} map, printed so a caller reading the
    digest back can tie a rendered row to the fixture key that produced it.
    """
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
    lines += ["", "Synthetic session ids (read the digest back against these):"]
    for row in rows:
        lines.append("  %-24s %s" % (row["key"], sids[row["key"]]))
    lines += ["", "Isolated roots — export these before dispatching the reader:"]
    for var, sub in ROOTS:
        lines.append("  export %s=%s" % (var, os.path.join(root, sub)))
    lines += [
        "  # ⚠️ REQUIRED, not optional. `who-needs-me.py` tries the HTTP store FIRST",
        "  # (`$ATTENTION_STORE_URL`, default localhost:18080) and falls back to the event",
        "  # log only when it is unreachable. Without this line the feed half of the replay",
        "  # reads the LIVE store, the fixture's gates are never seen, and the run looks",
        "  # isolated without being isolated — measured 2026-10-08 while building this.",
        "  export ATTENTION_STORE_URL=http://127.0.0.1:1",
        "  # ⚠️ `root` itself, NOT a parent of it. `fleet-sessions.py` reads `obsidian_dir()`",
        "  # and treats each CHILD as a vault, probing `25 Tasks` inside it — so the vault",
        "  # materialised at <root>/vault is reachable only when the export IS <root>. A",
        "  # parent holding no vault makes `root.is_dir()` false, the stamp leg returns",
        "  # nothing, and every `claude_session_id:` stamp written above becomes invisible",
        "  # to the reader's PRIMARY resolution path — the replay then degrades silently to",
        "  # the name leg and can render rows unowned while still looking isolated.",
        "  export OBSIDIAN_DIR=%s" % root,
        "  export SUPERVISOR_PROJECTS_DIR=%s" % os.path.join(root, "projects"),
        "",
        "⚠️ BOUNDARY — the FEED half is SUPPLIED, not read.",
        "  `who-needs-me.py` refuses outright when the WezTerm pane list is unreadable",
        "  (*\"Refusing to print a feed that would read as an empty queue\"*), and the",
        "  synthetic sessions own no pane — so its live output cannot be isolated here",
        "  without borrowing real pane ids off this machine, which would make the replay",
        "  depend on whichever panes happen to be open. The BLOCKED / CLOSERS rows below",
        "  are rendered by the harness and handed in as the reader's step-1 output.",
        "  ⚠️ `who-needs-me.py:38` also hardcodes `~/Documents/Obsidian` and IGNORES",
        "  `OBSIDIAN_DIR`, so its `task_status_from_closer` globs the LIVE tree — the",
        "  export block above cannot redirect that one path. Harmless for this fixture",
        "  (no `📌 Task:` closer link is emitted, so it never runs), but the isolation",
        "  there rests on the fixture's shape rather than on the exports. Stated rather",
        "  than assumed: this is the one read the env block does not actually bound.",
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


# The `CLASSIFICATION` row shape the reader is required to emit
# (`agents/fleet-sweep-reader.md` § output_format):
#   <name> [<session id 8>] · <status> · <class> · <task file | —> · <boxes | —> · <link | —>
# The class is therefore the SECOND `·`-delimited field after the id — which is why the
# check reads that field instead of testing whether the class name occurs in the line.
_ROW_RE = re.compile(r"^\s+\S.*?\[(?P<sid>[0-9a-f]{8})\]\s*·(?P<rest>.*)$")


def _norm_class(cell):
    """A rendered class cell reduced to its bare class name.

    The reader may prefix the class with a glyph (`⌛ parked-on-watcher`), so strip leading
    non-word decoration and collapse whitespace — but never touch the hyphen: `parked` and
    `parked-on-watcher` are different classes, and the hyphen is the only thing separating
    them.
    """
    return re.sub(r"\s+", " ", re.sub(r"^[^\w]+", "", cell.strip())).strip()


def _rendered_class(lines, sid8):
    """The class cell of `sid8`'s CLASSIFICATION row, or None when it rendered no row.

    ⚠️ Scanning for the row rather than taking the first line that mentions the id: a
    digest that names a session in NOTES or an unresolved list before its CLASSIFICATION
    row would otherwise be graded against the wrong line and report a false MISMATCH.
    """
    for line in lines:
        m = _ROW_RE.match(line)
        if not m or m.group("sid") != sid8:
            continue
        fields = m.group("rest").split("·")
        if len(fields) >= 2:
            return _norm_class(fields[1])
    return None


def _header_count(lines, cls):
    """The `<class>=<n>` term in the CLASSIFICATION header, or 0 when absent.

    ⚠️ Anchored on the `=` that follows the name, so a search for `parked` cannot pick up
    the `parked-on-watcher=1` term sitting beside it.
    """
    for line in lines:
        if line.startswith("CLASSIFICATION"):
            m = re.search(re.escape(cls) + r"=(\d+)", line)
            if m:
                return int(m.group(1))
    return 0


def check(digest_path):
    """Compare a returned digest's classes against the fixture. Exit 1 on any mismatch.

    ⚠️ **The comparison is on the class FIELD, never on a substring of the row.** A
    substring test (`expected_class not in line`) cannot tell `parked` from
    `parked-on-watcher`, and the fixture deliberately ships both — so a digest that
    misclassified the control row as `parked-on-watcher` passed. That is precisely the
    prefix conflation `agents/fleet-drive.md` step 1 warns must be matched exactly, and a
    checker that cannot fail for the reason the harness exists verifies nothing.
    """
    rows = load_fixture()["rows"]
    with open(digest_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    bad = []
    for row in rows:
        sid8 = row["session_id"][:8]
        want = row["expected_class"]
        got = _rendered_class(lines, sid8)
        if got is None:
            # ⚠️ A `progressing` row renders NO row by spec — `<output_format>` says the
            # CLASSIFICATION rows are "only non-progressing rows" — so its sole observable
            # is the header term. Without this branch every replay reports a false MISSING
            # on the one row that exists to prove a fresh transcript is not a stall.
            if want == "progressing" and _header_count(lines, want) > 0:
                continue
            seen = [ln.strip()[:90] for ln in lines if f"[{sid8}]" in ln]
            bad.append((row["key"], want,
                        "MISSING from the digest" if not seen
                        else "no CLASSIFICATION row · " + " | ".join(seen)))
            continue
        if _norm_class(want) != got:
            bad.append((row["key"], want, "rendered %r" % got))
    for key, want, got in bad:
        print(f"MISMATCH {key}: want {want} — {got}")
    if bad:
        print(f"\n{len(bad)} of {len(rows)} rows disagreed", file=sys.stderr)
        return 1
    print(f"all {len(rows)} rows match the fixture")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--materialise", action="store_true",
                    help="build a fresh isolated tree and print the reader's inputs")
    ap.add_argument("--check", metavar="DIGEST",
                    help="grade a returned digest against the fixture")
    ap.add_argument("--root", help="materialise here instead of under a temp dir")
    ap.add_argument("--keep", action="store_true",
                    help="leave the materialised temp tree on disk for inspection")
    a = ap.parse_args(argv)

    # ⚠️ Enforced, not decorative. `--materialise` used to be parsed and never read, so a
    # bare invocation materialised anyway and `--check` could not be told from it.
    if bool(a.materialise) == bool(a.check):
        ap.error("pass exactly one of --materialise or --check")

    if a.check:
        return check(a.check)

    root = a.root or tempfile.mkdtemp(prefix="fleet-park-replay-")
    sids = materialise(root)
    try:
        print(reader_inputs(root, sids))
        print(f"\nmaterialised at: {root}")
        if not a.keep and not a.root:
            print("(removed on exit — pass --keep to inspect the tree)")
    finally:
        # Only a tree this process created is ours to delete; an explicit --root is the
        # caller's own directory and is always left in place.
        if not a.keep and not a.root:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
