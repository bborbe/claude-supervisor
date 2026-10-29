#!/usr/bin/env python3
"""Tests for scripts/incident-ledger.py.

The property this file exists to protect: **an incident closes on a verified resume,
never on a card close.** Everything else here is in service of that one rule, because
the defect it replaces is silent -- a blocker whose card was cleared read as resolved
while the worker sat frozen, and nothing anywhere recorded the difference.

Three things are load-bearing, and each is a way the suite could be plausibly wrong:

- **The unanswered-but-answered case.** `test_answered_but_not_resumed_counts_in_
  denominator_only` is the whole point: an incident that received an answer and never
  resumed must stay `pending`, appear in `total`, and appear in NO numerator. A suite
  that only exercises the happy path would pass on an implementation whose `share`
  counts every non-pending outcome as resolved.

- **The caller stamp.** `test_escalate_stamps_the_round` and
  `test_escalate_without_a_round_stamps_manual` are a pair on purpose. The criterion
  they serve -- "escalated by the periodic caller, not typed" -- is only observable
  because a hand-run stamps something a round cannot. Testing one direction would
  leave the discriminator unproven in exactly the direction that matters.

- **The negative controls.** A check that fires on everything satisfies the positive
  case while catching nothing, so: an incident before its deadline must NOT escalate,
  an incident with no evidence must NOT close, a corrupt ledger must NOT read as
  empty, and `--dry-run` must NOT write.

`post_card` is stubbed throughout. The real one shells out to `attention-ask.py` and
would reach the operator's attention service from a unit test -- a test that posts a
board card is a test that needs the operator, which is the opposite of what this is.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "incident-ledger.py")

_spec = importlib.util.spec_from_file_location("incident_ledger", _SCRIPT)
il = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(il)

SESSION = "11111111-2222-3333-4444-555555555555"
OTHER_SESSION = "66666666-7777-8888-9999-aaaaaaaaaaaa"
OWNER = "manager-session-1"


def iso(delta=None):
    when = datetime.datetime.now(datetime.timezone.utc)
    if delta is not None:
        when = when + delta
    return when.strftime(il.TS_FORMAT)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # LEDGER and LOCK are redirected so no test can touch the real ledger.
        # LOCK is derived from LEDGER at import, so both must move together.
        self._ledger, self._lock = il.LEDGER, il.LOCK
        il.LEDGER = os.path.join(self.tmp, "ledger.json")
        il.LOCK = il.LEDGER + ".lock"
        self._env = dict(os.environ)
        os.environ.pop("SUPERVISOR_INCIDENT_DEADLINE_SECONDS", None)
        os.environ.pop("SUPERVISOR_INCIDENT_STALL_ROUNDS", None)
        # The real post_card would post a board card from a unit test.
        self._post_card = il.post_card
        self.posted = []
        il.post_card = self._stub_post

    def _stub_post(self, incident):
        self.posted.append(incident["dedup_key"])
        return True

    def tearDown(self):
        il.LEDGER, il.LOCK = self._ledger, self._lock
        il.post_card = self._post_card
        os.environ.clear()
        os.environ.update(self._env)

    def run_cli(self, *argv):
        """Run a subcommand in-process, returning (rc, stdout)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = il.main(list(argv))
        return rc, out.getvalue()

    def open_incident(self, session=SESSION, kind="permission", **over):
        argv = ["open", "--session", session, "--kind", kind, "--owner", OWNER]
        if over.get("detected_by"):
            argv += ["--detected-by", over["detected_by"]]
        if over.get("card_id"):
            argv += ["--card-id", over["card_id"]]
        if over.get("deadline_seconds") is not None:
            argv += ["--deadline-seconds", str(over["deadline_seconds"])]
        return self.run_cli(*argv)

    def answer(self, session=SESSION, kind="permission", by="manager"):
        rc, out = self.run_cli(
            "answer", "--session", session, "--kind", kind, "--by", by
        )
        self.assertEqual(rc, 0)
        return out

    def ledger(self):
        # An absent file is the ordinary "nothing has been written yet" state, not
        # an error: several tests assert that nothing was opened, and they must be
        # able to ask without the ask itself failing.
        try:
            with open(il.LEDGER, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {"version": 1, "incidents": []}

    def records(self):
        return self.ledger()["incidents"]


class TestOpen(Base):
    def test_open_populates_every_field(self):
        self.open_incident(detected_by="card:item-42", card_id="item-42")
        record = self.records()[0]
        self.assertEqual(record["dedup_key"], "incident:%s:permission" % SESSION)
        self.assertEqual(record["session_id"], SESSION)
        self.assertEqual(record["kind"], "permission")
        self.assertEqual(record["owner"], OWNER)
        self.assertEqual(record["outcome"], il.PENDING)
        self.assertIsNone(record["closed_at"])
        self.assertEqual(record["detected_by"], "card:item-42")
        self.assertEqual(record["card_id"], "item-42")
        self.assertIsNone(record["escalated_by"])
        self.assertTrue(record["opened_at"])
        self.assertTrue(record["deadline"])

    def test_deadline_is_opened_at_plus_the_default(self):
        self.open_incident()
        record = self.records()[0]
        opened = il.parse_ts(record["opened_at"])
        deadline = il.parse_ts(record["deadline"])
        self.assertEqual(int((deadline - opened).total_seconds()), 900)

    def test_deadline_seconds_override_is_honoured(self):
        self.open_incident(deadline_seconds=30)
        record = self.records()[0]
        opened = il.parse_ts(record["opened_at"])
        deadline = il.parse_ts(record["deadline"])
        self.assertEqual(int((deadline - opened).total_seconds()), 30)

    def test_env_deadline_is_honoured(self):
        os.environ["SUPERVISOR_INCIDENT_DEADLINE_SECONDS"] = "45"
        self.open_incident()
        record = self.records()[0]
        opened = il.parse_ts(record["opened_at"])
        deadline = il.parse_ts(record["deadline"])
        self.assertEqual(int((deadline - opened).total_seconds()), 45)

    def test_a_stall_gets_no_card_id_and_a_drive_detector(self):
        # A stall is a duration, not an event: nothing posts a card for it. A
        # fabricated card id here would be a join that never resolves.
        self.open_incident(kind="stall")
        record = self.records()[0]
        self.assertEqual(record["card_id"], "")
        self.assertTrue(record["detected_by"].startswith("drive:"))

    def test_reopening_the_same_blocker_does_not_duplicate_it(self):
        self.open_incident()
        rc, out = self.open_incident()
        self.assertEqual(rc, 0)
        self.assertIn("already open", out)
        self.assertEqual(len(self.records()), 1)

    def test_a_closed_incident_lets_a_new_episode_open(self):
        # Episodes are counted, not sessions: a session that blocks twice and
        # resolves twice is two incidents, and a share over one session must not
        # read as one.
        self.open_incident()
        self.answer()
        self.run_cli(
            "close", "--session", SESSION, "--kind", "permission",
            "--outcome", "manager", "--resume-evidence", "tool call after answer",
        )
        self.open_incident()
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(
            [r["outcome"] for r in self.records()], ["manager", il.PENDING]
        )


class TestClose(Base):
    def test_close_requires_resume_evidence(self):
        # The defect: a card close is a CLEAR, not an answer, so a close with no
        # resume would record a resolution nobody earned.
        self.open_incident()
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(
                "close", "--session", SESSION, "--kind", "permission",
                "--outcome", "manager", "--resume-evidence", "   ",
            )
        self.assertEqual(caught.exception.code, il.REFUSED)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_close_refuses_timed_out(self):
        # `timed-out` is an escalation and an escalated incident never resumed;
        # letting `close` write it would make the two paths indistinguishable.
        self.open_incident()
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(
                "close", "--session", SESSION, "--kind", "permission",
                "--outcome", "timed-out", "--resume-evidence", "x",
            )
        self.assertEqual(caught.exception.code, il.REFUSED)

    def test_close_records_outcome_and_evidence(self):
        self.open_incident()
        self.answer()
        self.run_cli(
            "close", "--session", SESSION, "--kind", "permission",
            "--outcome", "manager", "--resume-evidence", "next tool call at 14:31",
        )
        record = self.records()[0]
        self.assertEqual(record["outcome"], "manager")
        self.assertEqual(record["resume_evidence"], "next tool call at 14:31")
        self.assertTrue(record["closed_at"])

    def test_answer_is_not_a_close(self):
        # The gap between the two stamps is the whole measurement: an answered
        # incident that never resumed is the case SC4 exists for, and it can only
        # be seen if answering does not itself resolve anything.
        self.open_incident()
        self.answer(by="manager")
        record = self.records()[0]
        self.assertEqual(record["answered_by"], "manager")
        self.assertTrue(record["answered_at"])
        self.assertEqual(record["outcome"], il.PENDING)
        self.assertIsNone(record["closed_at"])

    def test_close_refuses_without_a_recorded_answer(self):
        """The core rule: a resume with no answer is not a resolution."""
        self.open_incident()
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_cli(
                    "close", "--session", SESSION, "--kind", "permission",
                    "--outcome", "manager", "--resume-evidence", "tool call",
                )
        self.assertEqual(caught.exception.code, il.REFUSED)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_close_derives_the_outcome_from_who_answered(self):
        self.open_incident()
        self.answer(by="operator")
        self.run_cli(
            "close", "--session", SESSION, "--kind", "permission",
            "--resume-evidence", "tool call",
        )
        self.assertEqual(self.records()[0]["outcome"], "operator")

    def test_self_cleared_needs_no_answer(self):
        # A blocker that cleared itself is one nobody answered. Requiring an
        # answer here would push it into `manager` and inflate the one number the
        # goal reads.
        self.open_incident()
        self.run_cli(
            "close", "--session", SESSION, "--kind", "permission",
            "--outcome", "self-cleared", "--resume-evidence", "cleared on its own",
        )
        self.assertEqual(self.records()[0]["outcome"], "self-cleared")

    def test_close_refuses_skipped_gone(self):
        """`skipped-gone` is the ledger's own retirement, not a verified resume."""
        self.open_incident()
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_cli(
                    "close", "--session", SESSION, "--kind", "permission",
                    "--outcome", il.SKIPPED_GONE, "--resume-evidence", "tool call",
                )
        self.assertEqual(caught.exception.code, il.REFUSED)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_close_of_an_absent_incident_is_a_no_op(self):
        rc, out = self.run_cli(
            "close", "--session", OTHER_SESSION, "--kind", "question",
            "--outcome", "operator", "--resume-evidence", "task change",
        )
        self.assertEqual(rc, 0)
        self.assertIn("no open incident", out)


class TestShare(Base):
    def share(self, days=1):
        rc, out = self.run_cli("share", "--days", str(days))
        self.assertEqual(rc, 0)
        return out

    def test_answered_but_not_resumed_counts_in_denominator_only(self):
        """The criterion this ledger exists for, in one test.

        An incident that received an answer and never resumed must be open, must
        be in `total`, and must be in no numerator. An implementation that counts
        every non-pending outcome as resolved fails here and nowhere else.
        """
        self.open_incident()
        out = self.share()
        self.assertIn("total: 1", out)
        self.assertIn("resolved-by-manager: 0", out)
        self.assertIn("share: 0.0%", out)
        self.assertIn("pending      1", out)

    def test_pending_is_never_resolved(self):
        self.open_incident()
        self.open_incident(session=OTHER_SESSION, kind="question")
        out = self.share()
        self.assertIn("total: 2", out)
        self.assertIn("resolved-by-manager: 0", out)

    def test_manager_outcome_counts(self):
        self.open_incident()
        self.answer()
        self.run_cli(
            "close", "--session", SESSION, "--kind", "permission",
            "--outcome", "manager", "--resume-evidence", "tool call",
        )
        out = self.share()
        self.assertIn("total: 1", out)
        self.assertIn("resolved-by-manager: 1", out)
        self.assertIn("share: 100.0%", out)

    def test_operator_and_self_cleared_do_not_count_as_manager(self):
        self.open_incident()
        self.answer(by="operator")
        self.run_cli(
            "close", "--session", SESSION, "--kind", "permission",
            "--outcome", "operator", "--resume-evidence", "tool call",
        )
        self.open_incident(session=OTHER_SESSION, kind="question")
        self.run_cli(
            "close", "--session", OTHER_SESSION, "--kind", "question",
            "--outcome", "self-cleared", "--resume-evidence", "task change",
        )
        out = self.share()
        self.assertIn("total: 2", out)
        self.assertIn("resolved-by-manager: 0", out)
        self.assertIn("operator     1", out)
        self.assertIn("self-cleared 1", out)

    def test_an_empty_window_says_so(self):
        # 0/0/0% must never read as a 0% resolution rate; it is an empty population.
        out = self.share()
        self.assertIn("total: 0", out)
        self.assertIn("0 / 0 / 0%", out)

    def test_the_window_excludes_older_incidents(self):
        self.open_incident()
        data = self.ledger()
        data["incidents"][0]["opened_at"] = iso(datetime.timedelta(days=-10))
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        out = self.share(days=1)
        self.assertIn("total: 0", out)


class TestEscalate(Base):
    def setUp(self):
        super().setUp()
        # The liveness probe is stubbed so a stall test's verdict does not depend on
        # which sessions happen to be running on the host. The default is "these
        # sessions are live"; the gone cases set it themselves.
        self._live_sessions = il.live_sessions
        self.live = {SESSION, SESSION[:8], OTHER_SESSION, OTHER_SESSION[:8]}
        il.live_sessions = lambda: self.live

    def tearDown(self):
        il.live_sessions = self._live_sessions
        super().tearDown()

    def make_overdue(self, session=SESSION, kind="permission"):
        self.open_incident(session=session, kind=kind, deadline_seconds=1)
        data = self.ledger()
        for record in data["incidents"]:
            if record["session_id"] == session and record["kind"] == kind:
                record["deadline"] = iso(datetime.timedelta(seconds=-5))
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_escalate_stamps_the_round(self):
        self.make_overdue()
        self.run_cli("escalate", "--round-id", "2026-10-22T14:00:00Z")
        record = self.records()[0]
        self.assertEqual(record["outcome"], il.TIMED_OUT)
        self.assertEqual(record["escalated_by"], "round:2026-10-22T14:00:00Z")
        self.assertEqual(self.posted, [record["dedup_key"]])

    def test_escalate_without_a_round_stamps_manual(self):
        """The discriminator, other direction.

        A hand-run must be distinguishable from a round's run, or the criterion
        "escalated by the periodic caller" could never fail.
        """
        self.make_overdue()
        os.environ["CLAUDE_CODE_SESSION_ID"] = "typed-by-me"
        self.run_cli("escalate")
        record = self.records()[0]
        self.assertEqual(record["escalated_by"], "manual:typed-by-me")

    def test_an_incident_before_its_deadline_is_left_alone(self):
        self.open_incident()
        self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)
        self.assertEqual(self.posted, [])

    def test_dry_run_writes_nothing(self):
        self.make_overdue()
        rc, out = self.run_cli("escalate", "--round-id", "r1", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("would escalate", out)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)
        self.assertEqual(self.posted, [])

    def test_a_failed_post_leaves_the_incident_pending(self):
        # Stamping after a failed post would record an escalation that never
        # reached the board -- the same class of false green the ledger exists for.
        self.make_overdue()
        il.post_card = lambda incident: False
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_escalate_is_idempotent(self):
        self.make_overdue()
        self.run_cli("escalate", "--round-id", "r1")
        self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(len(self.posted), 1)

    # --- the gone-session leg -------------------------------------------------
    def make_overdue_stall(self, session=SESSION):
        self.open_incident(session=session, kind="stall", deadline_seconds=1)
        data = self.ledger()
        for record in data["incidents"]:
            if record["session_id"] == session and record["kind"] == "stall":
                record["deadline"] = iso(datetime.timedelta(seconds=-5))
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_a_failed_probe_warns_and_still_posts(self):
        """SC1: the leg that POSTS the card says so when it cannot judge.

        `cmd_stall` already warned; `cmd_escalate` skipped the gate for the whole run
        in silence, so a probe outage disabled the gate in the leg that posts and the
        only visible effect was the absence of a skip line.
        """
        il.live_sessions = lambda: None
        self.make_overdue_stall()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, out = self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(rc, 0)
        self.assertIn("warning: session liveness unavailable", err.getvalue())
        self.assertEqual(len(self.posted), 1, "an outage must not suppress the card")
        self.assertEqual(self.records()[0]["outcome"], il.TIMED_OUT)

    def test_a_gone_stall_incident_is_retired_not_left_pending(self):
        """SC2: a skip is a terminal state, and it is NOT `timed-out`."""
        self.live = {OTHER_SESSION, OTHER_SESSION[:8]}
        self.make_overdue_stall()
        rc, out = self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(rc, 0)
        self.assertIn("gone sessions", out)
        record = self.records()[0]
        self.assertEqual(record["outcome"], il.SKIPPED_GONE)
        self.assertNotEqual(record["outcome"], il.TIMED_OUT, "a skip is not an escalation")
        self.assertEqual(self.posted, [])

    def test_a_retired_incident_is_not_re_listed_next_round(self):
        """SC2, second half: the next round must print no skip line for it."""
        self.live = {OTHER_SESSION, OTHER_SESSION[:8]}
        self.make_overdue_stall()
        self.run_cli("escalate", "--round-id", "r1")
        key = self.records()[0]["dedup_key"]
        rc, second = self.run_cli("escalate", "--round-id", "r2")
        self.assertEqual(rc, 0)
        self.assertNotIn(key, second)

    def test_a_retired_incident_leaves_the_share_denominator(self):
        """SC2, third half: an incident with no possible numerator is no denominator."""
        self.live = {OTHER_SESSION, OTHER_SESSION[:8]}
        self.make_overdue_stall()
        self.run_cli("escalate", "--round-id", "r1")
        rc, out = self.run_cli("share", "--days", "1")
        self.assertEqual(rc, 0)
        self.assertIn("total: 0", out)
        self.assertIn("retired 1 gone-session incident(s)", out)

    def test_a_dry_run_says_would_retire(self):
        """A dry run must not claim a state change it did not make.

        `would escalate` and `would close` already follow this rule; `retired` on a
        run that never calls `write_ledger` would be indistinguishable from a real
        one in its own output — the false signal this ledger exists to remove.
        """
        self.live = {OTHER_SESSION, OTHER_SESSION[:8]}
        self.make_overdue_stall()
        rc, out = self.run_cli("escalate", "--round-id", "r1", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("would retire 1", out)
        self.assertNotIn("retired 1", out)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_a_retired_incident_carries_the_probe_time(self):
        """The record names WHEN the judgement was made, not just that it was made."""
        self.live = {OTHER_SESSION, OTHER_SESSION[:8]}
        self.make_overdue_stall()
        self.run_cli("escalate", "--round-id", "r1")
        evidence = self.records()[0]["resume_evidence"]
        self.assertIn("liveness probe taken at", evidence)
        self.assertIn("round:r1", evidence)

    def test_bounded_names_at_exactly_the_limit_has_no_remainder(self):
        """The no-remainder branch: five names, no `(+N more)` tail."""
        self.assertEqual(il.bounded_names(["a", "b", "c", "d", "e"]), "a, b, c, d, e")
        self.assertEqual(
            il.bounded_names(["a", "b", "c", "d", "e", "f"]), "a, b, c, d, e, … (+1 more)"
        )

    def test_the_skip_line_is_bounded(self):
        """SC4: 120 gone rows must not render as 120 names on one line."""
        self.live = {OTHER_SESSION, OTHER_SESSION[:8]}
        sessions = ["%08x-aaaa-bbbb-cccc-dddddddddddd" % i for i in range(120)]
        for sid in sessions:
            self.open_incident(session=sid, kind="stall", deadline_seconds=0)
        rc, out = self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(rc, 0)
        line = [row for row in out.splitlines() if "gone sessions" in row][0]
        self.assertIn("(+115 more)", line)
        self.assertEqual(line.count("incident:"), 5, "five names, then a count")

    def test_a_malformed_session_id_fails_open(self):
        """SC5: one bad row must not abort the round with `AttributeError`."""
        self.make_overdue_stall()
        data = self.ledger()
        data["incidents"][0]["session_id"] = 12345
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, out = self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(rc, 0)
        self.assertIn("malformed session_id", err.getvalue())
        self.assertEqual(len(self.posted), 1, "an unjudgeable id escalates, not retires")

    def test_the_probe_is_not_paid_when_nothing_is_due(self):
        """SC6: a probe with nothing to judge is a subprocess plus an HTTP call for nothing."""
        calls = []
        il.live_sessions = lambda: calls.append(1) or {SESSION}
        self.open_incident(kind="stall")  # still before its deadline
        rc, out = self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])


class TestRefusal(Base):
    def test_a_corrupt_ledger_is_refused_not_read_as_empty(self):
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_cli("list")
        self.assertEqual(caught.exception.code, il.REFUSED)

    def test_a_corrupt_ledger_is_not_overwritten(self):
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.open_incident()
        with open(il.LEDGER, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{ this is not json")


class TestPrune(Base):
    def test_open_incidents_are_never_pruned(self):
        # Age is not a resolution: the record a reader most needs is the one that
        # never closed.
        self.open_incident()
        data = self.ledger()
        data["incidents"][0]["opened_at"] = iso(datetime.timedelta(days=-90))
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        self.run_cli("prune", "--max-age-hours", "1")
        self.assertEqual(len(self.records()), 1)

    def test_old_terminal_incidents_are_pruned(self):
        self.open_incident()
        self.answer()
        self.run_cli(
            "close", "--session", SESSION, "--kind", "permission",
            "--outcome", "manager", "--resume-evidence", "tool call",
        )
        data = self.ledger()
        data["incidents"][0]["closed_at"] = iso(datetime.timedelta(days=-90))
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        self.run_cli("prune", "--max-age-hours", "1")
        self.assertEqual(self.records(), [])

    def test_a_skipped_gone_incident_is_pruned_on_the_same_clock(self):
        """SC2: the retirement is TERMINAL, so `prune` drops it like any other.

        `cmd_prune` keeps only `PENDING`, so this works by construction — the test is
        here because the docstring now claims it and nothing pinned it.
        """
        self.open_incident(kind="stall", deadline_seconds=0)
        data = self.ledger()
        data["incidents"][0]["outcome"] = il.SKIPPED_GONE
        data["incidents"][0]["closed_at"] = iso(datetime.timedelta(days=-90))
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        self.run_cli("prune", "--max-age-hours", "1")
        self.assertEqual(self.records(), [])


class TestStall(Base):
    """The round-based stall — the one blocker that is a duration, not an event.

    ⚠️ **The streak is the drive leg's, and this suite exists mostly to prove it is
    READ rather than recounted.** `scripts/drive-unverifiable.py` owns
    `unverifiable_streak`; a second counter here would be a second answer to the
    same question and the two would drift. `test_the_streak_is_read_not_recounted`
    is the discriminator: it calls `stall` twice against an unchanged streak of 1,
    which a self-counting implementation turns into 2 and opens on.

    ⚠️ **The liveness probe is stubbed, and the default is "these sessions are
    live".** `live_sessions()` shells out to `session-liveness.py`, so an unstubbed
    run would answer from the host's real fleet — a test whose verdict depends on
    which sessions happen to be running. The default set keeps the pre-existing
    cases on the filter's happy path, and the gone/absent cases set it themselves.
    """

    def setUp(self):
        super().setUp()
        # The budget must not depend on the operator's shell: a host value larger
        # than the fixture population would make the cap test unfalsifiable.
        os.environ.pop("SUPERVISOR_INCIDENT_STALL_BUDGET", None)
        self._live_sessions = il.live_sessions
        self.live = {SESSION, SESSION[:8], OTHER_SESSION, OTHER_SESSION[:8]}
        il.live_sessions = lambda: self.live

    def tearDown(self):
        il.live_sessions = self._live_sessions
        super().tearDown()

    def write_drive(self, rows, legacy=False):
        """`rows` is {session_id: (verdict, streak)}."""
        path = os.path.join(self.tmp, "drive.json")
        if legacy:
            data = {
                sid[:8]: {"sid": sid, "verdict": v}
                for sid, (v, _) in rows.items()
            }
        else:
            data = {
                "round": iso(),
                "entries": [
                    {
                        "session_id": sid,
                        "verdict": verdict,
                        "unverifiable_streak": streak,
                        "unverifiable_round": iso(),
                    }
                    for sid, (verdict, streak) in rows.items()
                ],
            }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def stall(self, path, round_id="r1"):
        rc, out = self.run_cli("stall", "--drive-ledger", path, "--round-id", round_id)
        self.assertEqual(rc, 0)
        return out

    def test_a_streak_below_the_threshold_opens_nothing(self):
        path = self.write_drive({SESSION: ("unverifiable", 1)})
        out = self.stall(path)
        self.assertIn("no session has stalled", out)
        self.assertEqual(self.records(), [])

    def test_a_streak_at_the_threshold_opens(self):
        path = self.write_drive({SESSION: ("unverifiable", 2)})
        out = self.stall(path)
        self.assertIn("opened", out)
        record = self.records()[0]
        self.assertEqual(record["kind"], "stall")
        self.assertEqual(record["observations"], 2)
        self.assertTrue(record["opened_at"])
        self.assertTrue(record["deadline"])
        self.assertEqual(record["card_id"], "", "a stall issues no card")

    def test_the_streak_is_read_not_recounted(self):
        """The discriminator for the rework: two runs, one unchanged streak.

        An implementation that kept its own counter would read 1, then 2, and open
        on the second call — filing a blocker for a session the drive leg still
        calls one round in.
        """
        path = self.write_drive({SESSION: ("unverifiable", 1)})
        self.stall(path, "r1")
        self.stall(path, "r2")
        self.assertEqual(self.records(), [])

    def test_the_threshold_override_opens_at_a_streak_of_one(self):
        os.environ["SUPERVISOR_INCIDENT_STALL_ROUNDS"] = "1"
        path = self.write_drive({SESSION: ("unverifiable", 1)})
        out = self.stall(path)
        self.assertIn("opened", out)

    def test_a_legacy_flat_row_carries_no_streak_and_opens_nothing(self):
        # The legacy shape predates the streak field; absence reads as 0, never as
        # a threshold met.
        path = self.write_drive({SESSION: ("unverifiable", 0)}, legacy=True)
        out = self.stall(path)
        self.assertIn("no session has stalled", out)

    def test_non_unverifiable_verdicts_are_ignored(self):
        path = self.write_drive({
            SESSION: ("revive", 5),
            OTHER_SESSION: ("finished", 9),
        })
        out = self.stall(path)
        self.assertIn("no session has stalled", out)
        self.assertEqual(self.records(), [])

    def test_reopening_does_not_duplicate_the_incident(self):
        path = self.write_drive({SESSION: ("unverifiable", 3)})
        self.stall(path, "r1")
        self.stall(path, "r2")
        self.assertEqual(len(self.records()), 1)

    def test_a_corrupt_drive_ledger_is_refused(self):
        path = os.path.join(self.tmp, "drive.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_cli("stall", "--drive-ledger", path, "--round-id", "r1")
        self.assertEqual(caught.exception.code, il.REFUSED)

    def test_a_missing_drive_ledger_is_not_an_error(self):
        rc, out = self.run_cli(
            "stall", "--drive-ledger", os.path.join(self.tmp, "nope.json"),
            "--round-id", "r1",
        )
        self.assertEqual(rc, 0)
        self.assertIn("no session has stalled", out)

    def test_a_gone_session_opens_nothing(self):
        """A session absent from the heartbeat store is gone, not stalled.

        Measured 2026-10-22: 117 incidents opened in one round, every one of them
        for a session that no longer existed, and every one became a board card the
        operator could not act on.
        """
        self.live = set()
        path = self.write_drive({SESSION: ("unverifiable", 5)})
        out = self.stall(path)
        self.assertIn("gone session(s) past the threshold", out)
        self.assertNotIn("no session has stalled", out, "the skip line already says why")
        self.assertEqual(self.records(), [])

    def test_a_gone_session_does_not_block_a_live_one(self):
        """Negative control: the filter must drop only the gone row.

        Without it, a change that opened nothing at all would satisfy the case
        above while silently retiring the whole incident class.
        """
        self.live = {OTHER_SESSION, OTHER_SESSION[:8]}
        path = self.write_drive({
            SESSION: ("unverifiable", 9),
            OTHER_SESSION: ("unverifiable", 2),
        })
        out = self.stall(path)
        self.assertIn("gone session(s) past the threshold", out)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["session_id"], OTHER_SESSION)

    def test_a_drive_row_keyed_on_the_eight_char_handle_still_opens(self):
        """The pin for the key-space mismatch — the defect the bot caught in #506.

        The drive ledger keys `session_id` on the **8-char display handle**
        (`agents/fleet-drive.md:44`, `scripts/drive-ledger.py:108`), while
        `--list --json` returns **full UUIDs**. Comparing a full-UUID live set
        against 8-char keys classifies EVERY row as gone: the stall incident class
        is silently disabled and each round prints a plausible skip line per row —
        the mirror of the bug this change exists to fix, and invisible to a fixture
        that uses one id shape on both sides.
        """
        self.live = {SESSION, SESSION[:8]}
        path = self.write_drive({SESSION[:8]: ("unverifiable", 2)})
        out = self.stall(path)
        self.assertIn("opened", out)
        self.assertEqual(len(self.records()), 1)

    def test_an_eight_char_row_absent_from_the_live_set_is_skipped(self):
        """The negative control for the prefix join: an unmatched handle is gone."""
        self.live = {OTHER_SESSION, OTHER_SESSION[:8]}
        path = self.write_drive({SESSION[:8]: ("unverifiable", 2)})
        out = self.stall(path)
        self.assertIn("gone session(s) past the threshold", out)
        self.assertEqual(self.records(), [])

    def test_the_round_opens_at_most_the_budget(self):
        """One round cannot open an incident per eligible session.

        Measured 2026-10-22: 117 eligible sessions produced 117 incidents, and 116
        of them became board cards in one burst, because the drive leg's per-round
        cap had no counterpart here.
        """
        sessions = ["%08x-aaaa-bbbb-cccc-dddddddddddd" % i for i in range(117)]
        self.live = set(sessions)
        rows = {sid: ("unverifiable", 2) for sid in sessions}
        rows[sessions[-1]] = ("unverifiable", 7)
        path = self.write_drive(rows)
        out = self.stall(path)
        records = self.records()
        budget = il.incident_budget()
        self.assertEqual(len(records), budget)
        self.assertEqual(out.count("held back"), 117 - budget)
        # Longest streak first, so a backlog drains oldest-first rather than in
        # session-id order, which would starve the oldest row behind any trickle.
        self.assertEqual(records[0]["session_id"], sessions[-1])
        self.assertEqual(records[0]["observations"], 7)

    def test_a_failed_liveness_probe_fails_open(self):
        """An unreachable store must not read as "every session is dead".

        Suppressing the whole incident class on an outage is the false-empty
        `read_drive_ledger` refuses for a corrupt drive ledger, one store over.
        """
        il.live_sessions = lambda: None
        path = self.write_drive({SESSION: ("unverifiable", 2)})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, out = self.run_cli("stall", "--drive-ledger", path, "--round-id", "r1")
        self.assertEqual(rc, 0)
        self.assertIn("liveness unavailable", err.getvalue())
        self.assertEqual(len(self.records()), 1)


class TestLiveSessions(Base):
    """`live_sessions()`'s own parsing — the seam every stall test stubs past.

    ⚠️ **This suite exists because the stall tests stub `live_sessions` wholesale**,
    so the parse had no coverage at all. That is the worst place to have none: a
    change in `session-liveness.py`'s output shape makes the function return `None`,
    the caller fails OPEN, and the gate is silently off while every other test stays
    green.

    ⚠️ **The `rows=… registry_live=… degraded=…` header is written to STDERR, not
    stdout** (measured 2026-10-22: with stderr discarded, stdout begins at `[`). The
    real contract is therefore "stdout is the JSON array, stderr carries the human
    line", and `test_the_real_shape_parses` pins exactly that. The stdout-header case
    is kept as a separate tolerance test rather than as the fixture — asserting the
    tolerance as if it were the shape would pass for the wrong reason and stop
    covering the contract that actually holds.
    """

    def setUp(self):
        super().setUp()
        self._run = il.subprocess.run

    def tearDown(self):
        il.subprocess.run = self._run
        super().tearDown()

    def stub_run(self, stdout, stderr="", returncode=0):
        result = type("Result", (), {})()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        il.subprocess.run = lambda *args, **kwargs: result

    def test_the_real_shape_parses_and_returns_both_key_forms(self):
        """stdout is the array; the header rides stderr; both id forms come back.

        ⚠️ Both forms, because the drive ledger keys on the 8-char display handle
        while `--list` returns full UUIDs — see
        `TestStall.test_a_drive_row_keyed_on_the_eight_char_handle_still_opens`.
        """
        self.stub_run(
            '[\n  {"sessionId": "%s"},\n  {"sessionId": "%s"}\n]\n'
            % (SESSION, OTHER_SESSION),
            stderr="rows=2 registry_live=2 degraded=0\n",
        )
        self.assertEqual(
            il.live_sessions(),
            {SESSION, SESSION[:8], OTHER_SESSION, OTHER_SESSION[:8]},
        )

    def test_a_header_on_stdout_is_tolerated(self):
        """Tolerance, not the shape: the header moving to stdout must not fail open."""
        self.stub_run(
            "rows=2 registry_live=2 degraded=0\n"
            '[\n  {"sessionId": "a"},\n  {"sessionId": "b"}\n]\n'
        )
        self.assertEqual(il.live_sessions(), {"a", "b"})

    def test_a_row_without_a_session_id_is_dropped(self):
        self.stub_run('[{"sessionId": "a"}, {"nope": 1}]')
        self.assertEqual(il.live_sessions(), {"a"})

    def test_a_non_zero_exit_reads_as_unavailable(self):
        self.stub_run("[]", returncode=1)
        self.assertIsNone(il.live_sessions())

    def test_an_unparseable_body_reads_as_unavailable(self):
        self.stub_run("[ not json", stderr="rows=1 registry_live=1 degraded=0\n")
        self.assertIsNone(il.live_sessions())

    def test_a_body_with_no_array_reads_as_unavailable(self):
        self.stub_run("", stderr="rows=0 registry_live=0 degraded=0\n")
        self.assertIsNone(il.live_sessions())

    def test_an_empty_probe_is_unavailable_not_an_empty_fleet(self):
        """SC3: `[]` at exit 0 with `degraded=0` must not read as "everything is gone".

        Returning an empty set made every eligible row read as gone, so the whole
        stall class went quiet behind a plausible skip line -- the same silent-off
        reached from the fail-closed side.
        """
        self.stub_run("[]\n", stderr="rows=0 registry_live=0 degraded=0\n")
        self.assertIsNone(il.live_sessions())

    def test_a_degraded_but_non_empty_probe_is_still_usable(self):
        """`degraded=` marks uncorroborated rows, not missing ones — so it cannot veto.

        Measured 2026-10-29 on a healthy fleet: `rows=45 registry_live=43 degraded=7`.
        Treating `degraded > 0` as "unavailable" therefore returned None every round,
        warned every round, and never retired anything — the gone gate was silently
        off in the leg that posts the cards. A session ABSENT from a non-empty set is
        still absent; only an EMPTY set is unusable.

        The header is the shape the producer can actually emit: `degraded` is set only
        on REGISTRY rows, so `registry_live >= degraded` always holds. `rows=1
        registry_live=0 degraded=1` is impossible and would pin a synthetic case.
        """
        self.stub_run(
            '[{"sessionId": "a"}]', stderr="rows=1 registry_live=1 degraded=1\n"
        )
        self.assertEqual(il.live_sessions(), {"a"})

    def test_an_all_degraded_probe_is_unavailable(self):
        """No corroborated row means the set may be registry-only, so it cannot decide.

        The producer names this state: during a partial rollout the endpoint holds no
        `live: true` row while the registry still lists sessions. A cluster/a2a worker
        lives ONLY in the endpoint, so it would read as absent and its stall incident
        would be retired `skipped-gone` — terminally — for a session that is alive.
        """
        self.stub_run(
            '[{"sessionId": "a", "degraded": true}, '
            '{"sessionId": "b", "degraded": true}]',
            stderr="rows=2 registry_live=2 degraded=2\n",
        )
        self.assertIsNone(il.live_sessions())

    def test_one_corroborated_row_makes_the_probe_usable(self):
        """The negative control: a single endpoint-corroborated row restores the gate."""
        self.stub_run(
            '[{"sessionId": "a", "degraded": true}, '
            '{"sessionId": "b", "degraded": false}]',
            stderr="rows=2 registry_live=1 degraded=1\n",
        )
        self.assertEqual(il.live_sessions(), {"a", "b"})

    def test_an_unreadable_degraded_header_does_not_change_the_verdict(self):
        """A header this reader cannot parse is ignored, not read as a veto."""
        self.stub_run(
            '[{"sessionId": "a"}]', stderr="rows=1 registry_live=1 degraded=many\n"
        )
        self.assertEqual(il.live_sessions(), {"a"})

    def test_a_row_with_a_non_string_session_id_is_dropped(self):
        self.stub_run('[{"sessionId": "a"}, {"sessionId": 42}]')
        self.assertEqual(il.live_sessions(), {"a"})


class TestIncidentBudget(Base):
    """`incident_budget()`'s env parsing.

    ⚠️ The FALLBACK is the property under test, not the override: `0` parsed
    literally would mean "open nothing", so a typo'd override would silently disable
    the stall incident class — the same silent-off failure mode the cap exists to
    prevent, one input further out.
    """

    def test_the_default_is_three(self):
        os.environ.pop("SUPERVISOR_INCIDENT_STALL_BUDGET", None)
        self.assertEqual(il.incident_budget(), 3)

    def test_a_valid_override_is_honoured(self):
        os.environ["SUPERVISOR_INCIDENT_STALL_BUDGET"] = "7"
        self.assertEqual(il.incident_budget(), 7)

    def test_a_non_numeric_override_falls_back(self):
        os.environ["SUPERVISOR_INCIDENT_STALL_BUDGET"] = "many"
        self.assertEqual(il.incident_budget(), 3)

    def test_a_zero_or_negative_override_falls_back(self):
        for raw in ("0", "-2"):
            os.environ["SUPERVISOR_INCIDENT_STALL_BUDGET"] = raw
            self.assertEqual(il.incident_budget(), 3)


class TestEscalateSkipsGoneStallIncidents(Base):
    """The CARD is posted by `escalate`, not by `stall`, so the gate belongs there too.

    `cmd_stall` gates creation, but an incident that already exists still reaches the
    board through `cmd_escalate` — which is the leg the CHANGELOG measures ("116 open
    board cards in 41 seconds"). A creation-only gate would leave the stated harm
    untouched for everything already in the ledger.
    """

    def setUp(self):
        super().setUp()
        self._live_sessions = il.live_sessions
        self.live = set()
        il.live_sessions = lambda: self.live

    def tearDown(self):
        il.live_sessions = self._live_sessions
        super().tearDown()

    def test_a_gone_stall_incident_posts_no_card_and_is_retired(self):
        """No card, and NOT left pending: a pending skip re-lists and re-probes forever.

        Left `pending` it sat in `share`'s all-time denominator with no possible
        numerator, permanently depressing the one metric this ledger reports.
        """
        self.open_incident(kind="stall", deadline_seconds=0)
        rc, out = self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(rc, 0)
        self.assertIn("gone sessions", out)
        self.assertEqual(self.posted, [])
        self.assertEqual(
            self.records()[0]["outcome"], il.SKIPPED_GONE, "a skip is not an escalation"
        )

    def test_a_live_stall_incident_still_escalates(self):
        """Negative control: the gate must not disable escalation generally."""
        self.live = {SESSION, SESSION[:8]}
        self.open_incident(kind="stall", deadline_seconds=0)
        rc, out = self.run_cli("escalate", "--round-id", "r1")
        self.assertEqual(rc, 0)
        self.assertIn("escalated", out)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.records()[0]["outcome"], il.TIMED_OUT)


class TestResume(Base):
    """The automatic close: a tool call the WORKER made after its answer.

    This is the observer SC2's "let the worker act, confirm it closes" depends on,
    and the pairing is the point — a tool call BEFORE the answer must not close the
    incident, or every incident on an active worker would close on the first poll
    and the answer would be credited with a resume it never caused.
    """

    def write_transcript(self, session=SESSION, calls=None):
        root = os.path.join(self.tmp, "projects")
        directory = os.path.join(root, "-some-cwd")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, session[:8] + "-abcd.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for stamp, name in calls or []:
                handle.write(json.dumps({
                    "type": "assistant",
                    "timestamp": stamp,
                    "message": {"content": [
                        {"type": "tool_use", "id": "t1", "name": name, "input": {}}
                    ]},
                }) + "\n")
        return root

    def resume(self, root, dry_run=False):
        argv = ["resume", "--projects-root", root]
        if dry_run:
            argv.append("--dry-run")
        rc, out = self.run_cli(*argv)
        self.assertEqual(rc, 0)
        return out

    def answered_incident(self, by="manager"):
        self.open_incident()
        self.answer(by=by)

    def after_answer(self, seconds=30):
        """A transcript stamp strictly after the recorded answer.

        Derived rather than taken from `now()`: the answer and the transcript would
        otherwise land in the same second, `stamp <= since` would be true, and the
        test would fail while asserting the opposite of what it means to.
        """
        answered = il.parse_ts(self.records()[0]["answered_at"])
        return (answered + datetime.timedelta(seconds=seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )

    def test_a_tool_call_after_the_answer_closes_it(self):
        self.answered_incident()
        root = self.write_transcript(calls=[(self.after_answer(), "Bash")])
        out = self.resume(root)
        self.assertIn("closed", out)
        closed = self.records()[0]
        self.assertEqual(closed["outcome"], "manager")
        self.assertIn("Bash", closed["resume_evidence"])
        self.assertTrue(closed["closed_at"])

    def test_a_tool_call_BEFORE_the_answer_does_not_close_it(self):
        # The negative control. A worker that was already working when the answer
        # landed has not resumed — crediting that would close every incident on an
        # active worker at the first poll.
        self.answered_incident()
        earlier = (il.parse_ts(self.records()[0]["answered_at"])
                   - datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        root = self.write_transcript(calls=[(earlier, "Bash")])
        out = self.resume(root)
        self.assertIn("no verified resumes", out)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_an_answered_incident_with_no_tool_call_stays_open(self):
        self.answered_incident()
        root = self.write_transcript(calls=[])
        out = self.resume(root)
        self.assertIn("no verified resumes", out)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_an_unanswered_incident_is_untouched(self):
        # No answer, no close, however busy the worker is.
        self.open_incident()
        root = self.write_transcript(calls=[(iso(), "Bash")])
        self.resume(root)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_a_missing_answerer_is_left_open_not_defaulted(self):
        # Crediting `manager` for an incident whose answerer nobody recorded is
        # exactly the inflation this ledger exists to prevent.
        self.open_incident()
        data = self.ledger()
        answered_at = iso()
        data["incidents"][0]["answered_at"] = answered_at
        data["incidents"][0]["answered_by"] = None
        with open(il.LEDGER, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        later = (il.parse_ts(answered_at)
                 + datetime.timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        root = self.write_transcript(calls=[(later, "Bash")])
        self.resume(root)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_the_outcome_follows_who_answered(self):
        self.answered_incident(by="operator")
        root = self.write_transcript(calls=[(self.after_answer(), "Bash")])
        self.resume(root)
        self.assertEqual(self.records()[0]["outcome"], "operator")

    def test_dry_run_writes_nothing_and_does_not_contradict_itself(self):
        self.answered_incident()
        root = self.write_transcript(calls=[(self.after_answer(), "Bash")])
        out = self.resume(root, dry_run=True)
        self.assertIn("would close", out)
        # `closed` stays 0 on a dry run, so the tail used to print "no verified
        # resumes" directly under the "would close" lines it had just emitted.
        self.assertNotIn("no verified resumes", out)
        self.assertEqual(self.records()[0]["outcome"], il.PENDING)

    def test_the_transcript_is_resolved_by_id_prefix(self):
        # The watcher's own join; a full-uuid filename match would miss every
        # transcript whose directory is named from a munged cwd.
        self.answered_incident()
        root = self.write_transcript(calls=[(iso(), "Bash")])
        found = il.transcript_for(root, SESSION)
        self.assertIsNotNone(found)
        self.assertTrue(found.endswith(".jsonl"))

    def test_a_missing_transcript_is_not_an_error(self):
        self.answered_incident()
        out = self.resume(os.path.join(self.tmp, "no-such-root"))
        self.assertIn("no verified resumes", out)


if __name__ == "__main__":
    unittest.main()
