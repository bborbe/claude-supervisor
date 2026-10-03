"""Tests for scripts/manager-attention-watch.py.

The properties worth pinning, because each is a way the watcher could look like it
works while being wrong:

  - **The defect itself, as the negative probe.** A worker whose closer was
    displaced by ordinary assistant text, but which is STILL parked, must stay
    gated. An implementation keying on the transcript alone passes every other
    test here and fails this one — which is exactly the 2026-10-03 defect: three
    false `CLEARED` firings in ~25 minutes, each reading as progress on a worker
    that was blocked the whole time.
  - **`busy` with a stale closer is not a gate.** The union's whole point is that
    a half of it cannot be trusted alone; this is the case where the transcript
    half would produce a false `NEW GATE`.
  - **UNREGISTERED is held, never cleared.** `None` is a three-way answer. An
    implementation that reads it as "not gated" emits a `CLEARED` for a worker
    that died — the same silent direction as the defect.
  - **The id scan is field-scoped.** An id quoted in a task's own Progress prose
    must not become a watched session; measured 2026-10-01, an unanchored scan
    lifted a deliberately fake stub id out of a Progress entry and would have
    manufactured a phantom owner.
"""
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SID = "d91bf7c1-0000-0000-0000-000000000000"
SID8 = SID[:8]
TASK = "Wire the Supervisor's Cluster Spawn Target"
CLOSER = 'pick — 1. alpha · 2. beta'
NOTHING = 'nothing — the topic manager drives this pane; no operator action'
# The transcript carries the WHOLE line, prefix included; `closer_body()` scans
# for lines starting with `👤 You:` and returns what follows it. `CLOSER` above is
# the body (what the function returns), `CLOSER_LINE` is what a real transcript
# holds — conflating the two is what made the first run of these tests fail.
CLOSER_LINE = '👤 You: ' + CLOSER
NOTHING_LINE = '👤 You: ' + NOTHING
PROSE = "Working on it — checking the fixture state before I ask."


def load(name, filename):
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watch = load("manager_attention_watch", "manager-attention-watch.py")


def assistant(text=None, tool=None, thinking=None):
    """One assistant transcript record. `thinking`/`tool` carry NO text block.

    That is the shape measured live 2026-10-04: an `AskUserQuestion` renders as a
    `tool_use` record and its reasoning as a `thinking` record, so neither
    displaces the closer — which is why a prompt opening on its own is not the
    defect.
    """
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "t1", "name": tool, "input": {}})
    return {"type": "assistant", "message": {"content": content}}


class Fixture:
    """A temp vault + transcript tree + registry, one tracked task."""

    def __init__(self, records, status, task_body=None):
        self.dir = tempfile.mkdtemp(prefix="maw-test-")
        # `projects` is the ROOT — the script globs one level down, matching the
        # real `~/.claude/projects/<munged-cwd>/<sid>.jsonl` layout, because a
        # tracked worker may run in any cwd.
        self.projects = os.path.join(self.dir, "projects")
        self.projdir = os.path.join(self.projects, "-Users-someone-my-vault")
        self.tasks = os.path.join(self.dir, "tasks")
        self.sessions = os.path.join(self.dir, "sessions")
        for d in (self.projdir, self.tasks, self.sessions):
            os.makedirs(d)
        self.transcript = os.path.join(self.projdir, SID + ".jsonl")
        self.tracked = os.path.join(self.dir, "tracked.txt")
        with open(self.tracked, "w") as fh:
            fh.write(TASK + "\n")
        with open(os.path.join(self.tasks, TASK + ".md"), "w", encoding="utf-8") as fh:
            fh.write("---\nclaude_session_id: %s\n---\n%s\n" % (SID, task_body or "x"))
        with open(self.transcript, "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        if status is not None:
            with open(os.path.join(self.sessions, "96345.json"), "w") as fh:
                json.dump({"sessionId": SID, "status": status, "pid": 96345}, fh)

    def probe(self):
        return watch.probe(self.tracked, self.tasks, self.projects, self.sessions)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class PredicateTest(unittest.TestCase):
    """The composite predicate, as a table — every branch pinned."""

    def test_waiting_is_gated_regardless_of_transcript(self):
        for body in (CLOSER, NOTHING, None):
            gated, reason = watch.is_gated("waiting", body)
            self.assertIs(gated, True, body)
            self.assertEqual(reason, "registry:waiting")

    def test_idle_with_an_ask_is_gated(self):
        gated, reason = watch.is_gated("idle", CLOSER)
        self.assertIs(gated, True)
        self.assertEqual(reason, "idle+closer")

    def test_idle_without_an_ask_is_not_gated(self):
        for body in (NOTHING, None, "", "Nothing to do"):
            self.assertIs(watch.is_gated("idle", body)[0], False, body)

    def test_busy_with_a_stale_closer_is_not_gated(self):
        # The case the transcript half alone would get wrong in the other
        # direction: a worker that resumed and is working still carries its old
        # closer in the transcript. It is not a gate.
        self.assertIs(watch.is_gated("busy", CLOSER)[0], False)

    def test_shell_is_not_gated(self):
        self.assertIs(watch.is_gated("shell", CLOSER)[0], False)

    def test_unregistered_is_held_not_cleared(self):
        # None is a three-way answer: UNREGISTERED, never "no gate".
        gated, reason = watch.is_gated(None, CLOSER)
        self.assertIsNone(gated)
        self.assertEqual(reason, "unregistered")


class CloserBodyTest(unittest.TestCase):
    def test_last_closer_wins(self):
        text = "👤 You: first\nsome prose\n👤 You: second"
        self.assertEqual(watch.closer_body(text), "second")

    def test_no_closer_is_none(self):
        self.assertIsNone(watch.closer_body("just prose, no panel"))
        self.assertIsNone(watch.closer_body(""))


class DefectTest(unittest.TestCase):
    """The 2026-10-03 firing, reproduced — and it must NOT clear."""

    def setUp(self):
        self.fx = Fixture([assistant(text=CLOSER_LINE)], "idle")

    def tearDown(self):
        self.fx.cleanup()

    def test_t1_closer_up_is_gated(self):
        self.assertIn(SID8, watch.gated_keys(self.fx.probe()))

    def test_t2_prose_replaces_closer_while_still_parked_stays_gated(self):
        # Firing 1: the closer stops being the last text block because the worker
        # wrote prose before raising a prompt — and it is still parked. The
        # transcript half now reads "no closer"; the registry says `waiting`.
        self.fx.cleanup()
        self.fx = Fixture(
            [assistant(text=CLOSER_LINE),
             assistant(thinking="weighing the options"),
             assistant(text=PROSE, tool="AskUserQuestion")],
            "waiting")
        state = self.fx.probe()
        self.assertIn(SID8, watch.gated_keys(state),
                      "still parked -> must stay gated")
        self.assertEqual(state[SID8][2], "registry:waiting")
        # ...and the transcript really has lost the closer, so an implementation
        # keying on it alone would have dropped the session and emitted CLEARED.
        text = watch.last_assistant_text(
            self.fx.transcript)
        self.assertIsNone(watch.closer_body(text))

    def test_a_prompt_alone_does_not_displace_the_closer(self):
        # Measured live 2026-10-04: a park with no preceding prose reads `waiting`
        # AND keeps its closer, so both halves agree. This is why "any prompt
        # replaces the closer" — the filing row's original claim — is false, and
        # why a prompt opening on its own is not the defect.
        self.fx.cleanup()
        self.fx = Fixture(
            [assistant(text=CLOSER_LINE),
             assistant(thinking="weighing the options"),
             assistant(tool="AskUserQuestion")],
            "waiting")
        text = watch.last_assistant_text(
            self.fx.transcript)
        self.assertEqual(watch.closer_body(text), CLOSER)
        self.assertIn(SID8, watch.gated_keys(self.fx.probe()))

    def test_t2_transcript_only_would_have_cleared(self):
        # The contrast that makes the test above meaningful: with the registry
        # input removed, the same transcript reads not-gated.
        self.fx.cleanup()
        self.fx = Fixture(
            [assistant(text=CLOSER_LINE), assistant(text=PROSE, tool="AskUserQuestion")],
            "busy")
        text = watch.last_assistant_text(
            self.fx.transcript)
        self.assertIsNone(watch.closer_body(text))
        self.assertNotIn(SID8, watch.gated_keys(self.fx.probe()))

    def test_genuine_answer_clears(self):
        # SC2's shape: the worker resumed and is working, so it is not a gate.
        self.fx.cleanup()
        self.fx = Fixture([assistant(text=CLOSER_LINE), assistant(text=PROSE)], "busy")
        self.assertNotIn(SID8, watch.gated_keys(self.fx.probe()))

    def test_dead_session_is_held_not_cleared(self):
        # A session that dies while gated must not be announced as a clear. It
        # leaves the gated set (so no CLEARED), and its verdict stays None so the
        # caller can record the transition as HELD rather than as progress.
        self.fx.cleanup()
        self.fx = Fixture([assistant(text=CLOSER_LINE)], None)
        state = self.fx.probe()
        self.assertIn(SID8, state, "still tracked, so the caller can see it")
        self.assertIsNone(state[SID8][3], "UNREGISTERED, not False")
        self.assertNotIn(SID8, watch.gated_keys(state))


class TrackedIdsTest(unittest.TestCase):
    def test_id_quoted_in_progress_prose_is_not_watched(self):
        body = ("# Progress\n\n- 2026-10-01: an earlier draft carried\n"
                "    - session_id: deadbeef-0000-0000-0000-000000000000\n"
                "  which was a deliberate stub.\n")
        fx = Fixture([assistant(text=CLOSER_LINE)], "idle", task_body=body)
        try:
            ids = watch.tracked_ids(fx.tracked, fx.tasks)
            self.assertIn(SID8, ids)
            self.assertNotIn("deadbeef", ids)
        finally:
            fx.cleanup()

    def test_unreadable_tracked_set_is_not_a_quiet_sweep(self):
        # A watcher watching nothing must never look like a quiet one. Returning
        # an empty set for a failed read is exactly that failure, and this repo
        # names it: *a broken watcher looks exactly like a quiet one*.
        err = io.StringIO()
        with redirect_stderr(err):
            ids = watch.tracked_ids("/nonexistent/tracked.txt", "/nonexistent/tasks")
        self.assertEqual(ids, {})
        self.assertIn("NOTHING is being watched", err.getvalue())

    def test_empty_tracked_set_warns(self):
        d = tempfile.mkdtemp(prefix="maw-empty-")
        try:
            path = os.path.join(d, "tracked.txt")
            with open(path, "w"):
                pass
            err = io.StringIO()
            with redirect_stderr(err):
                watch.tracked_ids(path, d)
            self.assertIn("tracked set is empty", err.getvalue())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_metrics_sessions_entry_is_watched(self):
        body = ("# Progress\n\n- x\n\nmetrics_sessions:\n"
                "    - session_id: aaaaaaaa-1111-2222-3333-444444444444\n")
        # frontmatter carries metrics_sessions in the real vault
        fx = Fixture([assistant(text=CLOSER_LINE)], "idle")
        try:
            with open(os.path.join(fx.tasks, TASK + ".md"), "w", encoding="utf-8") as fh:
                fh.write("---\nclaude_session_id: %s\nmetrics_sessions:\n"
                         "    - session_id: aaaaaaaa-1111-2222-3333-444444444444\n"
                         "---\n%s\n" % (SID, body))
            ids = watch.tracked_ids(fx.tracked, fx.tasks)
            self.assertIn(SID8, ids)
            self.assertIn("aaaaaaaa", ids)
        finally:
            fx.cleanup()


class EventLogTest(unittest.TestCase):
    def test_transition_writes_a_durable_line(self):
        d = tempfile.mkdtemp(prefix="maw-log-")
        try:
            path = os.path.join(d, "sub", "events.jsonl")
            watch.log_event(path, "CLEARED", SID8, TASK, "left the gated set")
            with open(path) as fh:
                rec = json.loads(fh.read().strip())
            self.assertEqual(rec["kind"], "CLEARED")
            self.assertEqual(rec["session"], SID8)
            self.assertTrue(rec["ts"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_unwritable_log_warns_on_stderr_and_does_not_raise(self):
        # A watcher that dies because it cannot log is worse than one that logs
        # nothing: the doorbell goes silent and nothing says so.
        err = io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(err):
            watch.log_event("/dev/null/nope/events.jsonl", "CLEARED", SID8, TASK, "x")
        self.assertIn("could not write event log", err.getvalue())


class StateScopeTest(unittest.TestCase):
    """Two managers sharing a state dir must not clobber each other's `prev`.

    This is the defect the pr-reviewer bot found on PR #130: with one shared
    `state.json`, each poll reads the other manager's gated set as its own `prev`,
    every one of those sessions lands in `prevset - set(key)`, misses the HELD
    guard, and prints a bare `CLEARED` — the silent direction this file exists to
    close, reintroduced by a filename.
    """

    def test_state_file_is_keyed_by_tracked_set(self):
        a = watch.state_path_for("/s", "/vault/alpha.tracked.txt")
        b = watch.state_path_for("/s", "/vault/beta.tracked.txt")
        self.assertNotEqual(a, b, "two scopes must not share one state file")
        self.assertEqual(a, watch.state_path_for("/s", "/vault/alpha.tracked.txt"),
                         "same scope must be stable across polls")
        self.assertTrue(os.path.basename(a).startswith("state-"))

    def test_no_bare_state_json_is_ever_used(self):
        for name in ("/vault/alpha.tracked.txt", "/vault/beta.tracked.txt"):
            self.assertNotEqual(
                os.path.basename(watch.state_path_for("/s", name)), "state.json")

    def test_shared_state_manufactures_a_bare_cleared(self):
        # THE DEFECT ITSELF, reproduced rather than avoided — path inequality
        # alone would pin the fix by construction and never show the failure.
        # Manager B's poll reads manager A's gated set as its `prev`; A's session
        # is absent from B's `state`, so it misses the HELD guard and takes the
        # bare CLEARED branch.
        b_state = {"bbbb2222": ("B", "y", "idle+closer", True)}
        got = watch.transitions(["aaaa1111"], ["bbbb2222"], b_state)
        self.assertIn(("CLEARED", "aaaa1111", "", "left the gated set"), got,
                      "this is the failure the per-scope state file prevents")

    def test_unregistered_is_held_never_cleared(self):
        state = {"aaaa1111": ("A", "x", "unregistered", None)}
        self.assertEqual([k for k, *_ in watch.transitions(["aaaa1111"], [], state)],
                         ["HELD"])

    def test_genuine_answer_still_clears(self):
        state = {"aaaa1111": ("A", "x", "registry:busy", False)}
        self.assertEqual([k for k, *_ in watch.transitions(["aaaa1111"], [], state)],
                         ["CLEARED"])

    def test_new_gate_only_for_newly_gated(self):
        state = {"aaaa1111": ("A", "x", "idle+closer", True),
                 "bbbb2222": ("B", "y", "idle+closer", True)}
        got = watch.transitions(["aaaa1111"], ["aaaa1111", "bbbb2222"], state)
        self.assertEqual([(k, s) for k, s, *_ in got], [("NEW GATE", "bbbb2222")],
                         "an unchanged gate must not be re-announced")


class MissingTranscriptTest(unittest.TestCase):
    def test_tracked_id_without_a_transcript_warns(self):
        # The projects ROOT holds one dir per cwd; a tracked id with no
        # transcript anywhere under it is a scope gap, and must not read as quiet.
        fx = Fixture([assistant(text=CLOSER_LINE)], "idle")
        try:
            os.remove(fx.transcript)
            err = io.StringIO()
            with redirect_stderr(err):
                state = watch.probe(fx.tracked, fx.tasks, fx.projects, fx.sessions)
            self.assertEqual(state, {})
            self.assertIn("no transcript for tracked session", err.getvalue())
            self.assertIn("not a quiet sweep", err.getvalue())
        finally:
            fx.cleanup()

    def test_the_warning_fires_once_per_id(self):
        fx = Fixture([assistant(text=CLOSER_LINE)], "idle")
        try:
            os.remove(fx.transcript)
            warned = set()
            err = io.StringIO()
            with redirect_stderr(err):
                for _ in range(3):
                    watch.probe(fx.tracked, fx.tasks, fx.projects, fx.sessions,
                                warned)
            self.assertEqual(err.getvalue().count("no transcript"), 1,
                             "a 60s poll must not repeat the warning forever")
        finally:
            fx.cleanup()


class OnceModeTest(unittest.TestCase):
    def test_once_prints_the_verdict_and_exits(self):
        fx = Fixture([assistant(text=CLOSER_LINE)], "idle")
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = watch.main(["--tracked", fx.tracked, "--tasks-dir", fx.tasks,
                                 "--projects-root", fx.projects,
                                 "--sessions-dir", fx.sessions, "--state",
                                 os.path.join(fx.dir, "state"), "--once"])
            self.assertEqual(rc, 0)
            self.assertIn("GATED " + SID8, out.getvalue())
            self.assertIn("idle+closer", out.getvalue())
        finally:
            fx.cleanup()


if __name__ == "__main__":
    unittest.main()
