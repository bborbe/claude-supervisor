#!/usr/bin/env python3
"""Tests for scripts/notify-gate.py.

Covers the cadence decision, which is the whole reason the script exists: without
it the manager loop would put one open gate on the phone roughly 96 times a day.

Three cases carry the weight, each against a specific way the bound could be wrong:

  * a gate that stays open must re-raise at 1h and 4h and then STOP. A bound that
    re-raises but never stops is the noise this exists to prevent; a bound that
    stops too early is a gate the operator never hears about again.
  * a gate whose TEXT changes must count as a new identity. Otherwise a worker
    rephrasing its question is silently swallowed -- the question changed and the
    operator should hear it.
  * whitespace alone must NOT change the identity. A re-rendered line is the same
    gate, and treating it as new would defeat the cap entirely.

The constants are asserted directly as well: the cadence is a decided contract
(2026-09-19), so a later edit that widens it should have to change a test.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "notify-gate.py")

_spec = importlib.util.spec_from_file_location("notify_gate", _SCRIPT)
notify_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify_gate)

RAISED_AT = "2026-09-19T10:00:00Z"


def at(value):
    return notify_gate.parse_time(value)


def entry(deliveries):
    return {"deliveries": deliveries, "firstRaisedAt": RAISED_AT}


class Cadence(unittest.TestCase):
    def test_first_sight_delivers(self):
        self.assertTrue(notify_gate.decide(None, at(RAISED_AT)))

    def test_next_sweep_is_suppressed(self):
        self.assertFalse(notify_gate.decide(entry(1), at("2026-09-19T10:30:00Z")))

    def test_re_raises_at_one_hour(self):
        self.assertTrue(notify_gate.decide(entry(1), at("2026-09-19T11:00:00Z")))

    def test_second_re_raise_waits_until_four_hours(self):
        self.assertFalse(notify_gate.decide(entry(2), at("2026-09-19T13:00:00Z")))

    def test_second_re_raise_fires_at_four_hours(self):
        self.assertTrue(notify_gate.decide(entry(2), at("2026-09-19T14:00:00Z")))

    def test_cap_stops_a_gate_that_stays_open_all_day(self):
        self.assertFalse(notify_gate.decide(entry(3), at("2026-09-20T10:00:00Z")))

    def test_cadence_is_the_decided_contract(self):
        self.assertEqual(notify_gate.RE_RAISE_AFTER_SECONDS, (3600, 14400))
        self.assertEqual(notify_gate.MAX_DELIVERIES, 3)
        self.assertEqual(notify_gate.DEFAULT_TYPE, "pending-approval")


class Identity(unittest.TestCase):
    def test_whitespace_alone_is_the_same_gate(self):
        self.assertEqual(
            notify_gate.gate_key("pane-243", "approve:  make apply"),
            notify_gate.gate_key("pane-243", "approve: make apply\n"),
        )

    def test_a_changed_question_is_a_new_gate(self):
        self.assertNotEqual(
            notify_gate.gate_key("pane-243", "approve: make apply"),
            notify_gate.gate_key("pane-243", "approve: make upgrade"),
        )

    def test_the_same_gate_on_another_pane_is_a_new_gate(self):
        self.assertNotEqual(
            notify_gate.gate_key("pane-243", "approve: make apply"),
            notify_gate.gate_key("pane-251", "approve: make apply"),
        )


class Message(unittest.TestCase):
    def render(self, text="approve: make apply", owner="pane-243"):
        return notify_gate.render_message({"owner": owner, "text": text})

    def test_names_the_gate_and_its_owner(self):
        text = self.render()
        self.assertIn("pane-243", text)
        self.assertIn("approve: make apply", text)

    def test_the_gate_text_gets_a_line_of_its_own(self):
        """A phone wraps a long line mid-argument, so an INLINE command copied
        line-wise comes out with an unbalanced quote -- measured 2026-09-19, when
        the operator got `/vault-cli:complete-goal "The Manager Ranks` from a
        notification carrying the whole command. The gate text is where the command
        lives, so it starts a line rather than sitting after our prefix."""
        lines = self.render().split("\n")
        self.assertEqual(lines[1], "approve: make apply")
        self.assertTrue(lines[0].startswith("Manager gate open -- pane-243"))

    def test_a_long_gate_text_still_starts_its_own_line(self):
        long_text = 'Manager Layer Manager: pick — 1. /vault-cli:complete-goal "x" ' * 6
        text = self.render(text=long_text, owner="208")
        self.assertEqual(text.split("\n")[1], " ".join(long_text.split()))

    def test_it_states_the_wall_rather_than_advising(self):
        """'Answer it in the owning session, not here' did not work: the operator
        replied in Telegram anyway, which is the predictable response to a message
        that reads like a conversation. State that replies are not read."""
        self.assertIn("Replies here are not read", self.render())


class CurlConfig(unittest.TestCase):
    def test_escapes_quote_and_backslash(self):
        self.assertEqual(
            notify_gate.curl_config_user('a"b', "c\\d"),
            'user = "a\\"b:c\\\\d"\n',
        )

    def test_plain_credentials_pass_through(self):
        self.assertEqual(
            notify_gate.curl_config_user("user", "secret"),
            'user = "user:secret"\n',
        )


def _reload(suffix):
    """A fresh module instance, so STATE_PATH re-reads the current env."""
    spec = importlib.util.spec_from_file_location(f"notify_gate_{suffix}", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Harness(unittest.TestCase):
    """A module instance wired to temp config and ledger paths.

    Both env overrides exist for this: without them main()/commit() cannot run
    without touching the operator's real ledger, which is why these paths went
    untested until the local review called it out.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "ledger.json")
        self.config = os.path.join(self.dir, "config.json")
        with open(self.config, "w") as handle:
            json.dump(
                {
                    "notify": {
                        "env": "dev",
                        "endpoints": {
                            "dev": {
                                "baseUrl": "http://127.0.0.1:1",
                                "teamvaultKey": "unused-in-tests",
                            }
                        },
                    }
                },
                handle,
            )
        self._env = {
            key: os.environ.get(key)
            for key in (
                "SUPERVISOR_GATE_STATE",
                "SUPERVISOR_CONFIG",
                "CLAUDE_CODE_SESSION_ID",
            )
        }
        os.environ["SUPERVISOR_GATE_STATE"] = self.state
        os.environ["SUPERVISOR_CONFIG"] = self.config
        # Default identity: a stamp cannot be written without one, so every test that
        # is not specifically about the unset case needs a session to be.
        os.environ["CLAUDE_CODE_SESSION_ID"] = "session-a"
        # The identity this sweep escalates as, passed explicitly to commit() the
        # way publish_round() passes it. A test that wants another manager's sweep
        # uses a different value here rather than mutating the env mid-run.
        self.me = "session-a"
        self.last_out = ""
        self.mod = _reload(self._testMethodName)
        # main() sets these from --layer; the direct commit()/load_ledger() calls
        # in these tests need them set explicitly.
        self.mod.STATE_PATH = self.state
        # A fake attention store. The real one is HTTP and shared with the operator's
        # live fleet, so the suite must never reach it -- and these two functions are
        # exactly the seams that would. `items` is what the store holds; `stamped`
        # records every write, so a test can assert a gate was stamped exactly once.
        self.items = []
        self.stamped = []
        self.mod.store_items = lambda: self.items
        self.mod.stamp_item = self._stamp

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self):
        with open(self.state) as handle:
            return json.load(handle)

    def feed(self, raw, layer="test"):
        before_stdin, before_argv = sys.stdin, sys.argv
        sys.stdin = io.StringIO(raw)
        sys.argv = ["notify-gate.py", "--layer", layer]
        try:
            # main() prints its round summary; capturing it keeps the suite's own
            # output readable instead of interleaving the script's, and `last_out`
            # lets a test assert on what the round actually said -- which is how the
            # skip and `unresolved` lines are tested rather than merely intended.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result = self.mod.main()
            self.last_out = out.getvalue()
            return result
        finally:
            sys.stdin, sys.argv = before_stdin, before_argv

    def last_stdout(self):
        return self.last_out

    def _stamp(self, item_id, session_id):
        """Stand in for the store write: record the call and apply it locally.

        Applied rather than only recorded, so a later gate in the same round sees the
        stamp -- which is what makes the round's read-once/write-once shape testable.
        """
        self.stamped.append((item_id, session_id))
        for item in self.items:
            if item.get("item_id") == item_id:
                item["escalated_by"] = session_id
        return None

    def item(self, session, item_id=None, payload="", escalated_by=""):
        """A store item carrying the fields this script reads, and no others."""
        return {
            "item_id": item_id or f"item-{session}",
            "producer_id": session,
            "payload": payload,
            "escalated_by": escalated_by,
        }


class Ledger(_Harness):
    """commit() and load_ledger(), which the pure-function tests above cannot reach.

    Pruning is load-bearing -- it is what makes the delivery bound per-gate rather
    than per-lifetime, and both manager commands depend on it when they instruct a
    `{"gates": []}` sweep -- so it gets real coverage rather than a promise.
    """

    def test_commit_prunes_a_gate_that_cleared(self):
        key = self.mod.gate_key("pane-243", "approve: make apply")
        self.mod.commit(
            {
                key: {
                    "deliveries": 2,
                    "firstRaisedAt": RAISED_AT,
                    "escalatedBy": self.me,
                }
            },
            {},
            {},
            self.me,
        )
        self.assertEqual(self.read(), {})

    def test_commit_keeps_a_gate_that_is_still_open(self):
        key = self.mod.gate_key("pane-243", "approve: make apply")
        self.mod.commit(
            {key: {"deliveries": 2, "firstRaisedAt": RAISED_AT}},
            {key: {"owner": "pane-243", "text": "approve: make apply"}},
            {},
            self.me,
        )
        self.assertEqual(self.read()[key]["deliveries"], 2)

    def test_a_cleared_gate_raises_again_at_full_cadence(self):
        key = self.mod.gate_key("pane-243", "approve: make apply")
        self.mod.commit(
            {
                key: {
                    "deliveries": 3,
                    "firstRaisedAt": RAISED_AT,
                    "escalatedBy": self.me,
                }
            },
            {},
            {},
            self.me,
        )
        self.assertIsNone(self.mod.load_ledger().get(key))
        self.assertTrue(self.mod.decide(self.mod.load_ledger().get(key), at(RAISED_AT)))

    def test_an_unreadable_ledger_reads_as_empty(self):
        with open(self.state, "w") as handle:
            handle.write("{not json")
        self.assertEqual(self.mod.load_ledger(), {})

    def test_commit_leaves_no_temp_file_behind(self):
        self.mod.commit({}, {}, {}, self.me)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_shape_malformed_entries_are_dropped_not_fatal(self):
        """load_ledger()'s "never fatal" claim has to hold for entries that parse
        as JSON but carry the wrong shape -- otherwise a single bad entry crashes
        every later sweep."""
        with open(self.state, "w") as handle:
            json.dump(
                {
                    "good\x1ftext": {"deliveries": 1, "firstRaisedAt": RAISED_AT},
                    "no-deliveries\x1ftext": {"firstRaisedAt": RAISED_AT},
                    "bad-time\x1ftext": {"deliveries": 1, "firstRaisedAt": "nonsense"},
                    # TypeErrors in parse_time() rather than ValueError -- the hole
                    # the fourth review pass found in an earlier except tuple.
                    "int-time\x1ftext": {"deliveries": 1, "firstRaisedAt": 123},
                    "null-time\x1ftext": {"deliveries": 1, "firstRaisedAt": None},
                    # bool is an int subclass; 0 would index the re-raise table out
                    # of range inside decide().
                    "bool-deliveries\x1ftext": {"deliveries": True, "firstRaisedAt": RAISED_AT},
                    "zero-deliveries\x1ftext": {"deliveries": 0, "firstRaisedAt": RAISED_AT},
                    "not-an-object\x1ftext": "x",
                },
                handle,
            )
        self.assertEqual(list(self.mod.load_ledger()), ["good\x1ftext"])

    def test_a_dropped_entry_re_raises_instead_of_crashing(self):
        with open(self.state, "w") as handle:
            json.dump({"k\x1ftext": {"deliveries": 1}}, handle)
        self.assertTrue(
            self.mod.decide(self.mod.load_ledger().get("k\x1ftext"), at(RAISED_AT))
        )

    def test_a_non_mapping_ledger_reads_as_empty(self):
        with open(self.state, "w") as handle:
            json.dump(["not", "a", "mapping"], handle)
        self.assertEqual(self.mod.load_ledger(), {})

    def test_a_manager_keeps_a_foreign_gate_it_never_published(self):
        """The defect this whole scope exists for.

        `--layer worker` is every topic manager in the fleet, not one manager. A
        manager whose sweep raised nothing publishes `{"gates": []}`; before the
        fix that emptied `current`, so `commit` dropped EVERY entry in the file --
        including gates owned by other managers, which then re-notified as
        first-sight at full cadence, on every sweep, forever.
        """
        key = self.mod.gate_key("1011", "approve: decide the allowlist disambiguation")
        with open(self.state, "w") as handle:
            json.dump(
                {
                    key: {
                        "deliveries": 1,
                        "firstRaisedAt": RAISED_AT,
                        "lastDeliveryAt": RAISED_AT,
                        "owner": "1011",
                        "text": "approve: decide the allowlist disambiguation",
                        "escalatedBy": "another-manager-session",
                    }
                },
                handle,
            )
        # This manager's sweep raised nothing -- `current` is empty, which is the
        # case that used to drop the whole file.
        self.mod.commit(self.mod.load_ledger(), {}, {}, self.me)
        kept = self.read()
        self.assertIn(key, kept, "a foreign gate must survive this manager's sweep")
        self.assertEqual(kept[key]["deliveries"], 1)
        self.assertEqual(kept[key]["firstRaisedAt"], RAISED_AT)

    def test_a_manager_still_prunes_its_own_cleared_gate(self):
        """The positive half -- a fix that simply stops pruning fails this."""
        key = self.mod.gate_key("pane-243", "approve: make apply")
        self.mod.commit(
            {
                key: {
                    "deliveries": 1,
                    "firstRaisedAt": RAISED_AT,
                    "escalatedBy": self.me,
                }
            },
            {},
            {},
            self.me,
        )
        self.assertEqual(self.read(), {})

    def test_an_entry_with_no_owner_is_kept_rather_than_pruned(self):
        """A pre-`escalatedBy` entry has an unknown owner.

        Kept, not pruned: it is adopted the moment its own manager publishes it
        again, so keeping it costs one stale entry, while pruning it would repeat
        the foreign-prune defect once during the upgrade -- against the operator's
        live ledger, which is exactly what must not happen.
        """
        key = self.mod.gate_key("1064", "approve: git push origin dev")
        with open(self.state, "w") as handle:
            json.dump({key: {"deliveries": 1, "firstRaisedAt": RAISED_AT}}, handle)
        self.mod.commit(self.mod.load_ledger(), {}, {}, self.me)
        self.assertIn(key, self.read())

    def test_a_sweep_with_no_identity_prunes_nothing(self):
        """Without an identity, "mine" is unknowable -- so nothing is claimed.

        A gate published by an identity-less sweep records `escalatedBy: ""`, and a
        later identity-less sweep must not read that blank as a wildcard matching
        itself. Fails safe: the entry goes stale rather than being pruned by a
        manager that cannot prove it owns it.
        """
        key = self.mod.gate_key("pane-243", "approve: make apply")
        with open(self.state, "w") as handle:
            json.dump(
                {key: {"deliveries": 1, "firstRaisedAt": RAISED_AT, "escalatedBy": ""}},
                handle,
            )
        self.mod.commit(self.mod.load_ledger(), {}, {}, "")
        self.assertIn(key, self.read())

    def test_an_entry_with_no_owner_is_adopted_when_its_manager_republishes(self):
        """The migration path that makes keeping the legacy entry bounded."""
        key = self.mod.gate_key("1064", "approve: git push origin dev")
        gate = {"owner": "1064", "text": "approve: git push origin dev"}
        with open(self.state, "w") as handle:
            json.dump({key: {"deliveries": 1, "firstRaisedAt": RAISED_AT}}, handle)
        self.mod.commit(
            self.mod.load_ledger(),
            {key: gate},
            {
                key: {
                    "deliveries": 2,
                    "firstRaisedAt": RAISED_AT,
                    "escalatedBy": self.me,
                }
            },
            self.me,
        )
        self.assertEqual(self.read()[key]["escalatedBy"], self.me)
        # ...and once adopted, the same manager's later empty sweep does prune it.
        self.mod.commit(self.mod.load_ledger(), {}, {}, self.me)
        self.assertEqual(self.read(), {})

    def test_the_ledger_is_namespaced_per_layer(self):
        """The layers see different slices of the world, so they must not share a
        ledger: the fleet's sweep drops worker-owned gates, and a shared file would
        let it prune them as cleared, re-raising them on every later tick."""
        os.environ.pop("SUPERVISOR_GATE_STATE", None)
        fleet = self.mod.ledger_path("fleet")
        worker = self.mod.ledger_path("worker")
        self.assertNotEqual(fleet, worker)
        self.assertTrue(fleet.endswith("gate-notifications-fleet.json"))
        self.assertTrue(worker.endswith("gate-notifications-worker.json"))

    def test_the_state_override_wins_over_the_layer(self):
        self.assertEqual(self.mod.ledger_path("fleet"), self.state)

    def test_a_bare_filename_state_path_is_accepted(self):
        """SUPERVISOR_GATE_STATE=gate.json has no directory part to create."""
        self.mod.STATE_PATH = os.path.join(self.dir, "bare.json").replace(
            self.dir + "/", ""
        )
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            self.mod.commit({}, {}, {}, self.me)
        finally:
            os.chdir(cwd)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "bare.json")))


class ReadGates(_Harness):
    """read_gates(), whose whole job is turning a hand-typed one-liner into an
    actionable message. Untested, it tracebacked on the two likeliest typos.
    """

    def parse(self, raw):
        before = sys.stdin
        sys.stdin = io.StringIO(raw)
        try:
            return self.mod.read_gates()
        finally:
            sys.stdin = before

    def message(self, raw):
        with self.assertRaises(SystemExit) as caught:
            self.parse(raw)
        return str(caught.exception)

    def test_a_well_formed_call_parses(self):
        self.assertEqual(
            self.parse(
                '{"gates": [{"owner": "pane-1", "text": "approve: x",'
                ' "session": "subject-1"}]}'
            ),
            [{"owner": "pane-1", "text": "approve: x", "session": "subject-1"}],
        )

    def test_an_empty_list_is_a_clean_sweep(self):
        self.assertEqual(self.parse('{"gates": []}'), [])

    def test_a_bare_list_is_reported_not_tracebacked(self):
        self.assertIn("must be an object", self.message("[]"))

    def test_a_missing_gates_key_is_reported_not_read_as_empty(self):
        self.assertIn("no `gates` key", self.message("{}"))

    def test_a_non_object_gate_is_reported(self):
        self.assertIn("each gate must be an object", self.message('{"gates": ["x"]}'))

    def test_a_missing_field_is_reported(self):
        self.assertIn("missing `text`", self.message('{"gates": [{"owner": "p"}]}'))

    def test_invalid_json_is_reported(self):
        self.assertIn("not valid JSON", self.message("{not json"))


class Config(_Harness):
    """resolve_endpoint(), the sibling hand-authored input path.

    The second pass hardened read_gates() against raw tracebacks and left this one
    open -- the same defect class, on the other input an operator types by hand.
    """

    def endpoint_for(self, notify):
        return self.mod.resolve_endpoint({"notify": notify})

    def message(self, notify):
        with self.assertRaises(SystemExit) as caught:
            self.endpoint_for(notify)
        return str(caught.exception)

    def complete(self, **overrides):
        block = {
            "env": "dev",
            "endpoints": {"dev": {"baseUrl": "http://x", "teamvaultKey": "k"}},
        }
        block.update(overrides)
        return block

    def test_a_valid_config_file_loads(self):
        self.assertIsInstance(self.mod.load_config(), dict)

    def test_a_non_mapping_config_file_is_reported(self):
        """The top level was the one shape load_config() did not guard, while
        resolve_endpoint() guards three levels below it."""
        with open(self.config, "w") as handle:
            handle.write("[]")
        with self.assertRaises(SystemExit) as caught:
            self.mod.load_config()
        self.assertIn("must contain a JSON object", str(caught.exception))

    def test_a_valid_block_resolves(self):
        env, endpoint, notification_type = self.endpoint_for(self.complete())
        self.assertEqual(env, "dev")
        self.assertEqual(endpoint["baseUrl"], "http://x")
        self.assertEqual(notification_type, "pending-approval")

    def test_a_non_object_notify_block_is_reported(self):
        self.assertIn("must be an object", self.message("yes"))

    def test_a_non_object_endpoints_is_reported(self):
        self.assertIn(
            "must be an object keyed by env", self.message({"endpoints": ["a"]})
        )

    def test_env_defaults_to_dev_when_absent(self):
        env, _, _ = self.endpoint_for(
            {"endpoints": {"dev": {"baseUrl": "http://x", "teamvaultKey": "k"}}}
        )
        self.assertEqual(env, "dev")

    def test_an_unknown_type_is_passed_through_rather_than_second_guessed(self):
        """The core validates the type and rejects an unknown one with its own
        message; a local allowlist here would silently drift from the core's."""
        _, _, notification_type = self.endpoint_for(self.complete(type="made-up"))
        self.assertEqual(notification_type, "made-up")

    def test_a_null_type_falls_back_to_the_default(self):
        _, _, notification_type = self.endpoint_for(self.complete(type=None))
        self.assertEqual(notification_type, "pending-approval")


class FailureHandling(_Harness):
    """The paths the local review said were covered only by the commit message:
    a failed publish must not be recorded as delivered, and a mid-loop failure must
    not discard the deliveries that already succeeded.
    """

    def fail_on(self, nth):
        """Stub publish() so the nth call raises, after the earlier ones succeed."""
        calls = []

        def stub(base_url, teamvault_key, message, notification_type):
            calls.append(message)
            if len(calls) == nth:
                raise SystemExit("notify-gate: publish failed: synthetic")
            return "ok"

        self.mod.publish = stub
        return calls

    def test_a_failed_publish_is_not_recorded_as_delivered(self):
        self.fail_on(1)
        with self.assertRaises(SystemExit):
            self.feed(
                '{"gates": [{"owner": "pane-1", "text": "approve: x",'
                ' "session": "subject-1"}]}'
            )
        self.assertEqual(self.mod.load_ledger(), {})

    def test_a_later_failure_keeps_the_earlier_delivery(self):
        self.fail_on(2)
        with self.assertRaises(SystemExit):
            self.feed(
                '{"gates": [{"owner": "pane-1", "text": "approve: a",'
                ' "session": "subject-1"},'
                ' {"owner": "pane-2", "text": "approve: b",'
                ' "session": "subject-2"}]}'
            )
        ledger = self.mod.load_ledger()
        self.assertEqual(len(ledger), 1, "the successful publish must survive")
        self.assertEqual(list(ledger.values())[0]["deliveries"], 1)

    def test_a_clean_sweep_prunes_without_publishing(self):
        self.fail_on(1)
        self.feed('{"gates": []}')
        self.assertEqual(self.mod.load_ledger(), {})


class Stamp(_Harness):
    """The cross-layer stamp, which now lives on the attention store item.

    The three rules the retired ledger enforced and this must still enforce:

      * a DIFFERENT session's stamp must suppress -- the defect itself.
      * the SAME session's stamp must NOT suppress -- a manager that blocks on its
        own stamp deadlocks on every later sweep, the mirror-image bug, and the
        reason the item carries a session id rather than a boolean.
      * no resolvable item must NOT be read as "no stamp" -- it is reported as
        `unresolved`, because cross-layer de-dup is genuinely absent for that gate.

    What the move changed, asserted below rather than assumed: the join is the gate's
    SUBJECT session (`producer_id`), so two layers naming the owner differently now
    resolve to the SAME item -- which is what the text-keyed ledger was a workaround
    for, and why it could be retired rather than kept.
    """

    GATE = {"owner": "pane-243", "text": "approve: make apply", "session": "subject-1"}

    def ok(self):
        """Stub publish() to succeed, and record what was actually sent."""
        sent = []

        def stub(base_url, teamvault_key, message, notification_type):
            sent.append(message)
            return "ok"

        self.mod.publish = stub
        return sent

    def feed_as(self, session, layer="worker", gates=None):
        os.environ["CLAUDE_CODE_SESSION_ID"] = session
        payload = json.dumps({"gates": gates if gates is not None else [self.GATE]})
        return self.feed(payload, layer=layer)

    def open_item(self, **kwargs):
        """Put one open item for the subject session in the fake store."""
        entry = self.item("subject-1", **kwargs)
        self.items.append(entry)
        return entry

    def now(self):
        return notify_gate.datetime.datetime.now(notify_gate.datetime.timezone.utc)

    # --- the defect -------------------------------------------------------

    def test_a_second_session_skips_a_gate_the_first_already_raised(self):
        """SC1: the duplicate this whole task exists to prevent."""
        sent = self.ok()
        self.open_item()
        self.feed_as("session-a", layer="worker")
        self.assertEqual(len(sent), 1, "the first session must escalate")
        self.assertEqual(
            self.stamped, [("item-subject-1", "session-a")], "the first must stamp"
        )

        self.feed_as("session-b", layer="fleet")
        self.assertEqual(len(sent), 1, "the second session must NOT escalate")

    def test_the_skip_names_the_session_that_already_raised_it(self):
        """A silent skip is indistinguishable from a dropped gate, which is the
        failure mode the whole task is about -- so the skip has to be announced."""
        self.ok()
        self.open_item()
        self.feed_as("session-a", layer="worker")
        out = io.StringIO()
        os.environ["CLAUDE_CODE_SESSION_ID"] = "session-b"
        with contextlib.redirect_stdout(out):
            self.mod.publish_round(self.now(), [self.GATE])
        self.assertIn("already escalated by session session-a", out.getvalue())

    # --- the mirror-image bug ---------------------------------------------

    def test_a_session_is_never_blocked_by_its_own_stamp(self):
        """SC2: the case the obvious boolean implementation gets wrong.

        Exercised, not reasoned about -- a boolean flag passes the cross-session test
        above and fails exactly here.
        """
        sent = self.ok()
        self.open_item()
        self.feed_as("session-a", layer="worker")
        self.assertEqual(len(sent), 1)
        # Same session, next sweep: its own stamp must not be the reason it stays
        # quiet. The cadence is what decides a re-raise, and this asserts the STAMP is
        # not -- so the assertion is about the IDENTITY rule and not about a clock.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.mod.publish_round(self.now(), [self.GATE])
        self.assertNotIn("already escalated by", out.getvalue())

    # --- the join ---------------------------------------------------------

    def test_the_join_is_the_subject_session_and_not_the_owner(self):
        """`owner` is a pane id and cannot resolve an item; `session` must.

        An item exists for the OWNER's value and none for the subject, so a join on
        `owner` would resolve an item here -- and it must not.
        """
        self.ok()
        self.items.append(self.item("pane-243", item_id="item-by-owner"))
        self.feed_as("session-a", layer="worker")
        self.assertEqual(self.stamped, [], "the owner is not the join key")
        self.assertIn("unresolved", self.last_stdout())

    def test_two_layers_naming_the_owner_differently_resolve_one_item(self):
        """The workaround the text-keyed ledger existed for, now unnecessary.

        The old stamp keyed on normalised TEXT precisely because `owner` could be a
        session id from one layer and a pane id from the other. Resolving through the
        item makes that difference stop mattering.
        """
        sent = self.ok()
        self.open_item()
        self.feed_as(
            "session-a", layer="worker", gates=[dict(self.GATE, owner="pane-243")]
        )
        self.feed_as(
            "session-b", layer="fleet", gates=[dict(self.GATE, owner="subject-1")]
        )
        self.assertEqual(len(sent), 1, "one item, one stamp, one delivery")

    # --- what the move changed --------------------------------------------

    def test_a_gate_with_no_resolvable_item_is_reported_unresolved(self):
        """The accepted cost of retiring the ledger, made visible.

        Two layers CAN now both send this gate. The run says so rather than reading
        as a clean skip, which is the whole reason the cost is acceptable.
        """
        sent = self.ok()
        self.feed_as("session-a", layer="worker")
        self.assertEqual(len(sent), 1, "an unresolvable gate still escalates")
        self.assertEqual(self.stamped, [], "and is not stamped")
        self.assertIn("unresolved", self.last_stdout())
        self.assertIn("cross-layer de-dup is NOT in force", self.last_stdout())

    def test_an_ambiguous_session_with_no_text_match_is_unresolved(self):
        """The tiebreak must never silently pick -- the Fleet Manager's condition.

        Two open items for one session and a gate text matching neither is exactly the
        case a fall-back would get wrong, so it resolves to nothing.
        """
        self.ok()
        self.items.append(
            self.item("subject-1", item_id="item-a", payload="something else")
        )
        self.items.append(
            self.item("subject-1", item_id="item-b", payload="also not it")
        )
        self.feed_as("session-a", layer="worker")
        self.assertEqual(self.stamped, [], "no stamp when the tiebreak cannot pick")
        self.assertIn("unresolved", self.last_stdout())

    def test_the_tiebreak_resolves_an_ambiguous_session_when_it_matches(self):
        """A tiebreak that CAN pick does, and picks the right item."""
        self.ok()
        self.items.append(
            self.item("subject-1", item_id="item-a", payload="approve: make apply")
        )
        self.items.append(
            self.item("subject-1", item_id="item-b", payload="approve: make rollback")
        )
        self.feed_as("session-a", layer="worker")
        self.assertEqual(self.stamped, [("item-a", "session-a")])

    def test_an_unreachable_store_is_reported_and_the_gate_still_escalates(self):
        """Never fail closed on a real gate -- and never let an outage read as clean.

        An unreachable store and an empty queue both mean "no item to stamp", but only
        one of them is a degraded run. Collapsing them would make an outage look like
        a clean sweep.
        """
        sent = self.ok()
        self.mod.store_items = lambda: None
        self.feed_as("session-a", layer="worker")
        self.assertEqual(len(sent), 1, "the gate must still reach the operator")
        self.assertIn("unreachable", self.last_stdout())

    # --- identity ---------------------------------------------------------

    def test_without_a_session_id_the_gate_still_escalates_but_is_not_stamped(self):
        """Never fail closed on a real gate. A stamp written with a blank identity
        would reintroduce the boolean bug in another costume."""
        sent = self.ok()
        self.open_item()
        os.environ["CLAUDE_CODE_SESSION_ID"] = ""
        self.feed(json.dumps({"gates": [self.GATE]}), layer="worker")
        self.assertEqual(len(sent), 1, "the gate must still escalate")
        self.assertEqual(self.stamped, [], "no stamp without an id")

    def test_a_failed_publish_writes_no_stamp(self):
        """A stamp for a gate that never sent would suppress the other layer's attempt
        to raise it, turning a delivery failure into a gate nobody escalates."""
        self.open_item()

        def boom(*args, **kwargs):
            raise RuntimeError("publish failed")

        self.mod.publish = boom
        with self.assertRaises(RuntimeError):
            self.feed_as("session-a", layer="worker")
        self.assertEqual(self.stamped, [], "no stamp for an unsent gate")


class Hold(_Harness):
    """A held session's gate is never published -- the row stays visible, the message stops.

    Both directions are asserted by name. The too-tight case (a held gate is not
    published) and the too-loose case (an UNHELD gate in the SAME round still is) fail
    in opposite directions, and only one of them looks like a bug. The third case is the
    control: with no hold set, both gates publish -- which is what proves the fixture is
    able to publish at all, so the suppression is real rather than a round that emits
    nothing for any reason.

    `publish()` is mocked rather than the network reached: the real endpoint is the
    operator's live notification core.
    """

    def setUp(self):
        super().setUp()
        self.sent = []
        self.mod.publish = lambda *a, **k: self.sent.append(a)
        self.holds = os.path.join(self.dir, "holds.json")
        self.mod.HOLDS_PATH = self.holds

    def write_holds(self, *session_ids):
        entries = ", ".join(
            '"%s": {"reason": "operator: leave it"}' % sid for sid in session_ids
        )
        with open(self.holds, "w", encoding="utf-8") as handle:
            handle.write('{"version": 1, "holds": {%s}}' % entries)

    def round(self, gates):
        self.mod.publish_round(datetime.datetime.now(datetime.timezone.utc), gates)
        return " | ".join(str(call) for call in self.sent)

    def gate(self, session, text):
        return {"owner": "pane-243", "text": text, "session": session}

    def test_fires_on_a_held_session(self):
        """Too-tight: a held session's gate is not published."""
        self.write_holds(HELD)
        out = self.round([self.gate(HELD, "approve: make apply")])
        self.assertNotIn("make apply", out)

    def test_does_not_fire_on_a_clean_session(self):
        """Too-loose: an unheld session's gate in the SAME round still publishes."""
        self.write_holds(HELD)
        out = self.round(
            [
                self.gate(HELD, "approve: make apply"),
                self.gate(FREE, "approve: kubectl delete"),
            ]
        )
        self.assertIn("kubectl delete", out)
        self.assertNotIn("make apply", out)

    def test_the_control_fires_when_no_hold_is_set(self):
        """The fixture can publish: with no hold, both gates go out."""
        self.write_holds()
        out = self.round(
            [
                self.gate(HELD, "approve: make apply"),
                self.gate(FREE, "approve: kubectl delete"),
            ]
        )
        self.assertIn("make apply", out)
        self.assertIn("kubectl delete", out)

    def test_a_corrupt_store_reads_as_nothing_held(self):
        """Fail-open: an unreadable store must not silence a real escalation."""
        with open(self.holds, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        out = self.round([self.gate(HELD, "approve: make apply")])
        self.assertIn("make apply", out)


HELD = "aaaaaaaa-1111-2222-3333-444444444444"
FREE = "bbbbbbbb-1111-2222-3333-444444444444"


if __name__ == "__main__":
    unittest.main()
