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

import ast
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
    PATCHED = ("STATE_DIR", "HEARTBEAT", "KEEPER_LOG", "LAPSE", "EPISODES", "REGISTER_DIR",
               "verdict", "liveness", "notify", "stand_down",
               "answer_of", "death_corroborated", "resume_absent_session")

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
        fk.EPISODES = str(state / "episodes.json")
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
        # Every ABSENT tick now asks the board for an answer. Unpatched, a test would poll the
        # LIVE attention store with a fixture card id — and a resume could never be ruled out.
        fk.answer_of = lambda item: (None, None)
        fk.death_corroborated = lambda sid, now: (False, "not corroborated in a test", False)
        fk.resume_absent_session = lambda sid: self.fail("resume reached from an unpatched test")
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
        self.assertEqual(["fleet-keeper-lapse-%d-d0" % int(T0)], self.cards)
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
        self.assertTrue(any(k.startswith("fleet-keeper-state-unwritable-") for k in self.cards))

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
        self.assertFalse(any(k.startswith("fleet-keeper-absent-") for k in self.cards))

    def test_a_failed_reported_marker_still_holds_the_board_to_one_card(self):
        """The card posts but its marker cannot be saved, so the next tick re-posts. Both posts
        must share one dedup key. The marker save is failed BEFORE the threshold tick — failing
        it afterwards (as an earlier version of this test did) never reaches the re-post at all."""
        self.stale_run(T0, 39)
        real_save = fk.save_lapse
        fk.save_lapse = lambda rec: False
        self.addCleanup(lambda: setattr(fk, "save_lapse", real_save))
        self.tick(T0 + 40 * MIN)
        self.tick(T0 + 41 * MIN)
        self.assertEqual(2, len(self.cards), "the unsaved marker must cause a re-post")
        self.assertEqual(1, len(set(self.cards)), "and both posts must share one dedup key")

    def test_an_unclearable_record_is_reported_so_the_next_lapse_is_not_lost(self):
        """Must-Fix from the bot review: a record that survives recovery keeps `reported`, and the
        NEXT lapse would read as already reported and never get a card."""
        self.stale_run(T0, 40)
        real_clear = fk.clear_lapse
        fk.clear_lapse = lambda: False
        self.addCleanup(lambda: setattr(fk, "clear_lapse", real_clear))
        self.word = "ok"
        self.assertEqual("cannot-tell", self.tick(T0 + 41 * MIN)[1][0])
        self.assertTrue(any(k.startswith("fleet-keeper-state-unclearable-") for k in self.cards))

    def test_an_undecidable_liveness_probe_is_carded_at_once(self):
        self.live = "UNKNOWN"
        self.tick(T0)
        self.assertTrue(any(k.startswith("fleet-keeper-liveness-unknown-") for k in self.cards))

    # ---- SC3, stop marker, unknown ------------------------------------------

    def test_an_absent_fleet_manager_is_reported(self):
        self.live = "ABSENT"
        rc, out = self.tick(T0)
        self.assertEqual("dead", out[0])
        self.assertTrue(any(k.startswith("fleet-keeper-absent-") for k in self.cards))

    def test_a_deliberate_stop_is_left_alone_and_clears_the_lapse(self):
        self.stale_run(T0, 10)
        self.stood = "STOPPED"
        self.assertEqual("stopped", self.tick(T0 + 11 * MIN)[1][0])
        self.assertFalse(os.path.exists(fk.LAPSE))
        self.assertEqual([], self.cards)

    def test_an_unreadable_ledger_is_reported_never_fine(self):
        self.word = "unknown"
        self.assertEqual("cannot-tell", self.tick(T0)[1][0])
        self.assertTrue(any(k.startswith("fleet-keeper-unknown-") for k in self.cards))

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

    #: The one function allowed to hold a resume, by operator decision 2026-11-06 (card
    #: `7653b7b6…`, "add a resume option to the keeper's card, gated on your answer").
    EXECUTOR = "resume_absent_session"
    CALLER = "act_on_answer"

    @staticmethod
    def strip(src):
        """Docstrings and full-line comments removed, so prose can neither pass nor fail."""
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

    def parsed(self):
        """`(source, tree, executor node)` — extracted by `ast`, never by a regex.

        A regex body ("from `def X` to the next `def`") swallows any module-level code placed
        after the function, which would let a kill shape or a second call escape both pins.
        """
        src = (_SCRIPTS / "fleet-keeper.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == self.EXECUTOR]
        self.assertEqual(1, len(nodes), "the answered-resume executor is missing or duplicated")
        return src, tree, nodes[0]

    def test_the_keeper_source_has_no_kill_or_resume_path(self):
        """Narrowed 2026-11-06, NOT deleted: outside the one executor, nothing may match.

        The 2026-11-01 ruling (card efc771fb) removed every kill/resume/spawn path after the
        keeper killed a healthy manager. The operator's 2026-11-06 answer admits exactly one
        resume, gated on their answer, so the pin now holds everywhere but that function.
        """
        src, tree, node = self.parsed()
        rest = src.replace(ast.get_source_segment(src, node), "")
        # Narrowed again 2026-11-10: the answer-gated `restart fresh` executor is the second
        # and last function allowed to hold a restart-worker call.
        fresh = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == FreshRestartPinTest.EXECUTOR]
        self.assertEqual(1, len(fresh))
        rest = rest.replace(ast.get_source_segment(src, fresh[0]), "")
        self.assertEqual([], self.KILL_SHAPES.findall(self.strip(rest)))

    def test_the_executor_can_only_resume_an_absent_session_never_kill(self):
        """Every KILL_SHAPE but the one script it must call stays forbidden inside it."""
        src, _tree, node = self.parsed()
        body = self.strip(ast.get_source_segment(src, node))
        self.assertEqual(["restart-worker"], sorted(set(self.KILL_SHAPES.findall(body))))
        for forbidden in (r"--reason", r"\bkill\b", r"signal"):
            self.assertIsNone(re.search(forbidden, body), f"{forbidden} in the executor")

    def test_the_executor_runs_exactly_the_absent_argv(self):
        """`"--absent"` merely appearing in the body (a message string) is not enough."""
        _src, _tree, node = self.parsed()
        runs = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                and ast.unparse(c.func) == "subprocess.run"]
        self.assertEqual(1, len(runs), "the executor must make exactly one subprocess call")
        argv = runs[0].args[0]
        self.assertIsInstance(argv, ast.List)
        self.assertEqual(["sys.executable", "script", "sid", "'--absent'"],
                         [ast.unparse(e) for e in argv.elts])
        # ...and `script` must BE restart-worker.py: checking the name alone would pass
        # `script = os.path.join(HERE, "close-worker.py")`.
        assigns = [a for a in ast.walk(node) if isinstance(a, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "script" for t in a.targets)]
        self.assertEqual(1, len(assigns))
        self.assertEqual("os.path.join(HERE, 'restart-worker.py')", ast.unparse(assigns[0].value))

    def test_the_executor_is_reachable_only_from_the_answer_gate(self):
        """Any reference — a call, an alias, a `getattr` string — outside the gate fails."""
        _src, tree, _node = self.parsed()
        holders = []
        for top in tree.body:
            if isinstance(top, ast.FunctionDef) and top.name == self.EXECUTOR:
                continue
            for n in ast.walk(top):
                named = (isinstance(n, ast.Name) and n.id == self.EXECUTOR) or \
                        (isinstance(n, ast.Constant) and n.value == self.EXECUTOR) or \
                        (isinstance(n, ast.Attribute) and n.attr == self.EXECUTOR)
                if named:
                    holders.append(getattr(top, "name", type(top).__name__))
        self.assertEqual([self.CALLER], holders)

    def test_positive_control_the_pattern_fires_on_stripped_real_kill_code(self):
        """Runs through the SAME stripping as the check above, so a stripper that deleted code
        would make this fail rather than let the real check pass blind."""
        self.assertTrue(self.KILL_SHAPES.search(self.code_only("restart-worker.py")))


class FreshRestartPinTest(unittest.TestCase):
    """The `restart fresh` executor (operator ruling 2026-11-10) is answer-gated and exact."""

    EXECUTOR = "restart_fresh_session"
    CALLER = "act_on_fresh_answer"

    def parsed(self):
        src = (_SCRIPTS / "fleet-keeper.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == self.EXECUTOR]
        self.assertEqual(1, len(nodes))
        return src, tree, nodes[0]

    def test_the_executor_runs_exactly_the_fresh_argv(self):
        _src, _tree, node = self.parsed()
        runs = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                and ast.unparse(c.func) == "subprocess.run"]
        self.assertEqual(1, len(runs))
        self.assertEqual(["sys.executable", "script", "sid", "'--reason'", "'lapse'",
                          "'--fresh-prompt'", "FRESH_PROMPT"],
                         [ast.unparse(e) for e in runs[0].args[0].elts])
        assigns = [a for a in ast.walk(node) if isinstance(a, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "script" for t in a.targets)]
        self.assertEqual("os.path.join(HERE, 'restart-worker.py')", ast.unparse(assigns[0].value))
        self.assertEqual("/supervisor:fleet-loop", fk.FRESH_PROMPT)

    def test_the_executor_is_reachable_only_from_the_answer_gate(self):
        _src, tree, _node = self.parsed()
        holders = []
        for top in tree.body:
            if isinstance(top, ast.FunctionDef) and top.name == self.EXECUTOR:
                continue
            for n in ast.walk(top):
                if (isinstance(n, ast.Name) and n.id == self.EXECUTOR) or \
                   (isinstance(n, ast.Constant) and n.value == self.EXECUTOR) or \
                   (isinstance(n, ast.Attribute) and n.attr == self.EXECUTOR):
                    holders.append(getattr(top, "name", type(top).__name__))
        self.assertEqual([self.CALLER], holders)

    def test_the_gate_requires_the_fresh_answer(self):
        _src, tree, _node = self.parsed()
        gate = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == self.CALLER][0]
        self.assertIn("option != FRESH_OPTION", ast.unparse(gate))


class AnsweredFreshRestartTest(KeeperBase):
    """`restart fresh` runs ONLY on that answer to a LIVE manager's lapse card, once."""

    def setUp(self):
        super().setUp()
        saved = {k: getattr(fk, k) for k in ("answer_of", "restart_fresh_session")}
        self.addCleanup(lambda: [setattr(fk, k, v) for k, v in saved.items()])
        self.answer = None
        self.restarts = []
        self.options = {}
        fk.answer_of = lambda item: (self.answer, None)
        fk.restart_fresh_session = lambda sid: (self.restarts.append(sid) or (True, "pane 9"))

        def notify(key, payload, options, context=""):
            self.cards.append(key)
            self.options[key] = list(options)
            return f"item-{len(self.cards)}"
        fk.notify = notify
        self.live = "LIVE"

    def lapse_cards(self):
        return [k for k in self.cards if k.startswith("fleet-keeper-lapse-")]

    def test_the_lapse_card_of_a_live_manager_offers_restart_fresh(self):
        self.stale_run(T0, 45)
        self.assertIn(fk.FRESH_OPTION, self.options[self.lapse_cards()[0]])
        self.assertIn("acknowledge", self.options[self.lapse_cards()[0]])

    def test_no_answer_restarts_nothing(self):
        self.stale_run(T0, 90)
        self.assertEqual([], self.restarts)

    def test_acknowledge_restarts_nothing(self):
        self.answer = "acknowledge"
        self.stale_run(T0, 90)
        self.assertEqual([], self.restarts)

    def test_the_answer_restarts_exactly_once(self):
        self.answer = fk.FRESH_OPTION
        self.stale_run(T0, 90)
        self.assertEqual([SID], self.restarts, "an answer must never become a loop")
        self.assertTrue(any(k.startswith("fleet-keeper-fresh-") for k in self.cards))

    def test_a_busy_refusal_retries_within_the_bound_then_stops(self):
        self.answer = fk.FRESH_OPTION
        fk.restart_fresh_session = lambda sid: (self.restarts.append(sid)
                                                or (False, "exit 1: busy-target"))
        self.stale_run(T0, 41 + 40)
        tries_in_bound = len(self.restarts)
        self.assertGreater(tries_in_bound, 1, "a busy refusal must not spend the answer")
        self.assertLessEqual(tries_in_bound, fk.RESUME_DEFER_MAX // 60 + 1)
        self.stale_run(T0 + 81 * MIN, 30)
        self.assertEqual(tries_in_bound, len(self.restarts), "retries must stop after the bound")

    def test_a_non_live_manager_is_never_restarted_fresh(self):
        self.live = "UNKNOWN"
        self.answer = fk.FRESH_OPTION
        self.stale_run(T0, 90)
        self.assertEqual([], self.restarts)


class AnsweredResumeTest(KeeperBase):
    """The resume runs ONLY on the operator's `resume it` answer to a `dead` card.

    Operator decision 2026-11-06 (card `7653b7b6…`). The goal manager's four conditions:
    the resume exists only behind that answer; the old pin is narrowed, not deleted;
    `acknowledge` is not resolution; and death is corroborated before anything runs —
    `session-liveness.py` ABSENT published for live sessions three times in one day.
    """

    def setUp(self):
        super().setUp()
        self._saved_exec = {k: getattr(fk, k) for k in
                            ("answer_of", "death_corroborated", "resume_absent_session")}
        self.addCleanup(lambda: [setattr(fk, k, v) for k, v in self._saved_exec.items()])
        self.answer = None
        self.answered_at = None
        self.corroborated = (True, "registry empty, transcript quiet 900s", False)
        self.resumes = []
        self.options = {}
        fk.answer_of = lambda item: (self.answer, self.answered_at)
        fk.death_corroborated = lambda sid, now: self.corroborated
        fk.resume_absent_session = lambda sid: (self.resumes.append(sid) or (True, "pane 9"))

        def notify(key, payload, options, context=""):
            self.cards.append(key)
            self.options[key] = list(options)
            return f"item-{len(self.cards)}"
        fk.notify = notify
        self.live = "ABSENT"

    def absent_cards(self):
        return [k for k in self.cards if "-absent-" in k]

    def test_the_dead_card_offers_resume(self):
        self.tick(T0)
        key = self.absent_cards()[0]
        self.assertIn(fk.RESUME_OPTION, self.options[key])
        self.assertIn("acknowledge", self.options[key])

    def test_no_answer_resumes_nothing(self):
        self.tick(T0)
        self.tick(T0 + MIN)
        self.assertEqual([], self.resumes)

    def test_acknowledge_is_not_resolution_and_resumes_nothing(self):
        self.answer = "acknowledge"
        self.tick(T0)
        self.tick(T0 + MIN)
        self.assertEqual([], self.resumes)

    def test_an_uncorroborated_death_is_never_resumed(self):
        """The ABSENT trap: the strong limb (no registry entry) must hold, or nothing runs."""
        self.answer = fk.RESUME_OPTION
        self.corroborated = (False, "a registry entry still carries the session", False)
        self.tick(T0)
        self.tick(T0 + MIN)
        self.assertEqual([], self.resumes)

    def test_a_resume_answer_on_a_corroborated_death_resumes_exactly_once(self):
        self.answer = fk.RESUME_OPTION
        self.tick(T0)
        self.tick(T0 + MIN)
        self.tick(T0 + 2 * MIN)
        self.assertEqual([SID], self.resumes, "an answer must never become a loop")

    def test_a_live_but_lapsed_manager_is_never_offered_a_resume(self):
        """The lapse card's target is alive: resuming it would mean a kill."""
        self.live = "LIVE"
        self.answer = fk.RESUME_OPTION
        self.stale_run(T0, 45)
        lapse = [k for k in self.cards if k.startswith("fleet-keeper-lapse-")]
        self.assertTrue(lapse, self.cards)
        self.assertNotIn(fk.RESUME_OPTION, self.options[lapse[0]])
        self.assertEqual([], self.resumes)

    # ---- M4: an answer that fails corroboration must not stay armed --------------

    def test_a_live_session_consumes_the_answer_and_it_never_fires_later(self):
        """The registry says ALIVE. Even if the reads agree an hour later, nothing runs."""
        self.answer = fk.RESUME_OPTION
        self.corroborated = (False, "registry entry still carries the session — it is alive", False)
        self.tick(T0)
        self.corroborated = (True, "now dead", False)
        self.tick(T0 + 60 * MIN)
        self.assertEqual([], self.resumes, "a consumed answer fired later — the reads became the trigger")
        self.assertTrue(any(k.startswith("fleet-keeper-resume-dropped-") for k in self.cards))

    def test_a_too_fresh_transcript_may_be_waited_out_within_the_bound(self):
        self.answer = fk.RESUME_OPTION
        self.corroborated = (False, "transcript written 30s ago", True)
        self.tick(T0)
        self.corroborated = (True, "quiet now", False)
        self.tick(T0 + 10 * MIN)
        self.assertEqual([SID], self.resumes)

    def test_a_wait_that_outlasts_the_bound_is_dropped_unrun(self):
        self.answer = fk.RESUME_OPTION
        self.corroborated = (False, "transcript written 30s ago", True)
        self.tick(T0)
        self.tick(T0 + fk.RESUME_DEFER_MAX + MIN)
        self.corroborated = (True, "quiet now", False)
        self.tick(T0 + fk.RESUME_DEFER_MAX + 2 * MIN)
        self.assertEqual([], self.resumes)
        self.assertTrue(any(k.startswith("fleet-keeper-resume-dropped-") for k in self.cards))

    def test_an_hours_old_answer_gets_no_fresh_window(self):
        """The bound runs from the STORE's answer time, not from when the keeper first saw it."""
        self.answer = fk.RESUME_OPTION
        self.answered_at = T0 - 2 * 3600  # answered while the keeper was down
        self.corroborated = (False, "transcript written 30s ago", True)
        self.tick(T0)
        self.corroborated = (True, "quiet now", False)
        self.tick(T0 + 5 * MIN)
        self.assertEqual([], self.resumes, "an hours-old answer was handed a fresh 30-min window")
        self.assertTrue(any(k.startswith("fleet-keeper-resume-dropped-") for k in self.cards))

    def test_an_unwritable_record_does_not_repost_the_held_card(self):
        self.answer = fk.RESUME_OPTION
        self.corroborated = (False, "transcript written 30s ago", True)
        real = fk._save_episodes
        self.addCleanup(setattr, fk, "_save_episodes", real)
        fk._save_episodes = lambda rec: False
        for m in range(4):
            self.tick(T0 + m * MIN)
        self.assertEqual([], [k for k in self.cards if k.startswith("fleet-keeper-resume-held-")])
        self.assertEqual([], self.resumes)

    def test_the_held_notice_is_posted_once_not_every_tick(self):
        """A launchd producer cannot rely on the store's dedup — every tick is a new process."""
        self.answer = fk.RESUME_OPTION
        self.corroborated = (False, "transcript written 30s ago", True)
        for m in range(5):
            self.tick(T0 + m * MIN)
        held = [k for k in self.cards if k.startswith("fleet-keeper-resume-held-")]
        self.assertEqual(1, len(held), self.cards)

    # ---- #10: the marker is written BEFORE the resume, so a crash cannot repeat it -----

    def test_a_resume_that_crashes_is_never_run_again(self):
        self.answer = fk.RESUME_OPTION

        def boom(sid):
            self.resumes.append(sid)
            raise RuntimeError("died mid-resume")
        fk.resume_absent_session = boom
        with self.assertRaises(RuntimeError):
            self.tick(T0)
        fk.resume_absent_session = lambda sid: (self.resumes.append(sid) or (True, "pane 9"))
        self.tick(T0 + MIN)
        self.assertEqual([SID], self.resumes, "the marker was written after the run, not before")

    def test_an_unpersistable_marker_refuses_to_resume(self):
        self.answer = fk.RESUME_OPTION
        fk._save_episodes_real = fk._save_episodes
        self.addCleanup(setattr, fk, "_save_episodes", fk._save_episodes_real)
        fk._save_episodes = lambda rec: False
        self.tick(T0)
        self.assertEqual([], self.resumes)


class DeathCorroboratedTest(unittest.TestCase):
    """M2: the REAL corroboration, never a stub — replacing its body must turn this red."""

    SID = "ffff0000-0000-0000-0000-000000000006"
    NOW = 1_800_000_000.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.reg, self.proj = root / "sessions", root / "projects"
        self.reg.mkdir()
        (self.proj / "-vault").mkdir(parents=True)
        saved = (fk.SESSIONS_DIR, fk.PROJECTS_DIR)
        self.addCleanup(lambda: (setattr(fk, "SESSIONS_DIR", saved[0]),
                                 setattr(fk, "PROJECTS_DIR", saved[1])))
        fk.SESSIONS_DIR, fk.PROJECTS_DIR = str(self.reg), str(self.proj)

    def transcript(self, age):
        path = self.proj / "-vault" / f"{self.SID}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        os.utime(path, (self.NOW - age, self.NOW - age))

    def test_dead_and_quiet_is_corroborated(self):
        self.transcript(age=900)
        ok, _why, _retry = fk.death_corroborated(self.SID, self.NOW)
        self.assertTrue(ok)

    def test_a_registry_entry_means_alive_and_is_final(self):
        self.transcript(age=900)
        (self.reg / "42.json").write_text(json.dumps({"sessionId": self.SID}), encoding="utf-8")
        ok, _why, retry = fk.death_corroborated(self.SID, self.NOW)
        self.assertFalse(ok)
        self.assertFalse(retry, "a live session must consume the answer, never defer it")

    def test_a_half_written_registry_entry_is_not_absence(self):
        self.transcript(age=900)
        (self.reg / "42.json").write_text('{"sessionId": "' + self.SID[:9], encoding="utf-8")
        ok, _why, retry = fk.death_corroborated(self.SID, self.NOW)
        self.assertFalse(ok)
        self.assertTrue(retry)

    def test_a_fresh_transcript_is_not_yet_corroborated(self):
        self.transcript(age=30)
        ok, _why, retry = fk.death_corroborated(self.SID, self.NOW)
        self.assertFalse(ok)
        self.assertTrue(retry)

    def test_no_transcript_cannot_be_measured_and_is_final(self):
        ok, _why, retry = fk.death_corroborated(self.SID, self.NOW)
        self.assertFalse(ok)
        self.assertFalse(retry)


class AnswerOfTest(unittest.TestCase):
    """M2: only the exact single option `resume it` may read as a resume answer."""

    def answer(self, stdout):
        done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
        with mock.patch.object(fk.subprocess, "run", return_value=done):
            return fk.answer_of("item-1")[0]

    def test_the_exact_option_reads_as_resume(self):
        self.assertEqual(fk.RESUME_OPTION, self.answer("ANSWERED: option: resume it\nANSWERED_BY: board\n"))

    def test_nothing_else_reads_as_resume(self):
        for stdout in ("OPEN\n", "DECLINED\n", "ANSWERED: skip\n",
                       "ANSWERED: option: resume it, acknowledge\n",
                       "ANSWERED: text: resume it\n", "NOT_OPERATOR_ANSWERED: resume it\n",
                       "ANSWERED: option: acknowledge\n", ""):
            self.assertNotEqual(fk.RESUME_OPTION, self.answer(stdout), stdout)

    def test_the_store_answer_time_is_read(self):
        done = subprocess.CompletedProcess([], 0, stdout=(
            "ANSWERED: option: resume it\nANSWERED_BY: board\n"
            "ANSWERED_AT: 2026-11-06T15:07:54.588302Z\n"), stderr="")
        with mock.patch.object(fk.subprocess, "run", return_value=done) as run:
            option, answered = fk.answer_of("item-1")
        self.assertEqual(fk.RESUME_OPTION, option)
        self.assertAlmostEqual(1793977674.588302, answered, places=3)
        self.assertIn("--answered-at", run.call_args.args[0])

    def test_every_rfc3339nano_fraction_length_parses(self):
        """Go's RFC3339Nano drops trailing zeros: 0–9 fraction digits all occur in production.

        The 6-digit case above passes on `fromisoformat` everywhere; the launchd Python (3.9)
        rejects 4- and 9-digit fractions, which is exactly what this pins.
        """
        whole = 1793977674.0
        for stamp, expect in (
            ("2026-11-06T15:07:54Z", whole),
            ("2026-11-06T15:07:54.5Z", whole + 0.5),
            ("2026-11-06T15:07:54.5883Z", whole + 0.5883),
            ("2026-11-06T15:07:54.123456789Z", whole + 0.123456),
            ("2026-11-06T16:07:54.25+01:00", whole + 0.25),
        ):
            self.assertAlmostEqual(expect, fk.parse_rfc3339(stamp), places=5, msg=stamp)

    def test_garbage_is_none_not_a_crash(self):
        for stamp in ("", "yesterday", "2026-11-06", "2026-11-06T15:07:54.5"):
            self.assertIsNone(fk.parse_rfc3339(stamp), stamp)

    def test_an_unrunnable_poll_is_no_answer(self):
        with mock.patch.object(fk.subprocess, "run", side_effect=OSError("gone")):
            self.assertEqual((None, None), fk.answer_of("item-1"))


class ResumeOutcomeTest(unittest.TestCase):
    """#6: a crash or a timeout in restart-worker is reported honestly, never swallowed."""

    def run_with(self, **kw):
        with mock.patch.object(fk.subprocess, "run", **kw):
            return fk.resume_absent_session("s")

    def test_a_traceback_with_empty_stdout_is_reported_not_raised(self):
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="Traceback ...\nKeyError: 'x'\n")
        ok, detail = self.run_with(return_value=done)
        self.assertFalse(ok)
        self.assertIn("KeyError", detail)

    def test_a_timeout_is_an_unknown_outcome_not_nothing_happened(self):
        ok, detail = self.run_with(side_effect=subprocess.TimeoutExpired("x", 120))
        self.assertFalse(ok)
        self.assertIn("UNKNOWN", detail)


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


class EpisodeTest(KeeperBase):
    """Every condition card is posted once per EPISODE, never once per tick, and no two episodes
    share a card identity (the store swallows an open card whose identity matches an answered
    one — `suppressAnsweredTwins` in attention-controller)."""

    def test_a_condition_held_for_many_ticks_posts_one_card(self):
        self.live = "ABSENT"
        for m in range(30):
            self.tick(T0 + m * MIN)
        self.assertEqual(1, len([k for k in self.cards if k.startswith("fleet-keeper-absent-")]))

    def test_a_recurrence_after_recovery_gets_a_distinct_identity(self):
        self.live = "ABSENT"
        self.tick(T0)
        self.word = "ok"
        self.tick(T0 + 5 * MIN)
        self.word, self.live = "stale", "ABSENT"
        self.tick(T0 + 60 * MIN)
        absent = [k for k in self.cards if k.startswith("fleet-keeper-absent-")]
        self.assertEqual(2, len(absent))
        self.assertNotEqual(absent[0], absent[1],
                            "an answered first card must never swallow the second episode")

    def test_a_failed_post_is_retried_under_the_same_episode_key(self):
        posted = []
        fk.notify = lambda key, *a, **k: posted.append(key) or None
        self.live = "ABSENT"
        self.tick(T0)
        self.tick(T0 + MIN)
        self.assertEqual(2, len(posted))
        self.assertEqual(1, len(set(posted)))

    def test_an_episode_record_that_will_not_clear_is_reported(self):
        self.live = "ABSENT"
        self.tick(T0)
        real = fk.end_episodes
        fk.end_episodes = lambda: False
        self.addCleanup(lambda: setattr(fk, "end_episodes", real))
        self.word = "ok"
        self.assertEqual("cannot-tell", self.tick(T0 + MIN)[1][0])
        self.assertTrue(any(k.startswith("fleet-keeper-state-unclearable-") for k in self.cards))

    def test_a_lapse_longer_than_a_day_is_raised_again(self):
        """Cards expire after 24 h. A lapse that outlives its card must be raised again, or a
        day-long lapse goes silent: the failure this task exists to end."""
        self.stale_run(T0, 40)
        self.tick(T0 + 25 * 3600)
        lapse = [k for k in self.cards if k.startswith("fleet-keeper-lapse-")]
        self.assertEqual(2, len(lapse))
        self.assertTrue(lapse[1].endswith("-d1"))

    def test_an_absent_manager_lasting_over_a_day_is_raised_again(self):
        """A dead fleet manager with no successor is the episode that outlives a 24 h card."""
        self.live = "ABSENT"
        self.tick(T0)
        self.tick(T0 + 25 * 3600)
        absent = [k for k in self.cards if k.startswith("fleet-keeper-absent-")]
        self.assertEqual(2, len(absent))
        self.assertTrue(absent[1].endswith("-d1"))

    def test_a_deliberate_stop_ends_open_condition_episodes(self):
        """An episode left open across a stop would make the next one return the old,
        acknowledged card and post nothing."""
        self.live = "ABSENT"
        self.tick(T0)
        self.live, self.stood = "LIVE", "STOPPED"
        self.tick(T0 + MIN)
        self.stood, self.live = "running", "ABSENT"
        self.tick(T0 + 2 * MIN)
        self.assertEqual(2, len([k for k in self.cards if k.startswith("fleet-keeper-absent-")]))

    def test_a_new_manager_dying_before_any_ok_gets_its_own_card(self):
        self.live = "ABSENT"
        self.tick(T0)
        other = "9c0ffee0-0000-0000-0000-000000000000"
        self.register_fleet(other)
        os.utime(self.register / f"{other}.json", (T0 + 999999, T0 + 999999))
        self.tick(T0 + MIN)
        absent = {k.split("-")[3] for k in self.cards if k.startswith("fleet-keeper-absent-")}
        self.assertEqual({SID[:8], other[:8]}, absent)

    def test_an_unsaved_episode_record_is_carded(self):
        real = fk._save_episodes
        fk._save_episodes = lambda rec: False
        self.addCleanup(lambda: setattr(fk, "_save_episodes", real))
        self.live = "ABSENT"
        self.tick(T0)
        self.assertTrue(any(k.startswith("fleet-keeper-state-unwritable-") for k in self.cards))


class NotifyArgvTest(unittest.TestCase):
    """The deployed keeper runs under launchd with NO $CLAUDE_CODE_SESSION_ID, and attention-ask
    refuses a post without a producer. Every other test mocks `notify`, which is exactly how a
    keeper that could post nothing went unseen — so the real argv is pinned here."""

    def test_the_real_argv_names_an_explicit_producer(self):
        done = subprocess.CompletedProcess([], 0, stdout="ITEM_ID: x\n", stderr="")
        with mock.patch.object(fk.subprocess, "run", return_value=done) as run:
            _REAL_NOTIFY("k", "p", ["acknowledge"])
        argv = run.call_args[0][0]
        self.assertIn("--producer-id", argv)
        self.assertEqual(fk.PRODUCER_ID, argv[argv.index("--producer-id") + 1])
        self.assertEqual("cron", argv[argv.index("--producer-kind") + 1])

    def test_attention_ask_accepts_it_with_no_session_in_the_environment(self):
        """End to end through the REAL attention-ask.py, with the session id stripped exactly as
        launchd strips it, against a store that cannot answer: the producer gate must pass
        (`FAILED: … unreachable`), never refuse (`REFUSED: no --producer-id`)."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["ATTENTION_STORE_URL"] = "http://127.0.0.1:9"
        out = subprocess.run(
            [fk.sys.executable, fk.ATTENTION_ASK, "post", "--producer-id", fk.PRODUCER_ID,
             "--producer-kind", "cron", "--dedup-key", "fleet-keeper-test", "--payload", "p",
             "--option", "acknowledge"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        text = out.stdout + out.stderr
        self.assertNotIn("REFUSED", text)
        self.assertIn("FAILED", text)

    def test_positive_control_the_same_post_without_a_producer_is_refused(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["ATTENTION_STORE_URL"] = "http://127.0.0.1:9"
        out = subprocess.run(
            [fk.sys.executable, fk.ATTENTION_ASK, "post", "--dedup-key", "fleet-keeper-test",
             "--payload", "p", "--option", "acknowledge"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertIn("REFUSED", out.stdout + out.stderr)


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
