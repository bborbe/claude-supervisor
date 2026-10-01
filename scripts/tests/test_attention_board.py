#!/usr/bin/env python3
"""Tests for scripts/attention-board.py.

The manager commands print this line where a per-gate jump link used to go, and
SC1 asserts the number equals the board's own card count. So the property worth
pinning is that the number counts **open** cards and nothing else: the board
renders every answered record as a dimmed card too, so a count that took both
would be off by ~1600 on a live board, would never match the page, and would look
like a working count while doing it.

Second property: every way the board can be unreachable — refused, timed out,
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

# One open card, two answered ones. A count that took all three would pass a
# test written against an all-open fixture, which is the shape of the bug.
BOARD = (
    '<ul class="items">'
    '<li class="item" data-item-id="aa">open</li>'
    '<li class="item dimmed" data-item-id="bb">answered</li>'
    '<li class="item dimmed" data-item-id="cc">answered</li>'
    "</ul>"
)


class CountTest(unittest.TestCase):
    def test_counts_open_cards_and_not_dimmed_ones(self):
        self.assertEqual(ab.count_open_cards(BOARD), 1)

    def test_an_all_answered_board_counts_zero(self):
        html = '<li class="item dimmed" data-item-id="bb">answered</li>'
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
        self.assertEqual(out.strip(), "Board: 1 open — http://board.test")

    def test_json_carries_the_same_count(self):
        rc, out, _ = self._run(
            ["--store", "http://board.test", "--json"], lambda s, t: BOARD
        )
        self.assertEqual(rc, ab.EXIT_OK)
        self.assertIn('"open": 1', out)
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
