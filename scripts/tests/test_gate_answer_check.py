#!/usr/bin/env python3
"""Fixtures for `scripts/gate-answer-check.py` — every refusal branch, and the two answerable ones.

The script is the mechanical gate in front of `agents/gate-answerer.md`, so each branch here is
one reason a manager must NOT answer a headless card. A branch that silently passed would let a
background sub-agent allow a gate nobody can justify.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gac = _load("gate_answer_check", "gate-answer-check.py")
tier = _load("permission_tier_for_gac", "permission-tier.py")
opc = _load("own_park_check_for_gac", "own-park-check.py")

SELF = "7253a6aa-0000-4000-8000-000000000001"
OTHER = "3052a109-94b8-4984-96e5-d1d09721466d"
WORKER = "7619ab7a-9344-4165-81da-d604cba38918"
ITEM = "00e661592dd0d8d18b4e0aa96361867e"
TASK = "/vault/25 Tasks/Do The Thing.md"


def card(payload="Bash: ls -la /tmp", **over):
    base = {"item_id": ITEM, "state": "open", "answer_mechanism": "permission",
            "producer_id": WORKER, "payload": payload}
    base.update(over)
    return base


def session(command="ls -la /tmp", tool="Bash", kind="permission", **over):
    inp = {"command": command} if tool == "Bash" else {"file_path": command, "content": "x"}
    base = {"id": WORKER, "cwd": "/work/repo", "mode": "headless",
            "question": {"request_id": "req-1", "kind": kind, "tool_name": tool, "input": inp}}
    base.update(over)
    return base


def run(item=None, ledger=None, claims=None, holds=None, sessions=None, task_files=None, self_id=SELF):
    return gac.decide(
        ITEM,
        card() if item is None else item,
        self_id,
        {WORKER: {"spawner_session_id": SELF}} if ledger is None else ledger,
        claims or {},
        {} if holds is None else holds,
        [session()] if sessions is None else sessions,
        [TASK] if task_files is None else task_files,
        tier,
        tier.load_allowlist(),
        opc,
    )


class Answerable(unittest.TestCase):
    def test_allowlist_tier_own_worker(self):
        out = run()
        self.assertEqual(out["verdict"], gac.ANSWERABLE)
        self.assertEqual(out["tier"], "allowlist")
        self.assertEqual(out["task_file"], TASK)
        self.assertEqual(out["cwd"], "/work/repo")

    def test_manager_tier_write(self):
        out = run(item=card(payload="Write: /work/repo/x.md"),
                  sessions=[session(command="/work/repo/x.md", tool="Write")])
        self.assertEqual(out["verdict"], gac.ANSWERABLE)
        self.assertEqual(out["tier"], "manager")
        self.assertEqual(out["file_path"], "/work/repo/x.md")

    def test_adopted_by_claim(self):
        out = run(ledger={WORKER: {"spawner_session_id": OTHER}}, claims={WORKER: SELF})
        self.assertEqual(out["verdict"], gac.ANSWERABLE)
        self.assertIn("ownership claim", out["why"])


class Escalate(unittest.TestCase):
    def assertEscalate(self, out, fragment):
        self.assertEqual(out["verdict"], gac.ESCALATE, out)
        self.assertIn(fragment, out["why"])

    def test_unclassed_message_card(self):
        self.assertEscalate(run(item=card(answer_mechanism="message")), "not a pick")

    def test_ack_card(self):
        self.assertEscalate(run(item=card(answer_mechanism="ack")), "not a permission or pick card")

    def test_no_producer(self):
        self.assertEscalate(run(item=card(producer_id="")), "no producing session")

    def test_held_session(self):
        self.assertEscalate(run(holds={WORKER: {"reason": "x"}}), "operator hold")

    def test_unreadable_holds(self):
        out = gac.decide(ITEM, card(), SELF, {WORKER: {"spawner_session_id": SELF}}, {}, None,
                         [session()], [TASK], tier, [], opc)
        self.assertEscalate(out, "hold store unreadable")

    def test_other_spawner(self):
        self.assertEscalate(run(ledger={WORKER: {"spawner_session_id": OTHER}}), "not this session's worker")

    def test_claim_by_peer_is_not_ownership(self):
        self.assertEscalate(run(ledger={WORKER: {"spawner_session_id": OTHER}}, claims={WORKER: OTHER}),
                            "not this session's worker")

    def test_unreadable_ledger(self):
        out = gac.decide(ITEM, card(), SELF, None, {}, {}, [session()], [TASK], tier, [], opc)
        self.assertEscalate(out, "spawn ledger unreadable")

    def test_operator_tier_from_full_input(self):
        # The card payload reads harmless; the wrapper's full input does not. Stricter wins.
        out = run(sessions=[session(command="ls /tmp && git push --force origin feat/x")])
        self.assertEscalate(out, "operator tier")

    def test_operator_tier_from_payload(self):
        out = run(item=card(payload="Bash: rm -rf /Users/x"))
        self.assertEscalate(out, "operator tier")

    def test_wrapper_question_kind(self):
        self.assertEscalate(run(sessions=[session(kind="question")]), "not a permission")

    def test_no_brief(self):
        self.assertEscalate(run(task_files=[]), "brief unreadable")

    def test_two_briefs(self):
        self.assertEscalate(run(task_files=[TASK, TASK + "2"]), "brief unreadable")

    def test_vault_walk_failed(self):
        out = gac.decide(ITEM, card(), SELF, {WORKER: {"spawner_session_id": SELF}}, {}, {},
                         [session()], None, tier, [], opc)
        self.assertEscalate(out, "vault pages could not be walked")


class Skip(unittest.TestCase):
    def assertSkip(self, out, fragment):
        self.assertEqual(out["verdict"], gac.SKIP, out)
        self.assertIn(fragment, out["why"])

    def test_store_unreadable(self):
        out = gac.decide(ITEM, None, SELF, {}, {}, {}, [], [], tier, [], opc)
        self.assertSkip(out, "store unreadable")

    def test_not_found(self):
        out = gac.decide(ITEM, {}, SELF, {}, {}, {}, [], [], tier, [], opc)
        self.assertSkip(out, "not a store item")

    def test_answered_card(self):
        self.assertSkip(run(item=card(state="answered")), "card not open")

    def test_wrapper_unreadable(self):
        out = gac.decide(ITEM, card(), SELF, {WORKER: {"spawner_session_id": SELF}}, {}, {},
                         None, [TASK], tier, [], opc)
        self.assertSkip(out, "wrapper session list unreadable")

    def test_no_wrapper_session(self):
        self.assertSkip(run(sessions=[]), "holds no session")

    def test_no_parked_question(self):
        self.assertSkip(run(sessions=[session(question=None)]), "no parked question")


def pick(options=None, payload="pick — 1. split the helper (recommended) · 2. inline it", **over):
    base = {"item_id": ITEM, "state": "open", "answer_mechanism": "message",
            "interrupt_class": "pick", "producer_kind": "session", "producer_id": WORKER,
            "payload": payload, "context": "",
            "options": options if options is not None else [
                {"label": "split the helper", "recommended": True},
                {"label": "inline it", "recommended": False},
                {"label": "leave it unchanged", "recommended": False}]}
    base.update(over)
    return base


class Pick(unittest.TestCase):
    def assertVerdict(self, out, verdict, fragment=""):
        self.assertEqual(out["verdict"], verdict, out)
        self.assertIn(fragment, out["why"])

    def test_recommended_pick_is_answerable(self):
        # A pick needs no wrapper park — the controller delivers to tab and headless alike.
        out = run(item=pick(), sessions=[])
        self.assertVerdict(out, gac.ANSWERABLE)
        self.assertEqual(out["kind"], "pick")
        self.assertEqual(out["recommended"], "split the helper")
        self.assertEqual(out["task_file"], TASK)

    def test_recommended_marked_in_label(self):
        out = run(item=pick(options=[{"label": "a (recommended — small)"}, {"label": "b"}]))
        self.assertEqual(out["recommended"], "a (recommended — small)")

    def test_no_recommended_option(self):
        self.assertVerdict(run(item=pick(options=[{"label": "a"}, {"label": "b"}])),
                           gac.ESCALATE, "no single recommended option")

    def test_two_recommended_options(self):
        opts = [{"label": "a", "recommended": True}, {"label": "b", "recommended": True}]
        self.assertVerdict(run(item=pick(options=opts)), gac.ESCALATE, "no single recommended")

    def test_operator_only_classes_are_left(self):
        for text, cls in (("deploy it to prod", "prod/deploy"), ("merge the PR", "merge/release"),
                          ("delete the branch", "delete"), ("force push the fix", "force-push"),
                          ("rotate the API key", "secrets"), ("close the live trade", "live-trade"),
                          ("ack the TDR", "tdr"), ("approve: make apply", "prod/deploy")):
            out = run(item=pick(payload="pick — 1. %s (recommended)" % text))
            self.assertVerdict(out, gac.ESCALATE, cls)

    def test_deny_class_in_an_option_label(self):
        opts = [{"label": "release v1.2", "recommended": True}, {"label": "wait"}]
        self.assertVerdict(run(item=pick(options=opts, payload="what next?")),
                           gac.ESCALATE, "merge/release")

    def test_approve_class_is_not_a_pick(self):
        self.assertVerdict(run(item=pick(interrupt_class="approve")), gac.ESCALATE, "not a pick")

    def test_cron_pick_is_left(self):
        self.assertVerdict(run(item=pick(producer_kind="cron")), gac.ESCALATE, "not a worker session")

    def test_hook_menu_is_left(self):
        self.assertVerdict(run(item=pick(provenance_class="hook")), gac.ESCALATE, "hook menu")

    def test_foreign_worker_pick_is_left(self):
        self.assertVerdict(run(item=pick(), ledger={WORKER: {"spawner_session_id": OTHER}}),
                           gac.ESCALATE, "not this session's worker")

    def test_held_worker_pick_is_left(self):
        self.assertVerdict(run(item=pick(), holds={WORKER: {}}), gac.ESCALATE, "operator hold")

    def test_pick_without_brief_is_left(self):
        self.assertVerdict(run(item=pick(), task_files=[]), gac.ESCALATE, "brief unreadable")


class AnswerArgv(unittest.TestCase):
    def test_permission_answers_allow(self):
        argv = gac.answer_argv(run())
        self.assertEqual(argv[-2:], ["--decision", "allow"])
        self.assertIn("supervisor:gate-answerer", argv)

    def test_pick_answers_in_the_managers_voice(self):
        argv = gac.answer_argv(run(item=pick()))
        i = argv.index("--answer-text")
        self.assertEqual(argv[i + 1], "Manager answer, via supervisor: split the helper")
        self.assertEqual(argv[-2:], ["--answerer", "manager"])
        self.assertNotIn("--decision", argv)

    def test_nothing_but_answerable_gets_an_argv(self):
        self.assertIsNone(gac.answer_argv(run(holds={WORKER: {}})))
        self.assertIsNone(gac.answer_argv(run(item=card(state="answered"))))

    def test_answer_mode_takes_one_item(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            gac.main(["--self", SELF, "--item", ITEM, "--item", ITEM, "--answer"])
        self.assertEqual(ctx.exception.code, 2)


class Helpers(unittest.TestCase):
    def test_stricter(self):
        a = {"tier": "manager"}
        b = {"tier": "operator"}
        self.assertIs(gac.stricter(a, b), b)
        self.assertIs(gac.stricter(b, a), b)
        self.assertIs(gac.stricter({"tier": "allowlist"}, a), a)

    def test_command_cap_flags_truncation(self):
        out = run(sessions=[session(command="echo " + "a" * (gac.COMMAND_CAP + 10))],
                  item=card(payload="Bash: echo a"))
        self.assertTrue(out["command_truncated"])
        self.assertEqual(len(out["command"]), gac.COMMAND_CAP)

    def test_read_holds(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "none.json")
            self.assertEqual(gac.read_holds(missing), {})
            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as fh:
                fh.write("{not json")
            self.assertIsNone(gac.read_holds(bad))
            good = os.path.join(d, "good.json")
            with open(good, "w") as fh:
                json.dump({"version": 1, "holds": {WORKER: {"reason": "r"}}}, fh)
            self.assertIn(WORKER, gac.read_holds(good))

    def test_usage_error_without_self(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            gac.main(["--item", ITEM])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
