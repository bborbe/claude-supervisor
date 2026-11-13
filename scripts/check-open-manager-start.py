#!/usr/bin/env python3
"""`/supervisor:open` starts a manager's loop itself — the shape that makes it safe is pinned.

The GOAL and TOPIC branches spawn a manager and then type its start commands into the pane
they just created. That is the same act the WORKER path has always performed, and it is
bounded by the same rule: a `send-text` is only ever aimed at a pane THIS command just
spawned, while it is still idle.

Three things can regress silently, and each has been seen in a sibling form:

  (a) **The instruction comes back.** A rewrite that copies the branch from an older
      revision restores "tell the operator to run …", which costs the manual paste this
      change removes. The superseded wording is therefore a literal denial.
  (b) **The wait falls through.** A `for`-loop that breaks on success and then proceeds
      anyway types into exactly the panes `pane-wake-guard.py` refused — an operator ask,
      a modal, a non-empty composer. The block must exit on exhaustion.
  (c) **A literal pane id appears.** Every target must be the just-spawned pane variable;
      a hard-coded id is a send into a session this command does not own.

⚠️ **What this check does not prove.** It is a prose-shape check over a markdown command
file, because a command file *is* prose — there is no function to call and no return value
to thread. It proves the text cannot silently lose the send, the fail-closed wait or the
prohibition; the behavioural half is one real `/supervisor:open "<goal>"` observed running
its loop, which no grep can fake.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMMAND = ROOT / "commands" / "open.md"

#: Section boundaries. A renamed heading fails loudly instead of scanning an empty string
#: and reporting a clean branch — the failure mode that makes a prose check worthless.
SECTIONS = (
    ("Step 2B", "## Step 2B — Goal branch", "## Step 2C — Topic branch"),
    ("Step 2C", "## Step 2C — Topic branch", "## Step 3 — Spawn the tab"),
)

#: (a) — the superseded wording, pinned as a denial. Presence means the fix was reverted,
#: half-applied, or copied back from an older revision.
DENIED = ("tell the operator to run",)

#: The start command each branch must carry, as its own `send-text`.
SEND_COMMANDS = {
    "Step 2B": '/vault-cli:work-on-goal "<goal>"',
    "Step 2C": '/supervisor:goal-loop "<topic>"',
}

#: The GOAL branch sends a second command; the topic branch sends one.
SECOND_COMMAND = {"Step 2B": '/supervisor:goal-loop "<goal>"'}

#: The readiness test, and the fail-closed shape that must follow it.
GUARD = "pane-wake-guard.py"
FAIL_CLOSED = "exit 1"

#: `seq` is not on stock macOS (`jot` is), so a loop built on it dies on the operator's
#: machine while reading as portable. The block uses a POSIX counter instead.
DENIED_TOKENS = ("$(seq ",)

#: The prohibition the change must not widen. Pinned whole — a paraphrase is a different
#: rule, and this one is the whole safety argument for the feature.
PROHIBITION = (
    "Only ever send this into a pane THIS command just spawned, while it is still idle"
)

#: Every `--pane-id` target must be a variable, never a literal id.
LITERAL_PANE_ID = re.compile(r"--pane-id\s+[\"']?(\d+)[\"']?")


def section(text, start, end):
    """The text between two headings, or None when a heading is missing."""
    i = text.find(start)
    j = text.find(end)
    if i < 0 or j < 0 or j <= i:
        return None
    return text[i + len(start):j]


def main():
    problems = []
    if not COMMAND.is_file():
        print(f"open-manager-start FAILED: {COMMAND} is missing")
        return 1
    text = COMMAND.read_text(encoding="utf-8")

    for phrase in DENIED:
        if phrase in text:
            problems.append(
                f"the superseded instruction is back: {phrase!r} — the operator must not "
                f"be asked to paste a manager's start command"
            )

    for token in DENIED_TOKENS:
        if token in text:
            problems.append(
                f"{token!r} is used — `seq` is absent on stock macOS; use a POSIX counter"
            )

    if PROHIBITION not in text:
        problems.append(
            "the pane-prohibition sentence is missing or paraphrased — it is the whole "
            "safety argument for sending into a manager pane"
        )

    for literal in LITERAL_PANE_ID.findall(text):
        problems.append(
            f"--pane-id carries a literal id ({literal}) — every target must be the "
            f"just-spawned pane variable"
        )

    for name, start, end in SECTIONS:
        body = section(text, start, end)
        if body is None:
            problems.append(f"{name}: section heading missing — cannot check the branch")
            continue

        command = SEND_COMMANDS[name]
        if "wezterm cli send-text --pane-id" not in body or command not in body:
            problems.append(
                f"{name}: no `wezterm cli send-text` carrying {command} — the branch "
                f"must type its own start command"
            )
        if "$'\\r'" not in body:
            problems.append(
                f"{name}: no separate `$'\\r'` submit — a trailing return on the text "
                f"does not submit reliably"
            )
        if GUARD not in body:
            problems.append(
                f"{name}: readiness is not read from {GUARD} — a bare glyph grep reads a "
                f"mid-turn composer as ready and a dim suggestion as typed text"
            )
        if FAIL_CLOSED not in body:
            problems.append(
                f"{name}: the wait is not fail-closed — on exhaustion it must exit, never "
                f"fall through into a pane the guard refused"
            )

        second = SECOND_COMMAND.get(name)
        if second and second not in body:
            problems.append(
                f"{name}: {second} is not sent — the loop is what the open exists to start"
            )

    if problems:
        print(f"open-manager-start FAILED: {len(problems)} problem(s)")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(
        "open-manager-start ok: both manager branches send their start commands with a "
        "fail-closed guard-based wait, no operator paste, no literal pane id, and the "
        "pane-prohibition intact"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
