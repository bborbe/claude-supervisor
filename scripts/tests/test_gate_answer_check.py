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
from unittest import mock

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


PARKED_AT = "2026-11-18T21:04:27.246044+01:00"
CARD_AT = "2026-11-18T20:04:28.100000Z"


def card(payload="Bash: ls -la /tmp", **over):
    base = {"item_id": ITEM, "state": "open", "answer_mechanism": "permission",
            "producer_id": WORKER, "payload": payload, "created_at": CARD_AT}
    base.update(over)
    return base


def session(command="ls -la /tmp", tool="Bash", kind="permission", **over):
    inp = {"command": command} if tool == "Bash" else {"file_path": command, "content": "x"}
    base = {"id": WORKER, "cwd": "/work/repo", "mode": "headless",
            "question": {"request_id": "req-1", "kind": kind, "tool_name": tool, "input": inp,
                         "requested_at": PARKED_AT}}
    base.update(over)
    return base


def run(item=None, ledger=None, claims=None, holds=None, sessions=None, task_files=None, self_id=SELF,
        **kw):
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
        **kw,
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

    def test_adopted_dead_spawner(self):
        # Spawner is NOT in the live set → a live manager auto-adopts (operator-approved
        # 2026-11-19, card 0a5572c2fcb2edae48e00277b277cb55), rubric-limited.
        out = run(ledger={WORKER: {"spawner_session_id": OTHER}}, live=set())
        self.assertEqual(out["verdict"], gac.ANSWERABLE, out)
        self.assertIn("spawner", out["why"])
        self.assertIn("not live", out["why"])


class Escalate(unittest.TestCase):
    def assertEscalate(self, out, fragment):
        self.assertEqual(out["verdict"], gac.ESCALATE, out)
        self.assertIn(fragment, out["why"])

    @mock.patch.object(gac, "PICK_ANSWERING_ENABLED", True)
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

    def test_live_spawner_still_escalates(self):
        # Spawner IS live → never adopted; the spawner owns its worker.
        self.assertEscalate(run(ledger={WORKER: {"spawner_session_id": OTHER}}, live={OTHER}),
                            "not this session's worker")

    def test_unreadable_live_fails_closed(self):
        # Registry unreadable (live=None) → escalate, never adopt.
        self.assertEscalate(run(ledger={WORKER: {"spawner_session_id": OTHER}}, live=None),
                            "not this session's worker")

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


class PickEnabled(unittest.TestCase):
    def test_kill_switch_is_on(self):
        self.assertTrue(gac.PICK_ANSWERING_ENABLED)


@mock.patch.object(gac, "PICK_ANSWERING_ENABLED", False)
class PickDisabled(unittest.TestCase):
    def test_every_pick_is_skipped_while_the_kill_switch_is_off(self):
        out = run(item=pick())
        self.assertEqual(out["verdict"], gac.SKIP)
        self.assertEqual(out["why"], "pick answering disabled by the PICK_ANSWERING_ENABLED kill switch")

    def test_disabled_pick_gets_no_answer_argv(self):
        verdict = dict(run(item=pick()), verdict=gac.ANSWERABLE, recommended="x")
        self.assertIsNone(gac.answer_argv(verdict))


@mock.patch.object(gac, "PICK_ANSWERING_ENABLED", True)
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
                          ("ack the TDR", "tdr"), ("approve: make apply", "prod/deploy"),
                          ("approve the plan", "approve"), ("push the branch", "push"),
                          ("comment on the PR", "outbound"), ("email the vendor", "outbound"),
                          ("reply to the thread", "outbound"), ("send the summary", "outbound"),
                          ("kubectl scale it", "runtime"), ("launchctl kickstart", "runtime"),
                          ("restart the service", "runtime"), ("kill the worker", "runtime"),
                          ("abort the task", "disposition"), ("complete the task", "disposition"),
                          ("put it on hold", "disposition"), ("defer to Monday", "disposition")):
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


@mock.patch.object(gac, "PICK_ANSWERING_ENABLED", True)
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
            gac.main(["--self", SELF, "--item", ITEM, "--item", ITEM, "--answer", "--expect", "x"])
        self.assertEqual(ctx.exception.code, 2)

    def test_by_overrides_the_recorded_provenance(self):
        argv = gac.answer_argv(run(), by="supervisor:gate-preanswer")
        self.assertEqual(argv[argv.index("--by") + 1], "supervisor:gate-preanswer")

    def test_by_outside_the_supervisor_namespace_is_refused(self):
        for by in ("operator", "supervisor", "Manager answer, via supervisor:", ""):
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                gac.main(["--self", SELF, "--item", ITEM, "--by", by])
            self.assertEqual(ctx.exception.code, 2, by)


class Identity(unittest.TestCase):
    def test_matching_env_is_the_identity(self):
        self.assertEqual(gac.resolve_self(SELF, {"CLAUDE_CODE_SESSION_ID": SELF}), (SELF, None))

    def test_forged_self_is_refused(self):
        sid, why = gac.resolve_self(SELF, {"CLAUDE_CODE_SESSION_ID": OTHER})
        self.assertIsNone(sid)
        self.assertIn("does not match", why)

    def test_missing_env_is_refused(self):
        sid, why = gac.resolve_self(SELF, {})
        self.assertIsNone(sid)
        self.assertIn("unset", why)

    def test_main_refuses_a_forged_self_before_reading_anything(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": OTHER}), \
             mock.patch.object(gac, "load_inputs", side_effect=AssertionError("read")), \
             contextlib.redirect_stdout(out):
            rc = gac.main(["--self", SELF, "--item", ITEM])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", out.getvalue())

    def test_role_human_is_left(self):
        out = run(ledger={WORKER: {"spawner_session_id": SELF, "role": "human"}})
        self.assertEqual(out["verdict"], gac.ESCALATE)
        self.assertIn("role: human", out["why"])


class Tools(unittest.TestCase):
    def test_non_unattended_tools_are_left(self):
        for tool in ("mcp__supervisor__spawn_agent", "mcp__supervisor__set_agent_permission_mode",
                     "mcp__atlassian__createJiraIssue", "WebFetch", "Agent"):
            sess = session(tool="Bash")
            sess["question"]["tool_name"] = tool
            out = run(item=card(payload="%s: {}" % tool), sessions=[sess])
            self.assertEqual(out["verdict"], gac.ESCALATE, tool)
            self.assertIn("tool not unattended-answerable", out["why"])

    def test_unattended_tools_pass_the_gate(self):
        for tool in gac.UNATTENDED_TOOLS:
            sess = session(command="/work/repo/x.md", tool="Write")
            sess["question"]["tool_name"] = tool
            out = run(item=card(payload="%s: /work/repo/x.md" % tool), sessions=[sess])
            self.assertNotIn("tool not unattended", out["why"], tool)


class Freshness(unittest.TestCase):
    def test_missing_card_timestamp(self):
        item = card()
        del item["created_at"]
        out = run(item=item)
        self.assertEqual(out["verdict"], gac.ESCALATE)
        self.assertIn("freshness unverifiable", out["why"])

    def test_missing_park_timestamp(self):
        sess = session()
        del sess["question"]["requested_at"]
        self.assertIn("freshness unverifiable", run(sessions=[sess])["why"])

    def test_card_older_than_the_park(self):
        out = run(item=card(created_at="2026-11-18T19:00:00Z"))
        self.assertEqual(out["verdict"], gac.ESCALATE)
        self.assertIn("predates the park", out["why"])


class Strictness(unittest.TestCase):
    def test_unknown_tier_reads_as_operator(self):
        fake = mock.Mock(wraps=tier)
        fake.tier_for.return_value = {"tier": "weird", "reason": "?"}
        fake.tier_for_payload.return_value = {"tier": "manager", "reason": "m"}
        out = gac.decide(ITEM, card(), SELF, {WORKER: {"spawner_session_id": SELF}}, {}, {},
                         [session()], [TASK], fake, [], opc)
        self.assertEqual(out["verdict"], gac.ESCALATE)
        self.assertIn("unknown tier", out["why"])

    def test_truncated_bash_is_left(self):
        out = run(sessions=[session(command="echo " + "a" * (gac.COMMAND_CAP + 10))],
                  item=card(payload="Bash: echo a"))
        self.assertEqual(out["verdict"], gac.ESCALATE)
        self.assertIn("truncated", out["why"])

    def test_script_files_are_left(self):
        for cmd in ("bash x.sh", "sh ./run.sh", "python3 tool.py", "python3 -m pytest", "./x",
                    "cd /work/repo && ./build", "node scripts/a.mjs", "env FOO=1 python3 f.py",
                    "ls && /work/repo/scripts/do.sh", "echo $(python3 g.py)", "python3 - <<'EOF'\nprint(1)\nEOF"):
            out = run(sessions=[session(command=cmd)], item=card(payload="Bash: " + cmd))
            self.assertEqual(out["verdict"], gac.ESCALATE, cmd)
            self.assertIn("script file", out["why"], cmd)

    def test_inline_programs_and_plain_commands_are_not_script_files(self):
        for cmd in ("ls -la /tmp", "bash -c 'ls -la /tmp'", "git -C /work/repo status", "jq . f.json",
                    "/usr/bin/git log", "xargs -n 1 grep foo", "find . -name '*.md'"):
            self.assertFalse(gac.runs_script_file(cmd, tier), cmd)


class ScriptBypasses(unittest.TestCase):
    """Round 2: each of these hid a script file from the first cut of `runs_script_file`."""

    def test_bypasses_are_caught(self):
        for cmd in ("bash -c './evil.sh'", "sh -c 'sh x.sh'", "ls; bash -c ./x.sh",
                    "python3 -c 'exec(open(\"f.py\").read())'", "python3 -c 'import evil'",
                    "node -e 'require(\"./x\")'", "bash<x.sh", "xargs bash < list",
                    "xargs -I{} sh {}", "find . -name '*.sh' -exec sh {} \\;", "find . -execdir ./x \\;",
                    "env -S 'python3 f.py'", "uv run tool.py", "npx some-tool",
                    "git -c alias.x='!sh x.sh' x", "git -c core.pager='!./p' log"):
            self.assertTrue(gac.runs_script_file(cmd, tier), cmd)

    def test_bypass_escalates_end_to_end(self):
        cmd = "ls; bash -c ./x.sh"
        out = run(sessions=[session(command=cmd)], item=card(payload="Bash: " + cmd))
        self.assertEqual(out["verdict"], gac.ESCALATE)


class Confinement(unittest.TestCase):
    def test_non_uuid_self_is_refused(self):
        sid, why = gac.resolve_self("../../settings", {"CLAUDE_CODE_SESSION_ID": "../../settings"})
        self.assertIsNone(sid)
        self.assertIn("not a session id", why)

    def test_ledger_path_rejects_a_path_like_key(self):
        with self.assertRaises(ValueError):
            gac.ledger_path("../../settings")

    def test_traversal_self_writes_nothing(self):
        root = tempfile.mkdtemp()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "../../settings"}), \
             mock.patch.object(gac, "LEDGER_DIR", os.path.join(root, "a", "b")), \
             mock.patch.object(gac, "load_inputs", side_effect=AssertionError("read")), \
             contextlib.redirect_stdout(out):
            rc = gac.main(["--self", "../../settings", "--item", ITEM])
        self.assertEqual(rc, 2)
        self.assertEqual(os.listdir(root), [])

    def test_env_override_outside_state_is_ignored(self):
        with mock.patch.dict(os.environ, {"X_DIR": "/Users/someone/.claude"}):
            self.assertEqual(gac.confined("X_DIR", "/default"), "/default")
        inside = os.path.join(gac.STATE_ROOT, "gate-answer-test")
        with mock.patch.dict(os.environ, {"X_DIR": inside}):
            self.assertEqual(gac.confined("X_DIR", "/default"), inside)


class CardAndPark(unittest.TestCase):
    def test_card_tool_must_match_the_park(self):
        out = run(item=card(payload="Read: /work/repo/x"))
        self.assertEqual(out["verdict"], gac.ESCALATE)
        self.assertIn("card names tool Read but the park is Bash", out["why"])

    def test_write_outside_cwd_and_tmp_is_left(self):
        for path in ("/Users/x/.zshrc", "~/.claude/settings.json", "/etc/hosts", "../sibling/x.md"):
            out = run(item=card(payload="Write: " + path), sessions=[session(command=path, tool="Write")])
            self.assertEqual(out["verdict"], gac.ESCALATE, path)

    def test_write_inside_cwd_or_tmp_passes_the_gate(self):
        for path in ("/work/repo/x.md", "sub/y.md", "/tmp/scratch.md"):
            out = run(item=card(payload="Write: " + path), sessions=[session(command=path, tool="Write")])
            self.assertEqual(out["verdict"], gac.ANSWERABLE, (path, out["why"]))

    def test_write_with_unknown_cwd_is_left(self):
        sess = session(command="/work/repo/x.md", tool="Write", cwd="")
        out = run(item=card(payload="Write: /work/repo/x.md"), sessions=[sess])
        self.assertIn("cwd unknown", out["why"])

    def test_truncated_write_content_is_left(self):
        sess = session(command="/work/repo/x.md", tool="Write")
        sess["question"]["input"]["content"] = "x" * (gac.COMMAND_CAP + 1)
        out = run(item=card(payload="Write: /work/repo/x.md"), sessions=[sess])
        self.assertEqual(out["verdict"], gac.ESCALATE)
        self.assertIn("truncated", out["why"])


class Dedup(unittest.TestCase):
    def test_item_already_checked_is_skipped(self):
        seen = {ITEM: {"state": "checked", "verdict": gac.ESCALATE}}
        out = run(seen=seen)
        self.assertEqual(out["verdict"], gac.SKIP)
        self.assertIn("already handled", out["why"])

    def test_answer_requires_a_prior_answerable_check(self):
        out = run(answering=True, expect="x")
        self.assertEqual(out["verdict"], gac.SKIP)
        self.assertIn("not checked answerable", out["why"])
        left = run(answering=True, expect="x", seen={ITEM: {"state": "checked", "verdict": gac.ESCALATE}})
        self.assertIn("not checked answerable", left["why"])

    def test_answer_with_matching_expect(self):
        first = run()
        seen = {ITEM: {"state": "checked", "verdict": gac.ANSWERABLE, "expect": first["expect"]}}
        self.assertEqual(run(seen=seen, answering=True, expect=first["expect"])["verdict"], gac.ANSWERABLE)

    def test_answer_with_changed_question_is_refused(self):
        first = run()
        seen = {ITEM: {"state": "checked", "verdict": gac.ANSWERABLE}}
        out = run(seen=seen, answering=True, expect=first["expect"],
                  sessions=[session(command="ls -la /work/repo")])
        self.assertEqual(out["verdict"], gac.SKIP)
        self.assertIn("expect mismatch", out["why"])


class EndToEnd(unittest.TestCase):
    """`main()` with every store faked: check, then answer, through the real ledger file."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.inputs = {"tier_mod": tier, "own_mod": opc, "is_human": None,
                       "ledger": {WORKER: {"spawner_session_id": SELF}}, "claims": {}, "holds": {},
                       "sessions": [session()], "allowlist": tier.load_allowlist(),
                       "index": {WORKER: [TASK]}}

    def _main(self, argv, item):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SELF}), \
             mock.patch.object(gac, "LEDGER_DIR", self.dir), \
             mock.patch.object(gac, "load_inputs", return_value=self.inputs), \
             mock.patch.object(gac, "_get_json", return_value=item), \
             mock.patch.object(gac.subprocess, "run", return_value=mock.Mock(returncode=0)) as ran, \
             contextlib.redirect_stdout(out):
            rc = gac.main(argv)
        lines = out.getvalue().splitlines()
        return rc, json.loads(lines[0]), lines, ran

    def test_check_then_answer(self):
        rc, first, _, _ = self._main(["--self", SELF, "--item", ITEM], card())
        self.assertEqual((rc, first["verdict"]), (0, gac.ANSWERABLE))
        rc, _, _, ran = self._main(["--self", SELF, "--item", ITEM, "--answer", "--expect", first["expect"]], card())
        self.assertEqual(rc, 0)
        self.assertIn("--decision", ran.call_args.args[0])
        # Answered once: a second answer, or a re-arm's re-check, does nothing.
        rc, again, _, ran = self._main(["--self", SELF, "--item", ITEM, "--answer", "--expect", first["expect"]], card())
        self.assertEqual(rc, 4)
        ran.assert_not_called()
        _, recheck, _, _ = self._main(["--self", SELF, "--item", ITEM], card())
        self.assertIn("already handled", recheck["why"])

    def test_card_closed_between_check_and_answer_exits_4(self):
        _, first, _, _ = self._main(["--self", SELF, "--item", ITEM], card())
        rc, verdict, lines, ran = self._main(
            ["--self", SELF, "--item", ITEM, "--answer", "--expect", first["expect"]], card(state="answered"))
        self.assertEqual(rc, 4)
        self.assertIn("card not open", verdict["why"])
        self.assertTrue(lines[-1].startswith("NOT_ANSWERED"))
        ran.assert_not_called()

    def test_re_arm_re_emission_is_skipped(self):
        _, first, _, _ = self._main(["--self", SELF, "--item", ITEM], card(payload="Bash: rm -rf /x"))
        self.assertEqual(first["verdict"], gac.ESCALATE)
        _, second, _, _ = self._main(["--self", SELF, "--item", ITEM], card(payload="Bash: rm -rf /x"))
        self.assertEqual(second["verdict"], gac.SKIP)
        self.assertIn("already handled", second["why"])

    def test_answer_without_expect_is_a_usage_error(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            gac.main(["--self", SELF, "--item", ITEM, "--answer"])

    def test_dry_probe_does_not_book(self):
        """`--dry` is the pre-answer probe: repeatable, and it never consumes the card."""
        hard = card(payload="Bash: rm -rf /x")
        _, first, _, _ = self._main(["--self", SELF, "--item", ITEM, "--dry"], hard)
        self.assertEqual(first["verdict"], gac.ESCALATE)
        _, second, _, _ = self._main(["--self", SELF, "--item", ITEM, "--dry"], hard)
        self.assertEqual(second["verdict"], gac.ESCALATE, "a dry probe must not book")
        # The non-dry check still books, so the gate-answerer keeps its dedup.
        self._main(["--self", SELF, "--item", ITEM], hard)
        _, after, _, _ = self._main(["--self", SELF, "--item", ITEM], hard)
        self.assertIn("already handled", after["why"])

    def test_dry_probe_then_check_then_answer(self):
        """The Monitor's sequence: probe, then check (books), then answer."""
        _, probe, _, _ = self._main(["--self", SELF, "--item", ITEM, "--dry"], card())
        self.assertEqual(probe["verdict"], gac.ANSWERABLE)
        _, checked, _, _ = self._main(["--self", SELF, "--item", ITEM], card())
        self.assertEqual(checked["verdict"], gac.ANSWERABLE)
        rc, _, _, ran = self._main(
            ["--self", SELF, "--item", ITEM, "--answer", "--expect", checked["expect"]], card())
        self.assertEqual(rc, 0)
        self.assertIn("--decision", ran.call_args.args[0])

    def test_answer_records_the_named_provenance(self):
        _, first, _, _ = self._main(["--self", SELF, "--item", ITEM], card())
        _, _, _, ran = self._main(
            ["--self", SELF, "--item", ITEM, "--answer", "--expect", first["expect"],
             "--by", "supervisor:gate-preanswer"], card())
        argv = ran.call_args.args[0]
        self.assertEqual(argv[argv.index("--by") + 1], "supervisor:gate-preanswer")


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
