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
        self.shapes = {}
        self.posted = set()

    def task(self, rel, body):
        path = os.path.join(self.vault, rel + ".md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return rel

    def add(self, sid, pane, closer, *, status="waiting", name=None, role=None,
            task=None, kind="question", state="open", ts=1000, event=None,
            transcript=None):
        text = _transcript(closer, task) if transcript is None else transcript
        self.records.append({
            "session_id": sid, "pane": str(pane), "kind": kind,
            "detail": closer or "", "state": state, "ts": ts, "role": role,
            "item_id": f"item-{sid}", "session_name": name or "",
            "_transcript": text,
        })
        self.registry[sid] = name or ""
        self.status[sid] = status
        return sid

    def pmap(self):
        return {str(r["pane"]): {"title": self.registry.get(r["session_id"], "")}
                for r in self.records}

    def reg_records(self):
        return {sid: {"status": st, "name": self.registry.get(sid, "")}
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


if __name__ == "__main__":
    unittest.main()
