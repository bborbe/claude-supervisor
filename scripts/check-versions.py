#!/usr/bin/env python3
"""Verify the four version strings agree: plugin.json, marketplace metadata.version,
marketplace plugins[0].version, and the CHANGELOG's top released heading.

The changelog heading is read as the FIRST `## ` heading, not the first `## vX.Y.Z`
anywhere: a released entry routinely cites an older version in its own prose (v0.23.2
names v0.23.1), so a search for the first version-shaped heading picks up a mention from
inside the body and compares the wrong number. The top heading is `## Unreleased` between
releases and `## vX.Y.Z` at release time; only the latter is a version to compare.

A pre-first-release repo has no `## vX.Y.Z` heading yet, so the changelog string is
required only once one exists.
"""
import json
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parent.parent
changelog = (root / "CHANGELOG.md").read_text()
top = re.search(r"^## (.+)$", changelog, re.M)
released = re.match(r"v(\d+\.\d+\.\d+)$", top.group(1).strip()) if top else None
plugin = json.load(open(root / ".claude-plugin/plugin.json"))["version"]
market = json.load(open(root / ".claude-plugin/marketplace.json"))
values = {
    "plugin.json": plugin,
    "marketplace metadata.version": market["metadata"]["version"],
    "marketplace plugins[0].version": market["plugins"][0]["version"],
}
if released:
    values["CHANGELOG.md"] = released.group(1)
bad = {k: v for k, v in values.items() if v != plugin}
if bad:
    print(f"VERSION MISMATCH: {values}", file=sys.stderr)
    sys.exit(1)
suffix = "" if released else "  (top heading is not a released version — no bump yet)"
print(f"  versions aligned: {plugin}{suffix}")
