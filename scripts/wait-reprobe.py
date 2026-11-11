#!/usr/bin/env python3
"""wait-reprobe — the round's re-probe of declared waits whose observable is a PR.

`scripts/declared-wait.py` says WHICH sessions declare a `⏰ Ends:` wait and what its
text says. `scripts/observable-reprobe.py` answers whether one declaration's PR has
cleared. Nothing joined them, so a session whose PR merged an hour ago kept rendering as
parked — round after round — and the operator was told to wait on something that had
already happened. Measured 2026-11-10: of 8 blocked sessions, 2 had already cleared and
were never re-checked.

This script is the join, and it is a script for the reason `escalation-manager.py`
records: the sweep is handed a prompt, not a board, so prose naming a probe is a
mechanism nobody can run.

⚠️ **It renders; it never resumes.** Resuming a session is the drive leg's, and this
script does not own that decision. Its whole output is a block a reader pastes.

⚠️ **`unprobeable` is never a clearance**, here as in the module it delegates to. A
declaration this script cannot resolve a repo for, or whose probe fails, renders as
unprobeable and the row keeps its current class. Folding it into `resumed` would resume
a session on no evidence.

WHY THE REPO IS RESOLVED FROM THE TRANSCRIPT, NOT FROM THE TASK. A declaration usually
says `PR #786` with no repo. The session's transcript carries `cwd`, which is where it
works — so a bare reference resolves to the repo it is most likely about, and a
cross-repo wait is expected to be written qualified (`bborbe/nuke#939`), which
`observable-reprobe.py` reads without help. The task file's cited PR was the other
candidate and was rejected: it names a repo the session may only be *waiting on*, and
matching it to the declaration's number is a second parser with its own failure modes.

Exit codes: 0 ok · 2 refused (the wait reader could not be read).

Subcommands: render
"""
import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys

REFUSED = 2

#: `git@github.com:owner/repo.git` or `https://github.com/owner/repo` → `owner/repo`.
_ORIGIN = re.compile(r"[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def _load(filename, module_name):
    """A sibling script, loaded by path — the repo's pattern for hyphenated names."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(here, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_waits(script=None):
    """Every declaring session's record, from `declared-wait.py --detail-json`.

    Raises on a non-zero exit: a reader that cannot be read is a refusal, not "no waits".
    """
    path = script or os.path.join(os.path.dirname(os.path.abspath(__file__)), "declared-wait.py")
    out = subprocess.run(
        [sys.executable, path, "--detail-json"],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    data = json.loads(out)
    if not isinstance(data, list):
        raise ValueError("declared-wait.py --detail-json did not return a list")
    return data


def session_cwd(sid, projects_root=None):
    """The `cwd` the session's transcript records, or None.

    Read from the FIRST line only — the field is written on every record, and a
    transcript is unbounded over a session's life, so reading it whole would make one
    round's re-probe cost the fleet's entire history.
    """
    if not sid or "/" in sid or "\\" in sid:
        return None
    root = projects_root or os.path.expanduser("~/.claude/projects")
    for path in glob.glob(os.path.join(root, "*", f"{sid}.jsonl")):
        try:
            with open(path, encoding="utf-8") as f:
                first = f.readline()
            rec = json.loads(first) if first.strip() else None
        except Exception:
            return None
        if isinstance(rec, dict) and isinstance(rec.get("cwd"), str):
            return rec["cwd"]
    return None


def repo_for_cwd(cwd):
    """`owner/repo` for that checkout's origin, or None. Never raises."""
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        url = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True, timeout=20,
        ).stdout.strip()
    except Exception:
        return None
    m = _ORIGIN.search(url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def reprobe(records, probe=None, repo_for=None):
    """One row per declaration, carrying the probe's verdict.

    `probe` and `repo_for` are injected so the tests exercise every branch without a
    network call or a live transcript.
    """
    orx = _load("observable-reprobe.py", "observable_reprobe")
    probe = probe or orx.probe_pr
    repo_for = repo_for or (lambda sid: repo_for_cwd(session_cwd(sid)))
    rows = []
    for rec in records:
        sid = rec.get("session_id") or ""
        ends = rec.get("ends") or ""
        obs = orx.observables(ends, repo=repo_for(sid))
        verdict, reason = orx.verdict(obs, probe=probe, repo=repo_for(sid))
        rows.append({
            "session_id": sid,
            "ends": ends,
            "verdict": verdict,
            "reason": reason,
            "mechanism": rec.get("mechanism"),
        })
    return rows


def render_block(rows):
    """The `REPROBED` block: one line per declaration, however long the list.

    Rendered here rather than by the agent for the reason `drive-escalate.py` records:
    prose that forbids collapsing a block loses to an agent under load, and a rendered
    block cannot be collapsed without deleting lines a reader can count.
    """
    resumed = [r for r in rows if r["verdict"] == "resumed"]
    lines = ["REPROBED — declared waits whose observable was probed"]
    if not rows:
        lines.append("  (none)")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            "  [%s] %s · %s"
            % (row["session_id"][:8], row["verdict"], row["reason"])
        )
    lines.append(
        "  %d resumed · %d still blocked · %d unprobeable"
        % (
            len(resumed),
            len([r for r in rows if r["verdict"] == "still-blocked"]),
            len([r for r in rows if r["verdict"] == "unprobeable"]),
        )
    )
    return "\n".join(lines)


def cmd_render(args):
    try:
        records = read_waits()
    except Exception as exc:
        print(f"wait-reprobe: the wait reader could not be read ({exc})", file=sys.stderr)
        return REFUSED
    print(render_block(reprobe(records)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="print the REPROBED block")
    args = parser.parse_args(argv)
    if args.cmd == "render":
        return cmd_render(args)
    return REFUSED


if __name__ == "__main__":
    sys.exit(main())
