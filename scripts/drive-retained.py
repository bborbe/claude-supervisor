#!/usr/bin/env python3
"""Re-probe the drive ledger's retained rows, so a parked row is never dropped.

`agents/fleet-drive.md` step 1 rebuilds its candidates from the digest every round,
and the sweep classes a session `parked` only while it is idle with open boxes (or
sitting on an unanswered operator closer). A row that lands `blocked` or
`unverifiable` and then leaves that class therefore stops being a candidate, while
its ledger entry survives as the only record that it was ever parked.

Measured 2026-10-20 on round `round14-2026-10-20T13-10Z`: `9bcd20f1` and `f61ca42d`
are absent from that round's digest (`grep -c` returns 0 for both) and still present
in the ledger. No round had looked at either since they left.

WHY THIS IS A SCRIPT AND NOT A PARAGRAPH. The acceptance criterion is that a
stubbed probe is invoked once per retained row per round -- a markdown file has no
callable probe to stub, so the check could not discriminate against a build that
renders a stored string and never probes. The probe seam is `probe()`; the test
stubs it and counts.

WHAT IT CANNOT DECIDE. `revive` needs step 4's two live probes (the `CLOSERS` row and
the named-system probe), which read the digest this script does not hold. A row that
survives to that point is emitted as `needs-step-4` and the agent runs them; the
script never guesses a revive.

Storage and the merge belong to `drive-ledger.py` -- this reads through it and writes
nothing. Same rule as that script: one owner per file.

Subcommands: reprobe | render
"""
import argparse
import json
import os
import subprocess
import sys

#: Exit code for a ledger that could not be read. Distinct from 1 (a crash) so the
#: caller can tell "refused" from "broke".
REFUSED = 2

#: The verdicts that mean "not actionable this round, but not terminal either".
RETAINED = ("blocked", "unverifiable")

#: A row that survived every disk probe and needs step 4's live probes.
NEEDS_STEP_4 = "needs-step-4"

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_SCRIPT = os.path.join(HERE, "drive-ledger.py")
LIVENESS_SCRIPT = os.path.join(HERE, "session-liveness.py")

#: The command a `RETAINED` line names as the probe that produced its result.
#: A constant, not the invoking argv: the rendered line must stay stable when the
#: script is called by absolute path, so the payload records the same command every
#: round and a reader can re-run it verbatim.
PROBE_COMMAND = "python3 scripts/drive-retained.py reprobe"


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


def read_task(path):
    """(status, phase, open_boxes) from a task file. Raises OSError when absent.

    Deliberately line-based and order-independent: the frontmatter keys are read
    before the body's checkboxes, and an ISO date inside a quoted scalar cannot be
    mistaken for either.
    """
    status = ""
    phase = ""
    boxes = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not status and line.startswith("status:"):
                status = line.split(":", 1)[1].strip()
            elif not phase and line.startswith("phase:"):
                phase = line.split(":", 1)[1].strip()
            elif line.lstrip().startswith("- [") and ("[ ]" in line or "[/]" in line):
                boxes += 1
    return status, phase, boxes


def session_alive(sid, script=None):
    """True / False, or None when liveness could not be established.

    Delegates to `scripts/session-liveness.py` -- the plugin's one liveness instrument.
    It answers from the attention store's heartbeat, accepts the 8-char prefix the
    ledger carries, and distinguishes ABSENT from UNKNOWN.

    A local scan of `~/.claude/sessions/*.json` was the first draft and it was wrong
    twice over: a substring match over every session file finds the id inside
    unrelated content, and it cannot tell "not live" from "could not ask" -- which is
    exactly the distinction that decides whether a row is an orphan.

    Exit codes map 0 LIVE -> True and 1 ABSENT -> False; 2 UNKNOWN, 3 AMBIGUOUS and
    4 MALFORMED all map to None, because none of them licenses calling a session dead.
    """
    out = subprocess.run(
        [sys.executable, str(script or LIVENESS_SCRIPT), "--check", sid],
        capture_output=True,
        text=True,
    )
    return {0: True, 1: False}.get(out.returncode)


def probe(entry, read_task_fn=read_task, alive_fn=session_alive):
    """One retained row's disposition. This is the seam the test stubs.

    Returns {"verdict", "probe"}. `probe` is the evidence string, written back to the
    ledger and compared next round -- so a static copy of the previous round's string
    is what `already signalled` means, and a re-probe that never ran cannot fake it.
    """
    task_file = (entry.get("task_file") or "").strip()
    sid = entry.get("session_id") or ""
    if not task_file:
        return {"verdict": "unverifiable", "probe": "no task file recorded"}
    try:
        status, phase, boxes = read_task_fn(task_file)
    except OSError:
        alive = alive_fn(sid)
        detail = "task file missing"
        if alive:
            detail += ", session live"
        elif alive is None:
            detail += ", registry unreadable"
        return {"verdict": "unverifiable", "probe": detail}
    if status in ("completed", "aborted") or phase == "done" or boxes == 0:
        return {
            "verdict": "finished",
            "probe": "status=%s phase=%s open_boxes=%d" % (status, phase, boxes),
        }
    alive = alive_fn(sid)
    if alive is False:
        return {
            "verdict": "blocked",
            "probe": "orphan: task in_progress with open boxes, session absent from registry",
        }
    return {
        "verdict": NEEDS_STEP_4,
        "probe": "status=%s phase=%s open_boxes=%d, session live" % (status, phase, boxes),
    }


def disposition(entry, probe_fn=probe):
    """The entry as this round saw it, with the dedup key already resolved.

    `signalled` is emitted once per change; `already signalled` suppresses the repeat.
    Both the verdict and the evidence string are compared, so a row that stays
    `blocked` for a *different* reason still signals.

    The comparison is against `reprobe_verdict`/`reprobe_probe` -- THIS round's
    observation as the previous round recorded it -- and never against the entry's
    `verdict`/`probe`, which are the agent's own conclusions. The two are different
    vocabularies by construction: `probe()` emits `needs-step-4` for a live row, a
    disposition the ledger deliberately does not store (see `VERDICTS`), because the
    agent resolves it to revive|blocked|unverifiable in step 4. Comparing across the
    two could never match, so every live row re-signalled on every round -- measured
    2026-10-21 as byte-identical PARKED blocks in two consecutive rounds.

    `prior_verdict` stays the entry's `verdict` because that is what the RETAINED
    render shows; it is display only and never the dedup key.
    """
    prior_reprobe_verdict = entry.get("reprobe_verdict") or ""
    prior_reprobe_probe = entry.get("reprobe_probe") or ""
    result = probe_fn(entry)
    changed = (
        result["verdict"] != prior_reprobe_verdict
        or result["probe"] != prior_reprobe_probe
    )
    return {
        "session_id": entry.get("session_id") or "",
        "name": entry.get("name") or "",
        "task_file": entry.get("task_file") or "",
        "prior_verdict": entry.get("verdict") or "",
        "verdict": result["verdict"],
        "probe": result["probe"],
        "reprobe_verdict": result["verdict"],
        "reprobe_probe": result["probe"],
        "suppression": "signalled" if changed else "already signalled",
    }


def retained_entries(ledger):
    return [e for e in ledger.get("entries") or [] if e.get("verdict") in RETAINED]


def render_retained(rows, round_tag):
    """The `RETAINED` block: one line per row, however long the list.

    WHY THE SCRIPT RENDERS THIS AND NOT THE AGENT. `agents/fleet-drive.md` has
    required one line per row since v0.194.0, and the round of
    `2026-10-21T05-07-35Z` collapsed 88 re-probed rows into a single summary line
    anyway -- "82 unverifiable (no task file recorded), 5 needs-step-4, 1
    finished-by-script" -- carrying none of the three things the section exists to
    show. Prose that forbids a shape loses to an agent under load; a rendered block
    it pastes cannot be collapsed without deleting lines a reader can count.

    Each row carries the round tag, the command run that round, and that round's
    result. A row with no `task_file` renders `dropped: task file missing`, because
    that is what step 0 could do with it -- the honest evidence, not a blank.
    """
    lines = ["RETAINED — re-probed from the ledger, not the digest"]
    for row in rows:
        task_file = (row.get("task_file") or "").strip()
        lines.append(
            "  %s [%s] · round %s · %s · %s → %s"
            % (
                row.get("name") or "?",
                (row.get("session_id") or "")[:8],
                round_tag,
                task_file or "no task file",
                row.get("prior_verdict") or "none",
                row.get("verdict") or "?",
            )
        )
        lines.append("      probe: %s → %s" % (PROBE_COMMAND, row.get("probe") or ""))
        lines.append(
            "      ledger: %s"
            % ("excluded" if task_file else "dropped: task file missing")
        )
    if not rows:
        lines.append("  (none)")
    return "\n".join(lines)


def render_parked(rows):
    """The `PARKED` block: only the rows whose probe changed this round.

    The dedup key is `disposition()`'s, computed against the stored `reprobe_*` pair
    -- so an unchanged row is silent here and still present in `RETAINED`. Emitting
    `(none)` rather than dropping the section keeps a quiet round distinguishable
    from a round that never rendered it.
    """
    changed = [row for row in rows if row.get("suppression") == "signalled"]
    lines = ["PARKED — one line per row whose parking verdict or probe changed this round"]
    for row in changed:
        lines.append(
            "  %s [%s] · %s · %s"
            % (
                row.get("name") or "?",
                (row.get("session_id") or "")[:8],
                row.get("verdict") or "?",
                row.get("probe") or "",
            )
        )
    if not changed:
        lines.append("  (none)")
    return "\n".join(lines)


def cmd_render(args):
    """Blocks on stdout, and optionally the merge payload -- from ONE probe pass.

    `--json-out` exists so a round probes each retained row exactly once. Rendering
    and merging need the same observations, and making the agent run `reprobe` and
    then `render` would probe every row twice, which is precisely what
    `test_probe_runs_once_per_retained_row` forbids.
    """
    ledger, reason = read_ledger(args.ledger_script)
    if reason:
        print("drive-retained: %s" % reason, file=sys.stderr)
        return REFUSED
    rows = [disposition(entry) for entry in retained_entries(ledger)]
    round_tag = ledger.get("round") or "unknown round"
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(
                {"round": ledger.get("round"), "retained": rows},
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
    print(render_retained(rows, round_tag))
    print(render_parked(rows))
    return 0


def cmd_reprobe(args):
    ledger, reason = read_ledger(args.ledger_script)
    if reason:
        print("drive-retained: %s" % reason, file=sys.stderr)
        return REFUSED
    rows = [disposition(entry) for entry in retained_entries(ledger)]
    json.dump(
        {"round": ledger.get("round"), "retained": rows},
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="drive-retained.py")
    sub = parser.add_subparsers(dest="command")
    reprobe = sub.add_parser(
        "reprobe", help="disposition every blocked/unverifiable ledger row"
    )
    reprobe.add_argument(
        "--ledger-script",
        default=None,
        help="path to drive-ledger.py (default: the sibling copy)",
    )
    render = sub.add_parser(
        "render", help="print the payload's RETAINED and PARKED blocks, one line per row"
    )
    render.add_argument(
        "--ledger-script",
        default=None,
        help="path to drive-ledger.py (default: the sibling copy)",
    )
    render.add_argument(
        "--json-out",
        default=None,
        help="also write the merge payload here, from the same probe pass",
    )
    args = parser.parse_args(argv)
    if args.command == "reprobe":
        return cmd_reprobe(args)
    if args.command == "render":
        return cmd_render(args)
    parser.print_help(sys.stderr)
    return REFUSED


if __name__ == "__main__":
    sys.exit(main())
