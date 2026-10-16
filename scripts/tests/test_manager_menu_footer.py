#!/usr/bin/env python3
"""Tests for `scripts/manager-menu-footer.py` — SC2's card-clause instrument.

The instrument answers one question: does a manager pane render a modal footer? Three
properties are graded, and **all three are required** — any one alone is satisfied by a
broken probe:

1. **Positive** — a screen that renders a footer is reported. Without this the probe may
   simply never fire, and `0 footers` reads as a clean fleet forever.
2. **Negative control** — a screen carrying the bare tool name `AskUserQuestion` with no
   footer is NOT reported. ⚠️ This is the load-bearing one: it is the exact string that
   produced a false `FOOTER` report on 2026-10-16 at 15:01:28, and a containment match on
   it passes property 1 while reporting a modal on every busy pane.
3. **Anchoring** — a token appearing MID-line is content, not a render. Measured
   2026-10-03: a containment grep returned 1 on a pane holding no modal at all, the hit
   being a task page's own sentence rendered in the viewport.

⚠️ **The transport half is graded too, and it is the half that fails silently.** A failed
pane read must be `None`, never `False`: `False` asserts "this pane holds no modal" from a
read that never happened, which is the false-green this instrument exists to avoid.

Run: python3 -m unittest discover -s scripts/tests
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
SCRIPT = os.path.join(REPO, "scripts", "manager-menu-footer.py")


def _load():
    spec = importlib.util.spec_from_file_location("manager_menu_footer", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


footer = _load()

# The two anchors `who-needs-my-help.py` already uses, plus the variants this task names.
RENDERED_FOOTERS = (
    "Enter to select · ↑/↓ to navigate · Esc to cancel",
    "Esc to cancel · Tab to amend",
    "Enter to select",
    "Esc to cancel",
    "Tab to toggle",
)

# ⚠️ The falsification fixtures — screens a naive probe reports as gated.
NOT_FOOTERS = (
    ("bare tool name in the statusline",
     ["⏵⏵ auto mode on · AskUserQuestion · esc to interrupt"]),
    ("prose about a modal, mid-line",
     ["the runbook says Enter to select is the question-modal anchor"]),
    ("the phrase inside a quoted sentence",
     ['the worker asked: "Esc to cancel" is what the footer shows']),
    ("empty screen", []),
    ("ordinary working pane",
     ["Reading files…", "⏵⏵ auto mode on · Edit · esc to interrupt"]),
)


class TestFooterDetector(unittest.TestCase):
    def test_positive_every_rendered_footer_shape_is_reported(self):
        # ⚠️ One shape per call, so a probe matching one token cannot carry the list.
        for screen in RENDERED_FOOTERS:
            with self.subTest(screen=screen):
                self.assertTrue(footer.footer_in_lines([screen]),
                                f"{screen!r} is a rendered footer and must be reported")

    def test_negative_control_the_bare_tool_name_is_not_a_footer(self):
        # ⚠️ **THE REGRESSION CASE.** `AskUserQuestion` sits in the statusline as the name
        # of the tool in flight, so it is present on panes holding no modal. Measured
        # 2026-10-16 15:01:28: this exact string produced a false FOOTER report while the
        # guard's own record held no deny.
        # ⚠️ Never `assertIn(token, other + token)` — that is true for ANY token, because a
        # string contains itself, so it pins nothing. Assert the literal, then the behaviour.
        self.assertEqual(footer.NEVER_A_TOKEN, "AskUserQuestion",
                         "the negative control must name the exact token that caused the false report")
        self.assertFalse(footer.footer_in_lines([footer.NEVER_A_TOKEN]),
                         "the bare tool name alone must not be reported as a footer")
        for name, lines in NOT_FOOTERS:
            with self.subTest(fixture=name):
                self.assertFalse(footer.footer_in_lines(lines),
                                 f"{name!r} must NOT be reported as a footer")

    def test_anchoring_a_mid_line_token_is_content_not_a_render(self):
        # The 2026-10-03 measurement, pinned: a containment match reports a gate on any
        # pane that merely discusses one.
        self.assertFalse(footer.footer_in_lines(
            ["a diff line: + Enter to select was added to the fixture"]))
        self.assertTrue(footer.footer_in_lines(
            ["context", "Enter to select · ↑/↓ to navigate"]))

    def test_every_token_is_a_literal_footer_fragment_not_a_tool_name(self):
        # A future edit that adds a token must not add the bare tool name back.
        self.assertNotIn(footer.NEVER_A_TOKEN, footer.FOOTER_TOKENS)


class TestTransportContract(unittest.TestCase):
    """`docs/pane-reads.md`: a failed read must never be representable as an empty result."""

    def test_an_unreadable_pane_is_none_not_false(self):
        # ⚠️ The whole point: `False` would assert "no modal" from a read that never
        # happened. A pane id that cannot resolve must produce the THIRD answer.
        self.assertIsNone(footer.pane_lines(-1))
        self.assertIsNone(footer.scan_pane(-1))

    def test_self_test_passes_and_covers_both_directions(self):
        ok, lines = footer.self_test()
        self.assertTrue(ok, "the instrument's own controls must pass:\n" + "\n".join(lines))
        self.assertTrue(any("must be True" in line for line in lines),
                        "the positive control must be asserted")
        self.assertTrue(any("must be False" in line for line in lines),
                        "the negative control must be asserted")


if __name__ == "__main__":
    unittest.main()
