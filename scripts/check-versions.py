#!/usr/bin/env python3
"""Verify the four version strings agree: plugin.json, marketplace metadata.version,
marketplace plugins[0].version, and the CHANGELOG's top released heading.

A pre-first-release repo has no `## vX.Y.Z` heading yet, so the changelog string is
required only once one exists.
"""
import json
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parent.parent
changelog = (root / "CHANGELOG.md").read_text()
released = re.search(r"^## v(\d+\.\d+\.\d+)", changelog, re.M)
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
suffix = "" if released else "  (pre-first-release: no ## vX.Y.Z yet)"
print(f"  versions aligned: {plugin}{suffix}")
