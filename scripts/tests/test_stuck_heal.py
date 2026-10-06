"""Tests for scripts/stuck-heal.py — the heal ladder the manager executes on a
`stuck:` gate from scripts/manager-attention-watch.py.

The script only DECIDES; every effect is injected, so the ladder is tested as a
table plus a fake-effects run, and the ledger join against synthetic records.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SID = "e4fd1919-b082-4bb4-aebb-d7185cd61502"
SID8 = SID[:8]
TASK = "Example Task"


def load(name, filename):
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


heal = load("stuck_heal", "stuck-heal.py")


class HealDecideTest(unittest.TestCase):
    """The ladder, as a table. `deaths` counts RESUMES, not deaths."""

    def test_first_death_resumes_headless(self):
        self.assertEqual(heal.decide("stream-closed", 0), "resume-headless")

    def test_second_death_reopens_interactive(self):
        self.assertEqual(heal.decide("stream-closed", 1), "reopen-interactive")

    def test_any_prior_resume_reopens_and_never_resumes_headless_again(self):
        for deaths in (1, 2, 9):
            self.assertEqual(heal.decide("stream-closed", deaths), "reopen-interactive")

    def test_a_parked_gate_is_never_healed_whatever_the_count(self):
        # SC4's structural half: no count makes a judgement gate resumable.
        for deaths in (0, 1, 2):
            self.assertEqual(heal.decide("parked-gate", deaths), "report-only")

    def test_an_unknown_kind_is_reported_not_guessed(self):
        self.assertEqual(heal.decide("something-new", 0), "report-only")


class HealTest(unittest.TestCase):
    """SC2-SC4 — the ladder driven through recording effects."""

    def setUp(self):
        self.calls = []

        def rec(name):
            def f(*a):
                self.calls.append((name, a))
                if name == "spawn_interactive":
                    return "222"
                if name == "jump_link":
                    return "http://127.0.0.1:1337/jump?pane=" + str(a[0])
                return None
            return f

        self.effects = dict(
            spawn_headless=rec("spawn_headless"),
            spawn_interactive=rec("spawn_interactive"),
            set_task_mode=rec("set_task_mode"),
            post_card=rec("post_card"),
            jump_link=rec("jump_link"),
        )

    def named(self, name):
        return [a for n, a in self.calls if n == name]

    def test_first_death_resumes_exactly_once_in_the_same_session(self):
        action = heal.heal(SID, TASK, "stream-closed", 0, **self.effects)
        self.assertEqual(action, "resume-headless")
        self.assertEqual(self.named("spawn_headless"), [(SID,)],
                         "exactly one call, carrying the SAME session id")
        self.assertEqual(self.named("spawn_interactive"), [])
        self.assertEqual(self.named("set_task_mode"), [])
        self.assertEqual(self.named("post_card"), [])

    def test_second_death_goes_interactive_and_never_resumes_again(self):
        action = heal.heal(SID, TASK, "stream-closed", 1, **self.effects)
        self.assertEqual(action, "reopen-interactive")
        self.assertEqual(self.named("spawn_headless"), [],
                         "no third headless resume occurs")
        self.assertEqual(self.named("set_task_mode"), [(TASK, "interactive")])
        self.assertEqual(len(self.named("spawn_interactive")), 1)
        cards = self.named("post_card")
        self.assertEqual(len(cards), 1)
        self.assertEqual(self.named("jump_link"), [("222",)],
                         "the jump link is built from the NEW pane id")
        self.assertIn("pane=222", cards[0][1], "the card carries the jump link")

    def test_a_parked_judgement_gate_is_reported_and_never_answered(self):
        action = heal.heal(SID, TASK, "parked-gate", 0, **self.effects)
        self.assertEqual(action, "report-only")
        self.assertEqual(self.named("spawn_headless"), [])
        self.assertEqual(self.named("spawn_interactive"), [])
        self.assertEqual(self.named("set_task_mode"), [])
        self.assertEqual(len(self.named("post_card")), 1)
        # Structural: heal() is given no way to answer a permission at all.
        import inspect
        self.assertNotIn("answer_permission", inspect.signature(heal.heal).parameters)

    def test_a_failing_card_does_not_mask_the_spawn_failure(self):
        def boom(*a):
            raise RuntimeError("spawn failed")
        def card_boom(*a):
            raise ValueError("card failed")
        effects = dict(self.effects, spawn_interactive=boom, post_card=card_boom)
        with self.assertRaisesRegex(RuntimeError, "spawn failed"):
            heal.heal(SID, TASK, "stream-closed", 1, **effects)

    def test_a_failed_interactive_reopen_restores_the_mode(self):
        def boom(sid):
            self.calls.append(("spawn_interactive", (sid,)))
            raise RuntimeError("spawn failed")
        effects = dict(self.effects, spawn_interactive=boom)
        with self.assertRaises(RuntimeError):
            heal.heal(SID, TASK, "stream-closed", 1, **effects)
        self.assertEqual([a for a in self.named("set_task_mode")],
                         [(TASK, "interactive"), (TASK, "headless")])
        self.assertEqual(len(self.named("post_card")), 1)


class DeathsFromLedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="heal-ledger-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, **rec):
        with open(os.path.join(self.dir, name + ".json"), "w") as fh:
            json.dump(rec, fh)

    def test_a_missing_ledger_is_zero_not_an_exception(self):
        self.assertEqual(
            heal.deaths_from_ledger(SID, os.path.join(self.dir, "nope")), 0)

    def test_resumes_of_this_session_are_counted_by_resumed_from(self):
        # A resume keeps the SAME session id, so counting distinct ids would
        # always read 1 and the ladder would never leave the first rung.
        # Real shape: one `<session_id>.json` per session, overwritten in place
        # on a resume (server/ledger.mjs recordPath).
        self.write(SID, session_id=SID, resumed_from=SID)
        self.write("other", session_id="other", resumed_from="other")
        self.assertEqual(heal.deaths_from_ledger(SID, self.dir), 1)

    def test_a_fresh_record_is_zero(self):
        self.write(SID, session_id=SID, resumed_from=None)
        self.assertEqual(heal.deaths_from_ledger(SID, self.dir), 0)

    def test_non_object_json_is_skipped_not_a_crash(self):
        for i, body in enumerate(("[1,2,3]", '"str"', "7", "null")):
            with open(os.path.join(self.dir, f"odd{i}.json"), "w") as fh:
                fh.write(body)
        self.assertEqual(heal.deaths_from_ledger(SID, self.dir), 0)

    def test_an_eight_char_prefix_resolves(self):
        # The form a manager actually holds: the watcher prints 8 chars, and an
        # exact-match lookup would read 0 and re-resume an already-resumed session.
        self.write("a", session_id=SID, resumed_from=SID)
        self.assertEqual(heal.deaths_from_ledger(SID[:8], self.dir), 1)

    def test_an_unreadable_record_is_skipped_not_fatal(self):
        self.write("a", session_id=SID, resumed_from=SID)
        with open(os.path.join(self.dir, "broken.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(heal.deaths_from_ledger(SID, self.dir), 1)


    def test_a_different_session_sharing_only_a_longer_prefix_is_not_counted(self):
        # Full-id lookup: a record from another session that shares the first 8
        # chars must not count against this one.
        other = SID[:8] + "-0000-0000-0000-000000000000"
        self.write("r1", resumed_from=other)
        self.assertEqual(heal.deaths_from_ledger(SID, self.dir), 0)


class MainTest(unittest.TestCase):
    """The printed line is the whole contract with the manager."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="heal-main-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def run_main(self, *argv):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = heal.main(list(argv) + ["--ledger-dir", self.dir])
        return rc, out.getvalue().strip()

    def test_line_shape_for_a_first_stream_closed_death(self):
        rc, line = self.run_main("--session", SID8, "--task", TASK, "--kind", "stream-closed")
        self.assertEqual(rc, 0)
        self.assertEqual(line, f"HEAL {SID8} [stream-closed] resumes=0 -> resume-headless")

    def test_kind_is_passed_through_and_non_stream_closed_only_reports(self):
        _, line = self.run_main("--session", SID8, "--task", TASK, "--kind", "permission-failures")
        self.assertTrue(line.endswith("-> report-only"), line)

    def test_line_shape_after_one_resume_reopens(self):
        with open(os.path.join(self.dir, SID + ".json"), "w") as fh:
            json.dump({"session_id": SID, "resumed_from": SID}, fh)
        _, line = self.run_main("--session", SID8, "--task", TASK, "--kind", "stream-closed")
        self.assertEqual(line, f"HEAL {SID8} [stream-closed] resumes=1 -> reopen-interactive")

    def test_kind_is_required(self):
        import contextlib, io
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            heal.main(["--session", SID8, "--task", TASK])

    def test_a_short_session_is_refused(self):
        import contextlib, io
        for bad in ("", "abc"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                heal.main(["--session", bad, "--task", TASK, "--kind", "stream-closed"])


if __name__ == "__main__":
    unittest.main()
