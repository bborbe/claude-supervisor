#!/usr/bin/env python3
"""Refuse a page name that cannot be safely double-quoted into a shell word.

    python3 scripts/closure-name-safe.py "<name>"     # exit 0 safe, 1 unsafe, 2 usage

The closure step interpolates a page name into `vault-cli … complete "<name>"` and runs
unattended every tick. Inside a double-quoted shell word exactly four characters can still
do harm:

    $        starts a parameter expansion, including `$(…)` command substitution
    `        starts command substitution
    "        closes the quoting
    \\        escapes the character after it

Nothing else is special in that position, so this is the whole surface — and a page name
carrying one of them is far more likely a mistake than an intent.

⚠️ **This exists because prose is not a guard.** The rule was stated in the step and depended
on a model remembering it on every tick. A script turns it into a check with an exit code,
which is the difference between a convention and a gate.
"""
import sys

UNSAFE = ("$", "`", '"', "\\")


def offenders(name):
    """The unsafe characters in `name`, in order of first appearance, de-duplicated."""
    seen = []
    for char in name:
        if char in UNSAFE and char not in seen:
            seen.append(char)
    return seen


def main(argv):
    if len(argv) != 2:
        print(f"usage: {argv[0].rsplit('/', 1)[-1]} <name>", file=sys.stderr)
        return 2
    name = argv[1]
    bad = offenders(name)
    if bad:
        shown = " ".join(repr(char) for char in bad)
        print(f"UNSAFE: {name!r} carries {shown} — do not interpolate it into a shell word")
        return 1
    print(f"SAFE: {name!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
