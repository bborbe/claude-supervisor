#!/usr/bin/env python3
"""The round's hand-back gate must be exit-code-faithful, and this asserts it.

`commands/steps/round-dispatch.md` tells the manager to run
`check-handback-size.py --transcript <the delegate's transcript>` and branch on its exit code.
The rule lived only in prose, and the one live invocation measured on 2026-11-20 read:

    python3 "$P/check-handback-size.py" --transcript … 2>&1 | tail -5; echo "size_exit=$?"

`$?` after a pipeline is the **last stage's** status, not the script's — so a real exit 1
(`handback-size FAILED: no fence`) was logged as `size_exit=0` and the manager read the check
as passing. The gate never fired again. This is the same "a rule that lives only in prose"
defect the size check itself exists to close, one layer up: the instruction said *branch on the
exit code*, and the shape it was copied in could not carry one.

Two failures are refused, because each is a way for the gate to look present and measure nothing:

  - the invocation is **piped** before the exit code is read — `$?` then reports the pipe;
  - the exit code is not captured into `gate_rc` on the **next** line — a missing capture is
    indistinguishable from an unread one at the call site;
  - the invocation is **absent** entirely — a deleted gate is not a passing gate.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STEP = REPO / "commands" / "steps" / "round-dispatch.md"

GATE = "check-handback-size.py"
CAPTURE = re.compile(r"^\s*gate_rc=\$\?\s*$")


def logical_lines(text: str) -> list[str]:
    """Join backslash-continued lines into single logical commands, preserving order."""
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def check(text: str) -> list[str]:
    lines = logical_lines(text)
    idx = [i for i, ln in enumerate(lines) if GATE in ln]
    if not idx:
        return [
            f"{STEP.relative_to(REPO)}: no `{GATE}` invocation — the round's gate is gone. "
            "A deleted gate is not a passing gate."
        ]
    problems: list[str] = []
    for i in idx:
        cmd = lines[i]
        if "|" in cmd:
            problems.append(
                f"line {i + 1}: the gate is piped before its exit code is read — "
                f"`$?` would report the pipe's last stage, not {GATE}. "
                "Run it unpiped and capture `gate_rc=$?` on the next line."
            )
            continue
        if i + 1 >= len(lines) or not CAPTURE.match(lines[i + 1]):
            problems.append(
                f"line {i + 1}: no `gate_rc=$?` on the line after the invocation — "
                "the exit code is not captured, so the call site cannot branch on it."
            )
    return problems


def main() -> int:
    if not STEP.exists():
        print(f"round-gate-exit-code FAILED: {STEP.relative_to(REPO)}: missing", file=sys.stderr)
        return 1
    problems = check(STEP.read_text(encoding="utf-8"))
    if problems:
        print("round-gate-exit-code FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("round-gate-exit-code ok: the gate is unpiped and its exit code is captured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
