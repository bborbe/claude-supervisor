#!/usr/bin/env python3
"""Tests for scripts/flagged-subjects.py.

Each case pins a rule the selection depends on, and most pin it in BOTH directions — a
selector that opens everything and one that opens nothing must fail different cases:

  * a flagged goal at `status: in_progress` is a subject; an unflagged one, a terminal one
    and one carrying a `completed:` date are not.
  * a **topic** needs only to be non-terminal — the `in_progress` criterion is the goal's.
  * a manager named by `formerNames` still owns its subject (the registry match the page
    field alone cannot see).
  * a manager named for the goal's **topic** owns the goal too — the two-hop chain.
  * a page id that resolves `ABSENT` is a RESUME and stays openable; `LIVE` is managed;
    `UNKNOWN` is reported and never opened — an unreadable verdict is not permission.
  * `phase: todo` is the operator's inbox — `WAITING`, never opened.
  * an open propose/approve card is `CARDED` — the ask is outstanding.
  * the free-slot bound opens at most `free` subjects and NAMES every deferred one.
  * `free is None` is UNLIMITED, not zero — the operator's off switch.
  * topics rank before goals, and a goal whose topic is opened this round is `COVERED`,
    not opened twice.
  * `render()` prints the SUMMARY with the tally, so a probe can assert on one line.

Run: python3 -m unittest discover -s scripts/tests -v
"""
import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "flagged-subjects.py")

_spec = importlib.util.spec_from_file_location("flagged_subjects", _SCRIPT)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)


def write_page(directory, title, frontmatter, provenance="operator"):
    """A page whose `flag:` carries provenance by default.

    Under the operator's 2026-11-10 ruling an absent `flag_set_by` opens nothing at any
    tier, so a fixture that omitted it would model a refused page rather than the
    ordinary admitted one these tests are about. Pass `provenance=None` for the refusal
    shape.
    """
    if provenance and "flag:" in frontmatter and "flag_set_by" not in frontmatter:
        frontmatter = frontmatter.strip() + "\nflag_set_by: %s" % provenance
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, title + ".md"), "w", encoding="utf-8") as handle:
        handle.write("---\n" + frontmatter.strip() + "\n---\n\nbody\n")


class SubjectSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.goals = os.path.join(self.tmp.name, "24 Goals")
        self.topics = os.path.join(self.tmp.name, "23 Topics")
        os.makedirs(self.goals)
        os.makedirs(self.topics)
        self.vault = {"name": "v", "tasks_dir": os.path.join(self.tmp.name, "25 Tasks"),
                      "goals_dir": self.goals, "topics_dir": self.topics}

    def tearDown(self):
        self.tmp.cleanup()

    def test_flagged_in_progress_goal_is_a_subject(self):
        write_page(self.goals, "Open Me", "flag: \"true\"\nstatus: in_progress\nphase: execution")
        self.assertEqual([s["name"] for s in fs.subjects(self.vault, "goal")], ["Open Me"])

    def test_unflagged_goal_is_not_a_subject(self):
        write_page(self.goals, "Ignore Me", "flag: false\nstatus: in_progress\nphase: execution")
        self.assertEqual(fs.subjects(self.vault, "goal"), [])

    def test_terminal_status_is_not_a_subject(self):
        write_page(self.goals, "Done", "flag: true\nstatus: completed\nphase: execution")
        write_page(self.goals, "Parked", "flag: true\nstatus: hold\nphase: execution")
        self.assertEqual(fs.subjects(self.vault, "goal"), [])

    def test_completed_date_is_terminal_even_without_status_flip(self):
        write_page(self.goals, "Closed", "flag: true\nstatus: in_progress\nphase: execution\ncompleted: \"2026-11-05\"")
        self.assertEqual(fs.subjects(self.vault, "goal"), [])

    def test_goal_needs_in_progress_status(self):
        write_page(self.goals, "Queued", "flag: true\nstatus: next\nphase: execution")
        self.assertEqual(fs.subjects(self.vault, "goal"), [])

    def test_topic_needs_only_non_terminal(self):
        write_page(self.topics, "No Status", "flag: true\nphase: execution")
        write_page(self.topics, "Done", "flag: true\nstatus: completed")
        self.assertEqual([s["name"] for s in fs.subjects(self.vault, "topic")], ["No Status"])

    def test_unreadable_goals_dir_is_none_never_empty(self):
        # A path that is a FILE, not a directory: `os.listdir` raises, and the answer must be
        # "unreadable" — never "empty", which would render as a clean round.
        not_a_dir = os.path.join(self.tmp.name, "not-a-dir")
        with open(not_a_dir, "w", encoding="utf-8") as handle:
            handle.write("x")
        self.vault["goals_dir"] = not_a_dir
        self.assertIsNone(fs.subjects(self.vault, "goal"))

    def test_absent_topics_dir_is_empty_not_unreadable(self):
        self.vault["topics_dir"] = None
        self.assertEqual(fs.subjects(self.vault, "topic"), [])


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self._saved_state = fs.session_state
        self._saved_managed = fs.pm.managed

    def tearDown(self):
        fs.session_state = self._saved_state
        fs.pm.managed = self._saved_managed

    def goal(self, **kwargs):
        subject = {"kind": "goal", "name": "G", "vault": "v", "phase": "execution",
                   "page_id": None}
        subject.update(kwargs)
        return subject

    def test_registry_name_matches(self):
        fs.pm.managed = lambda titles, labels, board, registrations=(), vault=None: "G Manager" in labels
        state, _ = fs.classify(self.goal(), ["G Manager"], [], {}, set())
        self.assertEqual(state, "managed")

    def test_former_name_matches_because_callers_pass_former_names_as_labels(self):
        # The caller flattens `name` + `formerNames` into one label list; this pins that the
        # matcher sees a former name exactly as it sees a current one.
        fs.pm.managed = lambda titles, labels, board, registrations=(), vault=None: "G Manager" in labels
        state, _ = fs.classify(self.goal(), ["G Manager"], [], {}, set())
        self.assertEqual(state, "managed")

    def test_topic_chain_owns_the_goal(self):
        fs.pm.managed = lambda titles, labels, board, registrations=(), vault=None: "T" in titles
        state, _ = fs.classify(self.goal(), [], [], {"G": {"T"}}, set())
        self.assertEqual(state, "managed")

    def test_page_id_live_is_managed(self):
        fs.pm.managed = lambda *a, **k: False
        fs.session_state = lambda session_id: fs.LIVE
        state, _ = fs.classify(self.goal(page_id="abc"), [], [], {}, set())
        self.assertEqual(state, "managed")

    def test_page_id_absent_is_openable(self):
        fs.pm.managed = lambda *a, **k: False
        fs.session_state = lambda session_id: fs.ABSENT
        state, _ = fs.classify(self.goal(page_id="abc"), [], [], {}, set())
        self.assertEqual(state, "open")

    def test_page_id_unknown_is_unknown_never_open(self):
        fs.pm.managed = lambda *a, **k: False
        for code in (2, 3, 4, None):  # UNKNOWN, AMBIGUOUS, MALFORMED, probe could not run
            fs.session_state = lambda session_id, code=code: code
            state, reason = fs.classify(self.goal(page_id="abc"), [], [], {}, set())
            self.assertEqual(state, "unknown")
            self.assertIn("could not be read", reason)

    def test_todo_phase_waits_for_approval(self):
        fs.pm.managed = lambda *a, **k: False
        state, reason = fs.classify(self.goal(phase="todo"), [], [], {}, set())
        self.assertEqual(state, "waiting-approval")
        self.assertEqual(reason, "phase: todo")

    def test_open_card_is_carded(self):
        fs.pm.managed = lambda *a, **k: False
        keys = {fs.pm.dedup_key("G")}
        state, reason = fs.classify(self.goal(), [], [], {}, keys)
        self.assertEqual(state, "carded")
        self.assertIn("open card", reason)

    def test_no_card_is_open(self):
        fs.pm.managed = lambda *a, **k: False
        state, _ = fs.classify(self.goal(), [], [], {}, set())
        self.assertEqual(state, "open")


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self._saved = fs.classify
        self.tmp = tempfile.TemporaryDirectory()
        self.goals = os.path.join(self.tmp.name, "24 Goals")
        self.topics = os.path.join(self.tmp.name, "23 Topics")
        self.tasks = os.path.join(self.tmp.name, "25 Tasks")
        for path in (self.goals, self.topics, self.tasks):
            os.makedirs(path)
        self.vault = {"name": "v", "tasks_dir": self.tasks,
                      "goals_dir": self.goals, "topics_dir": self.topics}
        write_page(self.topics, "T", "flag: true\nphase: execution")
        write_page(self.goals, "A", "flag: true\nstatus: in_progress\nphase: execution")
        write_page(self.goals, "B", "flag: true\nstatus: in_progress\nphase: execution")
        write_page(self.goals, "C", "flag: true\nstatus: in_progress\nphase: execution")

    def tearDown(self):
        fs.classify = self._saved
        self.tmp.cleanup()

    def select(self, free):
        fs.classify = lambda subject, labels, registrations, topics, card_keys: ("open", "")
        return fs.select([self.vault], [], [], set(), free)

    def states(self, verdicts):
        return {v["subject"]["name"]: v["state"] for v in verdicts}

    def test_topic_ranks_before_goals(self):
        verdicts = self.select(10)
        self.assertEqual(verdicts[0]["subject"]["kind"], "topic")
        self.assertEqual(verdicts[0]["state"], "open")

    def test_goal_under_opened_topic_is_covered(self):
        write_page(self.topics, "T", "flag: true\nphase: execution\n\n# Goals\n\n- [[A]]")
        verdicts = self.select(10)
        self.assertEqual(self.states(verdicts)["A"], "covered")
        self.assertEqual(self.states(verdicts)["B"], "open")

    def test_free_slots_bound_and_deferred_named(self):
        verdicts = self.select(2)  # topic + one goal
        states = self.states(verdicts)
        self.assertEqual(states["T"], "open")
        self.assertEqual(list(states.values()).count("open"), 2)
        deferred = [v for v in verdicts if v["state"] == "deferred"]
        self.assertEqual(len(deferred), 2)
        self.assertTrue(all(v["reason"] == "manager cap" for v in deferred))

    def test_free_none_is_unlimited(self):
        verdicts = self.select(None)
        self.assertEqual(list(self.states(verdicts).values()).count("open"), 4)

    def test_free_zero_opens_nothing(self):
        verdicts = self.select(0)
        self.assertEqual(list(self.states(verdicts).values()).count("open"), 0)

    def test_render_carries_summary(self):
        lines = fs.render(self.select(2), 2)
        self.assertTrue(lines[-1].startswith("SUMMARY | free=2 open=2 deferred=2"))
        self.assertTrue(any(line.startswith("DEFERRED | v | goal |") for line in lines))


class RenderTest(unittest.TestCase):
    def test_open_line_shape(self):
        verdicts = [{"subject": {"kind": "topic", "name": "T", "vault": "v"},
                     "state": "open", "reason": ""}]
        self.assertEqual(fs.render(verdicts, 1)[0], "OPEN | v | topic | T")

    def test_waiting_line_shape(self):
        verdicts = [{"subject": {"kind": "goal", "name": "G", "vault": "v"},
                     "state": "waiting-approval", "reason": "phase: todo"}]
        self.assertEqual(fs.render(verdicts, 1)[0], "WAITING | v | goal | G | phase: todo")

    def test_unlimited_summary(self):
        self.assertIn("free=unlimited", fs.render([], None)[-1])


class ManagerIdentityTest(unittest.TestCase):
    """A worker named after its page must never suppress the open — the false-negative shape."""

    def board(self, sessions):
        return json.dumps({"sessions": sessions})

    def test_worker_label_is_not_a_manager_label(self):
        raw = self.board([
            {"role": "manager", "label": "G Manager", "session_id": "m1"},
            {"role": "agent", "label": "G", "session_id": "w1"},
        ])
        labels, count, _ = fs.manager_identity(raw, [])
        self.assertEqual(count, 1)
        self.assertEqual(labels, ["G Manager"])

    def test_former_names_widen_a_manager_only(self):
        raw = self.board([
            {"role": "manager", "label": "G Manager", "session_id": "m1"},
            {"role": "agent", "label": "G", "session_id": "w1"},
        ])
        sessions = [
            {"sessionId": "m1", "name": "G Manager", "formerNames": ["Old G Manager"]},
            {"sessionId": "w1", "name": "G", "formerNames": ["G Worker"]},
        ]
        labels, _, _ = fs.manager_identity(raw, sessions)
        self.assertIn("Old G Manager", labels)
        self.assertNotIn("G Worker", labels)

    def test_fleet_manager_is_excluded_from_the_count(self):
        raw = self.board([
            {"role": "manager", "label": "Fleet Manager", "session_id": "f"},
            {"role": "manager", "label": "G Manager", "session_id": "m1"},
        ])
        _, count, _ = fs.manager_identity(raw, [])
        self.assertEqual(count, 1)

    def test_unreadable_board_is_none(self):
        self.assertIsNone(fs.manager_identity("not json", []))


if __name__ == "__main__":
    unittest.main()
