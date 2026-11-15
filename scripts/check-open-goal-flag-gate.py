#!/usr/bin/env python3
"""Both manager branches of `/supervisor:open` gate their SPAWN on the operator's flag.

The goal branch had no flag gate while the topic branch did, so `/supervisor:open --goal`
started a manager for any goal whatever its `flag` — the one path that could still create
the state `commands/topic-drive.md` refuses (`open-unflagged`: *report, never open*).

Three regressions are silent, and each has a sibling form already seen in this repo:

  (a) **The gate is dropped from the goal branch.** A rewrite that copies the branch from
      an older revision restores the ungated spawn, and nothing goes red — the command
      still works, it just opens what the operator never asked for.
  (b) **The gate is moved onto RESUME.** Item 3 is how a lapsed manager comes back. Gating
      it strands a manager whose goal's flag was later cleared — the `stand-down` case
      `topic-drive` § `leave` defines as legitimate. The gate belongs on the OPEN site.
  (c) **The two branches drift apart again.** The topic side is asserted here too, so
      removing its gate is as red as removing the goal's.
  (d) **A refusal falls through.** Item 5 opens the goal's tasks when the manager branch
      "cannot run"; a `goal-unflagged` refusal read as that would open workers on the
      very goal the operator did not flag.
  (e) **The remedy disappears.** The gate script prints how to flag the goal; the prose
      points at that line rather than restating it, so the script is where it is pinned.

⚠️ **What this check does not prove.** It is a prose-shape check over a markdown command
file, because a command file *is* prose — there is no function to call. It proves the text
cannot silently lose the gate, the verdict token or the spawn-only scoping. The behavioural
half is `scripts/goal-open-gate.py`'s own exit code, exercised by
`scripts/tests/test_flag_opens_callers.py` over admitted, agent-set and absent flags.
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

#: Each manager branch: the gate script it must run, and the refusal token it must name.
GATES = {
    "Step 2B": ("goal-open-gate.py", "goal-unflagged"),
    "Step 2C": ("topic-spawn.py", "topic-unflagged"),
}

#: The resume item, which must never carry the gate. Matched on the item's opening words,
#: which both branches share, so one pattern covers them.
RESUME_ITEM = re.compile(r"^3\. \*\*`ABSENT` → RESUME", re.MULTILINE)


def section(text, start, end):
    begin = text.find(start)
    if begin == -1:
        raise SystemExit("check-open-goal-flag-gate: missing heading %r" % start)
    finish = text.find(end, begin)
    if finish == -1:
        raise SystemExit("check-open-goal-flag-gate: missing heading %r" % end)
    return text[begin:finish]


def resume_paragraph(body):
    """The RESUME item, up to the next numbered item or the end of the section."""
    match = RESUME_ITEM.search(body)
    if not match:
        raise SystemExit(
            "check-open-goal-flag-gate: no `ABSENT` → RESUME item found — the branch was "
            "restructured, so this check's scoping is unverified")
    rest = body[match.end():]
    following = re.search(r"^\d+\. ", rest, re.MULTILINE)
    return rest[:following.start()] if following else rest


def main():
    text = COMMAND.read_text(encoding="utf-8")
    failures = []

    bodies = {}
    for name, start, end in SECTIONS:
        bodies[name] = section(text, start, end)

    for name, (script, token) in GATES.items():
        body = bodies[name]
        if script not in body:
            failures.append("%s does not run %s — the SPAWN is ungated" % (name, script))
        if token not in body:
            failures.append("%s does not name the refusal token %r" % (name, token))
        if "flag_provenance" not in body:
            failures.append(
                "%s does not state that provenance is read — a bare `flag: true` written "
                "by an agent would then read as an approval" % name)

    # (d) A goal refusal must STOP, never fall through to item 5's open-the-goal's-tasks
    # fallback — that would turn "the operator did not flag this goal" into "open its
    # workers instead", the very opening the gate exists to refuse.
    goal = bodies["Step 2B"]
    if not re.search(r"goal-unflagged[^\n]*STOP", goal):
        failures.append("Step 2B's goal-unflagged refusal does not STOP")
    fallback = re.search(r"^5\. \*\*Only when the manager branch cannot run\*\*[^\n]*", goal,
                         re.MULTILINE)
    if not fallback or "goal-unflagged" not in fallback.group(0):
        failures.append(
            "Step 2B item 5 does not exclude a goal-unflagged refusal — a refused goal "
            "would fall back to opening its tasks")

    # (e) The remedy the operator needs is printed by the gate itself, so the prose can
    # name it without restating the command. Assert it on the script, where it lives.
    gate_src = (ROOT / "scripts" / "goal-open-gate.py").read_text(encoding="utf-8")
    if "flag it first" not in gate_src:
        failures.append("scripts/goal-open-gate.py no longer prints the how-to-flag remedy")

    for name, _start, _end in SECTIONS:
        resume = resume_paragraph(bodies[name])
        if "open-gate.py" in resume or "--topic-open" in resume:
            failures.append(
                "%s gates its RESUME item — a lapsed manager on a goal whose flag was "
                "cleared could never come back" % name)

    if failures:
        print("check-open-goal-flag-gate: FAILED")
        for failure in failures:
            print("  - %s" % failure)
        return 1

    print("  both manager branches gate their spawn on flag_admits(), resume untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
