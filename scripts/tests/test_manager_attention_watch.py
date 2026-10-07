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
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

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
        #
        # ⚠️ HELD (`None`), not `False`. Both keep it out of the gated set, so
        # neither raises a false `NEW GATE` — but `False` would also CLEAR a
        # session that was ALREADY gated, which is the 2026-10-04 defect. See
        # `test_a_gated_worker_that_starts_a_new_turn_stays_gated`.
        gated, reason = watch.is_gated("busy", CLOSER)
        self.assertIsNone(gated)
        self.assertEqual(reason, "held:busy")

    # --- The third limb: a pending modal on a frozen transcript ---------------
    #
    # Measured 2026-10-05 (task: "A Tab Worker Parked on AskUserQuestion Reads
    # busy in the Session Registry"). A CLEAN park reads `waiting` and the first
    # half catches it; a park with prose alongside the `AskUserQuestion` tool_use
    # reads `busy` AND has its closer displaced by that prose, so BOTH existing
    # halves miss. Neither half is repairable alone — the registry cannot see the
    # displaced closer, and the transcript cannot see "is it moving".
    # `CLAUDE.md` § Reading a worker states the pair exactly: "the transcript
    # answers *which call*, the registry answers *is it moving*".

    def test_busy_parked_on_a_modal_is_gated(self):
        gated, reason = watch.is_gated("busy", PROSE,
                                       pending="AskUserQuestion", frozen=True)
        self.assertIs(gated, True)
        self.assertEqual(reason, "busy+parked-modal")

    def test_busy_on_a_fresh_modal_is_held_not_gated(self):
        # An unmatched `tool_use` is not by itself proof of a park: between the
        # record landing and the modal rendering there is a window where the call
        # is pending and nothing is frozen. The freeze is what rules it out — and
        # the answer is HELD (`None`), never a clear, because a `busy` row is
        # mid-turn and nothing has been answered.
        self.assertIsNone(
            watch.is_gated("busy", PROSE, pending="AskUserQuestion",
                           frozen=False)[0])

    def test_busy_frozen_on_a_non_modal_tool_is_held_not_gated(self):
        # A long `Bash` call is frozen too. Only a modal is definitionally a wait
        # on the operator, so the call NAME is load-bearing and the freeze is not
        # sufficient alone — this is the case `CLAUDE.md:66` measured across 25
        # live sessions, where a pending call read identically parked or not.
        # HELD, not cleared: this limb must not widen into "any frozen busy row".
        self.assertIsNone(
            watch.is_gated("busy", PROSE, pending="Bash", frozen=True)[0])

    def test_parked_modal_is_gated_even_with_no_readable_closer(self):
        # The limb does not read the closer at all, so an unreadable transcript
        # tail must not demote it to HELD — which is what the `_CLOSER_UNKNOWN`
        # branch below it would otherwise do.
        gated, reason = watch.is_gated(
            "busy", watch._CLOSER_UNKNOWN,
            pending="AskUserQuestion", frozen=True)
        self.assertIs(gated, True)
        self.assertEqual(reason, "busy+parked-modal")

    def test_shell_is_not_gated(self):
        # Same shape as `busy`: a status that is neither `waiting` nor `idle`
        # cannot justify a clear.
        gated, reason = watch.is_gated("shell", CLOSER)
        self.assertIsNone(gated)
        self.assertEqual(reason, "held:shell")

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

    def test_a_gated_worker_that_starts_a_new_turn_stays_gated(self):
        """THE DEFECT — measured 2026-10-04, reproduced against v0.109.0 2026-10-06.

        A tracked worker sat gated (`idle` + closer), then began new work without
        answering: ordinary prose displacing the closer, registry `busy`. The
        watcher emitted a bare `CLEARED` at the mid-turn point even though nothing
        had been answered and the worker had not finished with the operator.

        The laziest passing fix is a suppression list keyed to the measured
        session; this fixture carries its own id, so that cannot satisfy it.
        """
        self.fx.cleanup()
        self.fx = Fixture(
            [assistant(text=CLOSER_LINE), assistant(text=PROSE)], "busy")
        state = self.fx.probe()
        self.assertIn(SID8, state, "still tracked, so the caller can see it")
        self.assertIsNone(state[SID8][3], "mid-turn is HELD, never False")
        self.assertEqual(state[SID8][2], "held:busy")
        self.assertNotIn(SID8, watch.gated_keys(state))
        # The diff over the gated set emits neither a clear nor a re-raise.
        got = watch.transitions([SID8], watch.gated_keys(state), state)
        self.assertEqual([k for k, *_ in got], [],
                         "a mid-turn session keeps its membership")

    def test_genuine_answer_clears_only_once_the_worker_settles(self):
        # SC2's shape. A worker that resumed and is WORKING is not a gate — but it
        # is HELD, not cleared, because `busy` is also exactly what a worker that
        # moved on WITHOUT answering reads. The clear lands when it settles at
        # `idle` with no live ask, the only state that justifies one.
        self.fx.cleanup()
        self.fx = Fixture([assistant(text=CLOSER_LINE), assistant(text=PROSE)], "busy")
        self.assertNotIn(SID8, watch.gated_keys(self.fx.probe()))
        self.fx.cleanup()
        self.fx = Fixture(
            [assistant(text=CLOSER_LINE), assistant(text=NOTHING_LINE)], "idle")
        state = self.fx.probe()
        self.assertEqual(state[SID8][3], False, "settled idle + no ask")
        self.assertEqual(state[SID8][2], "idle:no-ask")
        self.assertEqual([k for k, *_ in watch.transitions([SID8], [], state)],
                         ["CLEARED"], "the answer's one clear")

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
    and every one of those sessions lands in `prevset - set(key)`.

    ⚠️ **Two independent fixes cover it, and the second is the wider one.**
    `state_path_for()` keys the file by tracked set, so the shared file no longer
    arises; and `transitions()` HELDs an id absent from `state` rather than clearing
    it — the guard that makes the *class* unreachable, since a vanished transcript or
    a changed `claude_session_id` reaches the same branch with no shared file
    anywhere. The round-4 review is why the second exists.
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

    def test_shared_state_no_longer_manufactures_a_bare_cleared(self):
        # THE DEFECT ITSELF, reproduced rather than avoided — path inequality
        # alone would pin the fix by construction and never show the failure.
        # Manager B's poll reads manager A's gated set as its `prev`; A's session
        # is absent from B's `state`.
        #
        # ⚠️ The assertion is INVERTED from the version that first pinned this,
        # deliberately. It used to require the bare `CLEARED`, because that is what
        # the code did; the round-4 review showed the same branch is reachable with
        # no shared file at all, so `transitions()` now HELDs it. The case still
        # runs through the same call — it simply no longer clears.
        b_state = {"bbbb2222": ("B", "y", "idle+closer", True)}
        got = watch.transitions(["aaaa1111"], ["bbbb2222"], b_state)
        self.assertNotIn(("CLEARED", "aaaa1111", "", "left the gated set"), got,
                         "a bare CLEARED for a session this manager never saw")
        self.assertEqual([k for k, s, *_ in got if s == "aaaa1111"], ["HELD"],
                         "an id absent from state is HELD, never cleared")

    def test_an_id_absent_from_state_is_held_not_cleared(self):
        # The same branch, with no shared state file involved: the watcher lost
        # sight of a session it had been holding. `probe()` reaches this when a
        # transcript vanishes or the id leaves `tracked_ids()`.
        got = watch.transitions(["aaaa1111"], [], {})
        self.assertEqual([k for k, *_ in got], ["HELD"],
                         "losing sight of a session is not an answer")
        self.assertIn("no longer resolved", got[0][3])

    def test_unregistered_is_held_never_cleared(self):
        state = {"aaaa1111": ("A", "x", "unregistered", None)}
        self.assertEqual([k for k, *_ in watch.transitions(["aaaa1111"], [], state)],
                         ["HELD"])

    def test_genuine_answer_still_clears(self):
        # The verdict a genuine answer now produces is the settle at `idle` with
        # no ask — NOT `registry:busy`, which is HELD. See `is_gated`.
        state = {"aaaa1111": ("A", "x", "idle:no-ask", False)}
        self.assertEqual([k for k, *_ in watch.transitions(["aaaa1111"], [], state)],
                         ["CLEARED"])

    def test_a_mid_turn_session_keeps_its_membership(self):
        # HELD-mid-turn must NOT drop the membership the way UNREGISTERED does:
        # dropping it would forget the gate, and the answer that eventually
        # arrives would then clear silently — no `CLEARED` at all.
        state = {"aaaa1111": ("A", "held:busy", "held:busy", None)}
        self.assertEqual(watch.transitions(["aaaa1111"], [], state), [],
                         "neither CLEARED nor a re-raise")

    def test_new_gate_only_for_newly_gated(self):
        state = {"aaaa1111": ("A", "x", "idle+closer", True),
                 "bbbb2222": ("B", "y", "idle+closer", True)}
        got = watch.transitions(["aaaa1111"], ["aaaa1111", "bbbb2222"], state)
        self.assertEqual([(k, s) for k, s, *_ in got], [("NEW GATE", "bbbb2222")],
                         "an unchanged gate must not be re-announced")


class CloserVerbTest(unittest.TestCase):
    """`is_ask` must read the VERB, not its padding or its kind.

    Both inputs here are copied from `who-needs-me.py`, which fixed them first and
    measured both. Getting them wrong in this file has the mirror cost to the row's
    defect: a session that closed clean, or one deliberately parked on a future
    event, is held as gated for as long as the registry lists it `idle` — a gate
    stuck open for a worker that is not waiting on anyone.
    """

    def test_entity_padded_nothing_is_not_an_ask(self):
        # `&nbsp;` is six literal characters, not whitespace, so a raw
        # `startswith("nothing")` misses it.
        self.assertFalse(watch.is_ask("&nbsp;&nbsp;&nbsp;&nbsp;nothing"))
        self.assertFalse(watch.is_ask("&#160; nothing"))
        self.assertFalse(watch.is_ask("&#xa0;nothing"))

    def test_plain_nothing_is_not_an_ask(self):
        self.assertFalse(watch.is_ask("nothing"))
        self.assertFalse(watch.is_ask(NOTHING))

    def test_parked_wait_is_not_an_ask(self):
        # `later (on <trigger>):` names the event that resumes the work; until it
        # fires there is nothing for the operator to answer.
        self.assertFalse(watch.is_ask("later (on CI green): merge #130"))

    def test_a_real_ask_is_still_an_ask(self):
        self.assertTrue(watch.is_ask(CLOSER))
        self.assertTrue(watch.is_ask("approve: /vault-cli:session-close"))

    def test_entity_padded_nothing_does_not_hold_a_session(self):
        # The end-to-end consequence, not just the predicate: `idle` plus a padded
        # `nothing` must read NOT gated, or the gate never clears. `idle:no-ask` is
        # the one verdict that justifies a `CLEARED`.
        verdict, reason = watch.is_gated("idle", "&nbsp;&nbsp;&nbsp;&nbsp;nothing")
        self.assertIs(verdict, False, "a clean close must not be held as gated")
        self.assertEqual(reason, "idle:no-ask")


class UnknownCloserTest(unittest.TestCase):
    """An unread closer is unknown, never absent.

    The transcript tail is a fixed 400 KB window. A transcript that appended more
    than that in `tool_result` records after its closer pushes the closer out of
    reach, and reading "no text found" as "no closer" would clear a worker that is
    still parked — the exact false-`CLEARED` direction this file exists to close.
    """

    def test_closer_unknown_is_held_never_cleared(self):
        verdict, reason = watch.is_gated("idle", watch._CLOSER_UNKNOWN)
        self.assertIsNone(verdict, "an unread closer is held, never cleared")
        self.assertEqual(reason, "closer-unknown")

    def test_waiting_still_wins_over_an_unknown_closer(self):
        # A worker parked on a harness gate is gated on the registry alone, so an
        # unreadable transcript must not downgrade it.
        verdict, _ = watch.is_gated("waiting", watch._CLOSER_UNKNOWN)
        self.assertTrue(verdict)

    def test_last_assistant_text_returns_none_when_no_text_is_found(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(assistant(tool="Bash")) + "\n")
            self.assertIsNone(
                watch.last_assistant_text(p),
                "no text found must be distinguishable from no closer")

    def test_probe_holds_a_session_whose_tail_holds_no_text(self):
        fx = Fixture([assistant(tool="Bash")], "idle")
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                state = watch.probe(fx.tracked, fx.tasks, fx.projects,
                                    fx.sessions)
            self.assertIsNone(state[SID8][3], "an unread closer is HELD")
            self.assertEqual(state[SID8][2], "closer-unknown")
            self.assertIn("closer unknown", err.getvalue(),
                          "a degraded transcript read must not be silent")
        finally:
            shutil.rmtree(fx.dir, ignore_errors=True)

    def test_is_hold_admits_both_holds_and_refuses_unregistered(self):
        # The membership-preserving set, pinned as a table. `unregistered` is the
        # one that must NOT be here: it is also a `None` verdict, and collapsing
        # it into the preserving set would swallow its HELD record.
        for reason in ("held:busy", "held:shell", "closer-unknown"):
            self.assertTrue(watch.is_hold(reason), reason)
        for reason in ("unregistered", "idle:no-ask", "registry:waiting"):
            self.assertFalse(watch.is_hold(reason), reason)

    def test_a_closer_unknown_session_keeps_its_membership(self):
        """THE DEFECT, at `transitions()`.

        A failed read is not an answer, so it must not take the UNREGISTERED
        branch. Dropping the membership here forgets the gate, and the genuine
        answer that arrives later is then not a transition at all — it emits no
        `CLEARED`, replacing a false clear with no clear.
        """
        state = {SID8: ("A", "closer-unknown", "closer-unknown", None)}
        self.assertEqual(watch.transitions([SID8], [], state), [],
                         "a failed read keeps the membership; no record")

    def test_the_unknown_closer_pair_clears_once_the_worker_settles(self):
        """SC2's positive half: the membership kept above still earns its one
        `CLEARED` when the worker genuinely settles at `idle` with no ask."""
        unknown = {SID8: ("A", "closer-unknown", "closer-unknown", None)}
        settled = {SID8: ("A", "idle:no-ask", "idle:no-ask", False)}
        self.assertEqual(watch.transitions([SID8], [], unknown), [])
        self.assertEqual(
            [k for k, *_ in watch.transitions([SID8], [], settled)],
            ["CLEARED"], "the settle after a failed read must still clear")

    def test_probe_keeps_the_membership_of_a_session_whose_tail_holds_no_text(self):
        """End to end from the real probe: a tail with no assistant text leaves
        the session HELD, and HELD keeps its membership."""
        fx = Fixture([assistant(tool="Bash")], "idle")
        try:
            with redirect_stderr(io.StringIO()):
                state = fx.probe()
            self.assertIsNone(state[SID8][3], "the verdict is HELD")
            self.assertEqual(
                watch.transitions([SID8], watch.gated_keys(state), state), [],
                "closer-unknown keeps its membership — no HELD record")
        finally:
            shutil.rmtree(fx.dir, ignore_errors=True)


class StabilityGateTest(unittest.TestCase):
    """The gate is per SESSION, never per key-set.

    A whole-set test (`key == pending`) withholds EVERY session's transition for
    as long as ANY one of them churns: a set alternating `{A}` / `{A,B}` never
    equals its own previous value, so `prev` never advances and A's own `CLEARED`
    is never emitted. That is this file's own defect direction — a gate stuck
    open — reached through a peer's churn instead of the session's own. Found in
    review 2026-10-04.
    """

    def test_a_peers_churn_does_not_mask_another_sessions_stability(self):
        stable = watch.stable_sessions({"aaaa1111"},
                                       ["aaaa1111", "bbbb2222"],
                                       ["aaaa1111"])
        self.assertIn("aaaa1111", stable,
                      "A held its membership across both polls")
        self.assertNotIn("bbbb2222", stable,
                         "B genuinely churned and must still be debounced")

    def test_a_newly_gated_session_waits_one_poll(self):
        # The debounce itself: in `key` but not in `pending` is not yet stable,
        # so a single-poll flicker is never announced.
        self.assertEqual(watch.stable_sessions(set(), [], ["aaaa1111"]), set())

    def test_a_settled_gate_is_stable(self):
        self.assertEqual(
            watch.stable_sessions(set(), ["aaaa1111"], ["aaaa1111"]),
            {"aaaa1111"})

    def test_a_departure_needs_two_polls(self):
        # Gone from `key` but still in `pending` — not yet stable.
        self.assertNotIn("aaaa1111",
                         watch.stable_sessions({"aaaa1111"}, ["aaaa1111"], []))
        # Absent from both, still in `prev` — stable, and it owes a transition.
        self.assertIn("aaaa1111", watch.stable_sessions({"aaaa1111"}, [], []))


class CommitLoopTest(unittest.TestCase):
    """The commit loop, driven for real — `--once` returns before it.

    Everything this watcher guarantees about `CLEARED` honesty is decided inside
    this loop: the stability gate, the HELD-logged-not-printed branch, and the
    atomic state write. `--once` returns at the top of the loop, so before this
    test the tested units were the honest parts and the untested one was where
    dishonesty is decided. Found in review 2026-10-04.
    """

    GATED = {"aaaa1111": ("A", "pick — 1. alpha", "idle+closer", True)}

    def _drive(self, scripted):
        """Run main() over a scripted sequence of probe() results."""
        seq = list(scripted)
        out, err = io.StringIO(), io.StringIO()

        def fake_probe(*a, **kw):
            return seq.pop(0) if seq else {}

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(watch, "probe", fake_probe), \
                 mock.patch.object(watch.time, "sleep", lambda *_: None), \
                 redirect_stdout(out), redirect_stderr(err):
                watch.main(["--tracked", os.path.join(d, "t.txt"),
                            "--tasks-dir", d,
                            "--state", os.path.join(d, "state"),
                            "--max-polls", str(len(scripted))])
        return out.getvalue()

    def test_a_gate_is_announced_once_and_cleared_once(self):
        clear = {"aaaa1111": ("A", "idle:no-ask", "idle:no-ask", False)}
        out = self._drive([{}, self.GATED, self.GATED, clear, clear])
        self.assertEqual(out.count("NEW GATE"), 1, out)
        self.assertEqual(out.count("CLEARED"), 1, out)
        self.assertIn("CLEARED  aaaa1111", out)

    def test_a_gated_worker_that_starts_a_new_turn_emits_no_cleared(self):
        """THE DEFECT, at the commit loop — where CLEARED honesty is decided.

        The predicate test pins the verdict; this one pins that the loop turns
        neither that verdict nor a dropped membership into a printed `CLEARED`.
        """
        busy = {"aaaa1111": ("A", "held:busy", "held:busy", None)}
        out = self._drive([{}, self.GATED, self.GATED, busy, busy])
        self.assertEqual(out.count("NEW GATE"), 1, out)
        self.assertNotIn("CLEARED", out, "starting a new turn is not an answer")

    def test_the_clear_lands_when_the_worker_settles(self):
        """The pair that makes the hold meaningful: SC2's one clear still lands."""
        busy = {"aaaa1111": ("A", "held:busy", "held:busy", None)}
        settled = {"aaaa1111": ("A", "idle:no-ask", "idle:no-ask", False)}
        out = self._drive([{}, self.GATED, self.GATED, busy, busy, settled, settled])
        self.assertEqual(out.count("NEW GATE"), 1, out)
        self.assertEqual(out.count("CLEARED"), 1, out)

    def test_an_unregistered_session_is_held_and_never_printed(self):
        held = {"aaaa1111": ("A", "unregistered", "unregistered", None)}
        out = self._drive([{}, self.GATED, self.GATED, held, held])
        self.assertEqual(out.count("NEW GATE"), 1, out)
        self.assertNotIn("CLEARED", out,
                         "an unregistered session is HELD, never cleared")

    def test_a_closer_unknown_read_does_not_drop_the_gate(self):
        """THE DEFECT, at the commit loop — where the lost `CLEARED` is decided.

        The predicate test pins the membership; this pins the consequence: a
        failed closer read must not cost the worker the one `CLEARED` its later
        settle earns. Before the fix this emitted zero — the gate was dropped on
        the unknown read, so the settle was not a transition at all.
        """
        unknown = {"aaaa1111": ("A", "closer-unknown", "closer-unknown", None)}
        settled = {"aaaa1111": ("A", "idle:no-ask", "idle:no-ask", False)}
        out = self._drive([{}, self.GATED, self.GATED, unknown, unknown,
                           settled, settled])
        self.assertEqual(out.count("NEW GATE"), 1, out)
        self.assertEqual(out.count("CLEARED"), 1,
                         "the settle after a failed read must still clear once: "
                         + out)

    def test_a_churning_set_does_not_withhold_a_settled_clear(self):
        # A is gated throughout; B flips on every poll. Under the whole-set gate
        # this sequence emits nothing at all, including A's own CLEARED.
        b_on = {"bbbb2222": ("B", "pick — 1. alpha", "idle+closer", True)}
        a_clear = {"aaaa1111": ("A", "idle:no-ask", "idle:no-ask", False)}
        out = self._drive([
            {}, self.GATED,
            dict(self.GATED, **b_on),
            self.GATED,
            dict(a_clear, **b_on),
            a_clear,
            a_clear,
        ])
        self.assertIn("CLEARED  aaaa1111", out,
                      "a peer's churn must not withhold A's clear")


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


class ParkedModalProbeTest(unittest.TestCase):
    """The third limb end-to-end, through `probe()`.

    The predicate tests above pin the branch; these pin that `probe()` actually
    FEEDS it. A pending-call read or an mtime read that never reaches `is_gated`
    would leave every predicate test above green and the defect wide open, so the
    two halves are pinned separately on purpose.
    """

    def _fixture(self, tool, status, age_seconds):
        fx = Fixture([assistant(text=CLOSER_LINE),
                      assistant(text=PROSE, tool=tool)], status)
        past = time.time() - age_seconds
        os.utime(fx.transcript, (past, past))
        return fx

    def test_parked_modal_is_gated_through_probe(self):
        fx = self._fixture("AskUserQuestion", "busy", watch.LIVE_WINDOW + 60)
        try:
            self.assertIn(SID8, watch.gated_keys(fx.probe()))
        finally:
            fx.cleanup()

    def test_fresh_modal_is_not_gated_through_probe(self):
        fx = self._fixture("AskUserQuestion", "busy", 5)
        try:
            self.assertNotIn(SID8, watch.gated_keys(fx.probe()))
        finally:
            fx.cleanup()

    def test_frozen_long_tool_is_not_gated_through_probe(self):
        fx = self._fixture("Bash", "busy", watch.LIVE_WINDOW + 60)
        try:
            self.assertNotIn(SID8, watch.gated_keys(fx.probe()))
        finally:
            fx.cleanup()

    def test_answered_modal_is_not_gated_through_probe(self):
        # The worker ANSWERED and moved on: its `tool_result` is in the
        # transcript, so nothing is pending even though the file is old. Without
        # this the limb would fire on every long-lived session that ever asked a
        # question — the false-positive direction, which is the louder failure.
        fx = Fixture([assistant(text=CLOSER_LINE),
                      assistant(text=PROSE, tool="AskUserQuestion")], "busy")
        try:
            with open(fx.transcript, "a") as fh:
                fh.write(json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": "answered"}]}}) + "\n")
            past = time.time() - (watch.LIVE_WINDOW + 60)
            os.utime(fx.transcript, (past, past))
            self.assertNotIn(SID8, watch.gated_keys(fx.probe()))
        finally:
            fx.cleanup()


class LivenessRecordTest(unittest.TestCase):
    """A poll that finds nothing still says so.

    The defect this closes is not a wrong event but a MISSING one. Measured
    2026-10-06: an arm emitted nothing for 82 minutes, and the filing could not
    separate an inert arm from an over-suppressing gate from a correctly quiet
    one. It was the third — `busy`/`shell` are HELD, so no `CLEARED` was owed,
    and the one transition that WAS owed was emitted on time. The commit path was
    never wrong. But with no per-poll line, the log's newest record was the last
    TRANSITION, and its age was unreadable as anything but a fault.

    So the assertion is the honest one: **N quiet polls produce N records**, which
    is what makes "alive and quiet" a readable state instead of an inference.
    """

    GATED = {"aaaa1111": ("A", "pick — 1. alpha", "idle+closer", True)}

    def _drive(self, scripted):
        """Run main() over a scripted probe sequence; return events.jsonl records."""
        seq = list(scripted)
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            with mock.patch.object(watch, "probe",
                                   lambda *a, **kw: seq.pop(0) if seq else {}), \
                 mock.patch.object(watch.time, "sleep", lambda *_: None), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                watch.main(["--tracked", os.path.join(d, "my-topic.tracked.txt"),
                            "--tasks-dir", d,
                            "--state", state,
                            "--max-polls", str(len(scripted))])
            with open(os.path.join(state, "events.jsonl"), encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]

    def test_every_quiet_poll_still_writes_a_liveness_record(self):
        """The whole point: three quiet polls are three lines, not silence."""
        recs = self._drive([{}, {}, {}])
        live = [r for r in recs if r["kind"] == "LIVENESS"]
        self.assertEqual(len(live), 3, recs)
        self.assertTrue(all(r["detail"].endswith("· no change") for r in live), live)

    def test_the_liveness_record_names_the_tracked_set(self):
        """Two managers sharing a --state must still write attributable lines."""
        recs = self._drive([{}])
        self.assertEqual(recs[0]["task"], "my-topic", recs[0])

    def test_a_committing_poll_says_change(self):
        """The pair that makes the quiet line meaningful — a change is not
        reported as `no change`, so the field discriminates rather than
        decorating every record identically."""
        recs = self._drive([{}, self.GATED, self.GATED])
        live = [r["detail"] for r in recs if r["kind"] == "LIVENESS"]
        self.assertTrue(any(d.endswith("· change") for d in live), live)
        self.assertTrue(any(d.endswith("· no change") for d in live), live)

    def test_a_held_session_is_counted_not_hidden(self):
        """A HELD session is exactly what makes a quiet poll CORRECT rather than
        suspicious, so it is reported rather than folded into the gated count."""
        busy = {"aaaa1111": ("A", "held:busy", "held:busy", None)}
        recs = self._drive([busy])
        self.assertIn("0 gated · 1 held", recs[0]["detail"], recs[0])

    def test_a_quiet_poll_writes_nothing_to_stdout(self):
        """The load-bearing design claim, pinned.

        Every stdout line is a `Monitor` notification and therefore a full model
        turn, so moving the liveness record to stdout would cost ~30 turns per
        30-minute arm — the exact cost this change exists to avoid. `_drive`
        redirects stdout to a throwaway `StringIO` and asserts nothing about it,
        so without this a refactor could make that move with the suite still
        green.
        """
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(watch, "probe", lambda *a, **kw: {}), \
                 mock.patch.object(watch.time, "sleep", lambda *_: None), \
                 redirect_stdout(out), redirect_stderr(io.StringIO()):
                watch.main(["--tracked", os.path.join(d, "my-topic.tracked.txt"),
                            "--tasks-dir", d,
                            "--state", os.path.join(d, "state"),
                            "--max-polls", "3"])
        self.assertEqual(out.getvalue(), "", "a quiet poll must not wake the model")

    def test_a_failing_poll_still_writes_a_record(self):
        """The path where silence is MOST misleading.

        An arm that raises on every poll writes no record at all — the same blank
        surface the liveness record exists to remove, on a process that is
        demonstrably alive. stderr is not durable for a `Monitor`-captured arm, so
        without this the failure survives only in a stream nobody persists.
        """
        def boom(*a, **kw):
            raise RuntimeError("probe exploded")

        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state")
            with mock.patch.object(watch, "probe", boom), \
                 mock.patch.object(watch.time, "sleep", lambda *_: None), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                watch.main(["--tracked", os.path.join(d, "my-topic.tracked.txt"),
                            "--tasks-dir", d, "--state", state, "--max-polls", "2"])
            with open(os.path.join(state, "events.jsonl"), encoding="utf-8") as fh:
                recs = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual([r["kind"] for r in recs], ["LIVENESS", "LIVENESS"], recs)
        self.assertTrue(
            all("poll failed: RuntimeError" in r["detail"] for r in recs), recs)


class ParkAgeTest(unittest.TestCase):
    """The park-age clock — continuous park per REGISTRY STATUS, once each.

    ⚠️ The keyed-on-status half is the load-bearing one. A clock keyed on the
    watcher's gated SET would run once from the first park and never restart,
    because `is_gated` holds a session's membership through a mid-turn `busy` by
    design — so the SECOND park would never surface. Measured 2026-10-07: two
    tracked sessions parked on fresh `AskUserQuestion`s inside a 30-minute window
    and the arm delivered **0 events**, because neither park was a set transition.
    """

    T0 = 1_700_000_000.0
    SID8 = "aaaa1111"

    def test_aged_fires_once_at_each_threshold(self):
        ages = {}
        watch.advance_park_ages(ages, [self.SID8], self.T0)
        self.assertEqual(watch.aged_events(ages, self.T0), [])
        self.assertEqual(watch.aged_events(ages, self.T0 + 15 * 60),
                         [(self.SID8, 15)])
        self.assertEqual(watch.aged_events(ages, self.T0 + 16 * 60), [],
                         "the 15-minute mark fires ONCE, not every poll after it")
        self.assertEqual(watch.aged_events(ages, self.T0 + 60 * 60),
                         [(self.SID8, 60)])
        self.assertEqual(watch.aged_events(ages, self.T0 + 61 * 60), [])

    def test_no_aged_for_a_park_that_clears_before_fifteen(self):
        ages = {}
        watch.advance_park_ages(ages, [self.SID8], self.T0)
        watch.advance_park_ages(ages, [], self.T0 + 14 * 60)
        self.assertEqual(watch.aged_events(ages, self.T0 + 14 * 60), [])
        self.assertEqual(ages, {}, "a cleared park is dropped, not merely aged")

    def test_a_continuing_park_keeps_its_start(self):
        ages = {}
        watch.advance_park_ages(ages, [self.SID8], self.T0)
        watch.advance_park_ages(ages, [self.SID8], self.T0 + 60)
        self.assertEqual(ages[self.SID8]["since"], self.T0,
                         "a continuing park keeps its start, it does not restart")

    def test_a_busy_interval_starts_a_new_park(self):
        """THE SECOND-PARK CASE, and the reason the clock is not keyed on the set."""
        ages = {}
        watch.advance_park_ages(ages, [self.SID8], self.T0)
        # Mid-turn: not parked. ⚠️ The gated SET still holds this session — that
        # is the whole difference between the two keys.
        watch.advance_park_ages(ages, [], self.T0 + 5 * 60)
        # The worker asks a NEW question ten minutes after the first park began.
        watch.advance_park_ages(ages, [self.SID8], self.T0 + 10 * 60)
        self.assertEqual(watch.aged_events(ages, self.T0 + 15 * 60), [],
                         "the second park is 5 minutes old, not 15 — a clock keyed "
                         "on set membership would already have fired and would "
                         "then stay silent for this park forever")
        self.assertEqual(watch.aged_events(ages, self.T0 + 25 * 60),
                         [(self.SID8, 15)],
                         "the second park must surface its own escalation")


class AgedEmissionTest(unittest.TestCase):
    """`AGED` as the manager actually sees it — a STDOUT line, once each.

    ⚠️ Unlike the `LIVENESS` record, `AGED` must go to stdout. Every stdout line
    is a `Monitor` notification and therefore a full model turn — which is why the
    liveness record is `events.jsonl`-only — but `AGED` is the opposite case: the
    manager's instruction is to VOICE it, and a line written only to the log is
    durable and useless, because nothing is woken by it.
    """

    PARKED = {"aaaa1111": ("A", "pick — 1. alpha", "registry:waiting", True)}

    def _drive(self, scripted, clock):
        seq, ticks = list(scripted), list(clock)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(watch, "probe",
                                   lambda *a, **kw: seq.pop(0) if seq else {}), \
                 mock.patch.object(watch, "repost_empty_parks",
                                   lambda *a, **kw: []), \
                 mock.patch.object(watch.time, "sleep", lambda *_: None), \
                 mock.patch.object(watch.time, "time", lambda: ticks.pop(0)), \
                 redirect_stdout(out), redirect_stderr(io.StringIO()):
                watch.main(["--tracked", os.path.join(d, "t.txt"),
                            "--tasks-dir", d,
                            "--state", os.path.join(d, "state"),
                            "--max-polls", str(len(scripted))])
        return out.getvalue()

    def test_the_aged_line_is_printed_once_at_fifteen_minutes(self):
        t0 = 1_700_000_000.0
        out = self._drive([self.PARKED] * 4,
                          [t0, t0, t0 + 16 * 60, t0 + 17 * 60])
        self.assertEqual(out.count("AGED"), 1, out)
        self.assertIn("AGED  aaaa1111  15m", out,
                      "two spaces after the kind, matching NEW GATE — " + out)


class RepostTest(unittest.TestCase):
    """The re-post — a parked session the store no longer holds gets a card back.

    ⚠️ The producer is the WORKER, not the manager. A store item's session is its
    `producer_id` (`who-needs-me.py:268`), so posting as the worker is what makes
    the re-posted row attribute to the session that is actually parked — and what
    routes the operator's answer back to it.
    """

    SID = "aaaa1111-0000-0000-0000-000000000000"
    SID8 = "aaaa1111"
    ENTRIES = {SID8: ("A", SID)}

    def _run(self, open_items, parked=(SID8,)):
        posted = []
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(watch, "open_store_items",
                                   lambda *a, **kw: open_items):
                watch.repost_empty_parks(
                    list(parked), self.ENTRIES,
                    os.path.join(d, "events.jsonl"),
                    post=lambda sid8, full, label: (
                        posted.append((sid8, full)) or True))
        return posted

    def test_a_park_the_store_lost_is_re_posted(self):
        self.assertEqual(self._run((set(), set())), [(self.SID8, self.SID)])

    def test_an_open_item_for_the_worker_suppresses_the_re_post(self):
        self.assertEqual(self._run(({self.SID}, set())), [],
                         "the worker's own card is already open")

    def test_our_own_re_post_suppresses_a_second_one(self):
        self.assertEqual(self._run((set(), {"park:" + self.SID8})), [],
                         "the stable dedup key is what makes this idempotent")

    def test_a_failed_store_read_re_posts_nothing(self):
        """The direction that matters: a dead store is not 'the store holds nothing'."""
        self.assertEqual(self._run(None), [])

    def test_nothing_parked_does_not_touch_the_store(self):
        with mock.patch.object(
                watch, "open_store_items",
                side_effect=AssertionError("store read on an idle poll")):
            self.assertEqual(
                watch.repost_empty_parks([], self.ENTRIES, "/dev/null"), [])

    def test_a_refused_post_is_not_recorded_as_one(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(watch, "open_store_items",
                                   lambda *a, **kw: (set(), set())):
                out = watch.repost_empty_parks(
                    [self.SID8], self.ENTRIES, os.path.join(d, "events.jsonl"),
                    post=lambda *a: False)
        self.assertEqual(out, [], "a refused post is not a re-post")


    def test_a_headless_park_is_parked_too(self):
        """⚠️ The one park the registry cannot reach, and therefore the one this
        arm must not skip. A headless worker is an in-process SDK `query()` with
        no pid, so `registry_status` returns `None` and `is_gated` decides on the
        `headless_live` branch — and `who-needs-me.py:822` records a measured case
        of that class rendering no row in ANY feed section."""
        state = {"aaaa1111": ("A", "pick — 1. alpha", "headless+closer", True)}
        self.assertEqual(watch.parked_keys(state), ["aaaa1111"])

    def test_a_settled_idle_park_is_not_parked(self):
        """The deliberate exclusion, pinned so a later widening is a decision."""
        state = {"aaaa1111": ("A", "pick — 1. alpha", "idle+closer", True)}
        self.assertEqual(watch.parked_keys(state), [])


class PostRepostArgvTest(unittest.TestCase):
    """The re-post's command line, pinned.

    ⚠️ **Every other re-post test injects the `post` seam**, so none of them
    exercises the argv `post_repost` actually builds — a mistyped flag, a wrong
    dedup-key form, or an accidentally-added `--liveness-ref` would pass green.
    That last one is the dangerous one: `attention-ask.py:506-532` records that a
    `session:` liveness subject makes the store **prune** the card on the first
    read after the poster exits, which is the silent disappearance this whole
    change exists to prevent. So the assertion is on the LIST, never on a
    rendering of it.
    """

    FULL = "aaaa1111-0000-0000-0000-000000000000"

    def _run(self, result):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return result

        with mock.patch.object(watch.subprocess, "run", fake_run):
            ok = watch.post_repost("aaaa1111", self.FULL, "Some Task")
        return ok, captured.get("cmd")

    def test_the_argv_pins_producer_dedup_key_and_no_liveness_ref(self):
        ok, cmd = self._run(mock.Mock(returncode=0, stdout="", stderr=""))
        self.assertTrue(ok)
        self.assertTrue(cmd[1].endswith("attention-ask.py"), cmd)
        self.assertIn("post", cmd)
        self.assertEqual(cmd[cmd.index("--producer-id") + 1], self.FULL,
                         "the card must be attributed to the WORKER, not the "
                         "manager — that is what routes the answer back to the "
                         "parked session")
        self.assertEqual(cmd[cmd.index("--dedup-key") + 1], "park:aaaa1111",
                         "the stable key is what makes the re-post idempotent")
        self.assertNotIn("--liveness-ref", cmd,
                         "a session: liveness subject makes the store PRUNE the "
                         "card on the first read after the poster exits")

    def test_a_non_zero_exit_is_not_a_re_post(self):
        ok, _ = self._run(mock.Mock(returncode=2, stdout="REFUSED: nope",
                                    stderr=""))
        self.assertFalse(ok)


class IncidentReplayTest(unittest.TestCase):
    """SC4 — the 2026-10-07 incident, replayed to a catch.

    Session `15fce333` raised an `AskUserQuestion` at 12:28:45 and the registry
    read `waiting` from then on; the store held its item and then dropped it while
    the worker stayed parked. Nothing surfaced the park: the watcher's one
    `NEW GATE` had fired at 14:30:30, and it emits on a transition, never again on
    age. This drives the same shapes against the same clock and asserts both
    limbs fire — an `AGED 15m` by 12:44, and a re-post once the store loses the
    card.
    """

    SID = "15fce333-01cd-403e-82b8-5593f01cfb61"
    SID8 = "15fce333"
    TASK = "Widen the Dev mdm-contact-v1 Read Allowlist"

    @staticmethod
    def _at(hh, mm, ss=0):
        return time.mktime((2026, 10, 7, hh, mm, ss, 0, 0, -1))

    def test_the_incident_replays_to_a_re_post_and_an_aged_15m(self):
        parked = {self.SID8: (self.TASK, "pick — 1. alpha",
                              "registry:waiting", True)}
        clock = [self._at(12, 28, 45), self._at(12, 29), self._at(12, 43, 45),
                 self._at(12, 44), self._at(15, 29), self._at(15, 30)]
        # The store holds the worker's item for the first four polls, loses it on
        # the fifth, and holds OUR re-post from the sixth — which is what makes
        # the re-post idempotent rather than once-per-poll.
        items = [({self.SID}, set())] * 4 + [(set(), set()),
                                             (set(), {"park:" + self.SID8})]
        ticks, seq_items, posted = list(clock), list(items), []
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            tasks = os.path.join(d, "tasks")
            os.makedirs(tasks)
            with open(os.path.join(d, "t.txt"), "w") as fh:
                fh.write(self.TASK + "\n")
            with open(os.path.join(tasks, self.TASK + ".md"), "w") as fh:
                fh.write(f"---\nclaude_session_id: {self.SID}\n---\n")
            with mock.patch.object(watch, "probe",
                                   lambda *a, **kw: parked), \
                 mock.patch.object(watch, "open_store_items",
                                   lambda *a, **kw: seq_items.pop(0)), \
                 mock.patch.object(watch, "post_repost",
                                   lambda sid8, full, label, **kw: (
                                       posted.append((sid8, full)) or True)), \
                 mock.patch.object(watch.time, "sleep", lambda *_: None), \
                 mock.patch.object(watch.time, "time",
                                   lambda: ticks.pop(0)), \
                 redirect_stdout(out), redirect_stderr(io.StringIO()):
                watch.main(["--tracked", os.path.join(d, "t.txt"),
                            "--tasks-dir", tasks,
                            "--state", os.path.join(d, "state"),
                            "--max-polls", str(len(clock))])
        self.assertIn(f"AGED  {self.SID8}  15m", out.getvalue(),
                      "the park must be escalated by 12:44: " + out.getvalue())
        self.assertEqual(posted, [(self.SID8, self.SID)],
                         "the store losing the card must produce exactly one "
                         "re-post, not one per poll")


if __name__ == "__main__":
    unittest.main()
