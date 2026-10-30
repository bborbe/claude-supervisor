#!/usr/bin/env python3
"""Tests for scripts/fleet-keeper.py — the fleet tier's ACT leg.

The defect this pins: `fleet-keepalive.py` shipped the detection half and stopped. It printed
`stale` and the words "Resume the fleet manager", and nothing resumed. Every test here injects
the verdict and the liveness answer rather than manufacturing a session, because the two
verdicts that matter most — `ABSENT` and `UNKNOWN` — cannot be told apart by a fake id: an id
that was never real reads `ABSENT`, and `UNKNOWN` is the cannot-tell answer a fabricated case
never produces.

The four behaviours the task's DoD names, one test each:

  lapsed loop restarted · absent manager reported and NOT restarted · restart cap ·
  keeper death distinguishable from fleet health
"""

import importlib.util
import io
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location(
    "fleet_keeper", str(_HERE.parent / "fleet-keeper.py")
)
fk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fk)


class FakeCap:
    """`topic-keepalive.py`'s cap surface, backed by a temp file.

    A real import would write into `~/.claude/state/manager-resumes/` and arm the LIVE cap
    against the host — the one thing a test must never do.
    """

    RESUME_CAP = 3
    RESUME_WINDOW = 30 * 60

    def __init__(self, path):
        self.path = path

    def state_path(self, vault, subject, branch="goal"):
        return str(self.path)

    def load_resumes(self, path):
        try:
            import json

            with open(path, encoding="utf-8") as fh:
                return [float(s) for s in json.load(fh).get("resumes", [])]
        except (OSError, ValueError):
            return []

    def recent(self, stamps, now):
        return [s for s in stamps if now - s < self.RESUME_WINDOW]

    def record(self, path, stamps, now):
        import json

        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"resumes": self.recent(stamps, now) + [now]}, fh)


class FleetKeeperTest(unittest.TestCase):
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
        self._saved = {k: getattr(fk, k) for k in
                       ("STATE_DIR", "HEARTBEAT", "KEEPER_LOG", "REGISTER_DIR",
                        "verdict", "fleet_session", "liveness", "restart", "notify",
                        "load_sibling")}
        fk.STATE_DIR = str(self.state)
        fk.HEARTBEAT = str(self.state / "heartbeat")
        fk.KEEPER_LOG = str(self.state / "keeper.log")
        fk.REGISTER_DIR = str(self.register)
        fk.load_sibling = lambda _name: self.cap

        self.restarts = []
        self.cards = []
        self.verdict_word = "stale"
        self.live_word = "LIVE"
        self.session = ("ac24db61-1dad-4c89-b1fe-18b454067584", "private-personal")
        self.resume_lands = True

        # `fleet_session` is NOT patched: the registration reader is part of what these tests
        # cover, and a canned session would have hidden the "no registration" and "two
        # registrations" cases behind a lambda that always answers.
        fk.verdict = lambda ledger=None: (self.verdict_word, 0, "detail")
        fk.liveness = lambda sid: self.live_word
        fk.restart = lambda sid, dry=False: (self.restarts.append(sid) or True, "resumed")
        fk.notify = lambda key, payload, options, context="": (
            self.cards.append(key) or f"item-{key}"
        )

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(fk, key, value)

    def run_keeper(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = fk.main(list(args))
        return rc, out.getvalue().splitlines()

    def register_fleet(self, sid=None, vault="private-personal"):
        sid = sid or self.session[0]
        pathlib.Path(self.register, f"{sid}.json").write_text(
            '{"subject": "Fleet Manager", "branch": "fleet", "vault": "%s"}' % vault,
            encoding="utf-8",
        )

    # --- the four DoD behaviours ---------------------------------------------

    def test_a_lapsed_loop_is_restarted(self):
        """SC1/SC2 — the act leg fires on the shipped verdict, and the resume is recorded."""
        self.register_fleet()
        rc, out = self.run_keeper()
        self.assertEqual(0, rc)
        self.assertEqual("restarted", out[0])
        self.assertEqual([self.session[0]], self.restarts)
        self.assertEqual(1, len(self.cap.load_resumes(self.cap.path)))

    def test_an_absent_manager_is_reported_and_never_restarted(self):
        """SC3 — a dead fleet manager may have died for a reason a restart repeats."""
        self.register_fleet()
        self.live_word = "ABSENT"
        rc, out = self.run_keeper()
        self.assertEqual(1, rc)
        self.assertEqual("dead", out[0])
        self.assertEqual([], self.restarts, "ABSENT must never reach the restart leg")
        self.assertIn("fleet-keeper-absent", self.cards)

    def test_the_restart_cap_stops_after_three(self):
        """SC4 — the same bound `topic-keepalive.py` uses, not a second one."""
        self.register_fleet()
        now = 1_800_000_000.0
        self.cap.record(self.cap.path, [], now - 10)
        self.cap.record(self.cap.path, self.cap.load_resumes(self.cap.path), now - 5)
        self.cap.record(self.cap.path, self.cap.load_resumes(self.cap.path), now - 1)
        rc, out = self.run_keeper("--now", str(now))
        self.assertEqual(1, rc)
        self.assertEqual("crash-loop", out[0])
        self.assertEqual([], self.restarts)
        self.assertIn("fleet-keeper-crash-loop", self.cards)

    def test_a_keeper_that_never_ran_is_distinguishable_from_a_healthy_fleet(self):
        """SC6 — the heartbeat is the keeper's own instrument, written before anything can fail.

        Here the tick reaches `cannot-tell` (an unreadable verdict); the heartbeat must still
        be on disk, so a dead keeper cannot hide behind a fleet that reads fine.
        """
        self.verdict_word = "unknown"
        rc, out = self.run_keeper()
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])
        self.assertTrue(pathlib.Path(fk.HEARTBEAT).exists(),
                        "the heartbeat must survive a failed tick")

    # --- the negative cases: nothing may act ---------------------------------

    def test_a_healthy_round_does_nothing(self):
        self.verdict_word = "ok"
        rc, out = self.run_keeper()
        self.assertEqual(0, rc)
        self.assertEqual("ok", out[0])
        self.assertEqual([], self.restarts)

    def test_an_unreadable_ledger_is_never_fine(self):
        """`unknown` is cannot-tell: it must not read as healthy and must not restart."""
        self.verdict_word = "unknown"
        rc, out = self.run_keeper()
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual([], self.restarts)
        self.assertIn("fleet-keeper-unknown", self.cards)

    def test_an_undecidable_liveness_probe_is_never_death(self):
        """`UNKNOWN` must not be folded into `ABSENT` — only `ABSENT` licenses a resume."""
        self.register_fleet()
        for word in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            self.live_word = word
            rc, out = self.run_keeper()
            self.assertEqual(1, rc)
            self.assertEqual("cannot-tell", out[0], word)
        self.assertEqual([], self.restarts)

    def test_a_resume_that_does_not_land_does_not_arm_the_cap(self):
        """The cap counts only resumes that TOOK, or a failed resume blocks its own retry."""
        self.register_fleet()
        self.live_word = "LIVE"
        calls = {"n": 0}

        def flaky(_sid):
            calls["n"] += 1
            return "LIVE" if calls["n"] == 1 else "UNKNOWN"

        fk.liveness = flaky
        rc, out = self.run_keeper()
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual([], self.cap.load_resumes(self.cap.path),
                         "a resume that never landed must not arm the cap")

    def test_no_registration_cannot_resume(self):
        rc, out = self.run_keeper()
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual([], self.restarts)

    def test_two_fleet_registrations_are_refused_not_picked(self):
        """Resuming one of two fleet managers restarts the wrong half of a double writer."""
        self.register_fleet("aaaaaaaa-0000-0000-0000-000000000001")
        self.register_fleet("bbbbbbbb-0000-0000-0000-000000000002")
        rc, out = self.run_keeper()
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])
        self.assertEqual([], self.restarts)

    def test_a_dry_run_records_no_resume(self):
        self.register_fleet()
        rc, out = self.run_keeper("--dry-run")
        self.assertEqual(0, rc)
        self.assertEqual("restarted", out[0])
        self.assertEqual([], self.cap.load_resumes(self.cap.path))


if __name__ == "__main__":
    unittest.main()
