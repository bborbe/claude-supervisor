"""plugin-version-census.py — the fleet reading, and the two traps that make it wrong.

The load-bearing rule is **newest marker per live pid**. A reload writes a new `.in_use`
entry and never removes the old one, so one pid sits in several version directories at
once (measured 2026-10-07: pid 25483 in 5, pid 56704 in 9). A reader that takes the first
match, or counts markers, reports a fleet far staler than it is — the direction that looks
like more evidence. `NewestMarkerTest` pins that ordering.

The second rule is that **`unknown` is not `stale`**: a headless or cluster worker holds no
local marker, and naming a fix for a session the lever cannot reach is worse than saying
nothing. `CensusStateTest` pins the three states apart.
"""
import importlib.util
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "plugin-version-census.py"
spec = importlib.util.spec_from_file_location("plugin_version_census", SCRIPT)
census_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(census_mod)


def session(sid, pid, name="a session"):
    return {"sessionId": sid, "pid": pid, "name": name}


def touch(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    os.utime(path, (mtime, mtime))


class NewestMarkerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cache = pathlib.Path(self.dir)

    def marker(self, version, pid, mtime):
        touch(self.cache / "claude-supervisor" / "supervisor" / version / ".in_use" / str(pid), mtime)

    def test_newest_wins_when_a_pid_sits_in_several_versions(self):
        self.marker("0.61.1", 83726, 1_000)
        self.marker("0.62.4", 83726, 2_000)
        version, mtime = census_mod.newest_marker(self.cache, "claude-supervisor", "supervisor", 83726)
        self.assertEqual(version, "0.62.4")
        self.assertEqual(mtime, 2_000)

    def test_older_versions_are_left_behind_by_a_reload_not_removed(self):
        for version, mtime in (("0.111.1", 1), ("0.111.5", 2), ("0.114.0", 3)):
            self.marker(version, 25483, mtime)
        version, _ = census_mod.newest_marker(self.cache, "claude-supervisor", "supervisor", 25483)
        self.assertEqual(version, "0.114.0")
        self.assertEqual(len(list((self.cache / "claude-supervisor" / "supervisor").glob("*/.in_use/25483"))), 3)

    def test_no_marker_is_none_rather_than_a_guess(self):
        self.assertIsNone(census_mod.newest_marker(self.cache, "claude-supervisor", "supervisor", 1))


class InstalledVersionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = pathlib.Path(self.dir) / "installed_plugins.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload))

    def test_reads_the_install_path_basename(self):
        self.write({"plugins": {"supervisor@claude-supervisor": [
            {"version": "0.114.1", "installPath": "/x/cache/claude-supervisor/supervisor/0.114.1"}]}})
        self.assertEqual(census_mod.installed_version(self.path, "supervisor", "claude-supervisor"), "0.114.1")

    def test_absent_plugin_is_none(self):
        self.write({"plugins": {}})
        self.assertIsNone(census_mod.installed_version(self.path, "supervisor", "claude-supervisor"))

    def test_unreadable_file_is_none(self):
        self.assertIsNone(census_mod.installed_version(pathlib.Path(self.dir) / "nope.json",
                                                       "supervisor", "claude-supervisor"))


class CensusStateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cache = pathlib.Path(self.dir)

    def marker(self, version, pid, mtime):
        touch(self.cache / "claude-supervisor" / "supervisor" / version / ".in_use" / str(pid), mtime)

    def run_census(self, sessions, installed):
        return census_mod.census(sessions, installed, self.cache, "claude-supervisor", "supervisor")

    def test_behind_the_install_is_stale(self):
        self.marker("0.114.0", 10, 1)
        rows = self.run_census([session("aaaa1111", 10)], "0.114.1")
        self.assertEqual(rows[0]["state"], "stale")
        self.assertEqual(rows[0]["loaded"], "0.114.0")

    def test_on_the_install_is_current(self):
        self.marker("0.114.1", 10, 1)
        rows = self.run_census([session("aaaa1111", 10)], "0.114.1")
        self.assertEqual(rows[0]["state"], "current")

    def test_no_marker_is_unknown_not_stale(self):
        rows = self.run_census([session("aaaa1111", 10)], "0.114.1")
        self.assertEqual(rows[0]["state"], "unknown")
        self.assertIsNone(rows[0]["loaded"])

    def test_an_unreadable_install_makes_every_row_unknown(self):
        """No baseline means no comparison — `current` here would print `stale: 0`."""
        self.marker("0.114.0", 10, 1)
        rows = self.run_census([session("aaaa1111", 10)], None)
        self.assertEqual(rows[0]["state"], "unknown")
        self.assertEqual(rows[0]["loaded"], "0.114.0")

    def test_stale_rows_sort_first(self):
        self.marker("0.114.1", 10, 1)
        self.marker("0.114.0", 11, 1)
        rows = self.run_census([session("aaaa1111", 10), session("bbbb2222", 11)], "0.114.1")
        self.assertEqual([r["state"] for r in rows], ["stale", "current"])


class PanesForPidsTest(unittest.TestCase):
    def test_join_is_the_tty(self):
        panes = {"203": {"tty": "/dev/ttys003"}, "9": {"tty": "/dev/ttys014"}}
        with mock.patch.object(census_mod, "_pid_ttys", return_value={55378: "ttys000", 999: "ttys003"}):
            self.assertEqual(census_mod.panes_for_pids([55378, 999], panes), {999: "203"})

    def test_a_failed_wezterm_read_is_none_not_an_empty_fleet(self):
        with mock.patch.object(census_mod, "wezterm_panes", return_value=None):
            self.assertIsNone(census_mod.panes_for_pids([1]))

    def test_a_failed_ps_read_is_none_too(self):
        """Both halves carry the three-state contract, so the join must not drop either."""
        panes = {"203": {"tty": "/dev/ttys003"}}
        with mock.patch.object(census_mod, "_pid_ttys", return_value=None):
            self.assertIsNone(census_mod.panes_for_pids([1], panes))

    def test_a_readable_empty_fleet_stays_empty(self):
        with mock.patch.object(census_mod, "wezterm_panes", return_value={}), \
                mock.patch.object(census_mod, "_pid_ttys", return_value={}):
            self.assertEqual(census_mod.panes_for_pids([1]), {})


class RenderTest(unittest.TestCase):
    def test_header_carries_installed_live_stale(self):
        rows = [{"state": "stale", "sid8": "aaaa1111", "loaded": "0.114.0", "name": "n", "jump": "/supervisor:jump 1"},
                {"state": "current", "sid8": "bbbb2222", "loaded": "0.114.1", "name": "m"}]
        text = census_mod.render(rows, "0.114.1")
        self.assertEqual(text.splitlines()[0], "installed: 0.114.1 · live: 2 · stale: 1")
        self.assertIn("aaaa1111  loaded 0.114.0  n  /supervisor:jump 1", text)

    def test_unknown_is_named_in_the_header(self):
        rows = [{"state": "unknown", "sid8": "aaaa1111", "loaded": None, "name": "n"}]
        self.assertIn("unknown: 1", census_mod.render(rows, "0.114.1"))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cache = pathlib.Path(self.dir) / "cache"
        self.sessions = pathlib.Path(self.dir) / "sessions.json"
        installed = pathlib.Path(self.dir) / "installed.json"
        installed.write_text(json.dumps({"plugins": {"supervisor@claude-supervisor": [
            {"installPath": "/x/cache/claude-supervisor/supervisor/0.114.1"}]}}))
        self.installed = installed
        touch(self.cache / "claude-supervisor" / "supervisor" / "0.114.0" / ".in_use" / "11", 1)
        self.sessions.write_text(json.dumps([session("bbbb2222", 11, "behind")]))

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = census_mod.main(list(argv))
        return code, out.getvalue()

    def test_reports_the_stale_session(self):
        code, out = self.run_main("--sessions-json", str(self.sessions), "--cache-dir", str(self.cache),
                                  "--installed-json", str(self.installed), "--no-panes")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "installed: 0.114.1 · live: 1 · stale: 1")
        self.assertIn("bbbb2222  loaded 0.114.0  behind", out)

    def test_json_mode_carries_the_rows(self):
        code, out = self.run_main("--json", "--sessions-json", str(self.sessions), "--cache-dir", str(self.cache),
                                  "--installed-json", str(self.installed), "--no-panes")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["installed"], "0.114.1")
        self.assertEqual(payload["rows"][0]["state"], "stale")

    def test_reload_carries_its_results_beside_the_fresh_rows(self):
        """`census` rebuilds every row, so the lever's outcome cannot live on the old ones."""
        with mock.patch.object(census_mod, "panes_for_pids", return_value={11: "203"}), \
                mock.patch.object(census_mod, "reload_pane", return_value={"pane": "203", "ok": True,
                                                                           "why": "marker advanced"}):
            code, out = self.run_main("--json", "--reload", "--sessions-json", str(self.sessions),
                                      "--cache-dir", str(self.cache), "--installed-json", str(self.installed))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["lever"]["reached"], 1)
        self.assertTrue(payload["lever"]["results"]["bbbb2222"]["ok"])

    def test_unreadable_sessions_file_is_an_error_not_an_empty_fleet(self):
        code, _ = self.run_main("--sessions-json", str(pathlib.Path(self.dir) / "nope.json"),
                                "--cache-dir", str(self.cache), "--installed-json", str(self.installed))
        self.assertEqual(code, 2)

    def test_a_sessions_file_that_is_not_a_list_of_records_exits_2(self):
        bad = pathlib.Path(self.dir) / "bad.json"
        bad.write_text(json.dumps({"not": "a list"}))
        code, _ = self.run_main("--sessions-json", str(bad), "--cache-dir", str(self.cache),
                                "--installed-json", str(self.installed), "--no-panes")
        self.assertEqual(code, 2)

    def test_reload_with_no_panes_is_refused(self):
        """The combination is a fleet-wide no-op that would read as `no resolvable pane`."""
        code, _ = self.run_main("--reload", "--no-panes", "--sessions-json", str(self.sessions),
                                "--cache-dir", str(self.cache), "--installed-json", str(self.installed))
        self.assertEqual(code, 2)

    def test_an_unreadable_liveness_probe_exits_2(self):
        """Never `live: 0 · stale: 0`, which reads as a healthy fleet."""
        with mock.patch.object(census_mod, "live_sessions", return_value=(None, "probe failed")):
            code, _ = self.run_main("--cache-dir", str(self.cache), "--installed-json", str(self.installed),
                                    "--no-panes")
        self.assertEqual(code, 2)

    def test_a_failed_pane_read_is_reported_rather_than_faked_per_row(self):
        with mock.patch.object(census_mod, "panes_for_pids", return_value=None):
            code, out = self.run_main("--sessions-json", str(self.sessions), "--cache-dir", str(self.cache),
                                      "--installed-json", str(self.installed))
        self.assertEqual(code, 0)
        self.assertIn("bbbb2222  loaded 0.114.0  behind  —", out)


class ComposerTest(unittest.TestCase):
    """An EMPTY composer is ready — including one showing the TUI's DIM placeholder hint.

    The placeholder is drawn with SGR attribute 2 (faint), which is the only thing that
    separates it from text somebody typed; the real pane read is measured in the docstring of
    `pane_is_ready`. Typed text is never dim, so the concatenation hazard is still refused.
    """

    PLACEHOLDER = '\x1b[39m❯\xa0\x1b(B\x1b[0;2mTry "how does X work?"'

    def test_the_real_placeholder_line_is_ready(self):
        self.assertTrue(census_mod.pane_is_ready(self.PLACEHOLDER))

    def test_an_empty_composer_is_ready(self):
        self.assertTrue(census_mod.pane_is_ready("❯ "))

    def test_a_composer_holding_typed_text_is_not_ready(self):
        """Enter would submit a line this script did not write — the 2026-10-05 hazard."""
        self.assertFalse(census_mod.pane_is_ready("❯ draft ok"))
        self.assertFalse(census_mod.pane_is_ready("\x1b[39m❯\xa0draft ok"))

    def test_the_last_glyph_line_is_the_composer_not_an_echoed_prompt(self):
        text = "❯ /reload-plugins\n  ⎿  Reloaded: 13 plugins\n❯ "
        self.assertTrue(census_mod.pane_is_ready(text))

    def test_scrollback_alone_is_not_a_composer(self):
        """A glyph left above a busy pane must not read as 'at a prompt'."""
        self.assertFalse(census_mod.pane_is_ready("❯ /reload-plugins\n✻ Improvising... (5s)"))

    def test_a_dim_echoed_prompt_above_a_typed_composer_does_not_excuse_it(self):
        text = '❯ \x1b[0;2mold\n❯ draft ok'
        self.assertFalse(census_mod.pane_is_ready(text))

    def test_truecolor_is_not_faint(self):
        """`38;2;r;g;b` carries a bare `2` as a colour sub-parameter, not the attribute."""
        self.assertFalse(census_mod.pane_is_ready("❯ \x1b[38;2;255;0;0mdraft ok"))
        self.assertFalse(census_mod.pane_is_ready("❯ \x1b[38;5;2mdraft ok"))

    def test_a_dim_span_does_not_excuse_typed_text_after_the_reset(self):
        """The whole visible remainder must be dim, not merely contain a dim span."""
        self.assertFalse(census_mod.pane_is_ready("❯ \x1b[0;2mghost\x1b[0mdraft ok"))
        self.assertTrue(census_mod.pane_is_ready("❯ \x1b[0;2mTry \"x\"\x1b[0m"))

    def test_faint_is_cancelled_by_22(self):
        self.assertFalse(census_mod.pane_is_ready("❯ \x1b[2mghost\x1b[22mdraft ok"))


class ReloadPaneTest(unittest.TestCase):
    """The lever's confirmation is the NEWEST marker for the pid moving — never the
    pre-reload version directory, which a reload writes into the new dir and leaves alone."""

    OLD, NEW = "0.114.0", "0.114.1"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cache = pathlib.Path(self.dir)
        self.old = self.cache / "claude-supervisor" / "supervisor" / self.OLD / ".in_use" / "11"
        touch(self.old, 1_000)

    def reload(self, side_effect):
        with mock.patch.object(census_mod.subprocess, "run", side_effect=side_effect):
            return census_mod.reload_pane("203", 11, self.cache, "claude-supervisor", "supervisor")

    def ready_pane(self, cmd, **kwargs):
        if cmd[2] == "get-text":
            # The flag is the discriminator: without it the placeholder is indistinguishable
            # from typed text, the gate is always false, and the whole suite would still pass.
            self.assertIn("--escapes", cmd, "readiness must read the styled pane text")
            return mock.Mock(returncode=0, stdout="❯ ", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")

    def test_a_failed_get_text_is_reported_as_a_read_failure(self):
        """Never `composer not empty` — that would re-enter the fleet-wide misdiagnosis."""
        def run(cmd, **kwargs):
            if cmd[2] == "get-text":
                return mock.Mock(returncode=1, stdout="", stderr="unknown flag --escapes")
            return mock.Mock(returncode=0, stdout="", stderr="")
        result = self.reload(run)
        self.assertFalse(result["ok"])
        self.assertIn("get-text exited 1", result["why"])

    def test_success_is_a_new_marker_in_the_new_version_directory(self):
        """The realistic reload: a NEW entry appears and the OLD one is untouched."""
        def run(cmd, **kwargs):
            if cmd[2] == "send-text":
                touch(self.cache / "claude-supervisor" / "supervisor" / self.NEW / ".in_use" / "11", 2_000)
            return self.ready_pane(cmd, **kwargs)
        result = self.reload(run)
        self.assertTrue(result["ok"])
        self.assertIn(self.OLD, result["why"])
        self.assertIn(self.NEW, result["why"])
        # The old marker really is untouched — the case the earlier version could not see.
        self.assertEqual(os.path.getmtime(self.old), 1_000)

    def test_watching_the_old_marker_alone_would_never_fire(self):
        """Pins the defect: after a realistic reload the old marker's mtime is unchanged."""
        def run(cmd, **kwargs):
            if cmd[2] == "send-text":
                touch(self.cache / "claude-supervisor" / "supervisor" / self.NEW / ".in_use" / "11", 2_000)
            return self.ready_pane(cmd, **kwargs)
        self.reload(run)
        self.assertEqual(os.path.getmtime(self.old), 1_000)

    def test_a_reload_with_no_marker_before_the_send_is_unverified(self):
        self.old.unlink()
        result = self.reload(self.ready_pane)
        self.assertFalse(result["ok"])
        self.assertIn("no marker before the send", result["why"])

    def test_a_busy_pane_is_refused_before_any_send(self):
        sent = []
        def run(cmd, **kwargs):
            if cmd[2] == "send-text":
                sent.append(cmd)
            return mock.Mock(returncode=0, stdout="✻ Improvising... (5s)", stderr="")
        with mock.patch.object(census_mod, "RELOAD_READY_TIMEOUT", 0.1):
            result = self.reload(run)
        self.assertFalse(result["ok"])
        self.assertEqual(sent, [])

    def test_a_marker_that_does_not_move_is_unconfirmed(self):
        with mock.patch.object(census_mod, "RELOAD_SETTLE_TIMEOUT", 0.1):
            result = self.reload(self.ready_pane)
        self.assertFalse(result["ok"])
        self.assertIn("unconfirmed", result["why"])
        self.assertIn("unconfirmed", result["why"])


if __name__ == "__main__":
    unittest.main()
