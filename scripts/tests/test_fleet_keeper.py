#!/usr/bin/env python3
"""Tests for scripts/fleet-keeper.py, the report-only fleet keeper.

The contract (operator decision 2026-11-01, card efc771fb): REPORT a sustained lapse, never act
on it. The earlier restart-on-stale keeper killed a healthy Fleet Manager because its trigger
cannot tell a slow round from a dead loop. These tests pin the replacement:

  SC1  report after 40 min of continuous `stale` · a slow round (39 min, then `ok`) is not reported
  SC2  no kill / resume / spawn path in the source, with a positive control
  SC4  one card per lapse; a fresh lapse after `ok` gets its own
  SC3  an ABSENT fleet manager is reported      SC6  the heartbeat survives a failed tick
"""

import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

_HERE = pathlib.Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent

_spec = importlib.util.spec_from_file_location("fleet_keeper", str(_SCRIPTS / "fleet-keeper.py"))
fk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fk)

_REAL_VERDICT, _REAL_STAND_DOWN, _REAL_NOTIFY = fk.verdict, fk.stand_down, fk.notify

SID = "5b076ba2-4efc-4dcb-0000-000000000000"
T0 = 1_800_000_000.0
MIN = 60


class KeeperBase(unittest.TestCase):
    PATCHED = ("STATE_DIR", "HEARTBEAT", "KEEPER_LOG", "LAPSE", "REGISTER_DIR",
               "verdict", "liveness", "notify", "stand_down")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        state = root / "state"
        state.mkdir()
        self.register = root / "register"
        self.register.mkdir()

        self._saved = {k: getattr(fk, k) for k in self.PATCHED}
        self.addCleanup(lambda: [setattr(fk, k, v) for k, v in self._saved.items()])
        fk.STATE_DIR = str(state)
        fk.HEARTBEAT = str(state / "heartbeat")
        fk.KEEPER_LOG = str(state / "keeper.log")
        fk.LAPSE = str(state / "lapse.json")
        fk.REGISTER_DIR = str(self.register)

        self.word = "stale"
        self.live = "LIVE"
        self.stood = "running"
        self.cards = []
        fk.verdict = lambda ledger=None: (self.word, "detail")
        fk.liveness = lambda sid: self.live
        fk.stand_down = lambda: self.stood
        fk.notify = lambda key, payload, options, context="": (
            self.cards.append(key) or f"item-{len(self.cards)}"
        )
        self.register_fleet()

    def register_fleet(self, sid=SID):
        (self.register / f"{sid}.json").write_text(
            json.dumps({"subject": "Fleet Manager", "branch": "fleet", "vault": "v"}),
            encoding="utf-8",
        )

    def tick(self, at):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = fk.main(["--now", str(at)])
        return rc, out.getvalue().splitlines()

    def stale_run(self, start, minutes):
        """Tick once a minute for `minutes` minutes of continuous `stale`; return the last verdict."""
        self.word = "stale"
        last = None
        for m in range(minutes + 1):
            last = self.tick(start + m * MIN)[1][0]
        return last


class ReportOnlyContractTest(KeeperBase):
    # ---- SC1 ---------------------------------------------------------------

    def test_a_lapse_held_for_40_minutes_is_reported_once(self):
        self.assertEqual("reported", self.stale_run(T0, 40))
        self.assertEqual(["fleet-keeper-lapse-%d" % int(T0)], self.cards)
        self.assertIn("reported", pathlib.Path(fk.KEEPER_LOG).read_text())

    def test_a_slow_healthy_round_is_never_reported(self):
        """Negative control: 39 min stale and then `ok`. A stub that reports on the first stale
        tick fails here."""
        self.assertEqual("pending", self.stale_run(T0, 39))
        self.word = "ok"
        self.assertEqual("ok", self.tick(T0 + 40 * MIN)[1][0])
        self.assertEqual([], self.cards)

    def test_stale_ticks_before_the_threshold_are_pending(self):
        rc, out = self.tick(T0)
        self.assertEqual("pending", out[0])
        self.assertEqual(0, rc)

    # ---- SC4 ---------------------------------------------------------------

    def test_one_card_per_lapse_however_long_it_lasts(self):
        self.stale_run(T0, 120)
        self.assertEqual(1, len(self.cards))

    def test_a_fresh_lapse_after_recovery_gets_its_own_card(self):
        self.stale_run(T0, 45)
        self.word = "ok"
        self.tick(T0 + 46 * MIN)
        self.stale_run(T0 + 50 * MIN, 45)
        self.assertEqual(2, len(self.cards))
        self.assertNotEqual(self.cards[0], self.cards[1], "each lapse is deduplicated on its own start")

    def test_a_failed_card_post_is_retried_on_the_next_tick(self):
        fk.notify = lambda *a, **k: None
        self.stale_run(T0, 40)
        posted = []
        fk.notify = lambda key, *a, **k: posted.append(key) or "item-x"
        self.tick(T0 + 41 * MIN)
        self.assertEqual(1, len(posted), "a lapse whose card never posted must still be reported")

    def test_an_unwritable_lapse_state_is_reported_not_silent(self):
        """Without a persisted start, every tick restarts the timer and a real lapse is never
        reported. That must surface, not read as a quiet fleet."""
        fk.LAPSE = "/nonexistent-dir-for-fleet-keeper-test/lapse.json"
        fk.STATE_DIR = "/nonexistent-dir-for-fleet-keeper-test"
        rc, out = self.tick(T0)
        self.assertEqual("cannot-tell", out[0])
        self.assertIn("fleet-keeper-state-unwritable", self.cards)

    def test_a_start_time_in_the_future_restarts_the_timer(self):
        pathlib.Path(fk.LAPSE).write_text(json.dumps({"since": T0 + 3600, "reported": None}))
        self.assertEqual("pending", self.tick(T0)[1][0])
        self.assertEqual(T0, json.loads(pathlib.Path(fk.LAPSE).read_text())["since"])

    def test_an_unreadable_stop_state_still_reports_and_says_so_on_the_card(self):
        """Replaces the restart-era `kills nothing` test. Report-only cannot kill, so an
        unreadable stop state no longer has to block. It must still report, and the CARD — not
        just the log — must carry the caveat, because the operator reads the card."""
        payloads = []
        fk.notify = lambda key, payload, options, context="": (payloads.append(payload) or "item-1")
        self.stood = None
        self.assertEqual("reported", self.stale_run(T0, 40))
        self.assertTrue(payloads)
        self.assertIn("could not read whether the fleet was stood down", payloads[-1])

    def test_an_undecidable_liveness_probe_does_not_read_as_absent(self):
        """Replaces the restart-era liveness test: `UNKNOWN` must not raise the ABSENT card."""
        for word in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            self.live = word
            self.assertNotEqual("dead", self.tick(T0)[1][0], word)
        self.assertNotIn("fleet-keeper-absent", self.cards)

    def test_a_failed_reported_marker_still_holds_the_board_to_one_card(self):
        """If the card posts but the marker cannot be saved, the repost uses the same key."""
        self.stale_run(T0, 40)
        real_save = fk.save_lapse
        fk.save_lapse = lambda rec: False
        self.addCleanup(lambda: setattr(fk, "save_lapse", real_save))
        self.tick(T0 + 41 * MIN)
        self.assertEqual(1, len(set(self.cards)), "every post must share one dedup key")

    # ---- SC3, stop marker, unknown ------------------------------------------

    def test_an_absent_fleet_manager_is_reported(self):
        self.live = "ABSENT"
        rc, out = self.tick(T0)
        self.assertEqual("dead", out[0])
        self.assertIn("fleet-keeper-absent", self.cards)

    def test_a_deliberate_stop_is_left_alone_and_clears_the_lapse(self):
        self.stale_run(T0, 10)
        self.stood = "STOPPED"
        self.assertEqual("stopped", self.tick(T0 + 11 * MIN)[1][0])
        self.assertFalse(os.path.exists(fk.LAPSE))
        self.assertEqual([], self.cards)

    def test_an_unreadable_ledger_is_reported_never_fine(self):
        self.word = "unknown"
        self.assertEqual("cannot-tell", self.tick(T0)[1][0])
        self.assertIn("fleet-keeper-unknown", self.cards)

    # ---- SC6 ---------------------------------------------------------------

    def test_the_heartbeat_is_written_even_on_a_failed_tick(self):
        self.word = "unknown"
        self.tick(T0)
        self.assertEqual(f"{T0:.0f}", pathlib.Path(fk.HEARTBEAT).read_text().strip())


class NoKillPathTest(unittest.TestCase):
    """SC2: the keeper holds no kill, resume, re-arm or spawn path at all."""

    KILL_SHAPES = re.compile(
        r"restart-worker|os\.kill|\"spawn\"|send-text|killing-pid|--resume"
        r"|wezterm|launchctl|importlib|Popen|load_sibling"
    )

    @staticmethod
    def code_only(path):
        """The source with docstrings and full-line comments removed, so prose describing the
        old kill path can neither pass nor fail the check."""
        src = (_SCRIPTS / path).read_text(encoding="utf-8")
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

    def test_the_keeper_source_has_no_kill_or_resume_path(self):
        self.assertEqual([], self.KILL_SHAPES.findall(self.code_only("fleet-keeper.py")))

    def test_positive_control_the_pattern_fires_on_stripped_real_kill_code(self):
        """Runs through the SAME stripping as the check above, so a stripper that deleted code
        would make this fail rather than let the real check pass blind."""
        self.assertTrue(self.KILL_SHAPES.search(self.code_only("restart-worker.py")))


class VerdictTest(unittest.TestCase):
    """The keepalive's word and exit code must agree; a disagreement is `unknown`."""

    def check(self, stdout, rc):
        done = subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")
        with mock.patch.object(fk.subprocess, "run", return_value=done):
            return _REAL_VERDICT()[0]

    def test_agreement(self):
        self.assertEqual("stale", self.check("stale\nlate\n", 10))
        self.assertEqual("ok", self.check("ok\nfine\n", 0))

    def test_disagreement_is_unknown(self):
        self.assertEqual("unknown", self.check("stale\n", 2))
        self.assertEqual("unknown", self.check("ok\n", 10))

    def test_a_hung_keepalive_is_unknown(self):
        with mock.patch.object(fk.subprocess, "run", side_effect=subprocess.TimeoutExpired(["x"], 30)):
            self.assertEqual("unknown", _REAL_VERDICT()[0])


class StandDownTest(unittest.TestCase):
    def check(self, stdout, rc=0):
        done = subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")
        with mock.patch.object(fk.subprocess, "run", return_value=done):
            return _REAL_STAND_DOWN()

    def test_mapping(self):
        self.assertEqual("STOPPED", self.check("STOPPED\tfleet--fleet-manager\tmarker\n"))
        self.assertEqual("running", self.check("STALE\tfleet--fleet-manager\tage\n"))
        self.assertIsNone(self.check("garbage\n"))
        self.assertIsNone(self.check("STOPPED\tx\n", rc=2))

    def test_the_real_stop_writer_and_this_reader_agree(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"MANAGER_LIVENESS_STATE_DIR": tmp}):
            subprocess.run([fk.sys.executable, fk.MANAGER_LIVENESS, "--stop",
                            "--subject", fk.SUBJECT, "--branch", fk.BRANCH],
                           capture_output=True, text=True, timeout=30, check=True)
            self.assertEqual("STOPPED", _REAL_STAND_DOWN())


class NotifyParseTest(unittest.TestCase):
    def test_it_reads_the_item_id_line(self):
        done = subprocess.CompletedProcess([], 0, stdout="ITEM_ID: abc123\nPOLL: x\n", stderr="")
        with mock.patch.object(fk.subprocess, "run", return_value=done):
            self.assertEqual("abc123", _REAL_NOTIFY("k", "p", ["acknowledge"]))

    def test_a_failed_post_is_none(self):
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch.object(fk.subprocess, "run", return_value=done):
            self.assertIsNone(_REAL_NOTIFY("k", "p", ["acknowledge"]))


if __name__ == "__main__":
    unittest.main()
