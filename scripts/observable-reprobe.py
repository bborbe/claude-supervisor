#!/usr/bin/env python3
"""observable-reprobe — a `⏰ Ends:` observable, probed, as a verdict.

`⏰ Ends:` names the thing a parked session waits on. `scripts/declared-wait.py` reads
WHETHER a session declares one and how old the declaration is; it does not ask whether
the thing it names still exists. So a `🔴 BLOCKED` row whose PR merged an hour ago keeps
rendering as blocked, round after round, and the operator is told to wait on something
that already happened. Measured 2026-11-10: of 8 blocked sessions, 2 had already cleared
and were never re-checked.

This script is the callable seam for that second question, for the same reason
`scripts/escalation-manager.py` is one: the drive leg is handed a PROMPT, not a board, so
prose telling it to "probe the observable" names a mechanism nobody can run. A script is
the thing a reader can actually invoke.

WHAT IT DOES NOT DO. It does not resume anything and it does not read the transcript.
It answers one question — has this observable cleared — and prints a verdict. The round
resumes the session; this script never does.

⚠️ **`unprobeable` is never a clearance.** A missing repo, an unrecognised observable, or
a failed `gh` call all leave the row standing. Folding any of them into `resumed` would
resume a session on no evidence, which is the false-positive direction this script exists
to remove. This mirrors `declared-wait.py`'s failed-read rule: an unreadable input is not
a negative.

⚠️ **`CLOSED` is a clearance, and it is not `MERGED`.** Both mean the wait cannot be
waited on further, so both re-drive the session — but they call for different next moves
(land the work vs. decide whether to abandon it), so the reason keeps them apart. A
verdict that collapsed them would read as "your PR merged" for a PR that was thrown away.

Usage:

    observable-reprobe.py --ends "PR #786 merged and the release cut" --repo bborbe/claude-supervisor
    observable-reprobe.py --ends "bborbe/claude-supervisor#786 MERGED"

Prints one line, `<verdict>\\t<reason>`, and always exits 0 for a verdict — it is a
decision printer, not a gate. Usage errors exit 2.
"""

import argparse
import json
import re
import subprocess
import sys

#: The only observable this script can probe today. A second kind (a tag, a deploy, a
#: clock) is a new branch in `verdict()` and its own reasons — not a widened regex,
#: because each kind has a different notion of "cleared".
PR = "pr"

_RESUMED = "resumed"
_STILL = "still-blocked"
_UNPROBEABLE = "unprobeable"

#: `https://github.com/<owner>/<repo>/pull/<n>` — the shape a closer carries when the
#: session pasted a link rather than a reference.
_URL = re.compile(r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")

#: `<owner>/<repo>#<n>` — the shape a session writes when it names the repo inline.
_QUALIFIED = re.compile(r"\b([\w.-]+)/([\w.-]+)#(\d+)\b")

#: `PR #<n>` / `pull request <n>` — the common shape, which carries NO repo. Resolving it
#: needs `--repo`; without one it is `unprobeable` rather than a guess at the fleet's
#: default repo, because a wrong repo's PR #786 is a real PR and its state would be
#: reported as this row's evidence.
_BARE = re.compile(r"\b(?:PR|pull\s+request)\s*#?\s*(\d+)\b", re.IGNORECASE)


def observable(text):
    """The PR this `⏰ Ends:` text names, as `{"kind", "repo", "number"}`, or None.

    `repo` is None when the text names a number with no repo — the caller supplies one
    with `--repo`. Returns None for a text naming no PR at all: a clock, a person, a
    vendor reply. None is `unprobeable`, never "cleared".
    """
    if not isinstance(text, str) or not text.strip():
        return None
    m = _URL.search(text)
    if m:
        return {"kind": PR, "repo": f"{m.group(1)}/{m.group(2)}", "number": int(m.group(3))}
    m = _QUALIFIED.search(text)
    if m:
        return {"kind": PR, "repo": f"{m.group(1)}/{m.group(2)}", "number": int(m.group(3))}
    m = _BARE.search(text)
    if m:
        return {"kind": PR, "repo": None, "number": int(m.group(1))}
    return None


def probe_pr(repo, number):
    """`{"state", "reviewDecision", "mergeStateStatus"}` for that PR, via `gh`.

    Raises on any failure — a non-zero `gh`, an unparseable body, a missing field. The
    caller turns that into `unprobeable`; it must never be caught into a clearance.
    """
    out = subprocess.run(
        [
            "gh", "pr", "view", str(number),
            "-R", repo,
            "--json", "state,reviewDecision,mergeStateStatus",
        ],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout
    data = json.loads(out)
    if not isinstance(data, dict) or not data.get("state"):
        raise ValueError(f"gh returned no state for {repo}#{number}")
    return data


def verdict(obs, probe=probe_pr, repo=None):
    """`(verdict, reason)` for one observable. Never raises.

    `probe` is injected so the tests exercise both outcomes without a network call —
    the same seam `scripts/escalation-manager.py` uses for its registry read.
    """
    if obs is None:
        return _UNPROBEABLE, "no probeable observable in the declaration"
    target = obs.get("repo") or repo
    if not target:
        return _UNPROBEABLE, f"PR #{obs['number']} names no repo and none was given"
    ref = f"{target}#{obs['number']}"
    try:
        data = probe(target, obs["number"])
    except Exception as exc:  # every failure is the same verdict, and never a clearance
        return _UNPROBEABLE, f"{ref}: probe failed ({exc})"

    state = (data.get("state") or "").upper()
    review = (data.get("reviewDecision") or "").upper() or "none"
    merge = (data.get("mergeStateStatus") or "").upper() or "unknown"
    if state == "MERGED":
        return _RESUMED, f"{ref} MERGED"
    if state == "CLOSED":
        return _RESUMED, f"{ref} CLOSED without merging"
    # Anything else is still being waited on, and the reason quotes the merge state so
    # the row's reader can tell "approved, waiting on a merge click" from "still in
    # review" without opening the PR.
    return _STILL, f"{ref} {state or 'UNKNOWN'} · review {review} · merge {merge}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ends", required=True, help="the `⏰ Ends:` text to probe")
    parser.add_argument("--repo", default=None, help="owner/repo for a bare 'PR #<n>'")
    args = parser.parse_args(argv)

    if args.repo is not None and not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        print(f"not an owner/repo: {args.repo}", file=sys.stderr)
        return 2

    v, reason = verdict(observable(args.ends), repo=args.repo)
    print(f"{v}\t{reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
