#!/usr/bin/env python3
"""closer-verdict — one `CLOSERS` row's class in, the drive leg's verdict out.

`agents/fleet-drive.md` step 4a splits every parked candidate three ways — revive,
blocked, unverifiable — and until 2026-10-29 the split was prose in a markdown file,
so the only way to check it was to read it. This script is the callable seam: the
class token `who-needs-me.py --section last-closers` emits decides the verdict, and
`scripts/tests/test_closer_verdict.py` pins both outcomes of the case the task exists
for (routine continue → nudge, real gate → no nudge).

WHY A CLASS AND NOT THE CLOSER TEXT. The text is the wrong input and the wrong
granularity: `approve:` is a gate in one task and a routine continue in another, which
is why step 4a keeps its four-test table for it. The class answers the question that
needs no judgement — what did the turn end on — and only `routine` and `ask` differ in
verdict. `routine` is safe to nudge on precisely because a nudge is read-only context:
it authorises nothing and says so, so a routine row whose prose is ambiguous still
cannot have anything approved on its behalf.

⚠️ **`routine` does NOT overrule step 4a test 4.** A session whose last closer reads
`nothing` but which also carries a `BLOCKED` row for some other gate is still blocked —
the class describes the turn's last line, not the session's whole state. The caller
applies that test on top; this script sees only the class.

⚠️ **A `parked-wait` row is suppressed only while its declaration is LIVE.** The class
alone cannot tell a wait that will fire from one that never will: `later (on …)` names an
event, and `scripts/declared-wait.py` owns whether the declaration is still a wait. A
stale one — `expired`, "not re-rendered for `MAX_AGE_S` and naming no future clock" — is
**not** a wait, and its documented effect is "re-driven, re-checks its observable, and
works or re-parks", which is exactly what a `revive` does. So the class map below is the
existing path, and the declaration is a SECOND input that may only widen it, and only
for `parked-wait`. A live, absent or unreadable declaration keeps `unverifiable`; `none`
keeps it whatever the declaration says, because `none` means no closer was captured, not
that a wait exists.

Usage:

    closer-verdict.py --class routine
    closer-verdict.py --class ask --closer "pick — 1. hold prod"
    closer-verdict.py --class parked-wait --session 64a0e8f1

Prints one line, `<verdict>  <reason>`, and always exits 0 — it is a decision printer,
not a gate. An unrecognised class exits 2 with the accepted vocabulary, because a typo
there would otherwise read as a confident verdict.
"""

import argparse
import glob
import importlib.util
import os
import sys

#: Class → verdict, the whole of the class-driven half of step 4a. `ask` is `blocked`
#: rather than a fourth verdict because the routine-continue table can still rescue an
#: `approve:` — that stays the caller's judgement, on the text.
VERDICTS = {
    "routine": ("revive",
                "the turn ended on `nothing` — the session asked nothing, so a "
                "read-only nudge is allowed"),
    "ask": ("blocked",
            "the turn ended on a real ask — a gate, unless step 4a's routine-continue "
            "table passes on its text"),
    "parked-wait": ("unverifiable",
                    "`later (on …)` — clock-bound, nothing to nudge until it fires"),
    "none": ("unverifiable",
             "no closer line captured — never revived on absence"),
}

#: The one class an expired declaration may widen, and the verdict it widens to. Kept
#: beside `VERDICTS` so the widening is one visible rule rather than a branch in the
#: caller, and so `test_only_routine_revives` can keep pinning the STATIC map.
WIDENABLE = ("parked-wait", "expired", "revive")

REASON_EXPIRED = (
    "the `⏰ Ends:` declaration is expired — `declared-wait.py` has decided it is not a "
    "wait, so the session is re-driven to re-check its observable and work or re-park"
)


def verdict(closer_class, wait_verdict=None):
    """`(verdict, reason)` for a class token; raises `KeyError` on an unknown one.

    `wait_verdict` is `scripts/declared-wait.py`'s verdict for the session's newest
    `⏰ Ends:` declaration — `"declared"`, `"expired"`, or `None` when no readable
    declaration was found.

    ⚠️ **The static map is resolved FIRST, and the declaration may only widen it.** Only
    `parked-wait` + `expired` changes the answer; every other class keeps its static
    verdict even on an expired declaration. `None` (no declaration, an unreadable store,
    or an ambiguous session id) is treated as LIVE — the safe direction, and the same
    rule `declared-wait.py` states for a record with no readable timestamp.
    """
    word, reason = VERDICTS[closer_class]
    if closer_class == WIDENABLE[0] and wait_verdict == WIDENABLE[1]:
        return WIDENABLE[2], REASON_EXPIRED
    return word, reason


def _sibling(filename, module_name):
    """A sibling script, loaded by path — the repo's pattern for hyphenated names."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(here, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_verdict_for(session, state_dir=None):
    """`declared-wait.py`'s verdict for `session`, or `None` on any failed read.

    `session` may be a full id or the digest's 8-char prefix: the digest renders the
    prefix and `declared-wait.py` reads by full id, so a prefix is resolved by globbing
    the store. Exactly one match resolves; zero or several return `None`, because a guess
    here would revive a row on another session's declaration.

    `None` is the safe answer for every failure — an unreadable store, an ambiguous
    prefix, no declaration, or a missing sibling module all keep today's verdict.
    """
    try:
        dw = _sibling("declared-wait.py", "declared_wait")
        base = state_dir or dw.STATE
        sid = session
        if len(sid) < 36:
            hits = [os.path.basename(p)[:-len(".events.jsonl")]
                    for p in glob.glob(os.path.join(base, f"{sid}*.events.jsonl"))]
            if len(hits) != 1:
                return None
            sid = hits[0]
        rec = dw.wait_record(sid, state_dir=base)
        return rec["verdict"] if rec else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="closer_class", required=True,
                    help="the class token from `who-needs-me.py --section last-closers`")
    ap.add_argument("--closer", default="",
                    help="the closer text, echoed in the reason when given")
    ap.add_argument("--session", default=None,
                    help="session id (full, or the digest's 8-char prefix) — resolves "
                         "the `⏰ Ends:` declaration, which widens `parked-wait` to "
                         "`revive` when the declaration is expired")
    a = ap.parse_args()
    if a.closer_class not in VERDICTS:
        print("unknown class %r — expected one of: %s"
              % (a.closer_class, ", ".join(sorted(VERDICTS))), file=sys.stderr)
        return 2
    # Only `parked-wait` consults the declaration, so the store is not read for the
    # classes the widening cannot touch.
    wait_verdict = (wait_verdict_for(a.session)
                    if a.session and a.closer_class == WIDENABLE[0] else None)
    word, reason = verdict(a.closer_class, wait_verdict)
    if a.closer:
        # Flatten, strip control characters, then truncate: the echo is one line, and
        # a caller handing over raw pane or transcript text must not be able to forge a
        # second output line or leave a dangling ANSI escape (slicing at 120 can cut
        # `\x1b[31m` in half). `\x1b` survives `split()`, so it is removed explicitly.
        flat = "".join(c for c in " ".join(a.closer.split()) if c >= " " and c != "\x7f")
        reason = "%s · closer: %s" % (reason, flat[:120])
    print("%s  %s" % (word, reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
