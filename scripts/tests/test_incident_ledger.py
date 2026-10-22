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
        with open(il.LEDGER, encoding="utf-8") as handle:
            return json.load(handle)

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


class TestStall(Base):
    """The round-based stall — the one blocker that is a duration, not an event.

    It cannot be opened on sight: `SUPERVISOR_INCIDENT_STALL_ROUNDS` (default 2) is
    the threshold, and the two cases that matter are the pair — a single
    observation must NOT become a blocker, and a second CONSECUTIVE one must. A
    detector that opened on sight passes the second and fails the first, and would
    file a blocker for every session that had one bad round.
    """

    def write_drive(self, verdicts, legacy=False):
        path = os.path.join(self.tmp, "drive.json")
        if legacy:
            data = {sid[:8]: {"sid": sid, "verdict": v} for sid, v in verdicts.items()}
        else:
            data = {
                "round": iso(),
                "entries": [
                    {"session_id": sid, "verdict": v} for sid, v in verdicts.items()
                ],
            }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def stall(self, path, round_id="r1"):
        rc, out = self.run_cli(
            "stall", "--drive-ledger", path, "--round-id", round_id
        )
        self.assertEqual(rc, 0)
        return out

    def test_one_observation_is_not_yet_a_blocker(self):
        path = self.write_drive({SESSION: "unverifiable"})
        out = self.stall(path)
        self.assertIn("observing", out)
        record = self.records()[0]
        self.assertEqual(record["outcome"], il.PENDING)
        self.assertEqual(record["observations"], 1)
        self.assertIsNone(record["opened_at"], "not a blocker until the threshold")
        self.assertIsNone(record["deadline"])

    def test_a_second_consecutive_round_opens_it(self):
        path = self.write_drive({SESSION: "unverifiable"})
        self.stall(path, "r1")
        out = self.stall(path, "r2")
        self.assertIn("opened", out)
        record = self.records()[0]
        self.assertEqual(record["observations"], 2)
        self.assertTrue(record["opened_at"])
        self.assertTrue(record["deadline"])
        self.assertEqual(len(self.records()), 1, "one incident, not one per round")

    def test_a_recovering_session_resets_the_streak(self):
        # Flapping must not accumulate its way to a blocker across unrelated
        # rounds — that is the whole reason the threshold is on a CONTINUOUS run.
        path = self.write_drive({SESSION: "unverifiable"})
        self.stall(path, "r1")
        self.stall(self.write_drive({SESSION: "revive"}), "r2")
        self.assertEqual(self.records()[0]["observations"], 0)
        out = self.stall(self.write_drive({SESSION: "unverifiable"}), "r3")
        self.assertIn("observing", out)
        self.assertIsNone(self.records()[0]["opened_at"])

    def test_the_threshold_override_opens_on_the_first_round(self):
        os.environ["SUPERVISOR_INCIDENT_STALL_ROUNDS"] = "1"
        path = self.write_drive({SESSION: "unverifiable"})
        out = self.stall(path)
        self.assertIn("opened", out)
        self.assertTrue(self.records()[0]["opened_at"])

    def test_a_legacy_flat_drive_ledger_is_read(self):
        # The live file has carried both shapes at once; a detector that saw only
        # the canonical one would report a quiet fleet with half of it invisible.
        path = self.write_drive({SESSION: "unverifiable"}, legacy=True)
        out = self.stall(path)
        self.assertIn("observing", out)
        self.assertEqual(self.records()[0]["session_id"], SESSION)

    def test_non_unverifiable_verdicts_are_ignored(self):
        path = self.write_drive(
            {SESSION: "revive", OTHER_SESSION: "finished", "third": "blocked"}
        )
        out = self.stall(path)
        self.assertIn("no stalled sessions", out)
        self.assertEqual(self.records(), [])

    def test_a_corrupt_drive_ledger_is_refused(self):
        path = os.path.join(self.tmp, "drive.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_cli("stall", "--drive-ledger", path, "--round-id", "r1")
        self.assertEqual(caught.exception.code, il.REFUSED)

    def test_a_missing_drive_ledger_is_not_an_error(self):
        # Absent is the ordinary state before the drive leg has run once.
        rc, out = self.run_cli(
            "stall", "--drive-ledger", os.path.join(self.tmp, "nope.json"),
            "--round-id", "r1",
        )
        self.assertEqual(rc, 0)
        self.assertIn("no stalled sessions", out)

    def test_an_observing_stall_does_not_enter_the_share_denominator(self):
        # A session one round into a possible stall is not a blocker yet; counting
        # it would inflate the denominator with something nobody was asked about.
        path = self.write_drive({SESSION: "unverifiable"})
        self.stall(path)
        out = self.run_cli("share", "--days", "1")[1]
        self.assertIn("total: 0", out)


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
        record = self.records()[0]
        later = (il.parse_ts(record["answered_at"])
                 + datetime.timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        root = self.write_transcript(calls=[(later, "Bash")])
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
        record = self.records()[0]
        earlier = (il.parse_ts(record["answered_at"])
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
        root = self.write_transcript(
            calls=[(iso(), "Bash")]
        )
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
        # Strictly after the answer, so the only reason this can stay open is the
        # missing answerer — not a timestamp that never crossed the boundary.
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

    def test_dry_run_writes_nothing(self):
        self.answered_incident()
        root = self.write_transcript(calls=[(self.after_answer(), "Bash")])
        out = self.resume(root, dry_run=True)
        self.assertIn("would close", out)
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
