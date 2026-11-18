#!/usr/bin/env python3
"""A round that refused EVERY selected row must fail loudly, not render as `opened 0`.

The open leg's block is the round's only reading of what it opened. When every selected row is
refused, the block still reads like a leg that ran — `selected N` with a `skipped:` tally — and
the round's tally prints `opened: 0 of N` in the same shape as a round that legitimately resumed
rows. Measured 2026-11-18: round `1795012825` refused all 7 of its selected rows with
`SESSION UNREADABLE` and the round rendered `opened 0` — a silent fleet-wide stop-start.

⚠️ **`opened` alone is not the test.** A row that is `♻️ resumed` or `↪️ jumped` re-enters a live
session and is a success, so a round with `opened 0 · resumed 3 · refused 1` (round `1795020429`)
is healthy and must pass. The failure condition is *no selected row reached a live session at all*:
`opened + resumed + jumped == 0` while `selected > 0`.

Usage:
    check-open-leg-opened.py <round.open>      # one payload
    check-open-leg-opened.py --newest <dir>    # the most recent *.open in a directory
"""

import pathlib
import re
import sys

SELECTED = re.compile(r"^open leg: selected (\d+)", re.MULTILINE)
TALLY = re.compile(r"^opened: (\d+) of (\d+)\s*·\s*resumed=(\d+)\s*·\s*jumped=(\d+)", re.MULTILINE)
REFUSED = re.compile(r"^refused:", re.MULTILINE)


def fail(message: str) -> None:
    # ⚠️ Exit **2**, never 1: 1 is this check's "every selected row was refused" verdict, and a
    # missing payload or a usage error must not be reported as that — a round that could not read
    # its payload has not established that anything was refused.
    print("❌ " + message, file=sys.stderr)
    sys.exit(2)


def read_payload(path: pathlib.Path) -> str:
    if not path.is_file():
        fail("no such payload: %s" % path)
    return path.read_text(encoding="utf-8", errors="replace")


def verdict(text: str, path: pathlib.Path | None = None) -> tuple[int, str]:
    """Return (exit_code, one-line verdict). Never raises on an unreadable payload."""
    selected_match = SELECTED.search(text)
    if selected_match is None:
        # An unreadable block is `unknown`, never `0` — the leg prints `open leg: unknown` on a
        # read failure and that must not pass as a clean round.
        if "open leg: unknown" in text:
            return 2, "open leg: unknown — the round could not read its selection"
        return 2, "unparseable open-leg block — no `open leg: selected N` line"

    selected = int(selected_match.group(1))
    tally = TALLY.search(text)
    if tally is None:
        # Older payloads carry no tally. The refusal evidence then lives in the sibling
        # `<round>.open-refusals` (the truncation overflow) — read it rather than guessing.
        refused_here = len(REFUSED.findall(text))
        if path is not None:
            sibling = path.with_name(path.name + "-refusals")
            if sibling.is_file():
                refused_here += len(REFUSED.findall(sibling.read_text(encoding="utf-8",
                                                                       errors="replace")))
        if selected > 0 and refused_here >= selected:
            return 1, ("❌ open leg: EVERY selected row was refused — selected %d, refused %d "
                       "(no tally; refusals read from the block and its `-refusals` sibling)"
                       % (selected, refused_here))
        # Neither a tally nor enough refusals to conclude: a mid-write read, not a verdict.
        return 2, ("incomplete open-leg block — no `opened: <n> of <m> · resumed=<n> · jumped=<n>` "
                   "tally (a mid-write payload ends inside the `skipped:` line)")
    opened, resumed, jumped = int(tally.group(1)), int(tally.group(3)), int(tally.group(4))
    refused = len(REFUSED.findall(text))

    if selected == 0:
        return 0, "open leg: selected 0 — nothing to open"
    if opened + resumed + jumped > 0:
        return 0, ("open leg: %d of %d reached a live session (opened=%d resumed=%d jumped=%d "
                   "refused=%d)" % (opened + resumed + jumped, selected, opened, resumed, jumped,
                                    refused))
    return 1, ("❌ open leg: EVERY selected row was refused — selected %d, opened 0, resumed 0, "
               "jumped 0, refused %d" % (selected, refused))


def newest(directory: pathlib.Path) -> pathlib.Path:
    payloads = sorted(directory.glob("*.open"), key=lambda p: p.stat().st_mtime)
    if not payloads:
        fail("no *.open payload in %s" % directory)
    return payloads[-1]


def main() -> None:
    args = sys.argv[1:]
    if not args:
        fail("usage: check-open-leg-opened.py <round.open> | --newest <dir>")
    if args[0] == "--newest":
        if len(args) != 2:
            fail("usage: check-open-leg-opened.py --newest <dir>")
        path = newest(pathlib.Path(args[1]))
    else:
        path = pathlib.Path(args[0])

    code, line = verdict(read_payload(path), path)
    print(line, file=sys.stderr if code else sys.stdout)
    sys.exit(code)


if __name__ == "__main__":
    main()
