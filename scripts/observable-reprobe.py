#!/usr/bin/env python3
"""observable-reprobe — a `⏰ Ends:` observable, probed, as a verdict.

`⏰ Ends:` names the thing a parked session waits on. `scripts/declared-wait.py` reads
WHETHER a session declares one and how old the declaration is; it does not ask whether
the thing it names still exists. So a `🔴 BLOCKED` row whose PR merged an hour ago keeps
rendering as blocked, round after round, and the operator is told to wait on something
that already happened. Measured 2026-11-10: of 8 blocked sessions, 2 had already cleared
and were never re-checked.

WHAT IT DOES NOT DO. It does not resume anything and it does not read the transcript.
It answers one question — has this observable cleared — and prints a verdict. The round
resumes the session; this script never does.

⚠️ **`unprobeable` is never a clearance.** A missing repo, an unrecognised observable, or
a failed `gh` call all leave the row standing. Folding any of them into `resumed` would
resume a session on no evidence, which is the false-positive direction this script exists
to remove. This mirrors `declared-wait.py`'s failed-read rule: an unreadable input is not
a negative.

⚠️ **EVERY named PR must have cleared, not just the first.** A declaration routinely names
more than one ("PR #786 merged and PR #791 reviewed"). Probing the first match alone would
report `resumed` off a merged PR while a second is still open — a clearance on partial
evidence, which is the same false positive by a different route. `verdict()` therefore
probes every distinct PR and returns `still-blocked` if ANY of them is still blocked.

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


def observables(text):
    """Every DISTINCT PR this `⏰ Ends:` text names.

    Returns `[{"kind", "repo", "number"}, …]`, empty when the text names no PR at all —
    a clock, a person, a vendor reply. An empty list is `unprobeable`, never "cleared".

    ⚠️ **The list is grouped by SHAPE, not by position in the text** — URLs first, then
    `owner/repo#n`, then bare numbers. `verdict()` is order-independent (it ANDs the
    results), so this only matters to a reader of the reason string.

    `repo` is None when a number appears with no repo — the caller supplies one with
    `--repo`. A number that a URL or `owner/repo#n` already identified is not repeated
    as a bare entry, so `o/r#1 and PR #1` probes once rather than twice.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    found = []
    seen = set()

    def add(repo, number):
        if (repo, number) not in seen:
            seen.add((repo, number))
            found.append({"kind": PR, "repo": repo, "number": number})

    for m in _URL.finditer(text):
        add(f"{m.group(1)}/{m.group(2)}", int(m.group(3)))
    for m in _QUALIFIED.finditer(text):
        add(f"{m.group(1)}/{m.group(2)}", int(m.group(3)))
    # A bare number is only worth its own entry when nothing above resolved it — probing
    # it again would add a second call for one PR, and an un-resolvable duplicate would
    # fail the whole declaration that a qualified reference had already settled.
    resolved = {n for (r, n) in seen if r}
    for m in _BARE.finditer(text):
        number = int(m.group(1))
        if number not in resolved:
            add(None, number)
    return found


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


def _one(obs, probe, repo):
    """`(verdict, reason)` for a single observable. Never raises."""
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


def verdict(obs_list, probe=probe_pr, repo=None):
    """`(verdict, reason)` for a declaration naming one or more PRs. Never raises.

    Cleared only when EVERY named PR has cleared — one still-blocked PR makes the whole
    declaration `still-blocked`, and one failed probe makes it `unprobeable`. `probe` is
    injected so the tests exercise every branch without a network call.
    """
    if not obs_list:
        return _UNPROBEABLE, "no probeable observable in the declaration"
    reasons = []
    cleared = True
    for obs in obs_list:
        v, reason = _one(obs, probe, repo)
        if v == _UNPROBEABLE:
            return _UNPROBEABLE, reason
        if v == _STILL:
            cleared = False
        reasons.append(reason)
    return (_RESUMED if cleared else _STILL), " · ".join(reasons)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ends", required=True, help="the `⏰ Ends:` text to probe")
    parser.add_argument("--repo", default=None, help="owner/repo for a bare 'PR #<n>'")
    args = parser.parse_args(argv)

    if args.repo is not None and not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        print(f"not an owner/repo: {args.repo}", file=sys.stderr)
        return 2

    v, reason = verdict(observables(args.ends), repo=args.repo)
    print(f"{v}\t{reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
