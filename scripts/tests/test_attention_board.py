#!/usr/bin/env python3
"""Tests for scripts/attention-board.py.

The manager commands print this line where a per-gate jump link used to go, and
the number is meant to equal the board's own card count. So the property worth
pinning is that the number counts **open** cards and nothing else: the board
renders every answered record as a dimmed card too, so a count that took both
would be off by ~1600 on a live board, would never match the page, and would look
like a working count while doing it.

Second property: the count is over **rendered rows**, not raw text. The board's
script block names `data-item-id` on every row it reads, so the served page
carries more occurrences of the string than it has rows (measured 2026-10-24: 92
against 82). A raw-text count is inflated by 12% and reads as a pass — the one
failure direction this line exists to prevent.

Third property: every way the board can be unreachable — refused, timed out,
non-200 — takes the SAME branch, one line and one exit code. The caller has
nothing different to do about any of them, and a branch that distinguished them
would invite a caller to try. The reason still goes to stderr, so stdout stays the
single literal the fallback contract names.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import os
import unittest
import urllib.error
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ab = _load("attention_board", "attention-board.py")

# Two open cards of DIFFERENT risk classes and one answered. Three properties ride
# on this shape, and each is a bug the fixture is the only guard against:
#
# - the count is 2, so a count that took the dimmed row too (3) fails;
# - the open cards differ in risk class, so a predicate hardcoded to one class
#   scores 1 and fails — the live board renders risk-decision, risk-risk and
#   risk-routine, and a single-class literal is broken for the other two;
# - both open cards carry a `risk-*` class, so a predicate still matching the old
#   bare `class="item"` literal scores 0 and fails.
BOARD = (
    '<ul class="items">'
    '<li class="item risk-decision" data-item-id="aa">open</li>'
    '<li class="item risk-routine" data-item-id="bb">open</li>'
    '<li class="item risk-risk dimmed" data-item-id="cc">answered</li>'
    "</ul>"
)

# The board's own script block reads `data-item-id` off each row, so the served
# page carries 10 more occurrences of the string than it has rendered rows
# (measured 2026-10-24: 92 against 82). A count over the raw text is inflated by
# 12% and reads as a pass. This fixture is that gap in miniature.
SCRIPTED = (
    '<ul class="items">'
    '<li class="item risk-decision" data-item-id="aa">open</li>'
    '<li class="item risk-routine dimmed" data-item-id="bb">answered</li>'
    "</ul>"
    "<script>"
    "var i = row.getAttribute('data-item-id');"
    "document.querySelectorAll('li.item').forEach(function (r) {"
    "var j = r.getAttribute('data-item-id');"
    "});"
    "</script>"
)


class CountTest(unittest.TestCase):
    def test_counts_open_cards_and_not_dimmed_ones(self):
        self.assertEqual(ab.count_open_cards(BOARD), 2)

    def test_counts_across_risk_classes(self):
        for risk in ("risk-decision", "risk-risk", "risk-routine"):
            with self.subTest(risk=risk):
                html = f'<li class="item {risk}" data-item-id="x">open</li>'
                self.assertEqual(ab.count_open_cards(html), 1)

    def test_ignores_the_string_inside_script_and_style(self):
        # 2 rendered rows, 4 raw occurrences of the attribute name.
        self.assertEqual(SCRIPTED.count("data-item-id"), 4)
        self.assertEqual(ab.count_open_cards(SCRIPTED), 1)
        styled = '<style>li[data-item-id] { color: red }</style>' + BOARD
        self.assertEqual(ab.count_open_cards(styled), 2)

    def test_an_all_answered_board_counts_zero(self):
        html = '<li class="item risk-risk dimmed" data-item-id="bb">answered</li>'
        self.assertEqual(ab.count_open_cards(html), 0)

    def test_an_empty_board_counts_zero(self):
        self.assertEqual(ab.count_open_cards('<ul class="items"></ul>'), 0)


class MainTest(unittest.TestCase):
    def _run(self, argv, fetch):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(ab, "fetch_board", side_effect=fetch):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ab.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_success_prints_the_count_and_the_url(self):
        rc, out, _ = self._run(["--store", "http://board.test"], lambda s, t: BOARD)
        self.assertEqual(rc, ab.EXIT_OK)
        self.assertEqual(out.strip(), "Board: 2 open — http://board.test")

    def test_json_carries_the_same_count(self):
        rc, out, _ = self._run(
            ["--store", "http://board.test", "--json"], lambda s, t: BOARD
        )
        self.assertEqual(rc, ab.EXIT_OK)
        self.assertIn('"open": 2', out)
        self.assertIn('"available": true', out)

    def _failing(self, exc):
        def fetch(store, timeout):
            raise exc

        return fetch

    def test_every_unreachable_mode_takes_the_same_branch(self):
        cases = {
            "refused": urllib.error.URLError("Connection refused"),
            "timed out": urllib.error.URLError("timed out"),
            "non-200": OSError("HTTP 500"),
        }
        for name, exc in cases.items():
            with self.subTest(mode=name):
                rc, out, err = self._run([], self._failing(exc))
                self.assertEqual(rc, ab.EXIT_UNAVAILABLE)
                # The line is the contract; the reason is diagnostics and must
                # not reach stdout, where the caller reads one literal.
                self.assertEqual(out.strip(), ab.UNAVAILABLE)
                self.assertNotIn(ab.UNAVAILABLE, err)
                self.assertIn(type(exc).__name__, err)


if __name__ == "__main__":
    unittest.main()
