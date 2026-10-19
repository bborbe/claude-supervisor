#!/usr/bin/env python3
"""Tests for scripts/who-needs-my-help.py.

The sweep is one pass over every live session that classifies each park on TWO
axes and hands the operator-bound rows over. Every claim below is asserted
against a synthetic fleet, so the suite never reads a live WezTerm, a live
registry or a live attention store — `build()` is pure over its inputs and
`render()` is pure over `build()`'s output, which is what makes that possible.

What is pinned here, and why each one can actually fail:

  * **Two-channel liveness** — the same pane text with two different registry
    records must classify differently. One fixture cannot falsify a constant.
  * **The spoken count** — pinned by TWO fixtures with different totals, plus a
    fixture carrying a class-A and a class-B row that must NOT move it.
  * **The gate classes** — one item per class A-E plus a machine-bound wait, each
    asserted against the class its fixture declares.
  * **The negative probe** — across the whole fixture fleet, the render issues no
    `send-text`, no `send-keys`, no answer CLI and no subprocess of any kind.
  * **Recurrence** — distinct calendar days, not runs; and a class with no
    mechanism is omitted rather than listed without one.
  * **Ordering** — prod outranks closeness, proven by a prod item that is LESS
    close than a non-prod one.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import start_cache_isolation  # noqa: E402,F401  (side effect: isolate the start cache)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_ROOT, "who-needs-my-help.py")
_REPO = os.path.dirname(_ROOT)
_COMMAND = os.path.join(_REPO, "commands", "who-needs-my-help.md")

_spec = importlib.util.spec_from_file_location("who_needs_my_help", _SCRIPT)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)
wnm = h.wnm

EMPTY_STATE = {"version": 1, "days": {}, "counts": {}, "owners": {}}

# The fixture closers, one per gate class. Each is written to be reachable by
# exactly one rule in `gate_class()`, so a fixture landing in the wrong class
# names the rule that misfired rather than "something matched".
CLOSER_A = "pick — 1. spawn a ready-to-start worker for the remaining SC · 2. hold"
CLOSER_B = "pick — 1. re-apply the idempotent guard and measure its firing rate · 2. abandon"
CLOSER_C = "pick — 1. take the alpha approach to the schema split · 2. take beta"
CLOSER_D = "you run: `cd /tmp/w && make apply` — reply `applied`"
CLOSER_E = "approve: trade-close the funded account position — TDR field is open"
CLOSER_CLOCK = "later (on 2026-10-20): re-arm the watch and re-sweep\n⏰ Ends: the guard landing (watch, ~12:00)"


def _transcript(closer, task=None):
    """The assistant text a session's transcript ends on."""
    out = []
    if task:
        out.append(f"📌 Task: [T](obsidian://open?vault=private-personal&file={task})")
    if closer:
        out.append(f"👤 You: {closer}")
    return "\n".join(out) + ("\n" if out else "")


class Fleet:
    """A synthetic fleet: records, registry, pane map and task files on disk."""

    def __init__(self, root):
        self.root = root
        self.vault = os.path.join(root, "private-personal")
        os.makedirs(self.vault, exist_ok=True)
        self.records = []
        self.registry = {}
        self.status = {}
        # The registry's three-state identity verdict, kept beside `status`
        # because they answer different questions: `status` is what the pane
        # last looked like, `alive` is whether THIS session's process still
        # runs. A fixture that does not say otherwise is a live session.
        self.alive = {}
        self.shapes = {}
        self.posted = set()

    def task(self, rel, body):
        path = os.path.join(self.vault, rel + ".md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return rel

    def state(self, sid, subject, *, vault="private-personal", branch="goal"):
        """A manager's `worker-manager/<sid>.json` — its declared subject.

        Source 1 of `docs/subject-resolution.md`. Written by the manager itself
        when it resolves a goal or topic, which is why a manager session has one
        and a worker does not.
        """
        d = os.path.join(self.root, "worker-manager")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{sid}.json"), "w", encoding="utf-8") as f:
            json.dump({"subject": subject, "vault": vault, "branch": branch,
                       "resolved_at": "2026-10-15T00:00:00+02:00"}, f)
        return sid

    def add(self, sid, pane, closer, *, status="waiting", name=None, role=None,
            task=None, kind="question", state="open", ts=1000, event=None,
            transcript=None, alive=True):
        text = _transcript(closer, task) if transcript is None else transcript
        self.records.append({
            "session_id": sid, "pane": str(pane), "kind": kind,
            "detail": closer or "", "state": state, "ts": ts, "role": role,
            "item_id": f"item-{sid}", "session_name": name or "",
            "_transcript": text,
        })
        self.registry[sid] = name or ""
        self.status[sid] = status
        self.alive[sid] = alive
        return sid

    def pmap(self):
        return {str(r["pane"]): {"title": self.registry.get(r["session_id"], "")}
                for r in self.records}

    def reg_records(self):
        return {sid: {"status": st, "name": self.registry.get(sid, ""),
                      "alive": self.alive.get(sid, True)}
                for sid, st in self.status.items()}

    def sweep(self, causes=None):
        return h.build(self.pmap(), self.registry, self.reg_records(),
                       self.records, causes or dict(EMPTY_STATE),
                       shape_of=lambda p: self.shapes.get(str(p)),
                       link_of=lambda p: f"/supervisor:jump {p}")


class Base(unittest.TestCase):
    """Shared wiring: an isolated vault, a stubbed transcript, a recorded shell."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wnmh-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fleet = Fleet(self.root)
        self.calls = []

        def _run(argv, *a, **kw):
            self.calls.append(list(argv))
            raise AssertionError(f"unexpected subprocess: {argv}")

        patches = [
            mock.patch.object(wnm, "last_assistant_text",
                              side_effect=lambda rec: rec.get("_transcript", "")),
            mock.patch.object(wnm, "posted_closer",
                              side_effect=lambda sid: sid in self.fleet.posted),
            mock.patch.object(wnm, "OBSIDIAN", self.root),
            mock.patch.object(wnm, "STORE_OK", True),
            mock.patch.object(wnm, "SOURCE_NOTE", None),
            mock.patch.object(h, "PERSONAL_VAULT", self.fleet.vault),
            mock.patch.object(h, "WM_STATE_DIR",
                              os.path.join(self.root, "worker-manager")),
            mock.patch.object(h.subprocess, "run", side_effect=_run),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def rows(self, sweep):
        return {r["name"]: r for r in sweep["parked"]}


class GateClassTest(Base):
    """SC1 / SC5 — one item per class A-E plus a machine-bound wait."""

    def setUp(self):
        super().setUp()
        self.fleet.task("25 Tasks/A item", "# Tasks\n- [ ] hold\n")
        self.fleet.task("25 Tasks/B item",
                        "# Tasks\n- [ ] re-apply the idempotent guard and measure its firing rate\n")
        self.fleet.task("25 Tasks/C item", "# Tasks\n- [ ] something else entirely\n")
        self.fleet.task("25 Tasks/D item", "# Tasks\n- [ ] run the deploy\n")
        self.fleet.task("25 Tasks/E item", "# Tasks\n- [ ] ack the TDR\n")
        self.fleet.add("s-a", 101, CLOSER_A, name="Alpha Worker", task="25 Tasks/A item")
        self.fleet.add("s-b", 102, CLOSER_B, name="Bravo Worker", task="25 Tasks/B item")
        self.fleet.add("s-c", 103, CLOSER_C, name="Charlie Worker", task="25 Tasks/C item")
        self.fleet.add("s-d", 104, CLOSER_D, name="Delta Worker", task="25 Tasks/D item")
        self.fleet.add("s-e", 105, CLOSER_E, name="Echo Worker", task="25 Tasks/E item")
        self.fleet.add("s-k", 106, CLOSER_CLOCK, name="Kilo Worker", task="25 Tasks/C item")
        self.sweep = self.fleet.sweep()
        self.rows = self.rows(self.sweep)

    def test_every_item_lands_in_the_class_its_fixture_declares(self):
        expected = {"Alpha Worker": "A", "Bravo Worker": "B", "Charlie Worker": "C",
                    "Delta Worker": "D", "Echo Worker": "E", "Kilo Worker": None}
        actual = {name: r["gate"] for name, r in self.rows.items()}
        self.assertEqual(expected, actual)

    def test_a_machine_bound_wait_carries_no_gate_class_but_keeps_its_watcher(self):
        kilo = self.rows["Kilo Worker"]
        self.assertIsNone(kilo["gate"])
        self.assertEqual(wnm.CAUSE_CLOCK, kilo["cause"])
        self.assertEqual("2026-10-20", kilo["eta"])
        self.assertIn("the guard landing", kilo["watcher"])

    def test_every_parked_row_carries_a_cause_class(self):
        for name, r in self.rows.items():
            self.assertIn(r["cause"], wnm.CAUSE_CLASSES, name)

    def test_all_four_sections_render(self):
        out = h.render(self.sweep)
        for heading in ("Sweep (", "Handover", "Root cause", "Managers to query ("):
            self.assertIn(heading, out)


class TwoChannelTest(Base):
    """SC2 — the registry decides liveness; the pane text never does."""

    PANE_TEXT = "Enter to select · ↑/↓ to navigate · Esc to cancel"

    def test_same_pane_text_not_waiting_yields_no_gate(self):
        self.fleet.add("s1", 200, CLOSER_C, status="busy", name="Busy Worker")
        self.fleet.shapes["200"] = "question"
        sweep = self.fleet.sweep()
        self.assertEqual([], sweep["parked"])
        self.assertEqual(["Busy Worker"], [r["name"] for r in sweep["not_parked"]])

    def test_same_pane_text_waiting_yields_a_gate(self):
        self.fleet.add("s1", 200, CLOSER_C, status="waiting", name="Busy Worker")
        self.fleet.shapes["200"] = "question"
        sweep = self.fleet.sweep()
        self.assertEqual(["Busy Worker"], [r["name"] for r in sweep["parked"]])

    def test_the_two_fixtures_differ_only_in_the_registry_record(self):
        self.fleet.add("s1", 200, CLOSER_C, status="busy", name="W")
        self.fleet.shapes["200"] = "question"
        self.assertEqual([], self.fleet.sweep()["parked"])
        self.fleet.status["s1"] = "waiting"
        self.assertEqual(["W"], [r["name"] for r in self.fleet.sweep()["parked"]])

    def test_an_idle_record_with_a_closer_is_parked_and_with_nothing_is_not(self):
        self.fleet.add("s1", 201, "approve: do the thing now", status="idle", name="Idle Ask")
        self.fleet.add("s2", 202, "nothing", status="idle", name="Idle Nothing")
        parked = [r["name"] for r in self.fleet.sweep()["parked"]]
        self.assertIn("Idle Ask", parked)
        self.assertNotIn("Idle Nothing", parked)


class ParkedWithShellTest(Base):
    """SC2 — a `shell` session whose turn ended on a `👤 You:` ask is parked.

    `shell` is a shell HELD, not a turn running: the harness stamps it when a
    background child opens one and never clears it at turn end. Read as "not
    parked" — which `status != "idle"` did — every gate behind a background child
    goes invisible to the sweep. Each case below pins one clause of the widened
    limb, and the three negatives pin the two guards that keep a mid-turn `shell`
    and a prose `👤 You:` line out.
    """

    def names(self, *, resumed=False):
        with mock.patch.object(wnm, "turn_after_closer", return_value=resumed):
            return [r["name"] for r in self.fleet.sweep()["parked"]]

    def test_a_shell_session_parked_on_an_operator_ask_is_parked(self):
        self.fleet.add("s1", 300, "approve: restart vault-ui in the background",
                       status="shell", name="Shell Ask")
        self.assertIn("Shell Ask", self.names())

    def test_a_shell_session_parked_on_a_machine_bound_wait_is_parked(self):
        self.fleet.add("s1", 300, CLOSER_CLOCK, status="shell", name="Shell Clock")
        self.assertIn("Shell Clock", self.names())

    def test_a_shell_session_that_took_a_turn_after_its_closer_is_not_parked(self):
        self.fleet.add("s1", 300, "approve: restart vault-ui in the background",
                       status="shell", name="Shell Moved")
        self.assertEqual([], self.names(resumed=True))

    def test_a_shell_session_whose_closer_is_prose_is_not_parked(self):
        self.fleet.add("s1", 300, "Session left open, no files touched.",
                       status="shell", name="Shell Prose")
        self.assertEqual([], self.names())

    def test_a_shell_session_with_nothing_to_ask_is_not_parked(self):
        self.fleet.add("s1", 300, "nothing", status="shell", name="Shell Nothing")
        self.assertEqual([], self.names())

    def test_a_busy_session_is_still_never_parked(self):
        self.fleet.add("s1", 300, "approve: restart vault-ui in the background",
                       status="busy", name="Busy Ask")
        self.assertEqual([], self.names())


class SpokenLineTest(Base):
    """SC3 — the count is the C/D/E set, pinned by two fixtures, and has no `?`."""

    def test_one_c_plus_one_d_speaks_exactly_two(self):
        self.fleet.add("s1", 301, CLOSER_C, name="C One")
        self.fleet.add("s2", 302, CLOSER_D, name="D One")
        out = h.render(self.fleet.sweep())
        self.assertIn("\n2 items need you\n", out)

    def test_zero_c_plus_three_d_speaks_exactly_three(self):
        for i in range(3):
            self.fleet.add(f"s{i}", 310 + i, CLOSER_D, name=f"D {i}")
        out = h.render(self.fleet.sweep())
        self.assertIn("\n3 items need you\n", out)
        self.assertNotIn("2 items need you", out)

    def test_a_and_b_rows_do_not_move_the_count(self):
        self.fleet.add("s1", 301, CLOSER_C, name="C One")
        self.fleet.add("s2", 302, CLOSER_D, name="D One")
        before = h.render(self.fleet.sweep())
        self.fleet.task("25 Tasks/A item", "# Tasks\n- [ ] hold\n")
        self.fleet.add("s3", 303, CLOSER_A, name="A One", task="25 Tasks/A item")
        self.fleet.task("25 Tasks/B item",
                        "# Tasks\n- [ ] re-apply the idempotent guard and measure its firing rate\n")
        self.fleet.add("s4", 304, CLOSER_B, name="B One", task="25 Tasks/B item")
        after = h.render(self.fleet.sweep())
        self.assertIn("2 items need you", before)
        self.assertIn("2 items need you", after)

    def test_the_spoken_line_never_contains_a_question_mark(self):
        self.fleet.add("s1", 301, CLOSER_C + " — is this right?", name="C One")
        out = h.render(self.fleet.sweep())
        spoken = [ln for ln in out.splitlines() if ln.endswith("items need you")]
        self.assertEqual(1, len(spoken))
        self.assertNotIn("?", spoken[0])

    def test_chat_rows_may_still_contain_a_question_mark(self):
        self.fleet.add("s1", 301, "pick — 1. take the alpha approach to the schema split? · 2. beta",
                       name="C One")
        out = h.render(self.fleet.sweep())
        self.assertIn("?", out)


class NegativeProbeTest(Base):
    """SC5 — the whole fixture fleet produces no answering action of any kind."""

    def test_no_answer_path_is_ever_shelled_out_to(self):
        self.fleet.task("25 Tasks/C item", "# Tasks\n- [ ] something else entirely\n")
        self.fleet.add("s1", 401, CLOSER_C, name="C One", task="25 Tasks/C item")
        self.fleet.add("s2", 402, CLOSER_D, name="D One")
        self.fleet.add("s3", 403, CLOSER_E, name="E One")
        self.fleet.add("s4", 404, CLOSER_CLOCK, name="K One")
        out = h.render(self.fleet.sweep())
        self.assertTrue(out)
        banned = ("send-text", "send-keys", "answer_permission", "attention-answer",
                  "attention-ask", "activate-pane")
        for argv in self.calls:
            joined = " ".join(argv)
            for token in banned:
                self.assertNotIn(token, joined, f"{token} reached a subprocess: {argv}")


class RootCauseTest(Base):
    """SC4 — distinct calendar days, through `unowned_causes(threshold=2)`."""

    def _state(self, counts, owners=None):
        return {"version": 1, "days": {}, "counts": counts, "owners": owners or {}}

    def test_a_class_at_two_days_with_a_citation_and_no_owner_is_listed(self):
        state = self._state({wnm.CAUSE_OPERATOR: 2})
        out = h.render(h.build({}, {}, {}, [], state, shape_of=lambda p: None,
                               link_of=lambda p: ""))
        self.assertIn(wnm.CAUSE_OPERATOR, out)
        self.assertIn("scripts/who-needs-me.py:", out)

    def test_a_class_at_two_days_with_no_mechanism_is_omitted(self):
        state = self._state({wnm.CAUSE_OPERATOR: 2})
        with mock.patch.object(h, "CAUSE_MECHANISM", {}):
            out = h.render(h.build({}, {}, {}, [], state, shape_of=lambda p: None,
                                   link_of=lambda p: ""))
        self.assertNotIn(wnm.CAUSE_OPERATOR, out)
        self.assertIn("none —", out)

    def test_a_class_seen_once_is_omitted(self):
        state = self._state({wnm.CAUSE_OPERATOR: 1})
        out = h.render(h.build({}, {}, {}, [], state, shape_of=lambda p: None,
                               link_of=lambda p: ""))
        self.assertNotIn(wnm.CAUSE_OPERATOR, out)

    def test_the_threshold_is_passed_explicitly_not_inherited(self):
        self.assertEqual(2, h.RECURRENCE_THRESHOLD)
        self.assertNotEqual(wnm.OWNER_TASK_THRESHOLD, h.RECURRENCE_THRESHOLD)
        with mock.patch.object(wnm, "unowned_causes", return_value=[]) as m:
            h.render(h.build({}, {}, {}, [], self._state({}), shape_of=lambda p: None,
                             link_of=lambda p: ""))
        self.assertEqual(2, m.call_args.kwargs.get("threshold"))

    def test_two_runs_on_one_date_count_once(self):
        # The sibling's own fold: one date, one increment, however many runs.
        path = os.path.join(self.root, "causes.json")
        wnm.record_causes({wnm.CAUSE_OPERATOR}, path=path, today="2026-10-14")
        state = wnm.record_causes({wnm.CAUSE_OPERATOR}, path=path, today="2026-10-14")
        self.assertEqual(1, state["counts"][wnm.CAUSE_OPERATOR])
        self.assertEqual([], wnm.unowned_causes(state, threshold=2))


class BoardFlagTest(Base):
    """SC6 — the two flags are distinct, and a failed read emits neither."""

    def _parked(self, name, sid, pane):
        self.fleet.add(sid, pane, CLOSER_C, name=name)

    def test_a_gate_with_no_card_is_flagged_missing(self):
        self._parked("No Card", "s1", 501)
        rows = self.rows(self.fleet.sweep())
        self.assertEqual([wnm.FLAG_CARD_MISSING], rows["No Card"]["flags"])

    def test_a_gate_whose_card_was_answered_skip_is_flagged_expired(self):
        self._parked("Expired", "s1", 501)
        self.fleet.posted.add("s1")
        rows = self.rows(self.fleet.sweep())
        self.assertEqual([wnm.FLAG_CARD_EXPIRED], rows["Expired"]["flags"])

    def test_a_carded_gate_is_not_flagged(self):
        self._parked("Carded", "s1", 501)
        self.fleet.records[0]["store_vouched"] = True
        rows = self.rows(self.fleet.sweep())
        self.assertEqual([], rows["Carded"]["flags"])

    def test_a_failed_store_read_flags_neither_and_surfaces_the_note(self):
        self._parked("Outage", "s1", 501)
        with mock.patch.object(wnm, "STORE_OK", False), \
                mock.patch.object(wnm, "SOURCE_NOTE", "store unreachable — reading event log"):
            sweep = self.fleet.sweep()
        self.assertEqual([], self.rows(sweep)["Outage"]["flags"])
        self.assertIn("store unreachable", h.render(sweep))


class ManagerQueryTest(Base):
    """SC7 — one message per live manager, routed and carrying its own content."""

    def setUp(self):
        super().setUp()
        self.fleet.task("25 Tasks/M1",
                        "# Tasks\n- [ ] finish the census of parked panes\n- [ ] write it up\n")
        self.fleet.task("25 Tasks/M2", "# Tasks\n- [ ] land the ordering fix\n")
        self.fleet.add("m1", 601, "pick — 1. wait for the guard to land · 2. skip it",
                       name="Fleet Manager", role="manager", task="25 Tasks/M1")
        self.fleet.add("m2", 602, "you run: `make apply` — reply `applied`",
                       name="Alpha Manager", task="25 Tasks/M2")
        self.fleet.add("w1", 603, "pick — 1. take the alpha approach to the schema split · 2. beta",
                       name="Some Worker", task="25 Tasks/M2")
        self.sweep = self.fleet.sweep()
        self.out = h.render(self.sweep)

    def test_only_managers_are_listed(self):
        names = [r["name"] for r in self.sweep["managers"]]
        self.assertEqual(["Alpha Manager", "Fleet Manager"], sorted(names))

    def test_each_manager_appears_exactly_once(self):
        for name in ("Alpha Manager", "Fleet Manager"):
            self.assertEqual(1, self.out.count(f"  {name}  ["), name)

    def test_each_manager_block_carries_its_own_next_item_and_its_blocker(self):
        blocks = self.out.split("  Alpha Manager")[1]
        self.assertIn("land the ordering fix", blocks)
        self.assertIn("make apply", blocks)
        fleet_block = self.out.split("  Fleet Manager")[1].split("  Alpha Manager")[0]
        self.assertIn("finish the census of parked panes", fleet_block)
        self.assertIn("wait for the guard to land", fleet_block)

    def test_the_question_is_put_once_for_the_whole_block(self):
        self.assertEqual(1, self.out.count(f"Question: {h.MANAGER_QUESTION}"))

    def test_a_worker_is_never_queried(self):
        self.assertNotIn("Some Worker  [", self.out)

    def test_no_send_path_reaches_a_subprocess(self):
        banned = ("send-text", "send-keys")
        for argv in self.calls:
            for token in banned:
                self.assertNotIn(token, " ".join(argv))


class ManagerSubjectTest(Base):
    """SC7 — a manager's next item is read off the page it is ANCHORED to.

    The live shape no other fixture in this suite can reach: a manager carrying a
    `worker-manager` state file and a goal page, and **no `📌 Task:` line** in its
    transcript. Every other fixture hands its manager a task, so the subject path
    is the one branch the suite could not exercise — measured 2026-10-15 on the
    live fleet, where all four managers rendered no `next:` at all for exactly
    this reason.
    """

    def setUp(self):
        super().setUp()
        # The checked row above the open one is load-bearing: a reader that took
        # the page's FIRST row rather than its first OPEN row returns "already
        # met" and fails the assertion below.
        self.fleet.task("24 Goals/Alpha Goal",
                        "# Success Criteria\n"
                        "- [x] SC1: already met\n"
                        "- [ ] SC2: the census of parked panes is complete\n"
                        "# Tasks\n- [ ] a later row that must not win\n")
        self.fleet.state("m9", "Alpha Goal")
        self.fleet.add("m9", 901, "pick — 1. keep going · 2. stop",
                       name="Alpha Manager",
                       transcript="👤 You: pick — 1. keep going · 2. stop\n")
        self.sweep = self.fleet.sweep()
        self.out = h.render(self.sweep)

    def _block(self, name, out=None):
        return (out or self.out).split(f"  {name}  [")[1].split("Question:")[0]

    def test_a_managers_next_item_comes_from_its_subject_page(self):
        self.assertIn("next: SC2: the census of parked panes is complete",
                      self._block("Alpha Manager"))

    def test_the_first_OPEN_row_is_taken_not_the_first_row(self):
        self.assertNotIn("already met", self._block("Alpha Manager"))

    def test_the_page_is_read_through_the_subject_not_the_transcript(self):
        # The transcript names no task at all, so a task-file read cannot have
        # produced the row above — the state file is the only source it has.
        self.assertNotIn("📌 Task:", self.fleet.records[0]["_transcript"])

    def test_the_vault_name_is_compared_case_insensitively(self):
        # State files written before 2026-09-23 may carry display case while the
        # vault-cli config name is lowercase; a strict match silently drops them.
        self.fleet.state("m10", "Alpha Goal", vault="Private-Personal")
        self.fleet.add("m10", 902, "pick — 1. keep going · 2. stop",
                       name="Beta Manager",
                       transcript="👤 You: pick — 1. keep going · 2. stop\n")
        out = h.render(self.fleet.sweep())
        self.assertIn("next: SC2: the census of parked panes is complete",
                      self._block("Beta Manager", out))

    def test_an_unresolvable_subject_falls_back_to_the_task_file(self):
        # A silent miss, never an error: a manager whose goal was renamed or
        # completed is an ordinary fleet event. It must be reported exactly as it
        # was before this rule existed, not lose its line and not raise.
        self.fleet.task("25 Tasks/Gone", "# Tasks\n- [ ] the task's own next row\n")
        self.fleet.state("m11", "A Goal That Was Renamed Away")
        self.fleet.add("m11", 903, "pick — 1. keep going · 2. stop",
                       name="Gamma Manager", task="25 Tasks/Gone",
                       transcript="📌 Task: [T](obsidian://open?vault=private-personal"
                                  "&file=25 Tasks/Gone)\n"
                                  "👤 You: pick — 1. keep going · 2. stop\n")
        out = h.render(self.fleet.sweep())
        self.assertIn("next: the task's own next row", self._block("Gamma Manager", out))

    def test_a_topic_subject_resolves_in_the_topics_folder(self):
        # The `branch: topic` half of the page test, which no live manager
        # exercises — all four measured on 2026-10-15 carry `branch: goal`, so
        # without this the topic folder is a path nothing covers.
        self.fleet.task("23 Topics/Beta Topic",
                        "# Tasks\n- [ ] the topic's next open row\n")
        self.fleet.state("m12", "Beta Topic", branch="topic")
        self.fleet.add("m12", 905, "pick — 1. keep going · 2. stop",
                       name="Delta Manager",
                       transcript="👤 You: pick — 1. keep going · 2. stop\n")
        out = h.render(self.fleet.sweep())
        self.assertIn("next: the topic's next open row", self._block("Delta Manager", out))

    def test_a_long_page_row_is_bounded_like_the_blocker_line(self):
        # A goal's SC/DoD rows run to hundreds of characters; the task rows this
        # line used to carry did not, so a manager's block would swamp the
        # four-section report it sits in.
        self.fleet.task("24 Goals/Long Goal", "# Tasks\n- [ ] " + "x" * 400 + "\n")
        self.fleet.state("m13", "Long Goal")
        self.fleet.add("m13", 906, "pick — 1. keep going · 2. stop",
                       name="Epsilon Manager",
                       transcript="👤 You: pick — 1. keep going · 2. stop\n")
        block = self._block("Epsilon Manager", h.render(self.fleet.sweep()))
        line = [ln for ln in block.splitlines() if ln.strip().startswith("next:")][0]
        self.assertEqual(len("         next: ") + 120, len(line))

    def test_a_worker_is_never_subject_resolved(self):
        # The same state file that resolves for a manager must NOT redirect a
        # worker's next item: a worker is anchored to its task, and a subject read
        # for it would report a goal's row as the worker's own. Read off the row
        # rather than the render, because a worker is never listed in the
        # `Managers to query` block at all.
        self.fleet.task("25 Tasks/W", "# Tasks\n- [ ] the worker's own next row\n")
        self.fleet.state("w9", "Alpha Goal")
        self.fleet.add("w9", 904, "pick — 1. keep going · 2. stop",
                       name="Some Worker", task="25 Tasks/W")
        rows = {r["name"]: r for r in self.fleet.sweep()["parked"]}
        self.assertEqual("the worker's own next row", rows["Some Worker"]["next_item"])


class OrderingTest(Base):
    """SC8 — prod outranks closeness, and each row carries its own link and answer."""

    def setUp(self):
        super().setUp()
        # The prod item is LESS close: two unchecked boxes against the non-prod
        # item's one. A closeness-only implementation reverses the expected order.
        self.fleet.task("25 Tasks/Prod",
                        "# Tasks\n- [ ] ⚠️ production-touching — `make apply`\n- [ ] verify\n")
        self.fleet.task("25 Tasks/Plain", "# Tasks\n- [ ] verify the thing\n")
        self.fleet.add("p1", 701, "you run: `make apply` — reply `applied`",
                       name="Prod Item", task="25 Tasks/Prod")
        self.fleet.add("p2", 702, "pick — 1. take the alpha approach to the schema split · 2. take beta",
                       name="Plain Item", task="25 Tasks/Plain")
        self.rows = h.handover_order(self.fleet.sweep()["parked"])

    def test_prod_sorts_before_a_closer_non_prod_item(self):
        self.assertEqual(["Prod Item", "Plain Item"], [r["name"] for r in self.rows])

    def test_the_prod_item_is_less_close_than_the_non_prod_one(self):
        by = {r["name"]: r for r in self.rows}
        self.assertTrue(by["Prod Item"]["prod"])
        self.assertFalse(by["Plain Item"]["prod"])
        self.assertGreater(by["Prod Item"]["open_boxes"], by["Plain Item"]["open_boxes"])

    def test_a_cross_vault_item_is_non_prod_even_with_the_marker(self):
        other = os.path.join(self.root, "seibert-brogrammers")
        os.makedirs(os.path.join(other, "25 Tasks"), exist_ok=True)
        with open(os.path.join(other, "25 Tasks", "X.md"), "w", encoding="utf-8") as f:
            f.write("# Tasks\n- [ ] ⚠️ production-touching — `make apply`\n")
        self.assertFalse(h.is_prod(os.path.join(other, "25 Tasks", "X.md"),
                                   "# Tasks\n- [ ] ⚠️ production-touching — `make apply`\n"))

    def test_each_row_carries_its_own_pane_link(self):
        for r in self.rows:
            self.assertEqual(f"/supervisor:jump {r['pane']}", r["jump"])

    def test_each_recommended_answer_is_one_of_that_items_own_options(self):
        for r in self.rows:
            self.assertTrue(r["options"], r["name"])
            self.assertIn(r["answer"], r["options"], r["name"])

    def test_only_operator_bound_classes_are_handed_over(self):
        self.assertTrue(all(r["gate"] in h.OPERATOR_BOUND for r in self.rows))
        self.assertEqual(("C", "D", "E"), h.OPERATOR_BOUND)


class AnswerExtractionTest(Base):
    """The answer set is read off the closer, and never invented."""

    def test_a_pick_menu_recommends_its_first_option(self):
        options, answer = h.options_of(
            "pick — 1. take the alpha approach · 2. take beta")
        self.assertEqual(["take the alpha approach", "take beta"], options)
        self.assertEqual("take the alpha approach", answer)

    def test_approve_and_you_run_name_themselves(self):
        for text in ("approve: /vault-cli:session-close", "you run: `make apply` — reply `applied`"):
            options, answer = h.options_of(text)
            self.assertTrue(options, text)
            self.assertEqual(options[0], answer)

    def test_a_closer_naming_no_options_yields_none(self):
        self.assertEqual(([], ""), h.options_of("nothing"))
        self.assertEqual(([], ""), h.options_of(""))


class CommandContractTest(Base):
    """SC1's no-loop probe — the command invokes the sweep exactly once."""

    def setUp(self):
        super().setUp()
        with open(_COMMAND, encoding="utf-8") as f:
            self.text = f.read()

    def test_the_command_invokes_the_sweep_exactly_once(self):
        self.assertEqual(1, self.text.count("scripts/who-needs-my-help.py"))

    def test_the_command_grants_sendmessage_and_no_pane_typing_grant(self):
        self.assertIn("  - SendMessage", self.text)
        # The grant block is what a permission decision reads; the body may (and
        # must) still NAME the ban. Assert on the grant, not on the prose.
        grants = self.text.split("---")[1]
        self.assertNotIn("wezterm", grants)
        self.assertIn("Never `wezterm cli send-text` / `send-keys`", self.text)

    def test_the_command_names_all_four_sections(self):
        for section in ("`Sweep`", "`Handover`", "`Root cause`", "`Managers to query`"):
            self.assertIn(section, self.text)


class ReuseTest(Base):
    """The script reuses the sibling's classifiers rather than re-deriving them."""

    def test_every_cause_class_comes_from_the_sibling(self):
        for name in ("CAUSE_OPERATOR", "CAUSE_TASK", "CAUSE_CLOCK", "CAUSE_CAP",
                     "CAUSE_UNKNOWN"):
            self.assertIs(getattr(h, name, None), None)
            self.assertIsNot(getattr(wnm, name), None)

    def test_every_cited_mechanism_names_a_real_symbol(self):
        for cause, mechanism in h.CAUSE_MECHANISM.items():
            self.assertIn(cause, wnm.CAUSE_CLASSES)
            self.assertIn("scripts/who-needs-me.py:", mechanism)


class OperatorAskTest(Base):
    """A park is a gate only when the operator could actually clear it.

    The shapes the first live run supplied and the suite never did. `gate_class()`
    returns **C** for any park that is not A/B/D/E, and C is operator-bound — so
    without an ask test, a session sitting on arbitrary prose reaches `Handover`.
    Measured 2026-10-15: panes 1557 and 1619 did exactly that while all three
    instruments agreed they were not gated.
    """

    def test_a_prose_detail_with_no_closer_line_is_parked_but_not_a_gate(self):
        # 1557's shape: the transcript holds NO `👤 You:` line, so the closer read is
        # the record's `detail` — arbitrary assistant prose, not an ask.
        self.fleet.add("s1", 901, "📌 Conventions: run `/recall` if you feel drifted",
                       kind="idle",
                       transcript="📌 Conventions: run `/recall` if you feel drifted\n")
        sweep = self.fleet.sweep()
        row = {r["session_id"]: r for r in sweep["parked"]}["s1"]
        self.assertIsNone(row["gate"])
        self.assertNotIn("s1", [r["session_id"] for r in h.handover_order(sweep["parked"])])

    def test_a_prose_closer_line_is_recorded_as_not_gated(self):
        # 1619's shape: a real `👤 You:` line that is prose, not one of the verbs.
        self.fleet.add("s1", 903, "Session left open, no files touched, no commands run.")
        out = h.render(self.fleet.sweep())
        self.assertIn("not gated", out)
        self.assertNotIn("machine-bound", out)

    def test_a_pick_closer_still_gates_when_its_cause_is_unknown(self):
        # 1625/1572's shape, and the reason "cause == unknown ⇒ no gate" is wrong:
        # an answered record makes `is_open_gate()` refuse it, so the cause falls to
        # `unknown` — yet the closer is unmistakably an ask, and these are the two
        # rows that legitimately pass DoD 4(a).
        self.fleet.add("s1", 902,
                       "pick — 1. realign SC1 to the proven capability · 2. leave it",
                       kind="question", state="answered")
        sweep = self.fleet.sweep()
        row = {r["session_id"]: r for r in sweep["parked"]}["s1"]
        self.assertEqual(wnm.CAUSE_UNKNOWN, row["cause"])
        self.assertEqual("C", row["gate"])
        self.assertIn("s1", [r["session_id"] for r in h.handover_order(sweep["parked"])])

    def test_a_store_item_makes_a_park_a_gate_even_without_an_ask(self):
        # The second clause of the rule: the store holding an open item is its own
        # evidence that the operator is being waited on.
        self.fleet.add("s1", 904, "Session left open, no files touched, no commands run.")
        self.fleet.records[0]["store_vouched"] = True
        sweep = self.fleet.sweep()
        row = {r["session_id"]: r for r in sweep["parked"]}["s1"]
        self.assertIsNotNone(row["gate"])


class NotParkedManagerTest(Base):
    """A manager between rounds is NOT parked, and must still be queryable.

    `build()` builds the Managers section from `parked_rows + not_parked`, so a
    manager sitting `busy` between rounds has to carry `next_item` on the
    not-parked path too. Measured while adding the four-state label: that key was
    dropped from the not-parked row and no fixture noticed, because every manager
    fixture in this suite was parked. This is that fixture.
    """

    def setUp(self):
        super().setUp()
        self.fleet.task("25 Tasks/M1", "# Tasks\n- [ ] land the ordering fix\n")
        self.fleet.add("m1", 701, "", status="busy",
                       name="Between Rounds Manager", role="manager",
                       task="25 Tasks/M1")
        self.sweep = self.fleet.sweep()
        self.out = h.render(self.sweep)

    def test_a_busy_manager_is_listed_with_its_next_item(self):
        self.assertEqual(["Between Rounds Manager"],
                         [r["name"] for r in self.sweep["managers"]])
        self.assertIn("land the ordering fix", self.out)

    def test_its_state_is_working_not_parked(self):
        self.assertEqual([], self.sweep["parked"])
        self.assertEqual(h.STATE_WORKING, self.sweep["managers"][0]["state"])


CLOSER_WATCH = "nothing\n⏰ Ends: the PR review landing (github-pr-watch, #312)"


class FourStateTest(Base):
    """SC1 / SC2 / SC3 — one state per session, or `unclassified` with a reason.

    TWO FIXTURES PER BUCKET, and their input signals differ: `decision` from a
    pick-menu closer and from a `you run:` closer; `external` from an `⏰ Ends:`
    watcher and from a `later (on …)` clock; `working` from a `busy` record and
    from a `busy` session the attention store never covered; `closed` from a
    `completed` task and from an `aborted` one. One fixture per bucket would pass
    on a classifier that returned a constant, which is the whole reason this
    suite's standard is two.
    """

    def setUp(self):
        super().setUp()
        self.fleet.task("25 Tasks/open item",
                        "---\nstatus: in_progress\n---\n# Tasks\n- [ ] something else entirely\n")
        self.fleet.task("25 Tasks/done item",
                        "---\nstatus: completed\n---\n# Tasks\n- [x] shipped\n")
        self.fleet.task("25 Tasks/aborted item",
                        "---\nstatus: aborted\n---\n# Tasks\n- [x] abandoned\n")
        # decision — two closers, two different gate classes
        self.fleet.add("s-dec-pick", 301, CLOSER_C, name="Decision Pick",
                       task="25 Tasks/open item")
        self.fleet.add("s-dec-run", 302, CLOSER_D, name="Decision Run",
                       task="25 Tasks/open item")
        # external — a watcher line and a clock line
        self.fleet.add("s-ext-watch", 303, CLOSER_WATCH, name="External Watch")
        self.fleet.add("s-ext-clock", 304, CLOSER_CLOCK, name="External Clock")
        # external — a closer that itself names the machine event (pane 370 shape)
        self.fleet.add("s-ext-agent", 311, "",
                       name="External Agent",
                       transcript="👤 You: ⏰ Ends: orientation assistant returns "
                                  "(background agent a2d197b1)\n")
        # unclassified — the ask was written into the `⏰ Ends:` slot, not the
        # closer (pane 449 shape): the watcher asks the OPERATOR, so it is not a
        # machine wait and must not be promoted to `external`.
        self.fleet.add("s-unc-askslot", 312, "",
                       name="Ask In Watcher Slot",
                       transcript="👤 You: Orienting on the task — assistant is searching the vault.\n"
                                  "⏰ Ends: ` your reply — I run the three commands\n")
        # unclassified — a LONG lead-in before the operator verb (pane 483 shape).
        # The first cut bounded the lead-in to 24 characters, and this live one is
        # 30, so it still read `external` on the release that was meant to fix it.
        self.fleet.add("s-unc-longlead", 313, "",
                       name="Long Lead-In",
                       transcript="👤 You: Task discovery is running in the background…\n"
                                  "⏰ Ends: your `nukeprod-threshold-applied` reply\n")
        # working — a busy record, and a busy session with no attention record
        self.fleet.add("s-wrk-record", 305, "", status="busy", name="Working Record")
        self.fleet.registry["s-wrk-bare"] = "Working Bare"
        self.fleet.status["s-wrk-bare"] = "busy"
        # closed — a completed task and an aborted one, both idle with nothing to say
        self.fleet.add("s-cls-done", 306, "nothing", status="idle",
                       name="Closed Done", task="25 Tasks/done item")
        self.fleet.add("s-cls-abort", 307, "nothing", status="idle",
                       name="Closed Aborted", task="25 Tasks/aborted item")
        # unclassified — a settings ask rule and a bare shell
        self.fleet.add("s-unc-perm", 308, "", name="Ask Rule Worker")
        self.fleet.shapes["308"] = "permission"
        # a DEAD shell — `alive: False` is what makes it a bare shell at all;
        # a `status: shell` on a live process is stale metadata, not a verdict
        self.fleet.add("s-unc-shell", 309, "", status="shell", alive=False,
                       name="Bare Shell")
        self.sweep = self._sweep_with_registry_only()

    def _sweep_with_registry_only(self):
        """`Fleet.sweep()` over records PLUS the registry-only session.

        The supplement itself lives in `sweep_live()`; here it is applied by hand
        so `build()`'s classification is testable without the live readers. The
        `sweep_live()` path is pinned separately by `PopulationTest`.
        """
        records = list(self.fleet.records)
        records.append({"session_id": "s-wrk-bare", "pane": "310",
                        "detail": "", "kind": "idle", "ts": 0,
                        "_transcript": ""})
        self.fleet.records = records
        return self.fleet.sweep()

    def _state(self, name):
        for r in self.sweep["parked"] + self.sweep["not_parked"]:
            if r["name"] == name:
                return r["state"]
        self.fail(f"{name} rendered no row at all")

    def test_each_bucket_is_reached_by_two_differing_fixtures(self):
        expected = {
            "Decision Pick": h.STATE_DECISION,
            "Decision Run": h.STATE_DECISION,
            "External Watch": h.STATE_EXTERNAL,
            "External Clock": h.STATE_EXTERNAL,
            "External Agent": h.STATE_EXTERNAL,
            "Ask In Watcher Slot": h.STATE_UNCLASSIFIED,
            "Long Lead-In": h.STATE_UNCLASSIFIED,
            "Working Record": h.STATE_WORKING,
            "Working Bare": h.STATE_WORKING,
            "Closed Done": h.STATE_CLOSED,
            "Closed Aborted": h.STATE_CLOSED,
            "Ask Rule Worker": h.STATE_UNCLASSIFIED,
            "Bare Shell": h.STATE_UNCLASSIFIED,
        }
        self.assertEqual(expected, {n: self._state(n) for n in expected})

    def test_a_constant_label_cannot_pass(self):
        # The guard the two-fixture rule exists for: the label set is not one value.
        seen = {self._state(n) for n in
                ("Decision Pick", "External Watch", "Working Record", "Closed Done")}
        self.assertEqual(4, len(seen))

    def test_the_ask_rule_worker_is_unclassified_with_its_own_reason(self):
        row = {r["name"]: r for r in self.sweep["parked"]}["Ask Rule Worker"]
        self.assertEqual(h.STATE_UNCLASSIFIED, row["state"])
        self.assertEqual("permission prompt (ask rule)", row["state_reason"])

    def test_an_ask_written_in_the_watcher_slot_is_not_promoted_to_external(self):
        # Pane 449 on the first live run: the session put its ask in the
        # `⏰ Ends:` slot, so `watcher_of()` saw a watcher and the label read
        # `external` while it was waiting on the operator.
        row = {r["name"]: r for r in self.sweep["parked"]}["Ask In Watcher Slot"]
        self.assertEqual(h.STATE_UNCLASSIFIED, row["state"])
        self.assertEqual("watcher asks the operator, not a machine",
                         row["state_reason"])

    def test_a_closer_naming_the_machine_event_is_external(self):
        row = {r["name"]: r for r in self.sweep["parked"]}["External Agent"]
        self.assertEqual(h.STATE_EXTERNAL, row["state"])

    def test_a_bare_shell_is_unclassified_with_a_different_reason(self):
        row = {r["name"]: r for r in self.sweep["not_parked"]}["Bare Shell"]
        self.assertEqual(h.STATE_UNCLASSIFIED, row["state"])
        self.assertEqual("bare shell", row["state_reason"])

    def _row(self, name, sweep=None):
        sweep = sweep if sweep is not None else self.fleet.sweep()
        for r in sweep["parked"] + sweep["not_parked"]:
            if r["name"] == name:
                return r
        self.fail(f"{name} rendered no row at all")

    def test_a_live_shell_session_classifies_as_working(self):
        # The differential, and the reason a rename cannot satisfy this suite:
        # the SAME pane is rendered twice, once with `status: shell` and once
        # with `status: idle`, and the two rows must DIFFER. A live `shell` is a
        # held background child (the harness stamps it when a shell is spawned,
        # and a `run_in_background` child keeps it for its whole life), so the
        # session is working; the same pane on `idle` holds nothing.
        #
        # This replaces the 2026-10-18 differential, which asserted the two
        # agree. That one fixed the `bare shell` label on live panes; this one
        # gives them their true state rather than `idle with no ask` — measured
        # 2026-10-19 on pane 855, whose footer read `1 shell still running`.
        #
        # The empty closer matters: `parked()`'s shell limb takes a stricter
        # closer test than its idle limb, so a closer-bearing fixture could be
        # parked under one status and not the other. With no closer both limbs
        # agree the pane is unparked, which isolates the label.
        self.fleet.add("s-shell-live", 320, "", status="shell", alive=True,
                       name="Live Shell")
        shell_row = self._row("Live Shell")
        self.fleet.status["s-shell-live"] = "idle"
        idle_row = self._row("Live Shell")
        self.assertEqual(h.STATE_WORKING, shell_row["state"])
        self.assertEqual((h.STATE_UNCLASSIFIED, "idle with no ask"),
                         (idle_row["state"], idle_row["state_reason"]))

    def test_a_dead_shell_session_is_still_a_bare_shell(self):
        # The negative arm. Without it, deleting the shell branch outright
        # satisfies every other criterion while silently dropping real close
        # candidates from the report.
        self.fleet.add("s-shell-dead", 321, "", status="shell", alive=False,
                       name="Dead Shell")
        row = self._row("Dead Shell")
        self.assertEqual(h.STATE_UNCLASSIFIED, row["state"])
        self.assertEqual("bare shell", row["state_reason"])

    def test_an_unprovable_shell_session_is_not_a_bare_shell(self):
        # `alive: None` is "cannot tell" — the pid is occupied but the record
        # carries no usable `procStart`, so identity cannot be established. That
        # is never death (`session-liveness.py`: UNKNOWN != ABSENT), so the row
        # must not be handed to the operator as a close candidate.
        self.fleet.add("s-shell-unknown", 322, "", status="shell", alive=None,
                       name="Unknown Shell")
        row = self._row("Unknown Shell")
        self.assertNotEqual("bare shell", row["state_reason"])
        # Nor is it proof of a held child: `working` needs `alive is True`.
        self.assertEqual((h.STATE_UNCLASSIFIED, "idle with no ask"),
                         (row["state"], row["state_reason"]))

    def test_a_machine_bound_wait_is_external_and_names_its_watcher(self):
        row = {r["name"]: r for r in self.sweep["parked"]}["External Watch"]
        self.assertEqual(h.STATE_EXTERNAL, row["state"])
        self.assertIn("PR review landing", row["watcher"])

    def test_the_render_tallies_every_state_it_printed(self):
        out = h.render(self.sweep)
        for state in (h.STATE_DECISION, h.STATE_EXTERNAL, h.STATE_WORKING,
                      h.STATE_CLOSED, h.STATE_UNCLASSIFIED):
            self.assertIn(state, out)
        self.assertIn("decision 2", out)


# The 2026-10-19 live panes this class reproduces, as their sessions wrote them.
# A hand read of 23 panes found 11 mislabelled, and every shape below is one of
# them — the `idle`/`shell`/`busy` registry status is the one the session really
# carried, because a `waiting` fixture reaches the parked branch through the
# registry alone and would be green on the code these tests exist to change.
PANEL_CRON = (  # pane 808
    "🟡 WAITING\n"
    "   What:     the 24h recurrence window on the Sentry issue\n"
    "   Progress: 21h 49m of 24h; 0 new events\n"
    "   ETA:      by ~22:27 local (2026-10-19T20:26:42Z)\n"
    "   Blocks:   SC3, DoD6, DoD7\n"
    "   Done?:    in_progress · execution · 6 open\n"
    "   Help:     nothing — unless the Sentry write 403s at 22:33\n"
    "👤 You:    nothing\n"
    "⏰ Ends:   one-shot cron job 6864ab1d at 22:33 local re-reads Sentry and both "
    "pod logs, resolves the issue, closes the task\n")
PANEL_MANAGER = (  # pane 794
    "🟡 WAITING\n"
    "   What:     Re-Key worker planning; SC4 recurrence windows\n"
    "   Progress: SC4 1/3 counted, 2 shipped\n"
    "   ETA:      SC4 2026-10-26; Re-Key unknown — planning\n"
    "   Help:     nothing — no open gate\n"
    "👤 You:    nothing\n"
    "⏰ Ends:   Re-Key worker reaches its first decision (manager round wakeup at 20:42)\n")
PANEL_CLOCK = (
    "🟡 WAITING · nothing running (clock-bound until 2026-10-21) · nothing needed\n"
    "👤 You: nothing\n"
    "⏰ Next: 2026-10-21 09:00 — the one-shot ScheduleWakeup re-reads the alert count\n")
PANEL_SUBAGENT = (  # pane 832 — `busy`, because a background agent runs
    "🟡 WAITING\n"
    "   What:     make buca of backtest/agent on dev\n"
    "   Help:     nothing — automated\n"
    "👤 You:    nothing\n"
    "⏰ Ends:   the deploy runner reports (simple-bash-runner subagent, dispatched "
    "this turn)\n")
PANEL_DONE = (
    "⚪ DONE · nothing running · nothing needed\n"
    "👤 You: nothing\n"
    "⏰ Next: no mechanism is running\n")
PANEL_REPLY = (
    "🔵 READY · nothing running · you decide\n"
    "👤 You: nothing\n"
    "⏰ Ends: your `applied` reply — I then run the migration\n")
PANEL_ACTIVE_SHELL = (  # pane 855
    "🟢 ACTIVE · 1 running (post-commit e2e readout) · nothing needed\n"
    "👤 You: nothing\n"
    "⏰ Next: the background readout exits; I then record # Results\n")
PANEL_WAITING_SHELL = (  # pane 856's first shape: a 7-day run it watches
    "🟡 WAITING\n"
    "   What:     the 7-day accepted-changes backfill\n"
    "   ETA:      unknown — GitHub rate limit\n"
    "👤 You:    nothing\n"
    "⏰ Ends:   the background backfill exits (run_in_background shell)\n")
PROSE_SHELL = "Full run started with the batched GraphQL data layer.\n"  # pane 856 now
PANEL_DECLARED_WORKING = (  # pane 888 — worker-drive's own `State:` line
    "State:            working 🟢 · tool call in flight (this drive run) + live "
    "background shell pid 90678 ALIVE\n"
    "👤 You: nothing — I'm driving\n"
    "⏰ Next: background shell `sleep 600` (pid 90678) expires at ~21:47; cleanup "
    "runs before then\n")
PANEL_DONE_OPERATOR = (  # pane 889 — a closed session whose next step is the operator's
    "✅ Nothing to do — safe to close Claude :)\n\n"
    "⚪ DONE\n"
    "👤 You: nothing — session closed\n"
    "⏰ Next: you open a new session; the orchestrator picks the next anchor\n")
PANEL_PICK = (  # pane 854 — idle on a pick while a background agent keeps `busy`
    "PR #126: CI green, draft, awaiting the review loop before flipping ready.\n"
    "🔵 READY · 1 running (background agent) · you decide\n"
    "👤 You: pick — 1. flip PR #126 ready and trigger the bot review (recommended) "
    "· 2. run one more local review round\n"
    "⏰ Next: my next tool round\n")


class WatcherAndShellStateTest(Base):
    """The 2026-10-19 mislabels: a watched wait, a held shell, a busy-but-ended turn.

    Every row is asserted through `build()`, never `state_of()` alone: the parked
    branch of `state_of()` already answered `external` for these shapes, and the
    defect was that `parked()` never sent them there — a `nothing` closer was read
    as "nothing to report" before the `⏰` line was looked at.
    """

    def _transcript_file(self, sid, closer_text, *, turn_after):
        """A JSONL transcript whose last closer is or is not followed by a turn."""
        path = os.path.join(self.root, f"{sid}.jsonl")
        lines = [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": closer_text}]}}]
        if turn_after:
            lines.append({"type": "user", "message": {"content": "continue"}})
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        return path

    def _add(self, sid, pane, text, *, status, alive=True, turn_after=False):
        self.fleet.add(sid, pane, "", status=status, alive=alive, name=sid,
                       kind="idle", transcript=text)
        if status == "busy":
            self.fleet.records[-1]["transcript"] = self._transcript_file(
                sid, text, turn_after=turn_after)

    def _row(self, name):
        sweep = self.fleet.sweep()
        for r in sweep["parked"] + sweep["not_parked"]:
            if r["name"] == name:
                return r
        self.fail(f"{name} rendered no row at all")

    # --- a `nothing` closer whose `⏰` line names a machine --------------------

    def test_a_cron_watch_is_external_with_the_panel_eta(self):
        self._add("cron", 808, PANEL_CRON, status="idle")
        row = self._row("cron")
        self.assertEqual(h.STATE_EXTERNAL, row["state"])
        self.assertIn("cron job 6864ab1d", row["watcher"])
        self.assertEqual("by ~22:27 local (2026-10-19T20:26:42Z)", row["eta"])

    def test_a_manager_round_wakeup_is_external(self):
        self._add("manager", 794, PANEL_MANAGER, status="idle")
        row = self._row("manager")
        self.assertEqual(h.STATE_EXTERNAL, row["state"])
        self.assertIn("manager round wakeup", row["watcher"])

    def test_a_next_clock_is_external(self):
        self._add("clock", 824, PANEL_CLOCK, status="idle")
        row = self._row("clock")
        self.assertEqual(h.STATE_EXTERNAL, row["state"])
        self.assertIn("ScheduleWakeup", row["watcher"])

    def test_a_busy_session_whose_turn_ended_on_a_subagent_watch_is_external(self):
        self._add("subagent", 832, PANEL_SUBAGENT, status="busy")
        row = self._row("subagent")
        self.assertEqual(h.STATE_EXTERNAL, row["state"])
        self.assertIn("deploy runner reports", row["watcher"])

    def test_a_done_closer_is_not_a_machine_wait(self):
        self._add("done", 860, PANEL_DONE, status="idle")
        row = self._row("done")
        self.assertNotEqual(h.STATE_EXTERNAL, row["state"])
        # No task link contradicts the panel's `⚪ DONE`, so it is closed.
        self.assertEqual(h.STATE_CLOSED, row["state"])

    def test_a_done_panel_whose_next_step_is_the_operator_is_not_a_machine_wait(self):
        # Measured 2026-10-19 on the deployed v0.183.1 sweep: pane 889's `⚪ DONE`
        # panel read `external` because "you open a new session" names no
        # negation the guard knew — the actor it names is the operator.
        self._add("done-operator", 889, PANEL_DONE_OPERATOR, status="idle")
        row = self._row("done-operator")
        self.assertNotEqual(h.STATE_EXTERNAL, row["state"])
        # The panel declares the session finished and no task link contradicts
        # it, so it is `closed` — pane 890 on the v0.184.1 sweep read `idle with
        # no ask` for this exact shape.
        self.assertEqual(h.STATE_CLOSED, row["state"])

    def test_a_done_panel_holding_a_stray_shell_is_closed_not_working(self):
        # Pane 887 on the v0.184.1 sweep: `⚪ DONE` with a shell still held read
        # `working` — the live-shell rule fired because no task link resolved a
        # terminal status, though the session itself said it had finished.
        self._add("done-shell", 887, PANEL_DONE_OPERATOR, status="shell")
        self.assertEqual(h.STATE_CLOSED, self._row("done-shell")["state"])

    def test_a_done_panel_whose_task_is_still_open_is_not_closed(self):
        # The disk wins over the panel: a `⚪ DONE` closer anchored to a task
        # that is still `in_progress` is the anti-pattern the closer rule names,
        # and reporting it `closed` would hide exactly that.
        self.fleet.task("25 Tasks/still open", "---\nstatus: in_progress\n---\n# Tasks\n- [ ] x\n")
        self._add("done-open", 892,
                  "📌 Task: [T](obsidian://open?vault=private-personal&file=25%20Tasks%2Fstill%20open)\n"
                  + PANEL_DONE_OPERATOR, status="idle")
        self.assertNotEqual(h.STATE_CLOSED, self._row("done-open")["state"])

    def test_a_ready_panel_with_a_nothing_closer_is_not_a_machine_wait(self):
        # Pane 816 on the v0.184.1 sweep: `🔵 READY · you decide` with a `nothing`
        # closer and `⏰ Next: a fresh session picks up …` read `external`. A
        # `nothing` closer waits on a machine only when the panel says WAITING.
        self._add("ready-nothing", 816,
                  "🔵 READY · nothing running · you decide\n"
                  "👤 You: nothing — the checkpoint is complete and the next step is a fresh session\n"
                  "⏰ Next: a fresh session picks up at `/coding:pr-review` on the branch\n",
                  status="idle")
        self.assertNotEqual(h.STATE_EXTERNAL, self._row("ready-nothing")["state"])

    def test_a_session_that_declares_itself_working_is_not_a_machine_wait(self):
        # Measured the same run on pane 888: worker-drive writes its own
        # `State: working 🟢` line, and a `⏰ Next:` naming its background shell
        # was read as a wait — the 🟢 test only knew the `🟢 ACTIVE` panel.
        self._add("declared-working", 888, PANEL_DECLARED_WORKING, status="shell")
        self.assertEqual(h.STATE_WORKING, self._row("declared-working")["state"])

    def test_a_session_that_declares_itself_external_is_a_machine_wait(self):
        # The positive arm: the declared state is evidence in both directions.
        self._add("declared-external", 891,
                  "State:            external 🟡 · cron 6864ab1d at 22:33\n"
                  "👤 You: nothing\n"
                  "⏰ Ends: one-shot cron job 6864ab1d at 22:33 re-reads Sentry\n",
                  status="idle")
        self.assertEqual(h.STATE_EXTERNAL, self._row("declared-external")["state"])

    def test_an_idle_session_whose_panel_declares_itself_active_is_working(self):
        # Pane 796 on the v0.184.4 sweep (2026-10-19 22:38): an `idle` row whose
        # panel says `🟢 ACTIVE · 1 running (… resumed without a tab)` read
        # `unclassified — idle with no ask`. The `🟢 ACTIVE` guard keeps it out of
        # `external`; the panel's own declaration says where it belongs instead.
        self._add("declared-active", 796,
                  "🟢 ACTIVE · 1 running (NUKE-PROD-CB check, resumed without a tab) · nothing needed\n"
                  "👤 You: nothing\n"
                  "⏰ Next: 22:43 — manager round reads the check's result (ScheduleWakeup)\n",
                  status="idle")
        self.assertEqual(h.STATE_WORKING, self._row("declared-active")["state"])

    def test_a_dead_shell_declaring_itself_active_is_still_a_bare_shell(self):
        # Process death outranks the panel: a `🟢 ACTIVE` closer left behind by an
        # exited process is a stale claim, and `bare shell` is what the operator
        # closes.
        self._add("dead-active", 797,
                  "🟢 ACTIVE · 1 running (build) · nothing needed\n"
                  "👤 You: nothing\n"
                  "⏰ Next: the build exits\n",
                  status="shell", alive=False)
        row = self._row("dead-active")
        self.assertEqual((h.STATE_UNCLASSIFIED, "bare shell"), (row["state"], row["state_reason"]))

    def test_a_watcher_slot_asking_the_operator_is_not_a_machine_wait(self):
        self._add("reply", 861, PANEL_REPLY, status="idle")
        self.assertNotEqual(h.STATE_EXTERNAL, self._row("reply")["state"])

    def test_an_eta_slot_does_not_promote_an_operator_ask_to_external(self):
        # The slot is displayed, never evidence: a panel whose `⏰ Ends:` asks the
        # operator (pane 449's shape) still carries an estimate, and reading that
        # estimate as a machine trigger would undo the guard for that shape.
        self._add("reply-eta", 864,
                  "👤 You: Orienting on the task — assistant is searching the vault.\n"
                  "   ETA:      by ~21:30\n"
                  "⏰ Ends: ` your reply — I run the three commands\n",
                  status="idle")
        row = self._row("reply-eta")
        self.assertEqual((h.STATE_UNCLASSIFIED,
                          "watcher asks the operator, not a machine"),
                         (row["state"], row["state_reason"]))
        self.assertEqual("by ~21:30", row["eta"])

    # --- a held background shell ---------------------------------------------

    def test_an_active_shell_with_a_next_line_is_working_not_external(self):
        self._add("active-shell", 855, PANEL_ACTIVE_SHELL, status="shell")
        self.assertEqual(h.STATE_WORKING, self._row("active-shell")["state"])

    def test_a_shell_behind_a_prose_closer_is_working(self):
        self._add("prose-shell", 856, PROSE_SHELL, status="shell")
        self.assertEqual(h.STATE_WORKING, self._row("prose-shell")["state"])

    def test_a_waiting_shell_that_names_its_watch_is_external(self):
        # The precedence on a held shell is the closer's own state icon: a
        # session that says it is WAITING on a named machine is waiting on it.
        self._add("waiting-shell", 857, PANEL_WAITING_SHELL, status="shell")
        self.assertEqual(h.STATE_EXTERNAL, self._row("waiting-shell")["state"])

    def test_a_dead_shell_with_a_nothing_closer_is_still_a_bare_shell(self):
        self._add("dead-shell", 858, PROSE_SHELL, status="shell", alive=False)
        row = self._row("dead-shell")
        self.assertEqual((h.STATE_UNCLASSIFIED, "bare shell"),
                         (row["state"], row["state_reason"]))

    # --- a `busy` session whose foreground turn ended on a pick ----------------

    def test_a_busy_session_idle_on_a_pick_is_a_decision(self):
        self._add("pick", 854, PANEL_PICK, status="busy", turn_after=False)
        row = self._row("pick")
        self.assertEqual(h.STATE_DECISION, row["state"])
        self.assertIn(row["gate"], h.OPERATOR_BOUND)
        self.assertTrue(row["answer"].startswith("flip PR #126 ready"))

    def test_a_busy_session_with_a_turn_after_its_pick_is_working(self):
        # The negative arm: mid-turn, the last assistant text is the PREVIOUS
        # turn's panel, so reading it would hand a running session to the operator.
        self._add("pick-resumed", 859, PANEL_PICK, status="busy", turn_after=True)
        self.assertEqual(h.STATE_WORKING, self._row("pick-resumed")["state"])

    def test_a_busy_session_with_no_readable_transcript_stays_working(self):
        # No transcript means no proof the turn ended — stay with the registry.
        self.fleet.add("blind", 862, "", status="busy", name="blind",
                       kind="idle", transcript="")
        self.assertEqual(h.STATE_WORKING, self._row("blind")["state"])

    def test_a_busy_session_whose_tail_holds_no_closer_stays_working(self):
        # A readable tail with the closer outside it (large blobs written after
        # the message) is "cannot tell" too — `closer_turn_state()` is `None`.
        self._add("no-closer", 863, PANEL_PICK, status="busy")
        self.fleet.records[-1]["transcript"] = self._transcript_file(
            "no-closer", "Working through the review comments.", turn_after=False)
        self.assertEqual(h.STATE_WORKING, self._row("no-closer")["state"])


class PopulationTest(Base):
    """SC1 — the population is every live session, not only the ones that posted.

    The attention store holds a row only for a session with something to report,
    so a sweep fed by it alone omits every session that is simply working or
    finished. `sweep_live()` supplements the registry, and this pins that the
    supplement survives `is_live()`'s pane filter.
    """

    def setUp(self):
        super().setUp()
        self.fleet.add("s-posted", 401, CLOSER_C, name="Posted Worker")
        self.fleet.registry["s-silent"] = "Silent Worker"
        self.fleet.status["s-silent"] = "busy"

    def _sweep_live(self):
        pmap = {"401": {"title": "Posted Worker"}, "402": {"title": "Silent Worker"}}
        patches = [
            mock.patch.object(wnm, "wezterm_panes", return_value=pmap),
            mock.patch.object(wnm, "read_registry",
                              return_value=dict(self.fleet.registry)),
            mock.patch.object(wnm, "registry_records",
                              return_value=self.fleet.reg_records()),
            mock.patch.object(wnm, "load",
                              side_effect=lambda suffix: list(self.fleet.records)),
            mock.patch.object(wnm, "read_causes", return_value=dict(EMPTY_STATE)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return h.sweep_live(shape_of=lambda p: None,
                            link_of=lambda p: f"/supervisor:jump {p}")

    def test_a_session_with_no_attention_record_still_renders(self):
        sweep = self._sweep_live()
        names = [r["name"] for r in sweep["parked"] + sweep["not_parked"]]
        self.assertIn("Silent Worker", names)

    def test_the_supplemented_session_carries_its_state(self):
        sweep = self._sweep_live()
        row = {r["name"]: r for r in sweep["not_parked"]}["Silent Worker"]
        self.assertEqual(h.STATE_WORKING, row["state"])
        self.assertEqual("/supervisor:jump 402", row["jump"])

    def test_the_calling_session_is_not_added_back(self):
        # `load()` drops the caller because its gate is already in its own pane;
        # the registry supplement must not undo that through a second door.
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s-silent"}):
            sweep = self._sweep_live()
        names = [r["name"] for r in sweep["parked"] + sweep["not_parked"]]
        self.assertNotIn("Silent Worker", names)


if __name__ == "__main__":
    unittest.main()
