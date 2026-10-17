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


class _RenderHarness:
    """End-to-end over a temp state dir: the rendered table is the artifact SC1 reads.

    Deliberately not a `TestCase` — the harness is shared by two suites, and a
    `TestCase` base would re-run every one of its tests in each subclass.
    """

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


class RenderTest(_RenderHarness, unittest.TestCase):
    """Pane-liveness rendering — the guard that keeps a phantom pane out of the table."""

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


def ctx_row(**kw):
    """A context record as the statusline writes it."""
    base = {"session_id": "s1", "used_percentage": 50, "pane": "1039",
            "session_name": "mgr", "context_window_size": 1000000}
    return dict(base, **kw)


class UsedTokensTest(unittest.TestCase):
    def test_tokens_from_a_known_window(self):
        self.assertEqual(
            cu.used_tokens({"pct": 50.0, "rec": {"context_window_size": 1000000}}), 500000)

    def test_missing_window_is_none_not_zero(self):
        # The load-bearing case: `or 0` folded this to 0 tokens, which reads as
        # "under every bound" and drops exactly the sessions a token bound cannot judge.
        self.assertIsNone(cu.used_tokens({"pct": 50.0, "rec": {}}))

    def test_zero_window_is_none_not_zero(self):
        self.assertIsNone(cu.used_tokens({"pct": 50.0, "rec": {"context_window_size": 0}}))


class BoundVerdictTest(unittest.TestCase):
    def v(self, pct, size, tokens, threshold=70.0):
        rec = {} if size is None else {"context_window_size": size}
        return cu.bound_verdict({"pct": pct, "rec": rec}, threshold, tokens)

    def test_over_by_tokens(self):
        # 80% of a 1M window is 800k, over a 500k bound.
        self.assertIs(self.v(80.0, 1000000, 500000), True)

    def test_over_by_tokens_while_under_the_percentage(self):
        # 50% of a 1M window is 500k — the percentage gate would never fire here,
        # which is the whole reason the absolute bound exists on this fleet.
        self.assertIs(self.v(50.0, 1000000, 500000), True)

    def test_under_both(self):
        self.assertIs(self.v(15.0, 1000000, 500000), False)

    def test_a_small_window_below_the_floor_is_not_over(self):
        # 75% of 128k is 96k, far below the 500k floor. The percentage used to
        # fire here on its own, because the two bounds were ORed. The rule is
        # that nothing below the floor is offered on any path, so how full the
        # window is cannot matter — this is the case the change exists for.
        self.assertIs(self.v(75.0, 128000, 500000), False)

    def test_a_small_window_is_judged_by_tokens_not_by_its_shape(self):
        # The floor is about tokens, never about the window: the same 128k
        # window is over once its token count reaches the bound, even though no
        # percentage of 128k could ever reach 500k.
        self.assertIs(self.v(99.0, 128000, 100000), True)

    def test_unknown_window_is_unknown_not_under(self):
        self.assertIsNone(self.v(15.0, None, 500000))

    def test_unknown_window_without_a_token_bound_is_plainly_under(self):
        # No token bound in force means nothing needs the window size.
        self.assertIs(self.v(15.0, None, None), False)

    def test_no_token_bound_preserves_the_previous_behaviour(self):
        self.assertIs(self.v(50.0, 1000000, None), False)
        self.assertIs(self.v(70.0, 1000000, None), True)


class BoundLabelTest(unittest.TestCase):
    def test_percentage_only(self):
        self.assertEqual(cu.bound_label(70.0, None), "70%")

    def test_only_the_bound_that_can_fire_is_named(self):
        # Naming the percentage beside a token bound would describe a gate this
        # code does not have: below the token bound the percentage cannot fire.
        self.assertEqual(cu.bound_label(70.0, 500000), "500k tokens")


class TokenBoundRenderTest(_RenderHarness, unittest.TestCase):
    """SC2's evidence is the gate's OWN output naming the bound in force."""

    def test_the_output_names_the_bound_in_force(self):
        out = self.run_main({"s1.json": ctx_row()}, {}, {"1039"},
                            argv=["--threshold-tokens", "500000"])
        self.assertIn("Bound in force: 500k tokens", out)

    def test_the_token_bound_fires_where_the_percentage_never_would(self):
        # 50% of the 1M window is exactly 500k — at the bound, and the
        # percentage gate alone would never have offered it.
        out = self.run_main({"s1.json": ctx_row(used_percentage=50)}, {}, {"1039"},
                            argv=["--threshold-tokens", "500000"])
        self.assertIn("Over 500k tokens: 1", out)

    def test_a_small_window_below_the_floor_is_not_offered(self):
        # The regression this change exists for: 75% of a 128k window is 96k,
        # which the ORed percentage bound used to offer on its own.
        out = self.run_main(
            {"s1.json": ctx_row(used_percentage=75, context_window_size=128000)},
            {}, {"1039"}, argv=["--threshold-tokens", "500000"])
        self.assertNotIn("Over 500k tokens", out)

    def test_unknown_window_is_reported_and_never_read_as_zero_tokens(self):
        out = self.run_main(
            {"s1.json": ctx_row(used_percentage=15, context_window_size=None)},
            {}, {"1039"}, argv=["--threshold-tokens", "500000"])
        self.assertIn("Window UNKNOWN", out)
        self.assertIn("window unknown", out)
        self.assertNotIn("0k/0k", out)

    def test_unknown_window_is_never_offered_for_compaction(self):
        out = self.run_main(
            {"s1.json": ctx_row(used_percentage=15, context_window_size=None)},
            {}, {"1039"}, argv=["--threshold-tokens", "500000", "--compactable"])
        self.assertNotIn("Compaction candidates", out)


if __name__ == "__main__":
    unittest.main()
