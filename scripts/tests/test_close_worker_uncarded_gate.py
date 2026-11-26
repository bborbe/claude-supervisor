#!/usr/bin/env python3
"""Tests for the uncarded-operator-gate refusal in scripts/close-worker.py.

The defect this covers, measured 2026-11-26: a headless worker reached a
production-touching step, stopped, and ended without posting an attention card or setting
a `defer_date`. The generic `task-not-terminal` refusal already blocked the close, but its
message names only "a terminal status or a future defer_date" — so nothing told the worker
that the missing thing was the card, and the row sat invisible for ~25 h.

The five fixtures below are the criterion, not decoration. Each one kills a different
lazy guard:

  (a) the REAL 2026-11-26 wording, no card and no defer → refused
  (b) a declared `⚠️ production-touching` line, no card and no defer → refused
  (c) a gate with a future `defer_date` → allowed
  (d) no gate at all → allowed
  (e) a gate with a recorded 32-hex card id → allowed

(c), (d) and (e) are the negative controls: a guard that greps only for the marker passes
(b) and fails (a); one that refuses everything fails (c), (d) and (e); one that checks only
`defer_date` fails (e).

Run: python3 -m unittest discover -s scripts/tests -v
"""

import datetime
import importlib.util
import os
import sys
import tempfile
import unittest

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "close_worker_gate", os.path.join(_SCRIPTS, "close-worker.py")
)
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

TODAY = datetime.date(2026, 11, 26)
SID = "cccccccc-3333-4333-8333-333333333333"
CARD = "beb1c931e202057fe343f841ff2ff9a4"

# The 2026-11-26 wording, with the phrase the guard matches kept verbatim. The row's own
# text named a machine-local workspace path; it is dropped here because the arm matches on
# the phrase, not the path, and a local path in a shipped fixture is noise.
REAL_WORDING = (
    "Remaining: the launchd-reload step, which restarts the controller and is "
    "production-touching, so it is held for the operator's explicit go-ahead "
    "rather than run."
)


def page(*, tasks=(), progress=(), extra_front="", status="in_progress", defer=None):
    """Write a task page and return its path."""
    front = [f"status: {status}", f"claude_session_id: {SID}"]
    if defer:
        front.append(f"defer_date: {defer}")
    if extra_front:
        front.append(extra_front)
    body = ["# Success Criteria", "", "- [ ] one"]
    if tasks:
        body += ["", "# Tasks", ""] + list(tasks)
    if progress:
        body += ["", "# Progress", ""] + list(progress)
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    )
    handle.write("---\n" + "\n".join(front) + "\n---\n\n" + "\n".join(body) + "\n")
    handle.close()
    return handle.name


class UncardedGateTest(unittest.TestCase):
    def tearDown(self):
        for path in getattr(self, "_paths", []):
            try:
                os.unlink(path)
            except OSError:
                pass

    def _task(self, path):
        self._paths = getattr(self, "_paths", []) + [path]
        return (path, cw.frontmatter(path), False)

    def _verdict(self, path):
        return cw.task_verdict(self._task(path), TODAY)

    # (a) the real wording, no card and no defer → refused, naming the card
    def test_real_wording_without_card_is_refused(self):
        verdict = self._verdict(page(progress=["- 2026-11-25 10:22 — " + REAL_WORDING]))
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], "gate-uncarded")
        self.assertIn("card id", verdict[1])

    # (b) a declared marker line, no card and no defer → refused
    def test_declared_marker_without_card_is_refused(self):
        verdict = self._verdict(
            page(tasks=["- [ ] ⚠️ production-touching — `launchctl kickstart -k …`"])
        )
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], "gate-uncarded")

    # (c) gate + future defer_date → allowed (the task verdict permits the close)
    def test_gate_with_future_defer_is_allowed(self):
        verdict = self._verdict(
            page(
                tasks=["- [ ] ⚠️ production-touching — `launchctl kickstart -k …`"],
                defer="2026-12-01",
            )
        )
        self.assertIsNone(verdict)

    # (d) no gate at all → still refused, but by the generic rule, not this one
    def test_no_gate_falls_back_to_the_generic_refusal(self):
        verdict = self._verdict(page(progress=["- 2026-11-25 10:22 — wrote something"]))
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], "task-not-terminal")

    # (e) gate + recorded card id → the SPECIFIC refusal does not fire.
    #
    # Not "allowed": the task is still `in_progress`, so the generic `task-not-terminal`
    # still blocks the close — correctly, a card records the wait, it does not finish the
    # work. What the card buys is that the refusal stops claiming the gate is uncarded.
    def test_gate_with_recorded_card_suppresses_the_gate_refusal(self):
        verdict = self._verdict(
            page(
                tasks=["- [ ] ⚠️ production-touching — `launchctl kickstart -k …`"],
                progress=[f"- 2026-11-26 11:28 — carded on card `{CARD}`"],
            )
        )
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], "task-not-terminal")

    # A `card_id:` frontmatter key counts too — the vault writes both shapes.
    def test_gate_with_card_id_frontmatter_suppresses_the_gate_refusal(self):
        verdict = self._verdict(
            page(
                tasks=["- [ ] ⚠️ production-touching — `launchctl kickstart -k …`"],
                extra_front=f"card_id: {CARD}",
            )
        )
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], "task-not-terminal")

    # A terminal task closes regardless: the gate check must not resurrect it.
    def test_terminal_task_with_a_gate_is_allowed(self):
        verdict = self._verdict(
            page(
                tasks=["- [ ] ⚠️ production-touching — `launchctl kickstart -k …`"],
                status="completed",
            )
        )
        self.assertIsNone(verdict)


class GateDetectionTest(unittest.TestCase):
    def test_last_progress_line_ignores_trailing_blank_lines(self):
        body = "# Progress\n\n- first\n- second\n\n\n"
        self.assertEqual(cw.last_progress_line(body), "- second")

    def test_last_progress_line_stops_at_the_next_heading(self):
        body = "# Progress\n\n- last\n\n# Other\n\n- not this\n"
        self.assertEqual(cw.last_progress_line(body), "- last")

    # Both heading levels are in use in the vault: a structured task writes `# Progress`,
    # a page following the no-H1 rule writes `## Progress`. Matching only the first left
    # the phrase arm permanently dark on the second — a fallback that never fires reads
    # exactly like one that found nothing.
    def test_h2_progress_heading_is_recognised(self):
        body = "## Progress\n\n- first\n- last\n"
        self.assertEqual(cw.last_progress_line(body), "- last")

    def test_h2_progress_stops_at_the_next_h2(self):
        body = "## Progress\n\n- last\n\n## Other\n\n- not this\n"
        self.assertEqual(cw.last_progress_line(body), "- last")

    def test_absent_progress_section_is_none(self):
        self.assertIsNone(cw.last_progress_line("# Success Criteria\n\n- [ ] one\n"))

    def test_phrase_arm_does_not_match_the_primary_arm_text(self):
        # The marker lives in # Tasks, the phrase set in # Progress; neither arm reads the
        # other's section, so a prose mention in the wrong place declares nothing.
        self.assertFalse(cw.gate_declared("- [ ] something is production-touching\n"))

    def test_checked_box_is_not_a_declared_gate(self):
        self.assertFalse(cw.gate_declared("- [x] ⚠️ production-touching — done\n"))


class LiveTaskRefusalsTest(unittest.TestCase):
    """The shared token set, and the consumer that must read it rather than a literal."""

    def test_both_live_task_tokens_are_in_the_set(self):
        self.assertEqual(
            set(cw.LIVE_TASK_REFUSALS), {"task-not-terminal", "gate-uncarded"}
        )

    def test_gate_uncarded_is_a_live_task_refusal(self):
        # `reap-idle-headless.py` treats every token outside this set as
        # `task-unresolved`; a gate-uncarded row that landed there would drop out of the
        # reap verb's population and be misreported.
        self.assertIn("gate-uncarded", cw.LIVE_TASK_REFUSALS)

    def test_reap_reads_the_set_not_a_literal(self):
        import re

        source = os.path.join(_SCRIPTS, "reap-idle-headless.py")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("close_worker.LIVE_TASK_REFUSALS", text)
        # A literal comparison here is the regression: it silently narrows the population.
        self.assertNotRegex(text, re.escape('bad[0] != "task-not-terminal"'))


if __name__ == "__main__":
    sys.exit(unittest.main())
