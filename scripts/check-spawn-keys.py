#!/usr/bin/env python3
"""`KNOWN_SPAWN_KEYS` in `server/spawn-mode.mjs` must equal the key set the Go wrapper
actually reads.

The wrapper is the program that resolves and validates the spawn caps, so its
`pkg/spawn_axes.go` is the only authority on what a `spawn` block may carry. The list
in `spawn-mode.mjs` is a copy that exists solely so an unknown key can be warned
about — and it went stale exactly as copies do: through v0.283.1 it omitted
`maxSessions`, `maxHeadless`, `maxInteractive`, `maxTopicManagers` and
`maxGoalManagers`, so a config using any of them was told the key was ignored while
the wrapper honoured it. A warning that argues an operator out of a working
configuration is worse than no warning at all.

This check derives the expected set from the Go source rather than restating it:

  - the fourteen axis keys, from `var axisKeys` in `pkg/spawn_axes.go`
  - the eight pre-rename spellings, from `var renamedSpawnKeys` in
    `pkg/spawn_limits.go` — listed because the wrapper still PARSES them, so warning
    about one would be the same false-warning failure
  - `mode` and `maxStartsPerMinute`, which are not cap axes and keep their names

**Where it runs.** `make precommit` runs it locally, where a sibling `claude-wrapper`
checkout exists — that is the gate every change passes before a PR. CI has no such
checkout, so there the script prints a skip and exits 0; it cannot prove the lists
agree without the source. The skip is named rather than silent, because a check that
looks green while measuring nothing is the failure mode this repo keeps hitting.

**What this does not prove.** That the wrapper and this server agree on a key's
*meaning* — only on its spelling. Set-equality of names is all a copy can be held to.
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPAWN_MODE = ROOT / "server" / "spawn-mode.mjs"

# Keys that are not cap axes, so they live in neither Go table.
NON_AXIS_KEYS = ("mode", "maxStartsPerMinute")

PAIR = re.compile(r'\{"([A-Za-z0-9]+)",\s*"([A-Za-z0-9]+)"\}')
JS_LIST = re.compile(r"const KNOWN_SPAWN_KEYS = \[(.*?)\]", re.S)
JS_STRING = re.compile(r"'([^']+)'")


def wrapper_root():
    """The claude-wrapper checkout, or None when this machine has none."""
    for candidate in (
        os.environ.get("CLAUDE_WRAPPER_SRC"),
        pathlib.Path.home() / "Documents" / "workspaces" / "claude-wrapper",
        ROOT.parent / "claude-wrapper",
    ):
        if not candidate:
            continue
        path = pathlib.Path(candidate)
        if (path / "pkg" / "spawn_axes.go").is_file():
            return path
    return None


def block(text, marker):
    """The source from `marker` to the first closing brace at column zero."""
    start = text.find(marker)
    if start < 0:
        return None
    end = text.find("\n}", start)
    return text[start:end] if end >= 0 else text[start:]


def derived_keys(root):
    axes_src = (root / "pkg" / "spawn_axes.go").read_text(encoding="utf-8")
    limits_src = (root / "pkg" / "spawn_limits.go").read_text(encoding="utf-8")

    axes_block = block(axes_src, "var axisKeys")
    if axes_block is None:
        raise SystemExit("  spawn-keys FAIL: pkg/spawn_axes.go has no `var axisKeys` block")

    renamed_block = block(limits_src, "var renamedSpawnKeys")
    if renamed_block is None:
        raise SystemExit("  spawn-keys FAIL: pkg/spawn_limits.go has no `var renamedSpawnKeys` block")

    axis_keys = set()
    for soft, hard in PAIR.findall(axes_block):
        axis_keys.update((soft, hard))
    if len(axis_keys) != 14:
        raise SystemExit(
            f"  spawn-keys FAIL: pkg/spawn_axes.go yielded {len(axis_keys)} axis keys, expected 14 — "
            f"the parse no longer matches the source. Found: {sorted(axis_keys)}"
        )

    legacy_keys = set()
    for current, legacy in PAIR.findall(renamed_block):
        legacy_keys.add(legacy)
    if not legacy_keys:
        raise SystemExit("  spawn-keys FAIL: pkg/spawn_limits.go yielded no legacy keys")

    return axis_keys, legacy_keys


def js_keys():
    text = SPAWN_MODE.read_text(encoding="utf-8")
    match = JS_LIST.search(text)
    if match is None:
        raise SystemExit("  spawn-keys FAIL: server/spawn-mode.mjs has no `const KNOWN_SPAWN_KEYS = [`")
    return JS_STRING.findall(match.group(1))


def main():
    root = wrapper_root()
    if root is None:
        print(
            "  spawn-keys SKIP: no claude-wrapper checkout (set CLAUDE_WRAPPER_SRC to enable). "
            "CI has none, so the two lists are not compared here.",
            file=sys.stderr,
        )
        return

    axis_keys, legacy_keys = derived_keys(root)
    expected = axis_keys | legacy_keys | set(NON_AXIS_KEYS)
    actual = js_keys()

    duplicates = sorted({k for k in actual if actual.count(k) > 1})
    missing = sorted(expected - set(actual))
    extra = sorted(set(actual) - expected)

    failures = []
    if duplicates:
        failures.append(f"server/spawn-mode.mjs lists these keys more than once: {duplicates}")
    if missing:
        failures.append(
            f"server/spawn-mode.mjs is missing keys the wrapper reads: {missing} — a config using one "
            f"would be told it is ignored"
        )
    if extra:
        failures.append(
            f"server/spawn-mode.mjs lists keys the wrapper does not read: {extra} — an operator would "
            f"never be warned about a real typo"
        )

    if failures:
        for f in failures:
            print(f"  spawn-keys FAIL: {f}", file=sys.stderr)
        sys.exit(1)

    print(
        f"  spawn-keys ok: {len(expected)} keys — {len(axis_keys)} axis, {len(legacy_keys)} pre-rename, "
        f"{len(NON_AXIS_KEYS)} non-axis — derived from {root.name}/pkg"
    )


if __name__ == "__main__":
    main()
