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

⚠️ **It renders; it never resumes.** Resuming is the drive leg's decision, and this
script does not own it.

⚠️ **Only a declaration that NAMES ITS REPO is probed.** There is deliberately no
resolver, and that is a measured decision rather than a gap:

  * The obvious resolver — the session's transcript `cwd` → `git remote get-url origin`
    — returns the repo the session *lives in*, not the one it waits on. Every PR-bearing
    declaration in the live store comes from a **vault-hosted** session, so it would
    resolve to the vault's own repo; and the vault repo has a merged PR #1, so a bare
    `PR #1` would render `resumed` off a repository the wait never mentioned. That is a
    false clearance produced by the resolver itself, which is worse than not probing.
  * The task-cited-PR resolver finds nothing for the same population — none of those
    sessions has an anchored task carrying the PR.

So a bare `PR #N` renders `unprobeable`, and the unlock is a **declaration-writing
convention** (a closer naming a PR should write it qualified), filed as its own task
rather than absorbed here. Measured 2026-11-11: 324 declarations, 21-30 naming a PR
(the count moves with the live store), of which the 2 written qualified resolve — and
both were genuinely `MERGED` while still rendering as parked.

⚠️ **`unprobeable` is never a clearance**, here as in the module it delegates to.

⚠️ **A declaration's own verdict is carried, not flattened.** `declared-wait.py` returns
`declared` (a live wait) or `expired` (not a wait any more), and the live store is
overwhelmingly `expired`. Rendering an expired declaration as though it were live would
tell a reader to keep waiting on something `declared-wait.py` has already decided is not
a wait, so every row carries its `wait` state and the block counts them separately.

Exit codes: 0 ok · 2 refused (the wait reader could not be read, or the join failed).

Subcommands: render
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys

REFUSED = 2

_RESUMED = "resumed"
_STILL = "still-blocked"
_UNPROBEABLE = "unprobeable"


def _load(filename, module_name):
    """A sibling script, loaded by path — the repo's pattern for hyphenated names."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(here, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_waits(script=None):
    """Every declaring session's record, from `declared-wait.py --detail-json`.

    Raises on a non-zero exit or a body that is not a list of objects: a reader that
    cannot be read is a refusal, never "no waits", and a single malformed element must
    not silently become a short list.
    """
    path = script or os.path.join(os.path.dirname(os.path.abspath(__file__)), "declared-wait.py")
    out = subprocess.run(
        [sys.executable, path, "--detail-json"],
        capture_output=True, text=True, check=True, timeout=180,
    ).stdout
    data = json.loads(out)
    if not isinstance(data, list):
        raise ValueError("declared-wait.py --detail-json did not return a list")
    bad = [i for i, rec in enumerate(data) if not isinstance(rec, dict)]
    if bad:
        raise ValueError(f"--detail-json returned {len(bad)} non-object record(s)")
    return data


def reprobe(records, probe=None):
    """One row per declaration, carrying the probe's verdict and the wait's own state.

    `probe` is injected so the tests exercise every branch without a network call. No
    repo is passed: only a declaration naming its own repo can be probed — see the
    module docstring for why there is no resolver.
    """
    orx = _load("observable-reprobe.py", "observable_reprobe")
    probe = probe or orx.probe_pr
    rows = []
    for rec in records:
        ends = rec.get("ends") or ""
        obs = orx.observables(ends)
        verdict, reason = orx.verdict(obs, probe=probe)
        rows.append({
            "session_id": rec.get("session_id") or "",
            "ends": ends,
            "verdict": verdict,
            "reason": reason,
            "wait": rec.get("verdict") or "unknown",
            "mechanism": rec.get("mechanism"),
        })
    return rows


def render_block(rows):
    """The `REPROBED` block: one line per declaration, however long the list.

    Rendered here rather than by the agent for the reason `drive-escalate.py` records:
    prose that forbids collapsing a block loses to an agent under load, and a rendered
    block cannot be collapsed without deleting lines a reader can count.
    """
    lines = ["REPROBED — declared waits whose observable was probed"]
    if not rows:
        lines.append("  (none)")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            "  [%s] %s · %s · %s"
            % (row["session_id"][:12] or "?", row["wait"], row["verdict"], row["reason"])
        )
    counts = {v: len([r for r in rows if r["verdict"] == v])
              for v in (_RESUMED, _STILL, _UNPROBEABLE)}
    lines.append(
        "  %d resumed · %d still blocked · %d unprobeable · of %d rows, %d declared / %d expired"
        % (
            counts[_RESUMED], counts[_STILL], counts[_UNPROBEABLE], len(rows),
            len([r for r in rows if r["wait"] == "declared"]),
            len([r for r in rows if r["wait"] == "expired"]),
        )
    )
    return "\n".join(lines)


def cmd_render(args):
    # The whole join is inside the try: a missing sibling, a malformed record and an
    # unreadable store are all the same refusal, and none may surface as a traceback
    # when the documented contract is "exit 2, wrote nothing".
    try:
        records = read_waits()
        block = render_block(reprobe(records))
    except Exception as exc:
        print(f"wait-reprobe: refused ({exc})", file=sys.stderr)
        return REFUSED
    print(block)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="print the REPROBED block")
    args = parser.parse_args(argv)
    return cmd_render(args)


if __name__ == "__main__":
    sys.exit(main())
