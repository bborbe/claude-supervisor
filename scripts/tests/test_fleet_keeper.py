#!/usr/bin/env python3
"""Tests for scripts/fleet-keeper.py — the fleet tier's ACT leg.

The defect this pins: `fleet-keepalive.py` shipped the detection half and stopped. It printed
`stale` and the words "Resume the fleet manager", and nothing resumed. Most tests inject the
verdict and the liveness answer rather than manufacturing a session, because the two verdicts
that matter most — `ABSENT` and `UNKNOWN` — cannot be told apart by a fake id: an id that was
never real reads `ABSENT`, and `UNKNOWN` is the cannot-tell answer a fabricated case never
produces.

⚠️ **Two things a lambda-only suite hid, and these tests now cover directly:**

  * a resumed manager is idle with no armed loop, so the keeper must RE-ARM it — otherwise it
    re-kills a healthy manager every tick until the cap trips (`RearmTest`);
  * every resume kills first, so the cap must count a resume that RAN even when its landing
    cannot be confirmed — otherwise the kill loop has no bound (`test_an_unconfirmed_re_arm_
    still_arms_the_cap`, and `RealCapTest` against `topic-keepalive.py`'s real store).

The four behaviours the task's DoD names each have a test:

  lapsed loop restarted · absent manager reported and NOT restarted · restart cap ·
  keeper death distinguishable from fleet health
"""

import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

_HERE = pathlib.Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location(
    "fleet_keeper", str(_HERE.parent / "fleet-keeper.py")
)
fk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fk)

SID = "ac24db61-1dad-4c89-b1fe-18b454067584"
NOW = 1_800_000_000.0


class FakeCap:
    """`topic-keepalive.py`'s cap surface, backed by a temp file.

    A real import here would write into `~/.claude/state/manager-resumes/` and arm the LIVE cap
    against the host. `RealCapTest` below exercises the real module against a temp HOME.
    """

    RESUME_CAP = 3
    RESUME_WINDOW = 30 * 60

    def __init__(self, path):
        self.path = path

    def state_path(self, vault, subject, branch="goal"):
        return str(self.path)

    def load_resumes(self, path):
        try:
            with open(path, encoding="utf-8") as fh:
                return [float(s) for s in json.load(fh).get("resumes", [])]
        except (OSError, ValueError):
            return []

    def recent(self, stamps, now):
        return [s for s in stamps if now - s < self.RESUME_WINDOW]

    def record(self, path, stamps, now):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"resumes": self.recent(stamps, now) + [now]}, fh)


class KeeperBase(unittest.TestCase):
    PATCHED = ("STATE_DIR", "HEARTBEAT", "KEEPER_LOG", "REGISTER_DIR",
               "verdict", "liveness", "restart", "rearm", "notify", "load_sibling",
               "registry_pid")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)

        self.state = root / "state"
        self.state.mkdir()
        self.register = root / "register"
        self.register.mkdir()
        self.cap = FakeCap(root / "cap.json")

        # Point every state path at the temp tree, so a test can never touch the host's.
        self._saved = {k: getattr(fk, k) for k in self.PATCHED}
        self.addCleanup(self._restore)
        fk.STATE_DIR = str(self.state)
        fk.HEARTBEAT = str(self.state / "heartbeat")
        fk.KEEPER_LOG = str(self.state / "keeper.log")
        fk.REGISTER_DIR = str(self.register)
        fk.load_sibling = lambda _name: self.cap

        self.restarts = []
        self.rearms = []
        self.cards = []
        self.verdict_word = "stale"
        self.live = {}            # sid -> liveness answer; default LIVE
        self.restart_outcome = ("ran", "resumed", "404")
        self.rearm_result = (True, "session is busy")

        # `fleet_session` is NOT patched: the registration reader is part of what these tests
        # cover, and a canned answer would hide the no-registration and ambiguity cases.
        fk.verdict = lambda ledger=None: (self.verdict_word, "detail")
        fk.liveness = lambda sid: self.live.get(sid, "LIVE")
        fk.restart = lambda sid, dry=False: (self.restarts.append((sid, dry)) or self.restart_outcome)
        fk.rearm = lambda pane, sid, old_pid: (self.rearms.append((pane, sid, old_pid)) or self.rearm_result)
        fk.registry_pid = lambda sid: 4242
        fk.notify = lambda key, payload, options, context="": (self.cards.append(key) or f"item-{key}")

    def _restore(self):
        for key, value in self._saved.items():
            setattr(fk, key, value)

    def run_keeper(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = fk.main(list(args))
        return rc, out.getvalue().splitlines()

    def register_fleet(self, sid=SID, vault="private-personal", mtime=None):
        path = pathlib.Path(self.register, f"{sid}.json")
        path.write_text(
            json.dumps({"subject": "Fleet Manager", "branch": "fleet", "vault": vault}),
            encoding="utf-8",
        )
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def seed_resumes(self, *ages):
        """Record past resumes at `NOW - age` for each age."""
        with open(self.cap.path, "w", encoding="utf-8") as fh:
            json.dump({"resumes": [NOW - age for age in ages]}, fh)


class FleetKeeperTest(KeeperBase):
    # --- the four DoD behaviours ---------------------------------------------

    def test_a_lapsed_loop_is_restarted_and_re_armed(self):
        """SC1/SC2 — resume the recorded id, re-arm its loop, count it against the cap."""
        self.register_fleet()
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual(0, rc)
        self.assertEqual("restarted", out[0])
        self.assertEqual([(SID, False)], self.restarts)
        self.assertEqual([("404", SID, 4242)], self.rearms,
                         "the resumed manager must be re-armed, told apart by the pre-kill pid")
        self.assertEqual(1, len(self.cap.load_resumes(self.cap.path)))

    def test_an_absent_manager_is_reported_and_never_restarted(self):
        """SC3 — a dead fleet manager may have died for a reason a restart repeats."""
        self.register_fleet()
        self.live[SID] = "ABSENT"
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual(1, rc)
        self.assertEqual("dead", out[0])
        self.assertEqual([], self.restarts, "ABSENT must never reach the restart leg")
        self.assertIn("fleet-keeper-absent", self.cards)

    def test_the_restart_cap_stops_after_three(self):
        """SC4 — the same bound `topic-keepalive.py` uses, not a second one."""
        self.register_fleet()
        self.seed_resumes(1500, 1000, 600)  # three, all outside the settle window
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual(1, rc)
        self.assertEqual("crash-loop", out[0])
        self.assertEqual([], self.restarts)
        self.assertIn("fleet-keeper-crash-loop", self.cards)

    def test_a_keeper_that_never_ran_is_distinguishable_from_a_healthy_fleet(self):
        """SC6 — the heartbeat is the keeper's own instrument, written before anything can fail.

        The tick here reaches `cannot-tell`; the heartbeat must still be on disk, so a dead
        keeper cannot hide behind a fleet that reads fine.
        """
        self.verdict_word = "unknown"
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])
        self.assertTrue(pathlib.Path(fk.HEARTBEAT).exists())
        self.assertEqual(f"{NOW:.0f}", pathlib.Path(fk.HEARTBEAT).read_text().strip())

    # --- the cap, the settle window and the re-arm ----------------------------

    def test_an_unconfirmed_re_arm_still_arms_the_cap(self):
        """Every resume kills first. A resume whose landing cannot be confirmed must still
        count, or the keeper kills a live manager every tick with no bound."""
        self.register_fleet()
        self.rearm_result = (False, "pane never showed the colour line")
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual(1, len(self.cap.load_resumes(self.cap.path)))
        self.assertIn("fleet-keeper-rearm-unconfirmed", self.cards)

    def test_a_failed_restart_still_arms_the_cap(self):
        """`resume failed` is printed AFTER the kill, so it counts too."""
        self.register_fleet()
        self.restart_outcome = ("failed", "resume failed (exit 1)", None)
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual(1, len(self.cap.load_resumes(self.cap.path)))
        self.assertEqual([], self.rearms)
        self.assertIn("fleet-keeper-restart-failed", self.cards)

    def test_a_manager_still_coming_up_is_left_alone(self):
        """Within HOLD_OFF of a resume, a still-stale round is the manager booting, not a lapse."""
        self.register_fleet()
        self.seed_resumes(60)
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual(0, rc)
        self.assertEqual("settling", out[0])
        self.assertEqual([], self.restarts)

    def test_the_settle_window_ends(self):
        self.register_fleet()
        self.seed_resumes(fk.HOLD_OFF + 1)
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual("restarted", out[0])
        self.assertEqual(2, len(self.cap.load_resumes(self.cap.path)))

    def test_the_cap_is_reachable_through_the_settle_window(self):
        """A settle window as long as the cap window would make the cap unreachable; three
        resumes spaced past HOLD_OFF must still land inside 30 minutes."""
        self.assertLess(3 * fk.HOLD_OFF, self.cap.RESUME_WINDOW)

    # --- refusals: nothing may be killed --------------------------------------

    def test_a_busy_manager_is_not_a_lapse_and_arms_nothing(self):
        self.register_fleet()
        self.restart_outcome = ("busy", "busy-target", None)
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual("busy", out[0])
        self.assertEqual([], self.cap.load_resumes(self.cap.path))
        self.assertEqual([], self.cards, "mid-turn is transient — no card")

    def test_a_lasting_refusal_is_carded_and_arms_nothing(self):
        self.register_fleet()
        self.restart_outcome = ("refused", "held-session", None)
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual("refused", out[0])
        self.assertEqual([], self.cap.load_resumes(self.cap.path))
        self.assertIn("fleet-keeper-restart-refused", self.cards)

    def test_a_healthy_round_does_nothing(self):
        self.verdict_word = "ok"
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual(0, rc)
        self.assertEqual("ok", out[0])
        self.assertEqual([], self.restarts)

    def test_an_unreadable_ledger_is_never_fine(self):
        self.verdict_word = "unknown"
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual([], self.restarts)
        self.assertIn("fleet-keeper-unknown", self.cards)

    def test_an_undecidable_liveness_probe_is_never_death_and_is_carded(self):
        """`UNKNOWN` must not be folded into `ABSENT`, and must not be silent either."""
        self.register_fleet()
        for word in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            self.live[SID] = word
            rc, out = self.run_keeper("--now", str(NOW))
            self.assertEqual(1, rc)
            self.assertEqual("cannot-tell", out[0], word)
        self.assertEqual([], self.restarts)
        self.assertIn("fleet-keeper-liveness-unknown", self.cards)

    def test_a_dry_run_restarts_nothing_and_records_nothing(self):
        self.register_fleet()
        rc, out = self.run_keeper("--dry-run", "--now", str(NOW))
        self.assertEqual(0, rc)
        self.assertEqual("restarted", out[0])
        self.assertEqual([(SID, True)], self.restarts, "dry-run must pass --dry-run through")
        self.assertEqual([], self.rearms)
        self.assertEqual([], self.cap.load_resumes(self.cap.path))

    # --- the registration reader ---------------------------------------------

    def test_no_registration_cannot_resume(self):
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual([], self.restarts)

    def test_two_live_registrations_are_refused_not_picked(self):
        """Resuming one of two fleet managers restarts the wrong half of a double writer."""
        self.register_fleet("aaaaaaaa-0000-0000-0000-000000000001")
        self.register_fleet("bbbbbbbb-0000-0000-0000-000000000002")
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual([], self.restarts)

    def test_a_dead_registration_does_not_make_the_live_one_ambiguous(self):
        """Nothing deletes a registration, so a replaced manager leaves one behind. Read raw,
        the first replacement would freeze the keeper on `AMBIGUOUS` for good."""
        old = "aaaaaaaa-0000-0000-0000-000000000001"
        self.register_fleet(old, mtime=NOW - 86400)
        self.register_fleet(SID, mtime=NOW - 60)
        self.live[old] = "ABSENT"
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual("restarted", out[0])
        self.assertEqual([(SID, False)], self.restarts)

    def test_an_undecidable_old_registration_still_counts_as_a_claimant(self):
        """Only ABSENT is dropped — cannot-tell is never death, so an UNKNOWN claimant stays."""
        other = "aaaaaaaa-0000-0000-0000-000000000001"
        self.register_fleet(other)
        self.register_fleet(SID)
        self.live[other] = "UNKNOWN"
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual([], self.restarts)

    def test_a_dead_newest_registration_is_reported_never_routed_around(self):
        """If the current manager died while a replaced one is still open in some tab, the
        keeper must report the death — never resurrect the stale manager."""
        old = "aaaaaaaa-0000-0000-0000-000000000001"
        self.register_fleet(old, mtime=NOW - 86400)
        self.register_fleet(SID, mtime=NOW - 60)
        self.live[SID] = "ABSENT"
        self.live[old] = "LIVE"
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual("dead", out[0])
        self.assertIn(SID[:8], out[1])
        self.assertEqual([], self.restarts, "the stale manager must not be resumed")

    def test_when_every_claimant_is_gone_the_newest_is_reported_dead(self):
        old = "aaaaaaaa-0000-0000-0000-000000000001"
        self.register_fleet(old, mtime=NOW - 86400)
        self.register_fleet(SID, mtime=NOW - 60)
        self.live[old] = self.live[SID] = "ABSENT"
        rc, out = self.run_keeper("--now", str(NOW))
        self.assertEqual("dead", out[0])
        self.assertIn(SID[:8], out[1])
        self.assertEqual([], self.restarts)


class VerdictTest(unittest.TestCase):
    """The verdict word and the exit code must agree — a disagreement is `unknown`."""

    def check(self, stdout, rc):
        done = subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")
        with mock.patch.object(fk.subprocess, "run", return_value=done):
            return _REAL_VERDICT()

    def test_agreeing_stale_is_stale(self):
        self.assertEqual("stale", self.check("stale\nthe round is late\n", 10)[0])

    def test_agreeing_ok_is_ok(self):
        self.assertEqual("ok", self.check("ok\ninside\n", 0)[0])

    def test_a_stale_word_beside_exit_2_is_not_a_restart(self):
        self.assertEqual("unknown", self.check("stale\n", 2)[0])

    def test_an_ok_word_beside_exit_10_is_not_fine(self):
        self.assertEqual("unknown", self.check("ok\n", 10)[0])

    def test_an_unknown_word_is_unknown(self):
        self.assertEqual("unknown", self.check("garbage\n", 0)[0])

    def test_a_hung_keepalive_is_unknown(self):
        with mock.patch.object(fk.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["x"], 30)):
            self.assertEqual("unknown", _REAL_VERDICT()[0])


_REAL_VERDICT = fk.verdict
_REAL_REARM = fk.rearm
_REAL_RESTART = fk.restart


class RestartMappingTest(unittest.TestCase):
    """`restart-worker.py`'s output mapped to the keeper's four outcomes."""

    def check(self, stdout, rc):
        done = subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")
        with mock.patch.object(fk.subprocess, "run", return_value=done) as run:
            result = _REAL_RESTART(SID)
        return result, run.call_args[0][0]

    def test_it_always_passes_reason_lapse(self):
        _, argv = self.check("🔄 restart-worker\n   ✅ killed pid 1; resumed (pane 77)\n", 0)
        self.assertEqual(["--reason", "lapse"], argv[3:5])

    def test_success_names_the_new_pane(self):
        (outcome, _, pane), _ = self.check("   ✅ killed pid 1; resumed session x in a new tab (pane 77)\n", 0)
        self.assertEqual(("ran", "77"), (outcome, pane))

    def test_busy_target_is_transient(self):
        (outcome, _, _), _ = self.check("busy-target\n❌ busy-target: mid-turn\n", 1)
        self.assertEqual("busy", outcome)

    def test_a_held_session_is_a_lasting_refusal(self):
        (outcome, _, _), _ = self.check("held-session\n❌ held-session: held\n", 1)
        self.assertEqual("refused", outcome)

    def test_a_resume_that_failed_after_the_kill_is_failed(self):
        (outcome, _, _), _ = self.check(
            "🔄 restart-worker · session ac24db61\n   killing-pid 1\n"
            "unknown-session-id\n❌ unknown-session-id: resume failed (exit 1): boom\n", 1)
        self.assertEqual("failed", outcome)

    def test_a_signal_error_after_the_token_is_failed(self):
        (outcome, _, _), _ = self.check(
            "🔄 restart-worker\n   killing-pid 1\n❌ error: pid 1 resolved but cannot be signalled\n", 1)
        self.assertEqual("failed", outcome)

    def test_a_refusal_printed_after_the_header_but_before_the_kill_is_refused(self):
        """The header is the first line here, so a first-line read could not classify it —
        only the absent `killing-pid` token proves nothing was killed."""
        (outcome, _, _), _ = self.check(
            "🔄 restart-worker · session ac24db61\n   role: manager\n"
            "❌ error: no launcher could be resolved — refusing before the kill\n", 1)
        self.assertEqual("refused", outcome)

    def test_a_hang_is_failed_never_busy(self):
        """A timeout can land after `os.kill`, so it must not read as the transient case."""
        with mock.patch.object(fk.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["x"], 90)):
            self.assertEqual("failed", _REAL_RESTART(SID)[0])


class RearmTest(unittest.TestCase):
    """`commands/open.md` § 3.2's recipe: wait until ready, send, submit separately, confirm the
    RESUMED process went busy.

    `registry_records` is the seam: each call pops one snapshot of the registry, so a test
    states exactly what the keeper sees on each poll — including the killed process's record
    still sitting beside the new one.
    """

    OLD, NEW = 4242, 5151
    READY = "…old transcript…\nSession color set to: orange\n╭─────╮\n│ >   │\n╰─────╯"

    def setUp(self):
        self.calls = []
        self.screens = []
        self.snapshots = []
        self.t = [0.0]
        self._saved = (fk.wezterm, fk.registry_records)
        self.addCleanup(self._restore)

        def wezterm(args):
            self.calls.append(args)
            if args[0] == "get-text":
                return 0, (self.screens.pop(0) if len(self.screens) > 1 else
                           (self.screens[0] if self.screens else ""))
            return 0, ""

        def records(_sid):
            snap = self.snapshots.pop(0) if len(self.snapshots) > 1 else (
                self.snapshots[0] if self.snapshots else [])
            return [{"sessionId": SID, "pid": pid, "status": st} for pid, st in snap]

        fk.wezterm = wezterm
        fk.registry_records = records

    def _restore(self):
        fk.wezterm, fk.registry_records = self._saved

    def sleep(self, seconds):
        self.t[0] += seconds

    def clock(self):
        return self.t[0]

    def run_rearm(self, pane="77"):
        return _REAL_REARM(pane, SID, self.OLD, sleep=self.sleep, clock=self.clock)

    def sends(self):
        return [c for c in self.calls if c[0] == "send-text"]

    def test_it_waits_until_ready_then_sends_and_submits_separately(self):
        self.screens = ["replaying…", self.READY]
        self.snapshots = [[(self.NEW, "busy")],          # /color still running
                          [(self.NEW, "idle")],          # ready
                          [(self.NEW, "busy")]]          # the re-arm's own turn
        ok, _ = self.run_rearm()
        self.assertTrue(ok)
        sent = self.sends()
        self.assertEqual(2, len(sent), "text and submit are two separate sends")
        self.assertEqual("/supervisor:fleet-loop", sent[0][-1])
        self.assertEqual("\r", sent[1][-1])
        self.assertTrue(all(c[2] == "77" for c in sent), "only the pane this tick spawned")

    def test_an_old_marker_above_the_tail_is_not_ready(self):
        """A resume re-renders its transcript, which already holds the marker from every earlier
        open. Found only above the last lines, it proves nothing about the new `/color`."""
        stale = "Session color set to: orange\n" + "\n".join(f"history {i}" for i in range(40))
        self.screens = [stale]
        self.snapshots = [[(self.NEW, "idle")]]
        ok, note = self.run_rearm()
        self.assertFalse(ok)
        self.assertEqual([], self.sends(), "nothing may be typed into a session not yet ready")
        self.assertIn("did not reach ready", note)

    def test_the_marker_alone_is_not_ready_while_the_new_process_is_busy(self):
        self.screens = [self.READY]
        self.snapshots = [[(self.NEW, "busy")]]   # never idle
        ok, _ = self.run_rearm()
        self.assertFalse(ok)
        self.assertEqual([], self.sends())

    def test_the_killed_processes_busy_record_is_not_the_new_session_working(self):
        """After the submit the killed process's record still reads `busy`. Without the pid
        filter that would confirm a re-arm that never ran."""
        self.screens = [self.READY]
        self.snapshots = [[(self.OLD, "busy"), (self.NEW, "idle")]]   # forever: new stays idle
        ok, note = self.run_rearm()
        self.assertFalse(ok)
        self.assertEqual(2, len(self.sends()))
        self.assertIn("still reads idle", note)

    def test_no_pane_means_no_re_arm(self):
        ok, _ = _REAL_REARM(None, SID, self.OLD, sleep=self.sleep, clock=self.clock)
        self.assertFalse(ok)
        self.assertEqual([], self.calls)


class RealCapTest(KeeperBase):
    """The cap through `topic-keepalive.py`'s REAL store, against a temp HOME — the FakeCap
    above cannot prove the keeper and the topic tier agree on the file, the slug or the bound."""

    def setUp(self):
        super().setUp()
        home = pathlib.Path(self.tmp.name) / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = home
        fk.load_sibling = self._saved["load_sibling"]

    def test_the_real_store_records_under_the_fleet_slug_and_trips_at_three(self):
        self.register_fleet()
        for i, at in enumerate((NOW, NOW + fk.HOLD_OFF + 1, NOW + 2 * (fk.HOLD_OFF + 1))):
            rc, out = self.run_keeper("--now", str(at))
            self.assertEqual("restarted", out[0], f"resume {i + 1}")
        state = self.home / ".claude/state/manager-resumes/private-personal/fleet--fleet-manager.json"
        self.assertTrue(state.exists(), "the topic tier's store, under the fleet branch's slug")
        self.assertEqual(3, len(json.loads(state.read_text())["resumes"]))

        rc, out = self.run_keeper("--now", str(NOW + 3 * (fk.HOLD_OFF + 1)))
        self.assertEqual("crash-loop", out[0])
        self.assertEqual(3, len(self.restarts), "no fourth kill")


if __name__ == "__main__":
    unittest.main()
