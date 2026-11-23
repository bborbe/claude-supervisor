#!/usr/bin/env python3
"""Tests for scripts/topic-candidates.py.

The defect these pin: the leg's population was filtered on `claude_session_id`, the field
the stood-down handover clears, so a handed-over topic page dropped out and no replacement
manager was ever opened for it. These tests hand the selector a topic page carrying **no**
session id and assert it is still a candidate — the case the pre-fix heredoc dropped.

The verdicts are injected rather than manufactured: a fabricated session id answers
`UNKNOWN`, never the `no-session` a handed-over page reads, so a test that faked one would
be asserting a state the instrument does not produce.
"""

import importlib.util
import pathlib
import tempfile
import types
import unittest

_HERE = pathlib.Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location(
        "topic_candidates", str(_HERE.parent / "topic-candidates.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOPIC_PAGE = """---
page_type: topic
status: in_progress
phase: execution
{session}---
Tags: [[Topic]]
"""


def verdict(reason, verdict_word="not-owner"):
    return types.SimpleNamespace(verdict=verdict_word, reason=reason)


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = pathlib.Path(self._tmp.name)
        (self.vault / "23 Topics").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def topic(self, name, session=""):
        """Write a topic page; `session` is a raw frontmatter line, empty for none."""
        line = f"claude_session_id: {session}\n" if session else ""
        path = self.vault / "23 Topics" / f"{name}.md"
        path.write_text(TOPIC_PAGE.format(session=line), encoding="utf-8")
        return path

    def select(self, reason, verdict_word="not-owner", ladder=None):
        return self.mod.candidates(
            str(self.vault), "23 Topics", "ladder.py",
            of_page=lambda fm, subject, branch="goal", vault="": verdict(reason, verdict_word),
            ladder=ladder or (lambda *a: "open\n  / lapsed-managers"),
        )

    # -- the defect ---------------------------------------------------------

    def test_handed_over_page_is_a_candidate(self):
        """No session id + `no-session` is exactly what a handed-over page reads."""
        self.topic("Handed Over Topic")
        rows = self.select("no-session")
        self.assertEqual([name for name, _ in rows], ["Handed Over Topic"])

    def test_the_resolver_is_asked_about_a_page_carrying_no_session(self):
        """The field is never a prefilter — the resolver sees the page either way."""
        self.topic("Handed Over Topic")
        asked = []

        def of_page(fm, subject, branch="goal", vault=""):
            asked.append(subject)
            return verdict("no-session")

        self.mod.candidates(str(self.vault), "23 Topics", "ladder.py",
                            of_page=of_page, ladder=lambda *a: "open")
        self.assertIn("Handed Over Topic", asked)

    # -- the non-regression -------------------------------------------------

    def test_stopped_topic_is_excluded(self):
        """A deliberate stand-down is never re-opened — it must not even be a candidate."""
        self.topic("Stopped Topic", session="11111111-1111-4111-8111-111111111111")
        self.assertEqual(self.select("loop-stopped"), [])

    # -- behaviour preserved ------------------------------------------------

    def test_live_owner_is_still_listed(self):
        """`jump` rows exist so the operator is told where a live manager is."""
        self.topic("Live Topic", session="22222222-2222-4222-8222-222222222222")
        rows = self.select("live-armed", "owner")
        self.assertEqual([name for name, _ in rows], ["Live Topic"])

    def test_dotfiles_are_skipped(self):
        self.topic(".hidden")
        self.assertEqual(self.select("no-session"), [])

    def test_ladder_head_is_flattened_onto_one_tabbed_line(self):
        self.topic("Some Topic")
        rows = self.select("no-session", ladder=lambda *a: "open\n  / lapsed-managers")
        self.assertEqual(rows, [("Some Topic", "open\n  / lapsed-managers")])


if __name__ == "__main__":
    unittest.main()
