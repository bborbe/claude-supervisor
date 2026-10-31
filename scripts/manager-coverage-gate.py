#!/usr/bin/env python3
"""manager-coverage-gate.py — the dispatch gate for the manager round's coverage step (4b).

Why this exists
---------------
Step 4b dispatched one `vault-cli:task-manager-agent` per closure-box owner on every tick,
whether or not anything had moved. Measured 2026-10-16 on the Recurring Operator Gates
manager (session `beac5fd0-6a63-49dd-8017-5bde81eeafa0`), tick 09:05:58Z → 09:18:52Z: the
sweep reported **`Delta: no change`** — *"all 14 rows' status/phase/met/progress_hash match
the frozen snapshot"* — and step 4b dispatched **15** agents anyway (14 `task-manager-agent`
plus 1 `goal-manager-agent`), at 17,516 output tokens and 770,459 tokens counting cache
reads. The sweep had already answered the question the coverage map exists to answer; the
round then bought the same answer fifteen more times.

The round is not the cost; the dispatch is. So this gate runs FIRST, model-free, and answers
one question: given the sweep's own delta, which owners does this tick actually need a
coverage map for?

Contract
--------
    manager-coverage-gate.py --tracked <file> --delta <verdict> [--changed <names>]
                             [--subject <path>]

  --tracked FILE   one owner path per line. Blank lines and `#` comments are skipped. The
                   owners are the round's closure-box owners: the tracked task files plus
                   the subject page.
  --delta VERDICT  the sweep's own delta, as the sweep reader rendered it: the whole
                   `Delta: …` line, its leading clause, or the bare token. Recognised as
                   NO CHANGE: `no-change`, `no change`, `unchanged`, `nochange`. As CHANGE:
                   `change`, `changed`. Anything else — including an absent flag — fails
                   open.
  --changed NAMES  the names that moved, comma- or newline-separated. ⚠️ **Never
                   whitespace-separated** — task names contain spaces. An owner matches a
                   name when the owner path equals it, or when the owner's basename without
                   `.md` equals it. Matching is case-sensitive.
  --subject PATH   the subject page (a goal's, or a topic's). Emitted on any CHANGE even when
                   its own name is not in `--changed`, because the subject page's boxes are
                   the aggregate view: a task moving can move them, and the sweep reports the
                   task as what moved, never the page.

Exit codes
  0   no-change: nothing on stdout, dispatch no agent
  10  change: one owner path per line on stdout — dispatch exactly one agent per line
  2   usage error: `--tracked` is missing, unreadable or lists no owners. The caller cannot
      enumerate owners at all, so it falls back to its own tracked set and dispatches every
      owner.

Fail-open, by design
--------------------
A delta this gate does not recognise, a CHANGE carrying no names, and a name that resolves
to no owner all dispatch **every** owner rather than none. The asymmetric cost is the whole
reason: a dispatch not saved wastes one subagent call, while a coverage map silently skipped
on a tick that moved leaves the round reporting a stale SC-to-evidence table as current —
and nothing announces the loss. The same direction `manager-predispatch.py` and
`manager-quiet.py` take, for the same reason.

⚠️ **The delta is an INPUT, never recomputed here.** `agents/goal-sweep-reader.md` step 1
item 3 already computes it against the prior snapshot, and the coverage step's own line 37
forbids that step becoming "a second classifier". This gate consumes the verdict; it does
not derive a second one.

⚠️ **stdout is the dispatch list and nothing else.** The no-change marker goes to stderr,
because every caller counts stdout lines to decide how many agents to dispatch — a marker on
stdout would be counted as a row.

⚠️ **What this proves, and what it does not.** It proves which owners a given delta warrants.
It does not prove the round then dispatches exactly those — the dispatch is the caller's act
— and it does not prove the delta was computed correctly, which is the sweep reader's
contract, not this script's.

⚠️ **Only the no-change rendering is normalised.** The sweep reader renders a change as
prose (`Delta: <what moved>`) and renders `uncomputable` in the same shape, so both stay
unrecognised and fail open. Deciding between them is the sweep reader's contract, not a
string this gate may guess at — and guessing wrong would narrow the dispatch while the gate
believed it had read the delta.
"""

import argparse
import os
import re
import sys

EXIT_NOCHANGE = 0
EXIT_CHANGE = 10
EXIT_USAGE = 2

# The spellings the sweep reader renders. Kept as sets rather than a regex so an unfamiliar
# verdict falls through to the fail-open branch instead of half-matching.
NO_CHANGE = {"no-change", "no change", "unchanged", "nochange"}
CHANGE = {"change", "changed"}

NO_CHANGE_MARKER = "no change since the last sweep"


def normalise_delta(raw):
    """Reduce the sweep reader's rendered delta to the verdict token it leads with.

    `agents/goal-sweep-reader.md` renders `Delta: <what moved, or "no change">`, usually
    with a trailing clause explaining the verdict — measured 2026-10-16:
    `Delta: no change — all 14 rows' status/phase/met/progress_hash match the frozen
    snapshot (recorded_at …)`. The coverage step tells the caller to pass that verdict
    verbatim, so the rendered form is what this has to accept; a bare token still works.

    ⚠️ **Only the leading clause is compared, and only exactly.** A change description that
    happens to mention "no change" somewhere inside its prose must not read as a no-change
    verdict: failing open over-dispatches and costs tokens, while a wrong NO-CHANGE skips
    coverage silently — and only that direction is unsafe.
    """
    text = (raw or "").strip()
    text = re.sub(r"^delta\s*:\s*", "", text, flags=re.IGNORECASE)
    for separator in ("\n", "—", "–", " - "):
        text = text.split(separator, 1)[0]
    return text.strip().strip("\"'").strip().lower()


def read_owners(path):
    """Return (owners, error). One owner path per line; blank lines and `#` skipped."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        return [], f"cannot read --tracked {path}: {exc}"
    owners = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line not in owners:
            owners.append(line)
    if not owners:
        return [], f"--tracked {path} lists no owners"
    return owners, None


def owner_key(path):
    """The name a tracked owner is known by: its basename, without a `.md` suffix."""
    base = os.path.basename(path)
    return base[:-3] if base.endswith(".md") else base


def split_names(raw):
    """Split `--changed` on commas and newlines only.

    ⚠️ **Never on whitespace.** Task names contain spaces — *"The Manager Loop's Coverage
    Step Dispatches One Subagent per Tracked File Every Tick"* is one name, not fourteen —
    so a whitespace split shreds every real name, none of them then resolves, and the gate
    falls open on every call: the dispatch saving disappears while every test that only
    asserts the fail-open branch still passes. Caught by this gate's own suite on its first
    run, which is the reason the separator is stated here rather than assumed.
    """
    return [n.strip() for n in (raw or "").replace("\n", ",").split(",") if n.strip()]


def resolve(changed, owners):
    """The owners a set of changed names selects, or None when any name resolves to none.

    None is the fail-open signal, and it is deliberately whole-set rather than per-name: a
    name the tracked set cannot place means the delta and the tracked set disagree about what
    this round is looking at, and dispatching only the names that DID resolve would cover
    part of a set whose shape is in doubt.
    """
    matched = []
    for name in changed:
        hits = [o for o in owners if o == name or owner_key(o) == name]
        if not hits:
            return None
        for hit in hits:
            if hit not in matched:
                matched.append(hit)
    return matched


def emit(owners):
    """Print one owner path per line and return the verdict code. Empty list == no-change."""
    for owner in owners:
        print(owner)
    return EXIT_CHANGE if owners else EXIT_NOCHANGE


def main(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Decide which closure-box owners a manager tick warrants a coverage map for, "
            "from the sweep's own delta."
        )
    )
    parser.add_argument(
        "--tracked",
        required=True,
        help="one owner path per line; blank lines and # comments skipped",
    )
    parser.add_argument(
        "--delta",
        default=None,
        help=(
            "the sweep's own delta as rendered (the `Delta: …` line, its leading clause, "
            "or the bare token): no-change | change (anything else fails open)"
        ),
    )
    parser.add_argument(
        "--changed",
        default=None,
        help="names that moved, comma- or newline-separated (never whitespace)",
    )
    parser.add_argument(
        "--subject",
        default=None,
        help="the subject page, emitted on any change",
    )
    args = parser.parse_args(argv)

    owners, error = read_owners(args.tracked)
    if error:
        print(f"manager-coverage-gate: {error}", file=sys.stderr)
        return EXIT_USAGE

    verdict = normalise_delta(args.delta)

    if verdict in NO_CHANGE:
        print(f"⏸ {NO_CHANGE_MARKER} — 0 coverage dispatches", file=sys.stderr)
        return emit([])

    if verdict in CHANGE:
        changed = split_names(args.changed)
        selected = resolve(changed, owners) if changed else None
        if selected is None:
            # No names at all, or a name the tracked set cannot place: fail open.
            return emit(owners)
        if args.subject and args.subject not in selected:
            selected = selected + [args.subject]
        return emit(selected)

    print(
        f"manager-coverage-gate: unrecognised delta {args.delta!r} — failing open to "
        f"{len(owners)} owners",
        file=sys.stderr,
    )
    return emit(owners)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
