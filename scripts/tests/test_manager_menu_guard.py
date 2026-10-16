#!/usr/bin/env python3
"""Tests for `scripts/manager-menu-guard.py` and its `hooks/hooks.json` wiring.

The guard denies a MANAGER session's `AskUserQuestion` when an option offers a
manager-owned action as a menu choice. Three properties are graded, and **all three
are required** — any one alone is satisfied by a broken gate:

1. **Positive** — a manager session whose option carries a manager-owned verb is
   DENIED (`exit 2`, reason on stderr).
2. **Negative control** — the SAME manager session with an operator-only option
   (a `phase: todo` approval, a production-touching action, a TDR) is ALLOWED.
   ⚠️ Without this, a guard that denied every `AskUserQuestion` from a manager
   would pass case 1 and take the manager's one legitimate channel away with it.
   ⚠️ **Two negative controls, and the second is the load-bearing one:** a menu whose
   options contain no verb at all passes against a predicate that matches any verb
   anywhere, so `OPERATOR_ONLY_OVERLAP_LABELS` — the operator-only menus that DO carry
   a manager-owned word — is asserted separately. That overlap was measured denying two
   legitimate menus on the live `Sentry Agent Manager` session (2026-10-16) under the
   guard's first, bare-word-boundary predicate.
3. **Scope control** — the SAME manager-owned-verb payload from a NON-manager
   session is ALLOWED. ⚠️ Without this, a guard that denied the verb for every
   session would pass cases 1 and 2 while breaking every worker's menus — the
   scope the guard must keep working.

Plus the fail-open cases, which are the difference between a gate and an outage: a
malformed stdin, a missing `session_id`, an unreadable registry and an unresolvable
vault must all ALLOW. A gate that failed closed on a parse error would block every
`AskUserQuestion` in every session that loads this plugin.

⚠️ **The three graded cases run the script as a SUBPROCESS, not by importing it.**
The contract is an exit code and a stream, and a test that called `decide()` would
pass against a script whose `main()` returned 0 — the module-level wiring is exactly
where a hook goes silent.

The manifest half is modelled on `scripts/tests/test_hooks_manifest.py`: the
event → script mapping is pinned, a **negative control** holds that no other event
was wired to this guard, and the referenced script is asserted to exist under
`${CLAUDE_PLUGIN_ROOT}/` — a path typo disables a hook silently, with no error and
nothing to see.

Run: python3 -m unittest discover -s scripts/tests
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
SCRIPT = os.path.join(REPO, "scripts", "manager-menu-guard.py")
MANIFEST = os.path.join(REPO, "hooks", "hooks.json")

# The guard's own manifest entry, and the group that must NOT carry it. The negative
# control is stated as a value rather than inferred so a future edit that adds the
# guard to a second event has to change this line to go green.
GUARD_EVENT = "PreToolUse"
GUARD_SCRIPT = "manager-menu-guard.py"
GUARD_MATCHER = "AskUserQuestion"

# ⚠️ The same verbs the two rule homes carry. Duplicated here ON PURPOSE: a test that
# imported the list would pass against any list, including one an edit widened.
MANAGER_VERBS = (
    "open", "resume", "ready", "file", "repair",
    "create", "relay", "re-scope", "nudge", "abort", "flag", "clear", "retire",
    "disposition", "unlink", "add", "fix", "unpark", "split", "sync", "re-measure",
    "settle",
)

# The manager's one legitimate channel — an operator-only decision. This is the
# negative control's payload and it must never carry a manager-owned verb.
OPERATOR_ONLY_LABELS = (
    "Approve phase: todo",
    "Deploy to production now",
    "Ack the TDR decision",
)

# ⚠️ **The required regression cases, and the reason the predicate is not a bare
# word-boundary match.** Measured 2026-10-16 against the live `Sentry Agent Manager`
# session: both of these are legitimate OPERATOR-ONLY menus, both carry a manager-owned
# verb as an ordinary English word, and a `\b(verb)\b` predicate denied both. Each is
# asserted individually rather than as one list, so a partial fix cannot pass.
OPERATOR_ONLY_OVERLAP_LABELS = (
    ("keep the session open", "leave it unchanged"),   # verb late, not leading
    ("Keep working", "resume tomorrow"),               # verb leading, no owned object
)

# The five verbs in their natural manager-owned phrasing — the positive half of the
# predicate's reachability. ⚠️ A verb that stops matching here has become unreachable,
# which is a silent miss of exactly the action this guard exists to catch.
NATURAL_ACTION_LABELS = {
    "open": "Open the approved row",
    "resume": "Resume the paused Sentry row",
    "ready": "Ready the row",
    "file": "File a follow-up task",
    "repair": "Repair the page box",
    # The 17 widened 2026-10-16 by operator decision. Each is asserted individually, so
    # a regex that happens to match one verb cannot carry the rest of the list.
    "create": "Create a follow-up task",
    "relay": "Relay the answer to the row",
    "re-scope": "Re-scope the stale row",
    "nudge": "Nudge the stuck row",
    "abort": "Abort the stale row",
    "flag": "Flag the stale row",
    "clear": "Clear the blocked row",
    "retire": "Retire the old row",
    "disposition": "Disposition the open rows",
    "unlink": "Unlink the deny row",
    "add": "Add a follow-up task",
    "fix": "Fix the goal page",
    "unpark": "Unpark the stalled row",
    "split": "Split the large task",
    "sync": "Sync the goal list",
    "re-measure": "Re-measure the goal row",
    "settle": "Settle the open task",
}

# ⚠️ **The shape a manager actually uses, and the one the object list alone missed.** A
# manager offering its own work quotes the row's own title — and a real task title carries
# no manager-owned noun (`row` / `task` / `page` / `box` / `goal`), so the object list
# answers none of these. Measured 2026-10-16 by `scripts/manager-menu-probe.py`:
# `Resume "Sentry Agent - Retire the Legacy Gate"` was ALLOWED before the quoted-head
# object source existed — the canonical manager menu, through the gate. The titles are
# fixture-shaped on purpose: what matters is that each is bare.
BARE_TITLE_LABELS = (
    'Resume "Sentry Agent - Retire the Legacy Gate"',
    'Open "Sentry Agent - Adopt the New Menu Rule"',
    'Ready "A Manager Offers Its Own Work"',
)

# The other half of that source. A quote the verb does NOT take as its direct object is
# not a named target, so these stay operator-only — without the head-position test the
# quoted-head source would deny every operator-only option that happened to quote
# something, which is the same class of false positive the overlap cases below record.
QUOTED_BUT_NOT_A_TARGET_LABELS = (
    ('Resume the session — "keep going"', "Stop for today"),
    ('Open questions about "Attention Board Polish"', "Leave it unchanged"),
)


def load_guard():
    spec = importlib.util.spec_from_file_location("manager_menu_guard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    """A fixture fleet: one registry, one vault, one transcript per session."""

    MANAGER_NAME = "Sentry Agent Manager"      # resolves by name, via the page
    COLOUR_MANAGER_NAME = "Sentry Rollout Lead"  # resolves by COLOUR only
    WORKER_NAME = "⚙ Prove the Spawn Ledger End to End"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sessions = os.path.join(self.tmp, "sessions")
        self.obsidian = os.path.join(self.tmp, "obsidian")
        self.vault = os.path.join(self.obsidian, "Personal")
        self.projects = os.path.join(self.tmp, "projects")
        self.state = os.path.join(self.tmp, "state")
        self.cwd = "/tmp/fixture-cwd"
        self.escaped = self.cwd.replace("/", "-").replace(".", "-")
        for d in (self.sessions, os.path.join(self.vault, "23 Topics"),
                  os.path.join(self.vault, "24 Goals"),
                  os.path.join(self.projects, self.escaped), self.state):
            os.makedirs(d, exist_ok=True)
        # The topic page the manager's name resolves to. `Sentry Agent Manager` -> the
        # ` Manager` suffix is stripped, so the page is `Sentry Agent`, not the full name.
        open(os.path.join(self.vault, "23 Topics", "Sentry Agent.md"), "w").close()
        self.register(1, "manager-1", self.MANAGER_NAME, "orange")
        self.register(2, "colour-1", self.COLOUR_MANAGER_NAME, "orange")
        self.register(3, "worker-1", self.WORKER_NAME, "pink")

    def register(self, pid, session_id, name, colour):
        with open(os.path.join(self.sessions, f"{pid}.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "sessionId": session_id, "name": name, "cwd": self.cwd}, fh)
        with open(os.path.join(self.projects, self.escaped, f"{session_id}.jsonl"),
                  "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "text": "x"}) + "\n")
            fh.write(json.dumps({"type": "agent-color", "agentColor": colour}) + "\n")

    def env(self, **overrides):
        env = dict(os.environ)
        env["CLAUDE_SESSIONS_DIR"] = self.sessions
        env["OBSIDIAN_DIR"] = self.obsidian
        env["CLAUDE_PROJECTS_DIR"] = self.projects
        # ⚠️ The record's directory is redirected for EVERY case, so no test can write
        # into the operator's real ~/.claude/state/manager-menu-guard/.
        env["SUPERVISOR_MANAGER_MENU_GUARD_STATE_DIR"] = self.state
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("SUPERVISOR_MANAGER_MENU_GUARD", None)
        env.update(overrides)
        return env

    def run_guard(self, stdin_text, **env_overrides):
        """`(exit code, stderr)` for one hook invocation."""
        proc = subprocess.run(
            [sys.executable, SCRIPT], input=stdin_text, env=self.env(**env_overrides),
            capture_output=True, text=True, timeout=60,
        )
        return proc.returncode, proc.stderr

    def records(self, session_id):
        """Every record written for one session, oldest first — the reader's own path."""
        path = os.path.join(self.state, f"{session_id}.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def last_record(self, session_id):
        found = self.records(session_id)
        self.assertTrue(found, f"no record was written for {session_id!r}")
        return found[-1]

    @staticmethod
    def payload(session_id, labels):
        body = {"session_id": session_id, "tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{"question": "Which?", "multiSelect": False,
                                              "options": [{"label": l, "description": "d"}
                                                          for l in labels]}]}}
        return json.dumps(body)


class TestGradedCases(Base):
    """The three cases the brief requires. All three, in one class, because each is
    the other's control — splitting them across files is how one gets deleted."""

    def test_positive_manager_with_a_manager_owned_verb_is_denied(self):
        code, err = self.run_guard(self.payload("manager-1", ["Open the approved row",
                                                              "Leave it alone"]))
        self.assertEqual(code, 2, f"a manager offering a manager-owned verb must be denied; stderr={err!r}")
        self.assertIn("DENIED", err)
        self.assertIn("open", err)
        # The reason names the action to execute instead, not merely the refusal.
        self.assertIn("/supervisor:open", err)
        self.assertIn("commands/manager-loop.md", err)

    def test_positive_every_manager_owned_verb_is_reachable_in_its_natural_phrasing(self):
        # ⚠️ One verb per call, in the phrasing the rule homes actually use, so a regex
        # that happens to match `open` cannot carry the whole list. The list is asserted
        # against a literal, never against the module's own constant.
        self.assertEqual(set(NATURAL_ACTION_LABELS), set(MANAGER_VERBS))
        for verb, label in NATURAL_ACTION_LABELS.items():
            with self.subTest(verb=verb):
                code, err = self.run_guard(self.payload("manager-1", [label]))
                self.assertEqual(code, 2, f"{label!r} must be denied; stderr={err!r}")
                self.assertIn(verb, err)

    def test_negative_control_operator_only_options_are_allowed(self):
        code, err = self.run_guard(self.payload("manager-1", list(OPERATOR_ONLY_LABELS)))
        self.assertEqual(code, 0, f"an operator-only menu must survive the gate; stderr={err!r}")
        self.assertEqual(err, "")

    def test_negative_control_operator_only_options_carrying_a_verb_word_are_allowed(self):
        # ⚠️ **THE REGRESSION CASE.** The control above passes against a predicate that
        # matches any verb anywhere, because none of its labels contains one — the
        # overlap is what exercises the defect, and it was measured denying both of
        # these on the live `Sentry Agent Manager` session.
        for labels in OPERATOR_ONLY_OVERLAP_LABELS:
            with self.subTest(labels=labels):
                code, err = self.run_guard(self.payload("manager-1", list(labels)))
                self.assertEqual(code, 0, f"{labels} is an operator-only menu; stderr={err!r}")
                self.assertEqual(err, "")

    def test_scope_control_the_same_payload_from_a_non_manager_is_allowed(self):
        # ⚠️ The SAME labels as the positive case, deliberately: a scope control with a
        # different payload would pass against a guard that keyed on the payload.
        code, err = self.run_guard(self.payload("worker-1", ["Open the approved row"]))
        self.assertEqual(code, 0, f"a worker's menu must keep working; stderr={err!r}")
        self.assertEqual(err, "")

    def test_scope_control_a_worker_with_every_natural_action_label_is_allowed(self):
        code, err = self.run_guard(self.payload("worker-1", list(NATURAL_ACTION_LABELS.values())))
        self.assertEqual(code, 0, f"a worker's menu must keep working; stderr={err!r}")
        self.assertEqual(err, "")


class TestQuotedTitleObjectSource(Base):
    """The object source that catches a manager quoting a row's own title.

    ⚠️ Every label in the positive half carries NO manager-owned noun, so the object list
    cannot answer any of them — that is what makes this class the source's own test rather
    than a restatement of the graded cases above. All three were ALLOWED before the source
    existed, and `scripts/manager-menu-probe.py` is what found them.
    """

    def test_a_manager_quoting_a_bare_title_is_denied(self):
        for label in BARE_TITLE_LABELS:
            with self.subTest(label=label):
                code, err = self.run_guard(
                    self.payload("manager-1", [label, "Leave it closed"]))
                self.assertEqual(code, 2,
                                 f"{label!r} names a manager-owned action; stderr={err!r}")
                self.assertIn("DENIED", err)

    def test_a_quote_the_verb_does_not_take_as_its_object_stays_operator_only(self):
        for labels in QUOTED_BUT_NOT_A_TARGET_LABELS:
            with self.subTest(labels=labels):
                code, err = self.run_guard(self.payload("manager-1", list(labels)))
                self.assertEqual(code, 0,
                                 f"{labels} is an operator-only menu; stderr={err!r}")
                self.assertEqual(err, "")

    def test_the_same_quoted_title_from_a_non_manager_is_allowed(self):
        # ⚠️ The SAME label as the positive half, deliberately — a scope control with a
        # different payload would pass against a guard that keyed on the payload.
        code, err = self.run_guard(self.payload("worker-1", [BARE_TITLE_LABELS[0]]))
        self.assertEqual(code, 0, f"a worker's menu must keep working; stderr={err!r}")
        self.assertEqual(err, "")

    def test_the_record_names_the_verb_for_a_quoted_title(self):
        # The record is what SC4 reads, so the new source has to reach it too — a deny that
        # wrote no verb would leave the live check unable to say what was attempted.
        self.run_guard(self.payload("manager-1", [BARE_TITLE_LABELS[0]]))
        record = self.last_record("manager-1")
        self.assertEqual(record["decision"], "deny")
        self.assertEqual(record["verbs"], ["resume"])


class TestRecord(Base):
    """The per-invocation record — what makes "the guard DENIED" provable.

    ⚠️ Without a producer these cases have nothing to read: a guard whose only output is
    an exit code cannot be told from one that never ran, so the live check that asks
    "did the guard deny this manager-owned attempt" has no evidence to read at all.
    """

    def test_a_deny_writes_a_deny_record(self):
        self.run_guard(self.payload("manager-1", ["Open the approved row"]))
        rec = self.last_record("manager-1")
        self.assertEqual(rec["decision"], "deny")
        self.assertEqual(rec["reason"], "manager-owned-action")
        self.assertIs(rec["manager"], True)
        self.assertEqual(rec["manager_arm"], "name")
        self.assertEqual(rec["verbs"], ["open"])

    def test_an_allow_writes_an_allow_record(self):
        # ⚠️ The requirement is BOTH, and the allow half is the one that makes a quiet
        # window distinguishable from a broken guard.
        self.run_guard(self.payload("worker-1", ["Open the approved row"]))
        rec = self.last_record("worker-1")
        self.assertEqual(rec["decision"], "allow")
        self.assertEqual(rec["reason"], "non-manager")
        self.assertIs(rec["manager"], False)
        self.assertEqual(rec["verbs"], ["open"])

    def test_an_operator_only_menu_writes_an_allow_record(self):
        # ⚠️ None of these labels even LEADS with a manager-owned verb, so the cheap
        # pre-filter answers and the manager verdict is never evaluated — recorded as
        # `null`, which is a different answer from `false`.
        self.run_guard(self.payload("manager-1", list(OPERATOR_ONLY_LABELS)))
        rec = self.last_record("manager-1")
        self.assertEqual(rec["decision"], "allow")
        self.assertEqual(rec["reason"], "no-manager-action")
        self.assertEqual(rec["verbs"], [])
        self.assertIsNone(rec["manager"])
        self.assertIsNone(rec["manager_arm"])

    def test_a_leading_verb_with_no_owned_object_records_the_manager_verdict(self):
        # `resume tomorrow` leads with a manager-owned verb, so the payload clears the
        # pre-filter and the registry IS read — the verdict is evaluated and recorded,
        # even though no option ended up naming an action.
        self.run_guard(self.payload("manager-1", ["Keep working", "resume tomorrow"]))
        rec = self.last_record("manager-1")
        self.assertEqual(rec["reason"], "no-manager-action")
        self.assertEqual(rec["verbs"], [])
        self.assertIs(rec["manager"], True)
        self.assertEqual(rec["manager_arm"], "name")

    def test_the_record_is_addressable_by_session_id(self):
        # The reader's own path, spelled out rather than via the helper: `<dir>/<sid>.jsonl`.
        self.run_guard(self.payload("manager-1", ["Open the approved row"]))
        path = os.path.join(self.state, "manager-1.jsonl")
        self.assertTrue(os.path.isfile(path), f"expected a per-session file at {path}")
        with open(path, encoding="utf-8") as fh:
            line = json.loads(fh.readline())
        self.assertEqual(line["session_id"], "manager-1")

    def test_each_session_gets_its_own_file(self):
        self.run_guard(self.payload("manager-1", ["Open the approved row"]))
        self.run_guard(self.payload("worker-1", ["Open the approved row"]))
        self.assertEqual([r["session_id"] for r in self.records("manager-1")], ["manager-1"])
        self.assertEqual([r["session_id"] for r in self.records("worker-1")], ["worker-1"])

    def test_the_shape_is_exactly_what_is_documented(self):
        # ⚠️ Pinned against a LITERAL, not against the module's constant: a field added
        # or renamed without the docstring moving is exactly what breaks a reader.
        self.run_guard(self.payload("manager-1", ["Open the approved row"]))
        self.assertEqual(set(self.last_record("manager-1")),
                         {"ts", "session_id", "decision", "reason", "manager",
                          "manager_arm", "verbs"})
        guard = load_guard()
        self.assertEqual(set(guard.RECORD_FIELDS),
                         {"ts", "session_id", "decision", "reason", "manager",
                          "manager_arm", "verbs"})

    def test_the_timestamp_is_iso8601_with_an_offset(self):
        import datetime
        self.run_guard(self.payload("manager-1", ["Open the approved row"]))
        ts = self.last_record("manager-1")["ts"]
        parsed = datetime.datetime.fromisoformat(ts)
        self.assertIsNotNone(parsed.tzinfo, f"{ts!r} must carry an offset")
        self.assertEqual(parsed.utcoffset(), datetime.timedelta(0))

    def test_every_decision_is_from_the_fixed_vocabulary(self):
        guard = load_guard()
        self.run_guard(self.payload("manager-1", ["Open the approved row"]))       # deny
        self.run_guard(self.payload("worker-1", ["Open the approved row"]))        # non-manager
        self.run_guard(self.payload("manager-1", ["Keep working"]))                # no action
        self.run_guard(self.payload("manager-1", ["Open the approved row"]),
                       SUPERVISOR_MANAGER_MENU_GUARD="off")                        # kill switch
        reasons = {r["reason"] for sid in ("manager-1", "worker-1")
                   for r in self.records(sid)}
        self.assertEqual(reasons, {"manager-owned-action", "non-manager",
                                   "no-manager-action", "kill-switch"})
        for sid in ("manager-1", "worker-1"):
            for rec in self.records(sid):
                self.assertIn(rec["decision"], (guard.DECISION_ALLOW, guard.DECISION_DENY))

    def test_the_colour_arm_is_named_in_the_record(self):
        # The record has to say WHICH arm answered — two of the four live managers are
        # colour-only, and a reader that cannot see the arm cannot tell them apart.
        self.run_guard(self.payload("colour-1", ["Resume the row"]))
        rec = self.last_record("colour-1")
        self.assertEqual(rec["decision"], "deny")
        self.assertEqual(rec["manager_arm"], "colour")

    def test_the_root_arm_is_named_in_the_record(self):
        self.register(5, "root-1", "Fleet Manager", "pink")
        self.run_guard(self.payload("root-1", ["Ready the row"]))
        rec = self.last_record("root-1")
        self.assertEqual(rec["manager_arm"], "root")

    def test_the_kill_switch_records_its_own_line(self):
        self.run_guard(self.payload("manager-1", ["Open the approved row"]),
                       SUPERVISOR_MANAGER_MENU_GUARD="off")
        rec = self.last_record("manager-1")
        self.assertEqual(rec["decision"], "allow")
        self.assertEqual(rec["reason"], "kill-switch")

    def test_a_fail_open_path_records_its_cause(self):
        # ⚠️ The record is what separates "the guard allowed this" from "the guard never
        # ran", so a fail-open path has to leave a trace too. Malformed stdin cannot
        # carry a session id, so it lands in the unknown-session file.
        self.run_guard("{not json at all")
        rec = self.last_record("unknown-session")
        self.assertEqual(rec["decision"], "allow")
        self.assertEqual(rec["reason"], "fail-open: malformed-stdin")
        self.assertIsNone(rec["session_id"])

    def test_an_unknown_session_records_its_cause(self):
        self.run_guard(self.payload("no-such-session", ["Open the approved row"]))
        self.assertEqual(self.last_record("no-such-session")["reason"],
                         "fail-open: unknown-session")

    def test_consecutive_invocations_append_in_order(self):
        self.run_guard(self.payload("manager-1", ["Open the approved row"]))
        self.run_guard(self.payload("manager-1", ["Keep working"]))
        self.assertEqual([r["decision"] for r in self.records("manager-1")],
                         ["deny", "allow"])


class TestRecordFailOpen(Base):
    """⚠️ The write can never change the verdict — a lost record beats a wrong verdict."""

    def unwritable_state_dir(self):
        """A state dir whose parent is a regular FILE — unwritable for any user."""
        blocker = os.path.join(self.tmp, "not-a-directory")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        return os.path.join(blocker, "state")

    def test_a_deny_survives_an_unwritable_state_dir(self):
        code, err = self.run_guard(self.payload("manager-1", ["Open the approved row"]),
                                   SUPERVISOR_MANAGER_MENU_GUARD_STATE_DIR=self.unwritable_state_dir())
        self.assertEqual(code, 2, f"a lost record must never flip a deny; stderr={err!r}")
        self.assertIn("DENIED", err)

    def test_an_allow_survives_an_unwritable_state_dir(self):
        code, err = self.run_guard(self.payload("worker-1", ["Open the approved row"]),
                                   SUPERVISOR_MANAGER_MENU_GUARD_STATE_DIR=self.unwritable_state_dir())
        self.assertEqual(code, 0, f"a lost record must never flip an allow; stderr={err!r}")

    def test_write_record_returns_none_rather_than_raising(self):
        guard = load_guard()
        record = {"ts": "x", "session_id": "s", "decision": "deny", "reason": "r",
                  "manager": True, "manager_arm": "name", "verbs": ["open"]}
        self.assertIsNone(guard.write_record(record, directory=self.unwritable_state_dir()))


class TestRecordBound(Base):
    """⚠️ An unbounded append before every `AskUserQuestion` is a slow disk leak."""

    def test_the_file_is_trimmed_to_the_kept_tail(self):
        guard = load_guard()
        path = os.path.join(self.state, "bound.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(500):
                fh.write(json.dumps({"i": i}) + "\n")
        guard.trim_records(path, cap=1000, keep=5)
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[-1]["i"], 499, "the trim must keep the MOST RECENT lines")

    def test_a_file_under_the_cap_is_left_alone(self):
        guard = load_guard()
        path = os.path.join(self.state, "small.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"i": 1}) + "\n")
        before = os.path.getsize(path)
        guard.trim_records(path, cap=guard.RECORD_CAP_BYTES, keep=5)
        self.assertEqual(os.path.getsize(path), before)

    def test_a_missing_file_is_not_an_error(self):
        guard = load_guard()
        guard.trim_records(os.path.join(self.state, "never-written.jsonl"))

    def test_the_bound_holds_across_repeated_writes(self):
        # The integration, not just the unit: through the real `write_record` path, the
        # file stabilises instead of growing with every write. ⚠️ In-process rather than
        # through `run_guard`, because a subprocess re-imports the module and would not
        # see the patched constants.
        #
        # ⚠️ BOTH bounds are patched, because the file is bounded by the LARGER of them:
        # the trim keeps `keep` lines, so patching only the byte cap would leave a file
        # that grows to `keep` lines before anything is dropped — which is correct
        # behaviour, and not what this case is testing.
        guard = load_guard()
        original_cap, original_keep = guard.RECORD_CAP_BYTES, guard.RECORD_KEEP
        try:
            guard.RECORD_CAP_BYTES, guard.RECORD_KEEP = 600, 5
            for _ in range(30):
                guard.write_record({"ts": "2026-10-16T00:00:00+00:00", "session_id": "s-1",
                                    "decision": "deny", "reason": "manager-owned-action",
                                    "manager": True, "manager_arm": "name", "verbs": ["open"]},
                                   directory=self.state)
        finally:
            guard.RECORD_CAP_BYTES, guard.RECORD_KEEP = original_cap, original_keep
        lines = self.records("s-1")
        self.assertGreater(len(lines), 0)
        self.assertLessEqual(len(lines), 6, "the trim must keep the file at `keep` lines")
        self.assertEqual(lines[-1]["decision"], "deny", "the trim must keep the NEWEST lines")
        self.assertLessEqual(os.path.getsize(os.path.join(self.state, "s-1.jsonl")), 1200)


class TestColourArm(Base):
    """Rule 1's second arm, which is load-bearing rather than defensive.

    Measured 2026-10-16: of the four live manager sessions, two resolve by NAME
    (`Sentry Agent Manager`, `Fleet Manager`) and two resolve only by COLOUR
    (`Single Source of Session State Manager`, `⚙ A2A Worker Controller Manager` —
    neither names a topic or a goal page). A name-only guard misses half the fleet.
    """

    def test_a_manager_that_resolves_only_by_colour_is_denied(self):
        code, err = self.run_guard(self.payload("colour-1", ["Resume the row"]))
        self.assertEqual(code, 2, f"an orange session is a manager under rule 1; stderr={err!r}")
        self.assertIn("resume", err)

    def test_the_same_name_without_the_colour_is_allowed(self):
        # The negative control for the arm above: the name resolves to no page, so the
        # colour is the only thing that made it a manager. Without this, a guard that
        # called every unresolved name a manager would pass the case above.
        self.register(4, "colour-2", self.COLOUR_MANAGER_NAME, "pink")
        code, err = self.run_guard(self.payload("colour-2", ["Resume the row"]))
        self.assertEqual(code, 0, f"a pink session is not a manager; stderr={err!r}")

    def test_an_unreadable_transcript_leaves_the_colour_unknown(self):
        # Fail-open: the colour cannot be read, so the session is not PROVEN a manager.
        os.remove(os.path.join(self.projects, self.escaped, "colour-1.jsonl"))
        code, err = self.run_guard(self.payload("colour-1", ["Resume the row"]))
        self.assertEqual(code, 0, f"an unreadable colour must not deny; stderr={err!r}")

    def test_a_colour_record_buried_beyond_the_cap_is_not_found(self):
        # The bounded read's named gap, pinned so it is a decision rather than a
        # surprise: the record is pushed past the cap by filler, and the session then
        # reads as unproven — allow, never a guess.
        guard = load_guard()
        path = os.path.join(self.projects, self.escaped, "colour-1.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("x" * (guard.COLOUR_CAP + guard.COLOUR_WINDOW + 1) + "\n")
        self.assertIsNone(guard.last_colour(path, window=guard.COLOUR_WINDOW, cap=guard.COLOUR_CAP))


class TestRootAndDecoration(Base):
    """The two name arms that are not the page lookup."""

    def test_the_root_session_is_a_manager(self):
        # `Fleet Manager` names no page — it is matched exactly, as `build_grouping` does.
        self.register(5, "root-1", "Fleet Manager", "pink")
        code, err = self.run_guard(self.payload("root-1", ["Ready the row"]))
        self.assertEqual(code, 2, f"the root is a manager whatever its colour; stderr={err!r}")

    def test_a_decorated_manager_name_still_resolves(self):
        # `server/supervisor.mjs` writes `⚙ ${label}` at spawn, so a raw equality test
        # against the page title misses a decorated manager.
        self.register(6, "gear-1", "⚙ Sentry Agent Manager", "pink")
        code, err = self.run_guard(self.payload("gear-1", ["File a follow-up task"]))
        self.assertEqual(code, 2, f"the `⚙ ` decoration must be normalized away; stderr={err!r}")


class TestFailOpen(Base):
    """Every unproven path ALLOWS. This is what keeps the gate from being an outage."""

    def test_malformed_stdin_allows(self):
        code, err = self.run_guard("{not json at all")
        self.assertEqual(code, 0, f"stderr={err!r}")

    def test_empty_stdin_allows(self):
        code, _ = self.run_guard("")
        self.assertEqual(code, 0)

    def test_a_json_scalar_payload_allows(self):
        code, _ = self.run_guard('"a string, not an object"')
        self.assertEqual(code, 0)

    def test_a_missing_session_id_allows(self):
        code, _ = self.run_guard(json.dumps(
            {"tool_input": {"questions": [{"options": [{"label": "Open the row"}]}]}}))
        self.assertEqual(code, 0)

    def test_the_env_session_id_is_the_fallback(self):
        # The documented second source — and the reason `$$` is banned: the fallback is
        # the SESSION's id, which the harness exports, not this hook process's pid.
        body = json.dumps({"tool_input": {"questions": [{"options": [{"label": "Open the row"}]}]}})
        code, err = self.run_guard(body, CLAUDE_CODE_SESSION_ID="manager-1")
        self.assertEqual(code, 2, f"the env fallback must reach the registry; stderr={err!r}")

    def test_an_unknown_session_id_allows(self):
        code, _ = self.run_guard(self.payload("no-such-session", ["Open the row"]))
        self.assertEqual(code, 0)

    def test_an_unreadable_registry_entry_allows(self):
        with open(os.path.join(self.sessions, "1.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        code, _ = self.run_guard(self.payload("manager-1", ["Open the row"]))
        self.assertEqual(code, 0)

    def test_a_missing_registry_directory_allows(self):
        shutil.rmtree(self.sessions)
        code, _ = self.run_guard(self.payload("manager-1", ["Open the row"]))
        self.assertEqual(code, 0)

    def test_an_unresolvable_vault_allows(self):
        code, _ = self.run_guard(self.payload("manager-1", ["Open the row"]),
                                 OBSIDIAN_DIR=os.path.join(self.tmp, "no-such-vault-root"))
        self.assertEqual(code, 0)

    def test_a_payload_with_no_options_allows(self):
        code, _ = self.run_guard(json.dumps(
            {"session_id": "manager-1", "tool_input": {"questions": [{"question": "q"}]}}))
        self.assertEqual(code, 0)

    def test_a_payload_that_is_not_askuserquestion_shaped_allows(self):
        code, _ = self.run_guard(json.dumps(
            {"session_id": "manager-1", "tool_name": "Bash", "tool_input": {"command": "ls"}}))
        self.assertEqual(code, 0)

    def test_the_kill_switch_allows(self):
        code, _ = self.run_guard(self.payload("manager-1", ["Open the row"]),
                                 SUPERVISOR_MANAGER_MENU_GUARD="off")
        self.assertEqual(code, 0)


class TestVerbMatching(Base):
    """The predicate's boundaries — the half a reviewer cannot see from the exit codes.

    ⚠️ Every case here is a **unit** call, not a subprocess: the predicate is what
    changed when the bare word-boundary match was replaced, and a case that went
    through the guard's exit code could not tell which half of it answered.
    """

    def setUp(self):
        super().setUp()
        self.guard = load_guard()

    def actions(self, *labels, titles=()):
        return self.guard.manager_owned_actions_in(
            {"questions": [{"options": [{"label": l} for l in labels]}]}, titles)

    def test_both_halves_are_required(self):
        # ⚠️ **The predicate in one table.** Each row is a way one half alone fails:
        # the verb-late rows were measured denying a legitimate operator-only menu, and
        # the verb-no-object row is the same defect reached from the other side.
        for label, expected in (
            ("Open the approved row", ["open"]),        # leading verb + owned object
            ("Resume the paused Sentry row", ["resume"]),
            ("keep the session open", []),              # verb present, NOT leading
            ("Keep working", []),                       # no manager-owned verb at all
            ("resume tomorrow", []),                    # leading verb, NO owned object
            ("Ready for review", []),                   # leading verb, no owned object
            ("File it under later", []),                # leading verb, no owned object
        ):
            with self.subTest(label=label):
                self.assertEqual(self.actions(label), expected)

    def test_the_required_regression_payloads_are_allowed(self):
        for labels in OPERATOR_ONLY_OVERLAP_LABELS:
            with self.subTest(labels=labels):
                self.assertEqual(self.actions(*labels), [])

    def test_a_leading_enumerator_or_bullet_does_not_hide_the_verb(self):
        # An option label is not prose; `1. ` / `- ` / a backtick is decoration, and the
        # verb still leads. Without the strip the predicate would depend on numbering.
        for label in ("1. Open the approved row", "- Open the approved row",
                      "`Open the approved row`", "• Open the approved row",
                      "1) Open the approved row"):
            with self.subTest(label=label):
                self.assertEqual(self.actions(label), ["open"])

    def test_a_word_boundary_excludes_a_longer_word(self):
        # `profile` carries `file`, `OpenClaw` carries `open`, `Reopened` carries `opened`
        # — none is the verb, and all three are in the leading position.
        for label in ("Profile the row", "OpenClaw is down", "Reopened the row", "Filament row"):
            with self.subTest(label=label):
                self.assertEqual(self.actions(label), [])

    def test_inflections_of_a_listed_verb_are_matched(self):
        for label, verb in (("Opening the approved row", "open"),
                            ("Resumed the paused row", "resume"),
                            ("Filing a follow-up task", "file"),
                            ("Repairing the page box", "repair"),
                            ("Readying the row", "ready")):
            with self.subTest(label=label):
                self.assertEqual(self.actions(label), [verb])

    def test_the_base_verb_is_returned_not_the_surface_form(self):
        # The deny reads as the rule's vocabulary, not as a quotation of the menu.
        self.assertEqual(self.actions("Opening the approved row"), ["open"])

    def test_an_object_beyond_the_window_does_not_match(self):
        # The window's own boundary, pinned so it is a decision: `row` sits past
        # OBJECT_WINDOW characters and the option is then not naming an action.
        guard = self.guard
        far = "Open " + ("x " * ((guard.OBJECT_WINDOW // 2) + 2)) + "row"
        self.assertGreater(len(far), guard.OBJECT_WINDOW + len("Open "))
        self.assertEqual(self.actions(far), [])
        near = "Open " + ("x " * 4) + "row"
        self.assertLessEqual(len(near) - len("Open "), guard.OBJECT_WINDOW)
        self.assertEqual(self.actions(near), ["open"])

    def test_a_vault_page_title_is_an_owned_object(self):
        # The subjects a manager opens, resumes and readies ARE topic and goal titles,
        # and they are already in hand because rule 1 reads them.
        self.assertEqual(self.actions("Resume Sentry Agent", titles=("Sentry Agent",)),
                         ["resume"])
        self.assertEqual(self.actions("Open the A2A Worker Controller",
                                      titles=("A2A Worker Controller",)), ["open"])

    def test_a_title_mid_sentence_is_not_an_owned_object(self):
        # ⚠️ The title arm's own control: `Open questions about <topic>` is an
        # operator-only option that merely mentions a topic, so a title is accepted
        # only at the head of the window.
        self.assertEqual(self.actions("Open questions about Attention Board Polish",
                                      titles=("Attention Board Polish",)), [])

    def test_the_label_only_is_scanned(self):
        # A description is not the choice the manager is offering; scanning it would
        # deny a legitimate operator-only menu that merely mentions a verb.
        payload = {"questions": [{"options": [
            {"label": "Approve phase: todo", "description": "this will open the row"}]}]}
        self.assertEqual(self.guard.manager_owned_actions_in(payload), [])

    def test_actions_are_deduplicated_across_options(self):
        self.assertEqual(self.actions("Open the row", "Open the task"), ["open"])
        self.assertEqual(self.actions("Open the row", "File a task"), ["open", "file"])

    def test_the_verb_list_is_the_two_rule_homes_vocabulary(self):
        # ⚠️ Pinned against a literal, not against the module's own constant, so
        # widening the list fails here — the list is an operator decision.
        self.assertEqual(tuple(self.guard.MANAGER_VERBS), MANAGER_VERBS)

    def test_every_listed_verb_has_a_matching_form_in_the_pattern(self):
        # ⚠️ Three assertions, because the runtime degrades rather than raising: a verb
        # listed with no form still compiles (the bare spelling matches), so the pattern
        # alone cannot prove the omission. This is the only place that can.
        self.assertTrue(set(MANAGER_VERBS) <= set(self.guard._VERB_FORMS),
                        "every listed verb needs an entry in _VERB_FORMS")
        # ⚠️ Compared through the guard's own name map: a group name must be a Python
        # identifier and two verbs carry a hyphen (`re-scope`, `re-measure`). The map is
        # not trusted for the VERB it answers — `NATURAL_ACTION_LABELS` below asserts
        # that per verb, and that is what proves `rescope` still reads back `re-scope`.
        expected_groups = {self.guard._GROUP_NAMES.get(v, v) for v in MANAGER_VERBS}
        self.assertEqual(set(self.guard._MANAGER_VERB_RE.groupindex), expected_groups)
        for verb, label in NATURAL_ACTION_LABELS.items():
            with self.subTest(verb=verb):
                self.assertEqual(self.actions(label), [verb])


class TestNormalizerMirror(Base):
    """`norm_session_name` is a MIRROR of `fleet-board.py`'s `_norm_session_name`.

    ⚠️ Two copies, one rule. The guard cannot import the canonical one — it ships as a
    plugin artifact with no sibling on its path — so the two are pinned together here,
    the same way `test_manager_predispatch.py` pins its own three copies.
    """

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "fleet_board", os.path.join(REPO, "scripts", "fleet-board.py"))
        cls.board = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.board)

    def test_the_mirror_agrees_with_the_canonical_copy(self):
        guard = load_guard()
        for name in ("⚙ Sentry Agent Manager", "Sentry Agent Manager", "Fleet Manager",
                     "  ⚙  Double  Spaced  ", "", None, "✳ starred", "Already Geared ⚙"):
            with self.subTest(name=name):
                self.assertEqual(guard.norm_session_name(name),
                                 self.board._norm_session_name(name))

    def test_the_page_lookup_strips_the_manager_suffix(self):
        guard = load_guard()
        topics = {"sentry agent": "Sentry Agent"}
        self.assertEqual(guard.manager_subject("Sentry Agent Manager", topics, {}),
                         ("topic", "Sentry Agent"))
        self.assertIsNone(guard.manager_subject("Some Other Manager", topics, {}))


class TestManifest(unittest.TestCase):
    """The `hooks/hooks.json` wiring — modelled on `scripts/tests/test_hooks_manifest.py`."""

    @classmethod
    def setUpClass(cls):
        with open(MANIFEST, encoding="utf-8") as fh:
            cls.manifest = json.load(fh)
        cls.hooks = cls.manifest["hooks"]

    def guard_hooks(self, event):
        """Every hook entry of `event` whose args reference the guard script."""
        out = []
        for group in self.hooks.get(event, []):
            for hook in group.get("hooks", []):
                if any(GUARD_SCRIPT in str(a) for a in hook.get("args", [])):
                    out.append((group, hook))
        return out

    def test_the_guard_is_wired_to_pretooluse(self):
        found = self.guard_hooks(GUARD_EVENT)
        self.assertEqual(len(found), 1, f"{GUARD_EVENT} must carry exactly one guard entry")

    def test_no_other_event_carries_the_guard(self):
        # ⚠️ The negative control. A second wiring is a second firing site, and the
        # deny is only correct on the one tool it polices.
        for event in self.hooks:
            if event == GUARD_EVENT:
                continue
            with self.subTest(event=event):
                self.assertEqual(self.guard_hooks(event), [],
                                 f"{event} must not fire the manager-menu guard")

    def test_the_group_is_scoped_to_askuserquestion(self):
        # ⚠️ Without the matcher the hook runs on EVERY tool call. It would still allow
        # them, so the defect would cost a spawn per tool call and never show up.
        group, _ = self.guard_hooks(GUARD_EVENT)[0]
        self.assertEqual(group.get("matcher"), GUARD_MATCHER)

    def test_the_referenced_script_exists_under_the_plugin_root(self):
        # A typo in the path disables the hook silently — no error, no deny, and it
        # reads exactly like a manager that stopped offering menus.
        _, hook = self.guard_hooks(GUARD_EVENT)[0]
        arg = [a for a in hook["args"] if GUARD_SCRIPT in str(a)][0]
        self.assertTrue(str(arg).startswith("${CLAUDE_PLUGIN_ROOT}/"),
                        f"the script must resolve against the plugin root, got: {arg}")
        rel = str(arg).replace("${CLAUDE_PLUGIN_ROOT}/", "")
        self.assertTrue(os.path.isfile(os.path.join(REPO, rel)),
                        f"{GUARD_EVENT} points at a script that does not exist: {rel}")

    def test_the_group_carries_a_bounded_timeout(self):
        # ⚠️ This runs inside the operator's turn, before every AskUserQuestion.
        _, hook = self.guard_hooks(GUARD_EVENT)[0]
        self.assertIn("timeout", hook)
        self.assertIsInstance(hook["timeout"], int)
        self.assertLessEqual(hook["timeout"], 30)

    def test_the_manifest_description_names_purpose_fail_open_and_kill_switch(self):
        # ⚠️ This repo's convention: each group's purpose, fail-open direction and kill
        # switch are documented in the top-level description, and a group missing any of
        # the three is what reviewers name.
        description = self.manifest["description"]
        self.assertIn(f"{GUARD_EVENT} on {GUARD_MATCHER}", description)
        self.assertIn("manager-owned action", description)
        self.assertIn("Fail-open", description)
        self.assertIn("SUPERVISOR_MANAGER_MENU_GUARD=off", description)


if __name__ == "__main__":
    unittest.main()
