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
        self.stamps = os.path.join(self.dir, "stamps")
        self._env = {
            key: os.environ.get(key)
            for key in (
                "SUPERVISOR_GATE_STATE",
                "SUPERVISOR_CONFIG",
                "SUPERVISOR_GATE_STAMP_DIR",
                "CLAUDE_CODE_SESSION_ID",
            )
        }
        os.environ["SUPERVISOR_GATE_STATE"] = self.state
        os.environ["SUPERVISOR_CONFIG"] = self.config
        os.environ["SUPERVISOR_GATE_STAMP_DIR"] = self.stamps
        # Default identity: a stamp cannot be written without one, so every test that
        # is not specifically about the unset case needs a session to be.
        os.environ["CLAUDE_CODE_SESSION_ID"] = "session-a"
        # The identity this sweep escalates as, passed explicitly to commit() the
        # way publish_round() passes it. A test that wants another manager's sweep
        # uses a different value here rather than mutating the env mid-run.
        self.me = "session-a"
        self.mod = _reload(self._testMethodName)
        # main() sets these from --layer; the direct commit()/load_ledger() calls
        # in these tests need them set explicitly.
        self.mod.STATE_PATH = self.state
        self.mod.STAMP_DIR = self.stamps

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
            # output readable instead of interleaving the script's.
            with contextlib.redirect_stdout(io.StringIO()):
                return self.mod.main()
        finally:
            sys.stdin, sys.argv = before_stdin, before_argv


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
            self.parse('{"gates": [{"owner": "pane-1", "text": "approve: x"}]}'),
            [{"owner": "pane-1", "text": "approve: x"}],
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
            self.feed('{"gates": [{"owner": "pane-1", "text": "approve: x"}]}')
        self.assertEqual(self.mod.load_ledger(), {})

    def test_a_later_failure_keeps_the_earlier_delivery(self):
        self.fail_on(2)
        with self.assertRaises(SystemExit):
            self.feed(
                '{"gates": [{"owner": "pane-1", "text": "approve: a"},'
                ' {"owner": "pane-2", "text": "approve: b"}]}'
            )
        ledger = self.mod.load_ledger()
        self.assertEqual(len(ledger), 1, "the successful publish must survive")
        self.assertEqual(list(ledger.values())[0]["deliveries"], 1)

    def test_a_clean_sweep_prunes_without_publishing(self):
        self.fail_on(1)
        self.feed('{"gates": []}')
        self.assertEqual(self.mod.load_ledger(), {})


class Stamp(_Harness):
    """The cross-layer stamp: the counterweight to the per-layer cadence ledger.

    Measured twice on 2026-09-20 (Fleet Manager session `b700c650`): two managers
    raised the identical gate independently. Both duplicates cost the operator a
    decision; one also left a worker parked.

    The three cases that carry the weight, each against a specific way the stamp
    could be wrong:

      * a DIFFERENT session's stamp must suppress -- the defect itself.
      * the SAME session's stamp must NOT suppress -- a manager that blocks on its
        own stamp deadlocks on every later sweep, which is the mirror-image bug and
        the reason this stores a session id rather than a boolean.
      * an EXPIRED stamp must NOT suppress -- an unbounded stamp turns "the
        escalating session died" into "nobody ever escalates this gate again",
        which is strictly worse than the duplicate it prevents.
    """

    GATE = {"owner": "pane-243", "text": "approve: make apply"}

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

    def key(self):
        """The STAMP key -- normalised text, not the cadence `gate_key`.

        Reading it from `stamp_key` rather than re-deriving it keeps the tests
        honest about which identity the stamp uses.
        """
        return self.mod.stamp_key(self.GATE)

    # --- the defect -------------------------------------------------------

    def test_a_second_session_skips_a_gate_the_first_already_raised(self):
        """SC1: the duplicate this whole task exists to prevent."""
        sent = self.ok()
        self.feed_as("session-a", layer="worker")
        self.assertEqual(len(sent), 1, "the first session must escalate")
        self.assertIsNotNone(
            self.mod.load_stamp(self.key()), "the first session must stamp"
        )

        self.feed_as("session-b", layer="fleet")
        self.assertEqual(len(sent), 1, "the second session must NOT escalate")

    def test_the_skip_names_the_session_that_already_raised_it(self):
        """A silent skip is indistinguishable from a dropped gate, which is the
        failure mode the whole task is about -- so the skip has to be announced."""
        self.ok()
        self.feed_as("session-a", layer="worker")
        out = io.StringIO()
        os.environ["CLAUDE_CODE_SESSION_ID"] = "session-b"
        with contextlib.redirect_stdout(out):
            self.mod.publish_round(
                notify_gate.datetime.datetime.now(notify_gate.datetime.timezone.utc),
                [self.GATE],
            )
        self.assertIn("already escalated by session session-a", out.getvalue())

    # --- the mirror-image bug ---------------------------------------------

    def test_a_session_is_never_blocked_by_its_own_stamp(self):
        """SC2: the case the obvious boolean implementation gets wrong.

        Exercised, not reasoned about -- a boolean flag passes the cross-session
        test above and fails exactly here.
        """
        sent = self.ok()
        self.feed_as("session-a", layer="worker")
        self.assertEqual(len(sent), 1)
        # Same session, next sweep: its own stamp must not suppress it. The gate is
        # still open, so the cadence is what decides -- and the cadence allows a
        # re-raise only after 1h, so this asserts the STAMP is not the reason.
        # Read at the stamp's own instant, so the assertion is about the IDENTITY
        # rule and not about the TTL -- a hard-coded instant would silently become
        # an expiry test the day the stamp's clock and that literal diverged.
        fresh = at(self.mod.load_stamp(self.key())["ts"])
        self.assertIsNone(
            self.mod.stamp_of(self.GATE, fresh),
            "a session must not be suppressed by its own stamp",
        )
        # ...and a DIFFERENT session at the same instant IS suppressed, so the test
        # above is not passing merely because no stamp exists.
        os.environ["CLAUDE_CODE_SESSION_ID"] = "session-b"
        self.assertIsNotNone(
            self.mod.stamp_of(self.GATE, fresh),
            "a different session must be suppressed by the same stamp",
        )

    # --- expiry -----------------------------------------------------------

    def test_an_expired_stamp_stops_suppressing(self):
        """An unbounded stamp would mean a dead escalator permanently silences the
        gate for everyone else."""
        self.ok()
        self.feed_as("session-a", layer="worker")
        stamp = self.mod.load_stamp(self.key())
        self.assertIsNotNone(stamp)

        os.environ["CLAUDE_CODE_SESSION_ID"] = "session-b"
        fresh = at(stamp["ts"])
        expired = fresh + notify_gate.datetime.timedelta(
            seconds=notify_gate.STAMP_TTL + 1
        )
        self.assertIsNotNone(self.mod.stamp_of(self.GATE, fresh))
        self.assertIsNone(self.mod.stamp_of(self.GATE, expired))

    def test_reading_an_expired_stamp_removes_it(self):
        """Expiry doubles as the garbage collector, so the directory cannot grow one
        file per gate ever seen."""
        self.ok()
        self.feed_as("session-a", layer="worker")
        stamp = self.mod.load_stamp(self.key())
        os.environ["CLAUDE_CODE_SESSION_ID"] = "session-b"
        self.mod.stamp_of(
            self.GATE,
            at(stamp["ts"])
            + notify_gate.datetime.timedelta(seconds=notify_gate.STAMP_TTL + 1),
        )
        self.assertIsNone(self.mod.load_stamp(self.key()))

    # --- degraded identity ------------------------------------------------

    def test_without_a_session_id_the_gate_still_escalates_but_is_not_stamped(self):
        """Never fail closed on a real gate. A stamp written with a blank identity
        cannot tell 'mine' from 'someone else's', so none is written -- and the
        degradation is reported rather than hidden."""
        sent = self.ok()
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.mod.publish_round(
                notify_gate.datetime.datetime.now(notify_gate.datetime.timezone.utc),
                [self.GATE],
            )
        self.assertEqual(len(sent), 1, "the gate must still be escalated")
        self.assertIsNone(self.mod.load_stamp(self.key()), "no stamp without an id")
        self.assertIn("CLAUDE_CODE_SESSION_ID unset", out.getvalue())

    # --- ordering ---------------------------------------------------------

    def test_a_failed_publish_writes_no_stamp(self):
        """A stamp written for a gate that never sent would suppress the OTHER
        layer's attempt to raise it -- turning a delivery failure into a gate
        nobody escalates."""
        self.fail_on(1)
        with self.assertRaises(SystemExit):
            self.feed_as("session-a", layer="worker")
        self.assertIsNone(self.mod.load_stamp(self.key()))

    def fail_on(self, nth):
        calls = []

        def stub(base_url, teamvault_key, message, notification_type):
            calls.append(message)
            if len(calls) == nth:
                raise SystemExit("notify-gate: publish failed: synthetic")
            return "ok"

        self.mod.publish = stub
        return calls

    # --- the key must be comparable across layers -------------------------

    def test_the_same_gate_under_two_layers_shares_one_stamp(self):
        """The stamp is cross-layer, so the SAME gate raised under `worker` and
        `fleet` must land on one stamp file. If the key varied by layer the stamp
        would silently never deduplicate -- the ineffective direction, but still
        wrong."""
        self.ok()
        self.feed_as("session-a", layer="worker")
        before = sorted(os.listdir(self.stamps))
        self.feed_as("session-b", layer="fleet")
        self.assertEqual(sorted(os.listdir(self.stamps)), before)

    def test_dedup_survives_the_two_layers_naming_the_owner_differently(self):
        """THE case that makes or breaks this mechanism.

        `owner` is documented in both manager commands as `<session id or pane id>`
        and the choice is left to the manager, so the SAME logical gate can carry a
        session id from one layer and a pane id from the other. Keyed on the cadence
        `gate_key(owner, text)` those two never match: every gate double-fires
        exactly as before, and the defect reads as fixed -- a silent no-op, which is
        strictly worse than no fix because it stops anyone looking.

        This is why the stamp keys on the normalised TEXT. The test pins the
        behaviour against the specific regression: if someone "tidies" stamp_key
        back to gate_key, this fails.
        """
        sent = self.ok()
        self.feed_as("session-a", layer="worker", gates=[{"owner": "pane-243", "text": self.GATE["text"]}])
        self.assertEqual(len(sent), 1, "the first layer must escalate")
        self.feed_as("session-b", layer="fleet", gates=[{"owner": "aedb1af7", "text": self.GATE["text"]}])
        self.assertEqual(
            len(sent), 1, "the second layer must NOT escalate the same question"
        )

    def test_two_genuinely_different_gates_do_not_collide(self):
        """The accepted cost of keying on text, bounded: different questions still
        escalate independently."""
        sent = self.ok()
        self.feed_as("session-a", layer="worker", gates=[{"owner": "pane-1", "text": "approve: make apply"}])
        self.feed_as("session-b", layer="fleet", gates=[{"owner": "pane-2", "text": "approve: make rollback"}])
        self.assertEqual(len(sent), 2, "a different question is a different gate")

    def test_the_same_question_from_two_sessions_collides_deliberately(self):
        """Stated rather than discovered later: keyed on text, two different
        sessions asking the identical question are treated as one. That is the
        INTENT -- the operator is being asked the same thing either way -- and the
        record keeps `owner` so the skip line shows which session was declined."""
        sent = self.ok()
        self.feed_as("session-a", layer="worker", gates=[{"owner": "pane-1", "text": "approve: make apply"}])
        self.feed_as("session-b", layer="fleet", gates=[{"owner": "pane-2", "text": "approve: make apply"}])
        self.assertEqual(len(sent), 1, "identical text is one question")
        stamp = self.mod.load_stamp(self.key())
        self.assertEqual(stamp["owner"], "pane-1", "the declined owner stays visible")


if __name__ == "__main__":
    unittest.main()
