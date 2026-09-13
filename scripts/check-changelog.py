#!/usr/bin/env python3
"""CHANGELOG shape: frozen preamble, then either an `## Unreleased` section with
conventional-prefix entries (work in flight) or at least one `## vX.Y.Z` section
(a just-released tree, which legitimately has no Unreleased section)."""
import pathlib
import re
import sys

PREFIXES = ("feat:", "fix:", "refactor:", "test:", "docs:", "chore:", "perf:")
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
    print(f"  changelog ok: released tree, {len(released)} released section(s), no Unreleased")
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
