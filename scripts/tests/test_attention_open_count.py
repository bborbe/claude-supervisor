#!/usr/bin/env python3
"""Tests for scripts/attention-open-count.py.

The round prints this line beside `attention-board.py`'s board line, and the two
numbers are deliberately different: the board line counts undimmed rendered cards,
this counts every `state=open` item in the store. The property worth pinning is
that this line is the STORE's number — a line that quietly counted the board page
too would report `0` while the store held every card, which is exactly the
silence the task exists to remove (measured 2026-11-16: board 0, store 13).

Second property: the cron share is read from each item's `producer_kind`, not from
a hardcoded producer list. A hardcoded pair misses a new cron producer, so the
share reads low on exactly the growth the line exists to catch.

Third property: every way the store can be unreachable takes the SAME branch — one
line, one exit code. The caller has nothing different to do about any of them.

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


oc = _load("attention_open_count", "attention-open-count.py")


def _item(producer_id, producer_kind="cron"):
    return {"producer_id": producer_id, "producer_kind": producer_kind}


# Two cron producers and one operator producer. Three properties ride on this
# shape, and each is a bug the fixture is the only guard against:
#
# - cron share is 2/3, so a share computed over all producers (100%) fails;
# - the cron producers are DIFFERENT ids, so a hardcoded producer list scores
#   fewer than 2 and fails;
# - one non-cron item is present, so a share that ignores `producer_kind` and
#   assumes every card is cron (100%) fails.
ITEMS = [
    _item("sweep-gate"),
    _item("attention-unforwarded-closers"),
    _item("manager", producer_kind="manager"),
]


class SummariseTest(unittest.TestCase):
    def test_counts_open_and_splits_by_producer(self):
        s = oc.summarise(ITEMS)
        self.assertEqual(s["open"], 3)
        self.assertEqual(s["by_producer"], {"sweep-gate": 1, "attention-unforwarded-closers": 1, "manager": 1})

    def test_cron_share_is_over_the_whole_set(self):
        s = oc.summarise(ITEMS)
        self.assertEqual(s["cron"], 2)
        self.assertAlmostEqual(s["cron_share"], 2 / 3)

    def test_a_new_cron_producer_is_counted_without_an_edit(self):
        # `attention-sweeper` is not in any hardcoded pair; it must still count.
        items = [_item("attention-sweeper"), _item("manager", "manager")]
        s = oc.summarise(items)
        self.assertEqual(s["cron"], 1)
        self.assertAlmostEqual(s["cron_share"], 0.5)

    def test_empty_store_has_no_share(self):
        s = oc.summarise([])
        self.assertEqual(s["open"], 0)
        self.assertIsNone(s["cron_share"])


class RenderTest(unittest.TestCase):
    def test_line_names_the_split_and_the_share(self):
        self.assertEqual(
            oc.render(oc.summarise(ITEMS)),
            "Open: 3 — sweep-gate 1, attention-unforwarded-closers 1, manager 1 (cron 67%)",
        )

    def test_empty_store_renders_zero(self):
        self.assertEqual(oc.render(oc.summarise([])), "Open: 0")


class MainTest(unittest.TestCase):
    def _run(self, argv, fetch):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(oc, "fetch_open", side_effect=fetch):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = oc.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_success_prints_the_line(self):
        rc, out, _ = self._run(["--store", "http://store.test"], lambda s, t: ITEMS)
        self.assertEqual(rc, oc.EXIT_OK)
        self.assertTrue(out.strip().startswith("Open: 3 — "))

    def test_json_carries_the_same_count(self):
        rc, out, _ = self._run(
            ["--store", "http://store.test", "--json"], lambda s, t: ITEMS
        )
        self.assertEqual(rc, oc.EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["open"], 3)
        self.assertEqual(payload["cron"], 2)
        self.assertTrue(payload["available"])

    def _failing(self, exc):
        def fetch(store, timeout):
            raise exc

        return fetch

    def test_every_unreachable_mode_takes_the_same_branch(self):
        cases = {
            "refused": urllib.error.URLError("Connection refused"),
            "timed out": urllib.error.URLError("timed out"),
            "non-200": OSError("HTTP 500"),
            "unparseable": ValueError("unexpected payload type: str"),
        }
        for name, exc in cases.items():
            with self.subTest(mode=name):
                rc, out, err = self._run([], self._failing(exc))
                self.assertEqual(rc, oc.EXIT_UNAVAILABLE)
                # The line is the contract; the reason is diagnostics and must
                # not reach stdout, where the caller reads one literal.
                self.assertEqual(out.strip(), oc.UNAVAILABLE)
                self.assertNotIn(oc.UNAVAILABLE, err)
                self.assertIn(type(exc).__name__, err)


if __name__ == "__main__":
    unittest.main()
