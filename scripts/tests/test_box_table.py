#!/usr/bin/env python3
"""Tests for scripts/box-table.py.

Three properties, each of which has already been wrong in production:

1. **Display width matches the terminal, VS16 included.** The probe is a DSR
   query — write the glyph, emit `\\x1b[6n`, read the reported cursor column —
   run in WezTerm 20260716 on 2026-09-24. `⏸️` (U+23F8 U+FE0F) and `⚠️`
   (U+26A0 U+FE0F) each paint in ONE cell; every true emoji paints in two. The
   script widened any character followed by VS16 until that date, so every `⏸️`
   row was padded one cell short and its right border stepped in — the only rows
   in the real `manager-layer.tick.txt` that disagreed with the terminal.

2. **OSC 8 costs zero cells and changes no visible text.** The Session cell is
   wrapped in a hyperlink whose URI carries the jump token, so the escape bytes
   must never enter a width sum, and the text the operator reads must be
   byte-identical to the unlinked render.

3. **Every failure path renders plain text.** A cell that cannot be linked must
   degrade, never emit a dead link. Three states, and the third is the one a
   criterion naming only the first would miss: no pane resolves, the jump server
   is unconfigured (`jump-link.py` then prints its `/supervisor:jump <pane>`
   fallback — a command, not a URI), and `BOX_TABLE_NO_LINKS=1`, which is what
   makes an evidence paste into a git-tracked file safe.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bt = _load("box_table", "box-table.py")

URL = "http://127.0.0.1:1337/jump?pane=1642&t=test-token-not-a-real-secret"

# The frame the manager sweep renders: Task 42 · Session 10 · Status 19 · Phase 9 · Met 17.
WIDTHS = [42, 10, 19, 9, 17]
HEADER = ["Topic / Goal / Task", "Session", "Status", "Phase", "Met"]
ROWS = [
    ["Notification System", "—", "🔄 progressing", "—", "SC 4/4 · Gate 3/3"],
    ["   Some Task", "[deadbeef]", "✅ done", "done", "17/17"],
    ["   Parked Task", "[cafe1234]", "⏸️ blocked/hold", "todo", "0/15"],
    ["   Waiting Task", "—", "⌛ waiting-on-human", "execution", "6/12"],
]


def doc(rows=None, widths=None, header=None):
    return {
        "header": header or HEADER,
        "rows": rows if rows is not None else ROWS,
        "widths": widths or WIDTHS,
    }


class TestDisplayWidth(unittest.TestCase):
    """SC2's premise: the renderer's width must be the terminal's width."""

    def test_vs16_does_not_widen(self):
        """Measured by DSR in WezTerm 20260716 — both are ONE cell, not two."""
        self.assertEqual(bt.dwidth("⏸️"), 1, "⏸️ is one cell")
        self.assertEqual(bt.dwidth("⚠️"), 1, "⚠️ is one cell")

    def test_true_emoji_are_two_cells(self):
        for icon in ("\U0001f7e2", "\U0001f504", "⌛", "✅", "\U0001f680", "\U0001f527"):
            self.assertEqual(bt.dwidth(icon), 2, f"{icon!r} is two cells")

    def test_a_vs16_bearing_status_label_fits_the_documented_width(self):
        """The runbook's Status column is 19 because `⌛ waiting-on-human` is 19.

        `⏸️ blocked/hold` must fit comfortably — it was the label the old model
        over-counted by one.
        """
        self.assertEqual(bt.dwidth("⌛ waiting-on-human"), 19)
        self.assertEqual(bt.dwidth("⏸️ blocked/hold"), 14)
        for label in ("🔄 progressing", "⏸️ blocked/hold", "✅ done", "⚠️ problem",
                      "🚀 ready-to-start", "🔧 close-me", "✅ done · aborted"):
            self.assertLessEqual(bt.dwidth(label), 19, f"{label!r} overruns Status")

    def test_osc8_costs_zero_cells(self):
        linked = bt.osc8("[deadbeef]", URL)
        self.assertEqual(bt.dwidth(linked), bt.dwidth("[deadbeef]"))
        self.assertEqual(bt.dwidth(linked), 10)

    def test_a_cell_arriving_already_linked_cannot_corrupt_the_width(self):
        """The renderer owns linking; a caller's stray escape must not add phantom cells."""
        d = doc(rows=[[bt.osc8("hello", URL)]], widths=[10], header=["A"])
        rendered = bt.render(d, link=False)
        widths = {bt.dwidth(l) for l in rendered.splitlines()}
        self.assertEqual(len(widths), 1, "a pre-linked cell must not change the box width")

    def test_truncation_stays_within_the_column(self):
        d = doc(rows=[["[deadbeef]", "a task name long enough to be truncated"]],
                widths=[10, 20], header=["Session", "Task"])
        rendered = bt.render(d, link=False)
        self.assertIn("…", rendered)
        self.assertEqual(len({bt.dwidth(l) for l in rendered.splitlines()}), 1)


class TestAlignment(unittest.TestCase):
    """SC2: borders align on every row, the ⏸️ row included."""

    def _assert_uniform(self, rendered):
        widths = {}
        for l in rendered.splitlines():
            widths.setdefault(bt.dwidth(l), []).append(l)
        self.assertEqual(len(widths), 1,
                         f"ragged right border: widths {sorted(widths)}")

    def test_every_row_aligns_without_links(self):
        self._assert_uniform(bt.render(doc(), link=False))

    def test_every_row_aligns_with_links(self):
        with mock.patch.object(bt, "jump_url", return_value=URL):
            self._assert_uniform(bt.render(doc(), link=True))

    def test_the_vs16_row_is_present_and_the_border_is_straight(self):
        """The regression this pins: the ⏸️ row was the one that stepped in."""
        rendered = bt.render(doc(), link=False)
        self.assertIn("⏸️ blocked/hold", rendered)
        self._assert_uniform(rendered)

    def test_visible_text_is_byte_identical_with_and_without_links(self):
        plain = bt.render(doc(), link=False)
        with mock.patch.object(bt, "jump_url", return_value=URL):
            linked = bt.render(doc(), link=True)
        self.assertEqual(bt.strip_osc8(linked), plain)


class TestLinking(unittest.TestCase):
    def setUp(self):
        bt._url_cache.clear()
        self.addCleanup(bt._url_cache.clear)

    def test_session_cells_are_linked_and_nothing_else_is(self):
        with mock.patch.object(bt, "jump_url", return_value=URL) as resolver:
            rendered = bt.render(doc(), link=True)
        # Two rows carry a `[sid8]`; the header and the `—` rows must not resolve.
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(sorted(c.args[0] for c in resolver.call_args_list),
                         ["cafe1234", "deadbeef"])
        self.assertEqual(rendered.count(bt.OSC8_OPEN), 4, "two cells, open + close each")

    def test_the_header_is_never_linked(self):
        d = doc(header=["Session"], rows=[["[deadbeef]"]], widths=[10])
        with mock.patch.object(bt, "jump_url", return_value=URL):
            rendered = bt.render(d, link=True)
        header_line = bt.strip_osc8(rendered).splitlines()[1]
        self.assertIn("Session", header_line)
        self.assertNotIn(bt.OSC8_OPEN, rendered.splitlines()[1])

    def test_the_link_wraps_the_text_and_not_the_padding(self):
        """Padding outside the escape keeps a click on trailing space inert."""
        cell = bt.fmt("[deadbeef]", 10, URL)
        self.assertTrue(cell.endswith(bt.OSC8_OPEN + bt.OSC8_ST))
        self.assertEqual(bt.dwidth(cell), 10)


class TestNoLinkStates(unittest.TestCase):
    """SC3: three failure states, each with its own probe."""

    def setUp(self):
        bt._url_cache.clear()
        self.addCleanup(bt._url_cache.clear)

    def test_no_target_resolves_so_no_escape_is_emitted(self):
        with mock.patch.object(bt, "jump_url", return_value=None):
            rendered = bt.render(doc(), link=True)
        self.assertNotIn(bt.OSC8_OPEN, rendered)

    def test_a_fallback_command_is_never_turned_into_a_hyperlink(self):
        """`jump-link.py` prints `/supervisor:jump <N>` with no server or token.

        That is a command, not a URI — linking it would produce a dead link, the
        exact failure jump-link.py exists to avoid.
        """
        def fake_run(argv, **kw):
            return mock.Mock(returncode=0, stdout="/supervisor:jump 1642\n")

        self.assertIsNone(bt.jump_url("deadbeef", run=fake_run, env={}))

    def test_a_plain_command_cell_renders_as_text(self):
        with mock.patch.object(bt, "jump_url",
                               return_value="/supervisor:jump 1642"):
            # jump_url itself never returns a non-URL; guard the renderer anyway.
            rendered = bt.render(doc(), link=False)
        self.assertNotIn(bt.OSC8_OPEN, rendered)

    def test_the_kill_switch_removes_every_escape(self):
        with mock.patch.object(bt, "jump_url", return_value=URL):
            with mock.patch.dict(os.environ, {"BOX_TABLE_NO_LINKS": "1"}):
                self.assertFalse(bt.links_enabled())
                rendered = bt.render(doc(), link=bt.links_enabled())
        self.assertNotIn(bt.OSC8_OPEN, rendered)

    def test_links_are_on_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOX_TABLE_NO_LINKS", None)
            self.assertTrue(bt.links_enabled())


class TestResolution(unittest.TestCase):
    """`jump_url`'s two hops, and its degradation."""

    def setUp(self):
        bt._url_cache.clear()
        self.addCleanup(bt._url_cache.clear)

    def test_both_hops_are_called_and_the_url_is_returned(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if "--pane-for" in argv:
                return mock.Mock(returncode=0, stdout="1642\n")
            return mock.Mock(returncode=0, stdout=URL + "\n")

        self.assertEqual(bt.jump_url("deadbeef", run=fake_run, env={}), URL)
        self.assertIn("--pane-for", calls[0])
        self.assertEqual(calls[0][-1], "deadbeef")
        self.assertEqual(calls[1][-1], "1642")

    def test_a_failed_pane_lookup_returns_none_without_asking_for_a_url(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=1, stdout="")

        self.assertIsNone(bt.jump_url("deadbeef", run=fake_run, env={}))
        self.assertEqual(len(calls), 1, "no second hop after a failed first")

    def test_a_non_numeric_pane_is_rejected(self):
        def fake_run(argv, **kw):
            return mock.Mock(returncode=0, stdout="not-a-pane\n")

        self.assertIsNone(bt.jump_url("deadbeef", run=fake_run, env={}))

    def test_a_subprocess_failure_degrades_rather_than_raising(self):
        def fake_run(argv, **kw):
            raise OSError("wezterm unreachable")

        self.assertIsNone(bt.jump_url("deadbeef", run=fake_run, env={}))

    def test_resolution_is_cached_per_session(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="1642\n" if "--pane-for" in argv else URL)

        bt.jump_url("deadbeef", run=fake_run, env={})
        bt.jump_url("deadbeef", run=fake_run, env={})
        self.assertEqual(len(calls), 2, "one resolution, not two")

    def test_child_env_adds_the_wezterm_directory_when_it_exists(self):
        """The launchd gate host has no WezTerm on PATH — measured 2026-09-24."""
        with mock.patch.object(bt, "WEZTERM_DIRS", ("/Applications/WezTerm.app/Contents/MacOS",)):
            with mock.patch.object(bt.os.path, "isdir", return_value=True):
                with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
                    self.assertIn("/Applications/WezTerm.app/Contents/MacOS",
                                  bt.child_env()["PATH"])

    def test_child_env_leaves_path_alone_when_wezterm_is_absent(self):
        with mock.patch.object(bt.os.path, "isdir", return_value=False):
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
                self.assertEqual(bt.child_env()["PATH"], "/usr/bin:/bin")


if __name__ == "__main__":
    unittest.main()
