"""Tests for scripts/crashed-managers.py — the uncovered live-but-lapsed card.

The predicate has three conditions and every one of them is a way the card must NOT fire, so
each gets a case. `cadence_state` and `session_state` are stubbed: both shell out to another
script, and this file is about the *decision*, not about re-testing those two instruments.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "crashed_managers", os.path.join(REPO, "scripts", "crashed-managers.py")
)
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        cm._MEMO.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = os.path.join(self.tmp.name, "vault")
        self.goals = os.path.join(self.vault, "24 Goals")
        self.topics = os.path.join(self.vault, "23 Topics")
        os.makedirs(self.goals)
        os.makedirs(self.topics)
        self._cadence = cm.cadence_state
        self._verdict = cm.session_state

    def tearDown(self):
        cm.cadence_state = self._cadence
        cm.session_state = self._verdict
        self.tmp.cleanup()

    def goal(self, name, sid="aaaa1111-0000-0000-0000-000000000000"):
        write(os.path.join(self.goals, f"{name}.md"),
              f"---\nmanager_session_id: {sid}\npage_type: goal\n---\n\n# Impact\n")

    def topic(self, name, goals, sid="bbbb2222-0000-0000-0000-000000000000"):
        body = "\n".join(f"- [ ] [[{g}]]" for g in goals)
        write(os.path.join(self.topics, f"{name}.md"),
              f"---\nmanager_session_id: {sid}\npage_type: topic\n---\n\n## Goals\n\n{body}\n")

    def stub(self, state, verdict):
        cm.cadence_state = lambda name, branch: state
        cm.session_state = lambda sid: verdict

    def test_live_but_lapsed_and_uncovered_cards(self):
        self.goal("Lonely Goal")
        self.stub("STALE", "LIVE")
        row = cm.evaluate(self.vault, "goal", "lonely-goal")
        self.assertTrue(row["card"], row["reason"])
        self.assertEqual(row["subject"], "Lonely Goal")

    def test_covered_by_a_live_topic_manager_does_not_card(self):
        self.goal("Covered Goal")
        self.topic("Some Topic", ["Covered Goal"])
        # the goal's own loop is lapsed; the topic's loop is running and will restart it
        cm.cadence_state = lambda name, branch: "STALE" if branch == "goal" else "OK"
        cm.session_state = lambda sid: "LIVE"
        row = cm.evaluate(self.vault, "goal", "covered-goal")
        self.assertFalse(row["card"], row["reason"])
        self.assertIn("covered", row["reason"])

    def test_a_stopped_topic_manager_covers_nothing(self):
        """`/supervisor:stop` disarms the loop and leaves the session LIVE — it restarts nothing."""
        self.goal("Orphan Goal")
        self.topic("Stopped Topic", ["Orphan Goal"])
        cm.cadence_state = lambda name, branch: "STALE" if branch == "goal" else "STOPPED"
        cm.session_state = lambda sid: "LIVE"
        row = cm.evaluate(self.vault, "goal", "orphan-goal")
        self.assertTrue(
            row["card"],
            f"a STOPPED topic manager was treated as coverage: {row['reason']}",
        )

    def test_a_topic_declaring_the_goal_in_another_case_still_covers(self):
        """Obsidian resolves wiki links case-insensitively."""
        self.goal("Covered Goal")
        self.topic("Some Topic", ["covered goal"])
        cm.cadence_state = lambda name, branch: "STALE" if branch == "goal" else "OK"
        cm.session_state = lambda sid: "LIVE"
        row = cm.evaluate(self.vault, "goal", "covered-goal")
        self.assertFalse(row["card"], f"a case-different link read as not declaring: {row['reason']}")

    def test_a_dead_topic_manager_leaves_the_goal_uncovered(self):
        self.goal("Orphan Goal")            # sid aaaa1111-… (LIVE)
        self.topic("Dead Topic", ["Orphan Goal"])   # sid bbbb2222-… (ABSENT)
        cm.cadence_state = lambda name, branch: "STALE"
        cm.session_state = lambda sid: "LIVE" if sid.startswith("aaaa") else "ABSENT"
        row = cm.evaluate(self.vault, "goal", "orphan-goal")
        self.assertTrue(row["card"], row["reason"])

    def test_stopped_is_a_stand_down_never_a_crash(self):
        self.goal("Stood Down")
        self.stub("STOPPED", "LIVE")
        row = cm.evaluate(self.vault, "goal", "stood-down")
        self.assertFalse(row["card"])
        self.assertIn("STOPPED", row["reason"])

    def test_an_absent_session_is_an_abandoned_record(self):
        self.goal("Abandoned")
        self.stub("STALE", "ABSENT")
        row = cm.evaluate(self.vault, "goal", "abandoned")
        self.assertFalse(row["card"])
        self.assertIn("ABSENT", row["reason"])

    def test_an_undecidable_probe_is_never_a_card(self):
        self.goal("Undecidable")
        self.stub("STALE", "UNKNOWN")
        row = cm.evaluate(self.vault, "goal", "undecidable")
        self.assertFalse(row["card"])
        self.assertIn("UNKNOWN", row["reason"])

    def test_a_page_without_a_session_id_is_unverifiable(self):
        write(os.path.join(self.goals, "No Sid.md"), "---\npage_type: goal\n---\n")
        self.stub("STALE", "LIVE")
        row = cm.evaluate(self.vault, "goal", "no-sid")
        self.assertFalse(row["card"])
        self.assertIn("manager_session_id", row["reason"])

    def test_a_record_with_no_page_is_abandoned(self):
        self.stub("STALE", "LIVE")
        row = cm.evaluate(self.vault, "goal", "no-such-goal")
        self.assertFalse(row["card"])
        self.assertIn("abandoned record", row["reason"])

    def test_a_topic_manager_is_never_covered(self):
        write(os.path.join(self.topics, "Lapsed Topic.md"),
              "---\nmanager_session_id: cccc3333-0000-0000-0000-000000000000\n---\n\n# Goals\n")
        self.stub("STALE", "LIVE")
        row = cm.evaluate(self.vault, "topic", "lapsed-topic")
        self.assertTrue(row["card"], row["reason"])


class SubjectsTest(unittest.TestCase):
    def setUp(self):
        cm._MEMO.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_records_are_counted_and_never_attributed(self):
        for name in ("goal--a-goal.cadence", "topic--a-topic.cadence", "flat-legacy.cadence"):
            write(os.path.join(self.dir, name), "900\n")
        pairs, legacy = cm.subjects(self.dir)
        self.assertEqual(pairs, [("goal", "a-goal"), ("topic", "a-topic")])
        self.assertEqual(legacy, 1)

    def test_an_unreadable_dir_refuses_rather_than_reads_empty(self):
        with self.assertRaises(cm.Refused):
            cm.subjects(os.path.join(self.dir, "does-not-exist"))


class Proc:
    """A `subprocess.run` result, shaped like the real one."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class Recorder:
    """A `subprocess.run` replacement that records argv and answers by script name."""

    def __init__(self, answer=None):
        self.calls = []
        self._answer = answer or (lambda argv: Proc(stdout="posted item-1\n"))

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self._answer(list(argv))

    def posted(self):
        """The argv lists that invoked attention-ask.py post."""
        return [c for c in self.calls if any("attention-ask.py" in str(a) for a in c)]


class MainTest(unittest.TestCase):
    """`main()` and `post()` — the exit-code contract and the card loop."""

    def setUp(self):
        cm._MEMO.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = os.path.join(self.tmp.name, "vault")
        self.goals = os.path.join(self.vault, "24 Goals")
        self.topics = os.path.join(self.vault, "23 Topics")
        self.statedir = os.path.join(self.tmp.name, "state")
        for d in (self.goals, self.topics, self.statedir):
            os.makedirs(d)
        self._run = cm.subprocess.run
        self._cadence = cm.cadence_state
        self._verdict = cm.session_state
        self.addCleanup(self.restore)

    def restore(self):
        cm.subprocess.run = self._run
        cm.cadence_state = self._cadence
        cm.session_state = self._verdict

    def goal(self, name, sid="aaaa1111-0000-0000-0000-000000000000"):
        write(os.path.join(self.goals, f"{name}.md"),
              f"---\nmanager_session_id: {sid}\npage_type: goal\n---\n\n# Impact\n")

    def cadence(self, branch, base):
        write(os.path.join(self.statedir, f"{branch}--{base}.cadence"), "900\n")

    def stub(self, state="STALE", verdict="LIVE"):
        cm.cadence_state = lambda name, branch: state
        cm.session_state = lambda sid: verdict

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cm.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_refused_post_raises_rather_than_reporting_success(self):
        cm.subprocess.run = Recorder(lambda argv: Proc(stderr="store unreachable", returncode=2))
        with self.assertRaises(cm.Refused) as ctx:
            cm.post({"branch": "goal", "base": "lonely-goal", "subject": "Lonely Goal",
                 "session": "aaaa1111-0000-0000-0000-000000000000"})
        self.assertIn("store unreachable", str(ctx.exception))

    def test_a_failed_post_refuses_the_whole_round(self):
        self.goal("Lonely Goal")
        self.cadence("goal", "lonely-goal")
        self.stub()
        cm.subprocess.run = Recorder(lambda argv: Proc(stderr="store unreachable", returncode=2))
        code, out, err = self.run_main("--post", "--vault", self.vault, "--state-dir", self.statedir)
        self.assertEqual(code, 2, out)
        self.assertIn("REFUSED", err)
        self.assertNotIn("crashed-managers:", out)

    def test_one_card_per_crashed_subject_with_distinct_dedup_keys(self):
        for name, base in (("Lonely Goal", "lonely-goal"), ("Other Crash", "other-crash")):
            self.goal(name)
            self.cadence("goal", base)
        self.stub()
        rec = Recorder()
        cm.subprocess.run = rec
        code, out, _ = self.run_main("--post", "--vault", self.vault, "--state-dir", self.statedir)
        self.assertEqual(code, 0, out)
        posts = rec.posted()
        self.assertEqual(len(posts), 2, posts)
        keys = []
        for argv in posts:
            keys.append(argv[argv.index("--dedup-key") + 1])
        self.assertEqual(len(set(keys)), 2, f"colliding dedup keys: {keys}")
        self.assertIn("2 card(s)", out)

    def test_a_goal_and_a_topic_with_one_base_get_distinct_dedup_keys(self):
        """The branch is the only thing telling the two apart — a subject-only key collides."""
        write(os.path.join(self.goals, "Shared Name.md"),
              "---\nmanager_session_id: aaaa1111-0000-0000-0000-000000000000\n---\n")
        write(os.path.join(self.topics, "Shared Name.md"),
              "---\nmanager_session_id: bbbb2222-0000-0000-0000-000000000000\n---\n\n## Goals\n")
        self.cadence("goal", "shared-name")
        self.cadence("topic", "shared-name")
        self.stub()
        rec = Recorder()
        cm.subprocess.run = rec
        code, out, _ = self.run_main("--post", "--vault", self.vault, "--state-dir", self.statedir)
        self.assertEqual(code, 0, out)
        keys = [a[a.index("--dedup-key") + 1] for a in rec.posted()]
        self.assertEqual(len(keys), 2, keys)
        self.assertTrue(any("goal--shared-name" in k for k in keys), keys)
        self.assertTrue(any("topic--shared-name" in k for k in keys), keys)
        for argv in rec.posted():
            self.assertIn("--producer-kind", argv)
            self.assertEqual(argv[argv.index("--producer-kind") + 1], "manager")

    def test_a_bare_invocation_is_a_dry_run(self):
        self.goal("Lonely Goal")
        self.cadence("goal", "lonely-goal")
        self.stub()
        rec = Recorder()
        cm.subprocess.run = rec
        code, out, _ = self.run_main("--vault", self.vault, "--state-dir", self.statedir)
        self.assertEqual(code, 0, out)
        self.assertEqual(rec.posted(), [], "a bare invocation posted a card")
        self.assertEqual(json.loads(out)["cards"], 1)

    def test_json_posts_nothing(self):
        self.goal("Lonely Goal")
        self.cadence("goal", "lonely-goal")
        self.stub()
        rec = Recorder()
        cm.subprocess.run = rec
        code, out, _ = self.run_main("--json", "--vault", self.vault, "--state-dir", self.statedir)
        self.assertEqual(code, 0, out)
        self.assertEqual(rec.posted(), [], "--json posted a card")
        self.assertEqual(json.loads(out)["cards"], 1)

    def test_an_unreadable_state_dir_refuses_with_two(self):
        code, out, err = self.run_main("--json", "--vault", self.vault,
                                       "--state-dir", os.path.join(self.tmp.name, "gone"))
        self.assertEqual(code, 2, out)
        self.assertIn("REFUSED", err)
        self.assertNotIn("crashed-managers:", out)
        self.assertNotIn("0 card", out)

    def test_json_and_post_together_are_refused_by_argparse(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main("--json", "--post", "--vault", self.vault, "--state-dir", self.statedir)
        self.assertEqual(ctx.exception.code, 2)

    def test_an_unreadable_subject_page_does_not_escape_as_a_traceback(self):
        self.goal("Lonely Goal")
        self.cadence("goal", "lonely-goal")
        self.stub()
        page = os.path.join(self.goals, "Lonely Goal.md")
        os.chmod(page, 0o000)
        self.addCleanup(os.chmod, page, 0o644)
        code, out, err = self.run_main("--json", "--vault", self.vault, "--state-dir", self.statedir)
        self.assertNotIn("Traceback", err)
        self.assertIn(code, (0, 2), out)

    def test_an_unreadable_vault_is_not_reported_as_no_crashed_managers(self):
        self.cadence("goal", "lonely-goal")
        self.stub()
        code, out, err = self.run_main("--json", "--vault", os.path.join(self.tmp.name, "nope"),
                                       "--state-dir", self.statedir)
        self.assertFalse(
            code == 0 and json.loads(out)["cards"] == 0 and "abandoned" in out,
            "an unreadable vault read as a clean, empty fleet",
        )

    def test_a_post_with_empty_stdout_still_returns_a_string(self):
        cm.subprocess.run = Recorder(lambda argv: Proc(stdout="", returncode=0))
        got = cm.post({"branch": "goal", "base": "lonely-goal", "subject": "Lonely Goal",
                 "session": "aaaa1111-0000-0000-0000-000000000000"})
        self.assertTrue(isinstance(got, str) and got)


class CadenceStateTest(unittest.TestCase):
    def setUp(self):
        cm._MEMO.clear()
        self._run = cm.subprocess.run
        self.addCleanup(lambda: setattr(cm.subprocess, "run", self._run))

    def test_the_state_is_the_first_tab_field_of_the_first_line(self):
        cm.subprocess.run = lambda argv, **kw: Proc(stdout="STALE\t300\t2026-10-29T22:00:00Z\n")
        self.assertEqual(cm.cadence_state("Lonely Goal", "goal"), "STALE")

    def test_an_unparseable_or_failed_probe_is_invalid_and_never_cards(self):
        for proc in (Proc(stdout="", returncode=2), Proc(stdout="Traceback...\n", returncode=1)):
            cm.subprocess.run = lambda argv, **kw: proc
            self.assertEqual(cm.cadence_state("Lonely Goal", "goal"), cm.PROBE_FAILED)

    def test_the_probe_is_asked_about_this_subject_on_this_branch(self):
        rec = Recorder()
        cm.subprocess.run = rec
        cm.cadence_state("Lapsed Topic", "topic")
        argv = rec.calls[-1]
        self.assertIn("--state", argv)
        self.assertEqual(argv[argv.index("--subject") + 1], "Lapsed Topic")
        self.assertEqual(argv[argv.index("--branch") + 1], "topic")


class SessionStateTest(unittest.TestCase):
    """The one channel that can tell ABSENT from LIVE — read from exit codes, never stdout.

    ⚠️ Every other test stubs this, so a regression back to stdout matching, a wrong code map,
    or a dropped exception branch would leave the suite green while an `ABSENT` session became
    invisible again — the measured bug this script exists to avoid.
    """

    def setUp(self):
        cm._MEMO.clear()
        self._run = cm.subprocess.run
        self.addCleanup(lambda: setattr(cm.subprocess, "run", self._run))

    def test_each_exit_code_maps_to_its_verdict(self):
        for code, want in ((0, "LIVE"), (1, "ABSENT"), (2, "UNKNOWN"),
                           (3, "AMBIGUOUS"), (4, "MALFORMED")):
            cm._MEMO.clear()
            cm.subprocess.run = lambda argv, **kw: Proc(stdout="", stderr="", returncode=code)
            self.assertEqual(cm.session_state("aaaa1111-0000-0000-0000-000000000000"), want,
                             f"exit {code}")

    def test_an_undocumented_code_is_unknown_never_death(self):
        cm.subprocess.run = lambda argv, **kw: Proc(returncode=99)
        self.assertEqual(cm.session_state("aaaa1111-0000-0000-0000-000000000000"), "UNKNOWN")

    def test_a_timeout_is_unknown_never_death(self):
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 30)
        cm.subprocess.run = boom
        self.assertEqual(cm.session_state("aaaa1111-0000-0000-0000-000000000000"), "UNKNOWN")

    def test_an_oserror_is_unknown_never_death(self):
        def boom(argv, **kw):
            raise OSError("cannot exec")
        cm.subprocess.run = boom
        self.assertEqual(cm.session_state("aaaa1111-0000-0000-0000-000000000000"), "UNKNOWN")


class CoverageTest(unittest.TestCase):
    """The coverage test is the operator's decision: a covered manager must never card."""

    def setUp(self):
        cm._MEMO.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = os.path.join(self.tmp.name, "vault")
        self.goals = os.path.join(self.vault, "24 Goals")
        self.topics = os.path.join(self.vault, "23 Topics")
        os.makedirs(self.goals)
        os.makedirs(self.topics)
        self._cadence = cm.cadence_state
        self._verdict = cm.session_state
        self.addCleanup(lambda: (setattr(cm, "cadence_state", self._cadence),
                                 setattr(cm, "session_state", self._verdict)))

    def test_a_live_topic_manager_declaring_another_goal_does_not_cover(self):
        write(os.path.join(self.goals, "Orphan Goal.md"),
              "---\nmanager_session_id: aaaa1111-0000-0000-0000-000000000000\n---\n")
        write(os.path.join(self.topics, "A Topic.md"),
              "---\nmanager_session_id: bbbb2222-0000-0000-0000-000000000000\n---\n\n"
              "## Goals\n\n- [[Some Other Goal]]\n")
        cm.cadence_state = lambda n, b: "STALE"
        cm.session_state = lambda s: "LIVE"
        self.assertTrue(cm.evaluate(self.vault, "goal", "orphan-goal")["card"])

    def test_a_goal_mentioned_only_under_non_goals_is_not_declared(self):
        write(os.path.join(self.goals, "Lonely Goal.md"),
              "---\nmanager_session_id: aaaa1111-0000-0000-0000-000000000000\n---\n")
        write(os.path.join(self.topics, "A Topic.md"),
              "---\nmanager_session_id: bbbb2222-0000-0000-0000-000000000000\n---\n\n"
              "## Scope\n\n## Goals\n\n- [[In Scope Goal]]\n\n"
              "## Non-goals\n\n- [[Lonely Goal]]\n\n# Progress\n\n- restarted [[Lonely Goal]]\n")
        cm.cadence_state = lambda n, b: "STALE"
        cm.session_state = lambda s: "LIVE"
        row = cm.evaluate(self.vault, "goal", "lonely-goal")
        self.assertTrue(row["card"], f"a Non-goals mention read as coverage: {row['reason']}")

    def test_an_unreadable_topic_page_never_yields_a_card(self):
        write(os.path.join(self.goals, "Orphan Goal.md"),
              "---\nmanager_session_id: aaaa1111-0000-0000-0000-000000000000\n---\n")
        topic_page = os.path.join(self.topics, "A Topic.md")
        write(topic_page,
              "---\nmanager_session_id: bbbb2222-0000-0000-0000-000000000000\n---\n\n"
              "## Goals\n\n- [[Orphan Goal]]\n")
        os.chmod(topic_page, 0o000)
        self.addCleanup(os.chmod, topic_page, 0o644)
        cm.cadence_state = lambda n, b: "STALE"
        cm.session_state = lambda s: "LIVE"
        try:
            row = cm.evaluate(self.vault, "goal", "orphan-goal")
        except cm.Refused:
            return  # refusing is the stronger form of "no card"
        self.assertFalse(
            row["card"],
            "an unreadable topic page read as 'does not declare', carding a covered manager",
        )

    def test_a_fleet_subject_never_cards(self):
        cm.cadence_state = lambda n, b: "STALE"
        cm.session_state = lambda s: "LIVE"
        self.assertFalse(cm.evaluate(self.vault, "fleet", "fleet-manager")["card"])


if __name__ == "__main__":
    unittest.main()
