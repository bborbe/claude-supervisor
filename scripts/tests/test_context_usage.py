#!/usr/bin/env python3
"""Tests for scripts/context-usage.py's pane liveness guard.

The reader joins a pane id recorded at write time and never re-checks it, so a
stale id was printed with exactly the confidence of a live one. Measured
2026-09-20: `--compactable --threshold 70` returned `73% · pane 109 · idle —
compactable` for a session 72h43m old whose pane did not exist — `wezterm cli
list` held 20 panes, none of them 109, and `get-text` exited 1.

The guard matters more than the others because compaction is a **no-ask**
action: a phantom candidate is a run that reports success and does nothing.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "context_usage", os.path.join(HERE, "..", "context-usage.py")
)
cu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cu)


class PaneStateTest(unittest.TestCase):
    def test_live_pane_is_printed_and_addressable(self):
        self.assertEqual(cu.pane_state("1039", {"1039", "1040"}), ("1039", True, False))

    def test_phantom_pane_is_withheld_and_not_addressable(self):
        display, addressable, phantom = cu.pane_state("109", {"1039", "1040"})
        self.assertEqual(display, "—")
        self.assertFalse(addressable)
        self.assertTrue(phantom)

    def test_unreadable_query_is_unknown_not_gone(self):
        # A failed `wezterm cli list` cannot prove a pane is absent, so it must
        # not be reported as phantom either — UNKNOWN is its own answer.
        display, addressable, phantom = cu.pane_state("1039", None)
        self.assertEqual(display, "?")
        self.assertFalse(addressable)
        self.assertFalse(phantom)

    def test_no_recorded_pane_is_blank_not_phantom(self):
        self.assertEqual(cu.pane_state(None, {"1039"}), ("—", False, False))
        self.assertEqual(cu.pane_state("?", {"1039"}), ("—", False, False))

    def test_phantom_pane_is_never_printed_as_an_id(self):
        # SC1's shape: a listed pane id must be one `wezterm cli list` confirms.
        for pane, live in [("109", {"1039"}), ("109", set()), ("1039", None)]:
            display, _, _ = cu.pane_state(pane, live)
            self.assertNotIn("109", display)
            self.assertNotIn("1039", display)


def row(**kw):
    # module-level, not a method: `self` is one of the row's own keys and would
    # otherwise collide with the bound instance parameter.
    base = {"blocked": False, "busy": False, "self": False, "addressable": True}
    return dict(base, **kw)


class IsCandidateTest(unittest.TestCase):
    def test_idle_live_pane_is_a_candidate(self):
        self.assertTrue(cu.is_candidate(row()))

    def test_phantom_pane_is_never_a_candidate(self):
        self.assertFalse(cu.is_candidate(row(addressable=False)))

    def test_blocked_busy_and_self_are_not_candidates(self):
        self.assertFalse(cu.is_candidate(row(blocked=True)))
        self.assertFalse(cu.is_candidate(row(busy=True)))
        self.assertFalse(cu.is_candidate(row(**{"self": True})))


class RenderTest(unittest.TestCase):
    """End-to-end over a temp state dir: the rendered table is the artifact SC1 reads."""

    def run_main(self, context, attention, live, argv=None):
        with tempfile.TemporaryDirectory() as ctx, tempfile.TemporaryDirectory() as att:
            for d, files in ((ctx, context), (att, attention)):
                for name, body in files.items():
                    with open(os.path.join(d, name), "w") as fh:
                        json.dump(body, fh)
            buf = io.StringIO()
            with mock.patch.object(cu, "CONTEXT", ctx), \
                 mock.patch.object(cu, "ATTENTION", att), \
                 mock.patch.object(cu, "live_panes", lambda: live), \
                 mock.patch.object(cu, "CLAUDE_CODE_SESSION_ID", ""), \
                 mock.patch("sys.argv", ["context-usage.py"] + (argv or [])), \
                 redirect_stdout(buf):
                cu.main()
            return buf.getvalue()

    def test_phantom_pane_id_never_appears_in_the_table(self):
        out = self.run_main(
            {"s1.json": {"session_id": "s1", "used_percentage": 75, "pane": "109",
                         "session_name": "ghost"}},
            {},
            {"1039"},
        )
        self.assertNotIn("109", out)
        self.assertIn("—", out)

    def test_phantom_pane_is_not_offered_as_a_compaction_candidate(self):
        out = self.run_main(
            {"s1.json": {"session_id": "s1", "used_percentage": 75, "pane": "109",
                         "session_name": "ghost"}},
            {},
            {"1039"},
            argv=["--compactable"],
        )
        self.assertNotIn("Compaction candidates", out)

    def test_live_pane_is_listed(self):
        out = self.run_main(
            {"s1.json": {"session_id": "s1", "used_percentage": 75, "pane": "1039",
                         "session_name": "real"}},
            {},
            {"1039"},
        )
        self.assertIn("1039", out)

    def test_unreadable_pane_query_warns_and_withholds_ids(self):
        out = self.run_main(
            {"s1.json": {"session_id": "s1", "used_percentage": 75, "pane": "1039",
                         "session_name": "real"}},
            {},
            None,
        )
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("1039", out)

    def test_unreadable_pane_query_offers_no_candidate(self):
        out = self.run_main(
            {"s1.json": {"session_id": "s1", "used_percentage": 75, "pane": "1039",
                         "session_name": "real"}},
            {},
            None,
            argv=["--compactable"],
        )
        self.assertNotIn("Compaction candidates", out)


if __name__ == "__main__":
    unittest.main()
