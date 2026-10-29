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

Usage:

    closer-verdict.py --class routine
    closer-verdict.py --class ask --closer "pick — 1. hold prod"

Prints one line, `<verdict>  <reason>`, and always exits 0 — it is a decision printer,
not a gate. An unrecognised class exits 2 with the accepted vocabulary, because a typo
there would otherwise read as a confident verdict.
"""

import argparse
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


def verdict(closer_class):
    """`(verdict, reason)` for a class token; raises `KeyError` on an unknown one."""
    return VERDICTS[closer_class]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="closer_class", required=True,
                    help="the class token from `who-needs-me.py --section last-closers`")
    ap.add_argument("--closer", default="",
                    help="the closer text, echoed in the reason when given")
    a = ap.parse_args()
    try:
        word, reason = verdict(a.closer_class)
    except KeyError:
        print("unknown class %r — expected one of: %s"
              % (a.closer_class, ", ".join(sorted(VERDICTS))), file=sys.stderr)
        return 2
    if a.closer:
        reason = "%s · closer: %s" % (reason, a.closer)
    print("%s  %s" % (word, reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
