#!/usr/bin/env python3
"""Tests for scripts/attention-store.py.

The fleet round prints this line beside `attention-board.py`'s, and the pair exists
because the two numbers answer different questions: the board counts the cards it
RENDERED, this counts the items the STORE holds open. So the properties worth
pinning are:

- the total is the store's open-item count, and the per-producer split adds up to it;
- the split is sorted descending, so consecutive rounds compare line-for-line;
- the cron share is computed from `producer_kind`, so it covers every cron producer
  rather than a hardcoded pair of ids;
- a full page is reported truncated rather than silently undercounted;
- every way the store can be unreachable takes the SAME branch, one line and one
  exit code, exactly as `attention-board.py` does for the board;
- the line never reads like the board's — a reader must not be able to quote
  `Store: … open` as the rendered-card count or vice versa.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
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


st = _load("attention_store", "attention-store.py")

# The measured 2026-11-16 20:47 shape: 13 open, all cron, split across two cron
# producers. A total-only reading cannot tell this from 13 operator gates, which is
# why the split is asserted rather than the total alone.
ITEMS = (
    [{"producer_id": "attention-unforwarded-closers", "producer_kind": "cron"}] * 8
    + [{"producer_id": "sweep-gate", "producer_kind": "cron"}] * 5
)


class SplitTest(unittest.TestCase):
    def test_split_sums_to_the_total_and_sorts_descending(self):
        split = st.producer_split(ITEMS)
        self.assertEqual(sum(n for _, n in split), len(ITEMS))
        self.assertEqual(split[0], ("attention-unforwarded-closers", 8))
        self.assertEqual([n for _, n in split], sorted((n for _, n in split), reverse=True))

    def test_ties_break_on_producer_id_so_rounds_compare(self):
        items = [
            {"producer_id": "b", "producer_kind": "session"},
            {"producer_id": "a", "producer_kind": "session"},
        ]
        self.assertEqual(st.producer_split(items), [("a", 1), ("b", 1)])

    def test_a_missing_producer_id_does_not_vanish(self):
        self.assertEqual(st.producer_split([{"producer_kind": "session"}]), [("<unknown>", 1)])


class CronTest(unittest.TestCase):
    def test_cron_share_uses_producer_kind_not_a_hardcoded_pair(self):
        items = [
            {"producer_id": "sweep-gate", "producer_kind": "cron"},
            {"producer_id": "a-new-cron-producer", "producer_kind": "cron"},
            {"producer_id": "some-session", "producer_kind": "session"},
        ]
        self.assertEqual(st.cron_count(items), 2)
        self.assertEqual(st.share_line(len(items), st.cron_count(items)), "cron 2/3 (67%)")

    def test_empty_store_has_no_share_to_report(self):
        self.assertEqual(st.share_line(0, 0), "cron 0/0 (n/a)")


class RenderTest(unittest.TestCase):
    def test_line_carries_the_total_the_split_and_the_share(self):
        self.assertEqual(
            st.render_line(ITEMS, 500),
            "Store: 13 open — attention-unforwarded-closers 8, sweep-gate 5 · cron 13/13 (100%)",
        )

    def test_empty_store_prints_its_total_without_a_split(self):
        self.assertEqual(st.render_line([], 500), "Store: 0 open · cron 0/0 (n/a)")

    def test_a_full_page_is_reported_truncated(self):
        # An undercount that reads as a quiet store is the failure this guards.
        self.assertIn("(truncated at 2)", st.render_line(ITEMS[:2], 2))

    def test_the_line_is_not_the_board_line(self):
        # `Board page: <n> undimmed cards` is the rendered-card count; this one is
        # the store's open count and must never share its wording.
        line = st.render_line(ITEMS, 500)
        self.assertTrue(line.startswith("Store: "))
        self.assertNotIn("undimmed", line)
        self.assertNotIn("Board", line)


class MainTest(unittest.TestCase):
    def _run(self, argv, fetch):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(st, "fetch_items", side_effect=fetch):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = st.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_success_prints_the_line(self):
        rc, out, _ = self._run(["--store", "http://store.test"], lambda s, t, l: ITEMS)
        self.assertEqual(rc, st.EXIT_OK)
        self.assertEqual(
            out.strip(),
            "Store: 13 open — attention-unforwarded-closers 8, sweep-gate 5 · cron 13/13 (100%)",
        )

    def test_json_carries_the_same_reading(self):
        rc, out, _ = self._run(
            ["--store", "http://store.test", "--json"], lambda s, t, l: ITEMS
        )
        self.assertEqual(rc, st.EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["open"], 13)
        self.assertEqual(payload["cron"], 13)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["producers"]["sweep-gate"], 5)
        self.assertTrue(payload["available"])

    def _failing(self, exc):
        def fetch(store, timeout, limit):
            raise exc

        return fetch

    def test_every_unreachable_mode_takes_the_same_branch(self):
        cases = {
            "refused": urllib.error.URLError("Connection refused"),
            "timed out": urllib.error.URLError("timed out"),
            "non-200": OSError("HTTP 500"),
            "unparseable": ValueError("Expecting value"),
        }
        for name, exc in cases.items():
            with self.subTest(mode=name):
                rc, out, err = self._run([], self._failing(exc))
                self.assertEqual(rc, st.EXIT_UNAVAILABLE)
                # The line is the contract; the reason is diagnostics and must not
                # reach stdout, where the caller reads one literal.
                self.assertEqual(out.strip(), st.UNAVAILABLE)
                self.assertNotIn(st.UNAVAILABLE, err)
                self.assertIn(type(exc).__name__, err)


if __name__ == "__main__":
    unittest.main()
