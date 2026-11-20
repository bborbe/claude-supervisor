#!/usr/bin/env python3
"""`/supervisor:open` is a THIN CALLER: it must never hand-run a spawn itself.

Rewritten 2026-11-20. The file used to guard the flag/ceiling/`--hand-run` semantics of
the spawn branches open.md ran itself. Those branches are gone: every routing decision —
role→mode, the answer-path probe, the headless resume, the tab window, the caps — moved
into the wrapper's open endpoint (`POST /api/1.0/open` and the `open_rows` MCP tool).
What remains here is the guard for the defect class that move exists to end.

The 2026-11-18 incident: a session hand-rolled `wezterm cli spawn --window-id 2 --resume`
for 13 `role: agent` rows, opening them as tabs in the operator's Hold window. A command
file that carries its own spawn instructions is how that gets re-derived — differently
each time. So this check fails when open.md regains a spawn path of its own, and fails
when it loses the endpoint call that replaced them.

⚠️ **What this check does not prove.** It is a text check on one file. It cannot see a
spawn assembled from a variable, and it says nothing about a session that types the
spawn itself — that is `scripts/spawn-route-guard.py`'s job (the PreToolUse hook).
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMMAND = ROOT / "commands" / "open.md"

#: Every way this file used to spawn. Any reappearance is the regression.
FORBIDDEN = (
    ("wezterm cli spawn", "a hand-run tab spawn — the endpoint picks the window"),
    ("spawn_agent", "a hand-run headless spawn — the endpoint owns the probe and the caps"),
    ("--hand-run", "the manager/topic spawn recipe — the endpoint owns the ceiling"),
    ("topic-spawn.py", "a hand-run topic manager open"),
    ("manager-launcher.py", "a hand-run goal manager open"),
    ('"Agents"', "a window that does not exist — removed from ~/.config/wezterm 2026-11-12"),
)

#: The call that replaced them. At least one must be present.
REQUIRED = (
    "api/1.0/open",
    "open_rows",
)


def main():
    if not COMMAND.is_file():
        print("open-hand-run FAILED: %s is missing" % COMMAND)
        return 1
    text = COMMAND.read_text(encoding="utf-8")
    problems = []
    for needle, why in FORBIDDEN:
        if needle in text:
            problems.append("carries `%s` — %s" % (needle, why))
    if not any(needle in text for needle in REQUIRED):
        problems.append(
            "names neither `api/1.0/open` nor `open_rows` — the command has no route "
            "to the endpoint, so whatever it does instead is a hand-run"
        )
    if problems:
        print("open-hand-run FAILED: %d problem(s)" % len(problems))
        for problem in problems:
            print("  - commands/open.md: %s" % problem)
        return 1
    print("open-hand-run OK: commands/open.md is a thin caller of the open endpoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
