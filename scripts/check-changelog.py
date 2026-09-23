#!/usr/bin/env python3
"""CHANGELOG shape: frozen preamble, then either an `## Unreleased` section with
conventional-prefix entries (work in flight) or at least one `## vX.Y.Z` section
with nothing unreleased behind it.

A released tree legitimately has no `## Unreleased` section only while nothing is
unreleased behind it. A merge that lands *after* the release cut folds its entry under
the released heading and leaves no `## Unreleased` at all — the release watcher then
has nothing to cut, so the change can never ship. Measured 2026-09-23: #138's entry
folded under `## v0.39.1`, and master carried unreleased *code* with no Unreleased
section to cut it from. That is the case this check fails on.

Counting commits since the tag is too coarse — a merge commit can land after the cut
carrying nothing unreleased, and repairing a released section's text leaves CHANGELOG.md
itself differing from the tag. So the test is: does any file *other than* CHANGELOG.md
differ from the newest tag?
"""
import pathlib
import re
import subprocess
import sys

PREFIXES = ("feat:", "fix:", "refactor:", "test:", "docs:", "chore:", "perf:")


def unreleased_files():
    """Files other than CHANGELOG.md that differ from the newest tag, or None when
    git cannot answer — no git, no repo, or no tags. An unanswerable check is
    reported as such, never passed silently, because a silent pass is
    indistinguishable from a real one."""
    root = pathlib.Path(__file__).resolve().parent.parent
    try:
        tag = subprocess.run(["git", "describe", "--tags", "--abbrev=0"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()
        changed = subprocess.run(["git", "diff", "--name-only", tag, "HEAD"], cwd=root,
                                 capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [f.strip() for f in changed.splitlines() if f.strip() and f.strip() != "CHANGELOG.md"]


text = (pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text()

if not text.startswith("# Changelog\n"):
    print("CHANGELOG.md must start with the '# Changelog' preamble", file=sys.stderr)
    sys.exit(1)

released = re.findall(r"^## v\d+\.\d+\.\d+$", text, re.M)
heading = re.search(r"^## Unreleased$", text, re.M)
if heading is None:
    if not released:
        print("CHANGELOG.md has neither '## Unreleased' nor any '## vX.Y.Z'", file=sys.stderr)
        sys.exit(1)
    others = unreleased_files()
    if others:
        shown = ", ".join(others[:3]) + (" …" if len(others) > 3 else "")
        print(f"CHANGELOG.md has no '## Unreleased' section, but {len(others)} file(s) differ from the newest "
              f"tag: {shown}. A merge landing after the release cut folds its entry under the released heading "
              f"and the release watcher then has nothing to cut, so the change can never ship. Restore a "
              f"'## Unreleased' section above the top released heading and move the folded entry into it.",
              file=sys.stderr)
        sys.exit(1)
    unchecked = "" if others == [] else " (git could not answer — unreleased files NOT checked)"
    print(f"  changelog ok: released tree, {len(released)} released section(s), no Unreleased, nothing unreleased{unchecked}")
    sys.exit(0)

body = text[heading.end():].split("\n## ", 1)[0]
entries = [line for line in body.splitlines() if line.startswith("- ")]
if not entries:
    print("'## Unreleased' has no entries", file=sys.stderr)
    sys.exit(1)
bad = [e for e in entries if not e[2:].startswith(PREFIXES)]
if bad:
    print("entries without a conventional prefix: " + "; ".join(bad), file=sys.stderr)
    sys.exit(1)
print(f"  changelog ok: {len(entries)} unreleased entries, all prefixed")
