#!/usr/bin/env python3
"""Backfill `task_file` on ledger rows written before the field existed.

WHY. `agents/fleet-drive.md` step 0 re-probes every retained row from its **task
file**, and an entry carrying no `task_file` is deliberately unverifiable -- the path
is never guessed, because neither other identity field can resolve one (`session_id`
is an 8-char display handle; `name` is a session name that need not equal the
filename). That rule is right, but the field postdates most of the ledger: measured
2026-10-21, 82 of 88 retained rows carried an empty `task_file`, so under the
never-guess rule they were permanently unverifiable -- never `needs-step-4`, never
`revive`. The retention leg served 6 rows out of 88.

WHAT THIS DOES. It supplies the missing path from an **authoritative** source rather
than a guess: a task file *declares* its session in `claude_session_id`, and the
ledger's 8-char sid is a prefix of that declared value by construction. A prefix that
matches exactly one task is a resolution, not an inference. Zero matches and more than
one both leave the row empty -- an ambiguous prefix is exactly the case the
never-guess rule exists for, and this script must not become the guess it avoids.

MEASURED 2026-10-21 over the live ledger: 50 of 82 resolved uniquely, 0 ambiguous,
32 unmatched. Both rows named by the task's SC2 resolve, and one of them proves `name`
cannot be the join -- the ledger calls it `Renaming Task Re-Mints id` while its task
is `A Vault Write Re-Mints a Present task_identifier and Orphans Identifier-Keyed
Links`, which no name match would find.

WRITES GO THROUGH `drive-ledger.py merge`. This script never touches the ledger file:
one owner per file, and the merge is the only writer that keeps an entry it was not
handed. `plan` is the default and writes nothing, so the migration is inspectable
before it runs.

Subcommands: plan (default, dry run) | apply
"""
import argparse
import json
import os
import re
import subprocess
import sys

#: Exit code for a ledger that could not be read. Distinct from 1 (a crash).
REFUSED = 2

#: The verdicts that mean "not actionable this round, but not terminal either".
#: Only these are backfilled: a `revive`/`finished` row is not re-probed by step 0,
#: so giving it a path would change a record without changing any behaviour.
RETAINED = ("blocked", "unverifiable")

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_SCRIPT = os.path.join(HERE, "drive-ledger.py")

#: `claude_session_id:` in a task's frontmatter. Unquoted, quoted and empty all appear.
#: The whitespace class is horizontal on purpose: `\s` matches the newline too, so an
#: empty value would capture the following line (`---`) as if it were the id.
SESSION_ID = re.compile(r"^claude_session_id:[ \t]*(.*?)[ \t]*$", re.M)


def read_ledger(script=None):
    """(ledger, None) or (None, reason). Never treats a refusal as an empty ledger."""
    out = subprocess.run(
        [sys.executable, str(script or LEDGER_SCRIPT), "read"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None, (out.stderr or "").strip() or "drive-ledger.py read refused"
    try:
        return json.loads(out.stdout), None
    except ValueError as exc:
        return None, "drive-ledger.py read emitted unparseable JSON: %s" % exc


def task_index(tasks_dir):
    """{declared session id: [absolute paths]}.

    Read from the file itself, never from a task's name: the two disagree often
    enough that a name match is a guess (measured 2026-10-21, `f61ca42d`).

    Absolute, because `drive-retained.py` opens `task_file` exactly as given and its
    cwd is wherever the round ran. A relative path resolves against that cwd and
    reads as `task file missing` -- which is how the one relative row already in the
    ledger (a bare basename) reads today.
    """
    index = {}
    for root, _dirs, files in os.walk(tasks_dir):
        for name in files:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    head = handle.read(8192)
            except OSError:
                continue
            found = SESSION_ID.search(head)
            if not found:
                continue
            declared = found.group(1).strip().strip("\"'")
            if not declared or declared == "null":
                continue
            index.setdefault(declared, []).append(os.path.abspath(path))
    return index


def resolve(sid, index):
    """(path, None) | (None, 'unmatched') | (None, 'ambiguous')."""
    hits = [p for declared, paths in index.items()
            if declared.startswith(sid) for p in paths]
    if len(hits) == 1:
        return hits[0], None
    return None, ("ambiguous" if hits else "unmatched")


def candidates(ledger):
    """Retained rows carrying no task file. The only rows this script may touch."""
    return [
        row for row in ledger.get("entries") or []
        if row.get("verdict") in RETAINED
        and not (row.get("task_file") or "").strip()
    ]


def plan(ledger, index):
    """(resolved, skipped). `resolved` is the merge payload; `skipped` is the report."""
    resolved = []
    skipped = []
    for row in candidates(ledger):
        path, reason = resolve(row.get("session_id") or "", index)
        if reason:
            skipped.append((row.get("session_id"), reason))
            continue
        updated = dict(row)
        updated["task_file"] = path
        resolved.append(updated)
    return resolved, skipped


def report(resolved, skipped):
    print(
        "backfill: %d resolved · %d unmatched · %d ambiguous"
        % (
            len(resolved),
            sum(1 for _sid, why in skipped if why == "unmatched"),
            sum(1 for _sid, why in skipped if why == "ambiguous"),
        )
    )
    for row in resolved:
        print("  %s -> %s" % (row.get("session_id"), row["task_file"]))
    for sid, why in skipped:
        print("  %s -> (%s, left empty)" % (sid, why))


def cmd_plan(args):
    ledger, reason = read_ledger(args.ledger_script)
    if reason:
        print("drive-backfill: %s" % reason, file=sys.stderr)
        return REFUSED
    resolved, skipped = plan(ledger, task_index(args.tasks_dir))
    report(resolved, skipped)
    print("\nnothing written (plan). Re-run with `apply` to merge these.")
    return 0


def cmd_apply(args):
    ledger, reason = read_ledger(args.ledger_script)
    if reason:
        print("drive-backfill: %s" % reason, file=sys.stderr)
        return REFUSED
    resolved, skipped = plan(ledger, task_index(args.tasks_dir))
    report(resolved, skipped)
    if not resolved:
        print("\nnothing to write.")
        return 0
    payload = json.dumps(
        {"round": ledger.get("round"), "entries": resolved}
    )
    out = subprocess.run(
        [sys.executable, str(args.ledger_script or LEDGER_SCRIPT), "merge"],
        input=payload,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        print("drive-backfill: merge refused: %s" % (out.stderr or "").strip(),
              file=sys.stderr)
        return REFUSED
    sys.stdout.write(out.stdout)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="drive-backfill.py",
        description="Backfill task_file on retained ledger rows from the task's "
                    "declared claude_session_id.",
    )
    sub = parser.add_subparsers(dest="command")
    for name, func, help_text in (
        ("plan", cmd_plan, "report what would be backfilled (writes nothing)"),
        ("apply", cmd_apply, "merge the backfilled task_file values"),
    ):
        one = sub.add_parser(name, help=help_text)
        one.add_argument("--tasks-dir", required=True,
                         help="the vault root holding the task files")
        one.add_argument("--ledger-script", default=None,
                         help="path to drive-ledger.py (default: the sibling copy)")
        one.set_defaults(func=func)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return REFUSED
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
