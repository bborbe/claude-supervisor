#!/usr/bin/env python3
"""`/supervisor:open` is the operator's own open: it never refuses a goal or topic on its flag.

Operator ruling 2026-11-15: an unflagged task, goal or topic never opens AUTOMATICALLY, but
the operator's own `/supervisor:open` opens what they name. The automatic fresh opens are
gated upstream of the command — `topic-keepalive.py` prints `open-unflagged` instead of
`open`, and `topic-spawn.py --goal` refuses `goal-unflagged` — so a flag check inside the
command only ever fires on the hand-run, refusing the one open the rule allows. Until this
change the topic branch did exactly that: `topic-spawn.py --topic-open` refused
`topic-unflagged`, so the operator could not open a manager on an unflagged topic by hand.

Three regressions are silent:

  (a) **A flag refusal returns to the command.** A rewrite copying the topic branch from an
      older revision restores `topic-unflagged` as a STOP, or a new `goal-unflagged` STOP
      appears in the goal branch. The command still works; it just refuses the operator.
  (b) **The ceiling is dropped with the flag.** `--hand-run` skips the flag and nothing
      else; a call without `--topic-open` would skip the manager cap too.
  (c) **An upstream gate disappears.** If `topic-keepalive.py` stopped printing
      `open-unflagged`, or `topic-spawn.py --goal` stopped refusing `goal-unflagged`, a
      round would hand `/supervisor:open` an unflagged subject and the command — which no
      longer checks — would open it automatically.

⚠️ **What this check does not prove.** It is a prose-and-source shape check, because a
command file is prose. The behaviour of `--hand-run` is tested in
`scripts/tests/test_topic_spawn.py`; the upstream verdicts in
`scripts/tests/test_topic_keepalive.py` and `test_topic_spawn.py`.
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

#: The flag refusals that must never be a STOP inside the command.
FLAG_REFUSALS = ("goal-unflagged", "topic-unflagged")

#: (c) The upstream gates, as `(file, literal that must be present)`.
UPSTREAM = (
    ("scripts/topic-keepalive.py", 'print("open-unflagged")'),
    ("scripts/topic-spawn.py", 'REFUSE_UNFLAGGED = "goal-unflagged"'),
)


def section(text, start, end):
    begin = text.find(start)
    if begin == -1:
        raise SystemExit("check-open-hand-run: missing heading %r" % start)
    finish = text.find(end, begin)
    if finish == -1:
        raise SystemExit("check-open-hand-run: missing heading %r" % end)
    return text[begin:finish]


def main():
    text = COMMAND.read_text(encoding="utf-8")
    failures = []

    for name, start, end in SECTIONS:
        body = section(text, start, end)
        for token in FLAG_REFUSALS:
            if re.search(r"`%s`[^\n]{0,80}STOP" % re.escape(token), body):
                failures.append(
                    "%s STOPs on %s — the command refuses the operator's own open" % (name, token))

    topic = section(text, SECTIONS[1][1], SECTIONS[1][2])
    calls = re.findall(r"topic-spawn\.py[^`\n]*--topic-open[^`\n]*", topic)
    if not calls:
        failures.append("Step 2C no longer runs topic-spawn.py --topic-open — the manager "
                        "ceiling is unchecked on a topic open")
    for call in calls:
        if "--hand-run" not in call:
            failures.append("Step 2C runs --topic-open without --hand-run — it refuses an "
                            "unflagged topic the operator named: %s" % call.strip())

    for rel, literal in UPSTREAM:
        source = (ROOT / rel).read_text(encoding="utf-8")
        if literal not in source:
            failures.append(
                "%s no longer carries %s — an automatic round could hand /supervisor:open an "
                "unflagged subject, and the command no longer checks" % (rel, literal))

    if failures:
        print("check-open-hand-run: FAILED")
        for failure in failures:
            print("  - %s" % failure)
        return 1

    print("  /supervisor:open opens what the operator names; automatic opens stay gated "
          "upstream; the manager ceiling still binds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
