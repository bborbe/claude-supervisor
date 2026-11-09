#!/usr/bin/env python3
"""Tests for scripts/subject-reap.py — the manager-subject reap.

The leg exists because the reap contract (`agents/fleet-drive.md` step 1b) reads three
facts from a **task** file, and a manager's subject is a goal or topic page. Measured
2026-10-30: a completed goal whose manager session was `LIVE` with its loop `STOPPED`,
idle that way for 31 minutes.

Load-bearing properties, each pinned against a fixture vault so the assertion cannot pass
on this machine's real fleet:

  * a terminal subject + a LIVE manager is the ONLY reap — one line, naming the subject,
    the session and the three reads it was decided from (SC1, SC3);
  * an OPEN subject is never scanned, whatever its manager's state — the positive control,
    load-bearing: the restart leg owns those and a reap here would close live work (SC2);
  * no id / a malformed id / an `ABSENT` manager is silence, not a report — and the four
    fixtures together produce exactly one reap (SC4);
  * the script kills nothing and writes no session state — no `signal`, no `os.kill`, no
    write under a state path (SC3).

Run: python3 -m unittest discover -s scripts/tests -v
"""

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_SCRIPTS, "subject-reap.py")

sys.path.insert(0, _HERE)
import endpoint_fixture  # noqa: E402  — the store stand-in, same dir

# Two ids, one live in the fixture store and one it has never heard of.
LIVE_ID = "11111111-2222-4333-8444-555555555555"
GONE_ID = "99999999-8888-4777-8666-555555555555"
NOT_AN_ID = "not-a-session-id"


def load():
    spec = importlib.util.spec_from_file_location("subject_reap", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load()


def write_page(directory, name, body):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name + ".md"), "w", encoding="utf-8") as handle:
        handle.write("---\n" + body + "---\n\nbody\n")


class FixtureVault:
    """A throwaway vault root: `24 Goals/` and `23 Topics/` the scan reads."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="subject-reap-")
        self.goals = os.path.join(self.root, "24 Goals")
        self.topics = os.path.join(self.root, "23 Topics")
        os.makedirs(self.goals)
        os.makedirs(self.topics)

    def goal(self, name, **fields):
        write_page(self.goals, name, "".join("%s: %s\n" % kv for kv in fields.items()))

    def topic(self, name, **fields):
        write_page(self.topics, name, "".join("%s: %s\n" % kv for kv in fields.items()))


def run(vault, endpoint, registry_dir, *extra):
    return subprocess.run(
        [sys.executable, _SCRIPT, "--vault-root", vault.root,
         "--endpoint", endpoint, "--registry-dir", registry_dir] + list(extra),
        capture_output=True, text=True,
    )


class ReapTest(unittest.TestCase):
    def setUp(self):
        self.vault = FixtureVault()
        # An EMPTY identity registry: readable, and holding nothing. ABSENT is licensed
        # only while the registry can be read, so an absent directory would turn the
        # `ABSENT` fixture into `UNKNOWN` and the silence assertion into a false pass.
        self.registry = tempfile.mkdtemp(prefix="subject-reap-registry-")

    # --- SC1 / SC3: terminal subject + live manager is the reap -----------------------

    def test_terminal_subject_with_live_manager_reaps(self):
        self.vault.goal("Done Goal", status="completed", manager_session_id=LIVE_ID)
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("✅ REAP:"), 1, result.stdout)
        self.assertIn("Done Goal", result.stdout)
        self.assertIn(LIVE_ID, result.stdout)
        # The three reads the decision was made from ride the line (SC3).
        self.assertIn("subject goal status: completed", result.stdout)
        self.assertIn("manager_session_id: %s" % LIVE_ID, result.stdout)
        self.assertIn("liveness: LIVE", result.stdout)
        self.assertIn("SUMMARY | terminal=1 reap=1 silent=0 unknown=0", result.stdout)

    def test_aborted_subject_reaps_too(self):
        self.vault.topic("Dropped Topic", status="aborted", manager_session_id=LIVE_ID)
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry)
        self.assertIn("✅ REAP:", result.stdout)
        self.assertIn("subject topic status: aborted", result.stdout)

    def test_retired_field_is_read_as_a_fallback(self):
        # `claude_session_id` is the current name; a page still carrying only the retired
        # `manager_session_id` must not read as "no manager recorded".
        self.vault.goal("Legacy Goal", status="completed", manager_session_id=LIVE_ID)
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry)
        self.assertIn("✅ REAP:", result.stdout)

    # --- SC2: an open subject is never reaped, whatever its manager's state -----------

    def test_open_subject_is_never_reaped(self):
        # The positive control, load-bearing: same LIVE manager, non-terminal subject.
        self.vault.goal("Open Goal", status="in_progress", manager_session_id=LIVE_ID)
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("✅ REAP:", result.stdout)
        self.assertNotIn("Open Goal", result.stdout)
        self.assertIn("SUMMARY | terminal=0 reap=0 silent=0 unknown=0", result.stdout)

    def test_hold_is_not_terminal(self):
        # A held subject is paused, not finished — its manager may legitimately wait on it.
        self.vault.goal("Held Goal", status="hold", manager_session_id=LIVE_ID)
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry)
        self.assertNotIn("✅ REAP:", result.stdout)

    # --- SC4: absent / none / malformed / ABSENT produce exactly one reap --------------

    def test_four_fixtures_produce_exactly_one_reap(self):
        self.vault.goal("Live Manager", status="completed", manager_session_id=LIVE_ID)
        self.vault.goal("No Manager", status="completed")
        self.vault.goal("Bad Manager", status="completed", claude_session_id=NOT_AN_ID)
        self.vault.goal("Gone Manager", status="aborted", manager_session_id=GONE_ID)
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("✅ REAP:"), 1, result.stdout)
        self.assertIn("Live Manager", result.stdout)
        # Silence, not a false report: none of the other three is named in a reap line.
        reap_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("✅ REAP:")]
        self.assertEqual(len(reap_lines), 1)
        for name in ("No Manager", "Bad Manager", "Gone Manager"):
            self.assertNotIn(name, reap_lines[0])
        self.assertIn("SUMMARY | terminal=4 reap=1 silent=3 unknown=0", result.stdout)

    def test_absent_is_silent_not_unknown(self):
        self.vault.goal("Gone Manager", status="completed", manager_session_id=GONE_ID)
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry)
        self.assertIn("SILENT |", result.stdout)
        self.assertIn("liveness: ABSENT", result.stdout)

    def test_malformed_is_silent(self):
        self.vault.goal("Bad Manager", status="completed", claude_session_id=NOT_AN_ID)
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry)
        self.assertIn("SILENT |", result.stdout)
        self.assertIn("liveness: MALFORMED", result.stdout)

    def test_unreachable_store_is_unknown_never_a_reap(self):
        self.vault.goal("Live Manager", status="completed", manager_session_id=LIVE_ID)
        result = run(self.vault, endpoint_fixture.dead_url(), self.registry)
        self.assertNotIn("✅ REAP:", result.stdout)
        self.assertIn("UNKNOWN |", result.stdout)

    # --- SC4: one ack card per reaped subject, and none for the silent rows -----------

    def test_card_key_is_one_per_subject_and_vault(self):
        # The key, not the post: a stable key is what makes a second round re-post nothing.
        self.assertEqual(M.card_key("v", "A Goal"), "manager-subject-reap:v:A Goal")
        self.assertNotEqual(M.card_key("v", "A Goal"), M.card_key("v", "B Goal"))
        # Titles are not namespaced across vaults, so the vault must be in the key — a
        # title-only key would let one vault's card suppress the other's reap.
        self.assertNotEqual(M.card_key("v1", "A Goal"), M.card_key("v2", "A Goal"))

    # --- exit codes: an unreadable root is REFUSED, never a clean scan ----------------

    def test_missing_vault_root_is_refused(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, "--vault-root", "/nonexistent/subject-reap-root"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("UNKNOWN", result.stderr)

    def test_root_with_no_goals_or_topics_dir_is_refused(self):
        # A root with neither directory would scan nothing and report `terminal=0` — a
        # clean-looking round for a vault that has no subjects at all.
        bare = tempfile.mkdtemp(prefix="subject-reap-bare-")
        result = subprocess.run(
            [sys.executable, _SCRIPT, "--vault-root", bare],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("no goals or topics directory", result.stderr)

    def test_undecodable_page_is_reported_not_dropped(self):
        # An unreadable page could be a terminal subject carrying a live manager; skipping
        # it silently would suppress exactly the reap this leg exists for.
        with open(os.path.join(self.vault.goals, "Broken.md"), "wb") as handle:
            handle.write(b"---\nstatus: completed\n\xff\xfe not utf-8\n---\n")
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry)
        self.assertIn("UNKNOWN |", result.stdout)
        self.assertIn("page unreadable", result.stdout)
        self.assertNotIn("✅ REAP:", result.stdout)

    def test_json_emits_the_same_computation(self):
        self.vault.goal("Live Manager", status="completed", manager_session_id=LIVE_ID)
        self.vault.goal("No Manager", status="completed")
        with endpoint_fixture.FixtureEndpoint(rows=[endpoint_fixture.row(LIVE_ID)]) as fx:
            result = run(self.vault, fx.url, self.registry, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["reap"], 1)
        by_name = {row["subject"]: row for row in payload["rows"]}
        self.assertEqual(by_name["Live Manager"]["verdict"], "reap")
        self.assertEqual(by_name["Live Manager"]["liveness"], "LIVE")
        self.assertIn("✅ REAP:", by_name["Live Manager"]["line"])
        self.assertEqual(by_name["No Manager"]["verdict"], "silent")
        self.assertIsNone(by_name["No Manager"]["line"])

    # --- SC3: the leg kills nothing and writes no session state ----------------------

    def test_line_addresses_the_roster_name_not_the_id(self):
        # `SendMessage` takes the roster name; the session id is the identity, not an
        # address. A line still carrying the id is UNaddressable and the step says so.
        named = M.reap.subject_reap_line(
            "A Goal", "goal", "completed", LIVE_ID, "live", roster="fleet--a-topic")
        self.assertIn("SendMessage fleet--a-topic:", named)
        self.assertNotIn("SendMessage %s" % LIVE_ID, named)
        # The id still rides the line as the identity and the re-check subject.
        self.assertIn(LIVE_ID, named)
        unnamed = M.reap.subject_reap_line("A Goal", "goal", "completed", LIVE_ID, "live")
        self.assertIn("SendMessage %s" % LIVE_ID, unnamed)

    def test_no_kill_and_no_state_write(self):
        # Parsed, not grepped: the module docstring states the rule in prose ("there is no
        # `os.kill`, no `signal`"), and a substring scan would fail on the very sentence
        # that documents the property. AST asks the code what it does.
        with open(_SCRIPT, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertNotIn("signal", imported)

        calls = [ast.unparse(node.func) for node in ast.walk(tree)
                 if isinstance(node, ast.Call)]
        self.assertEqual([c for c in calls if "kill" in c], [])
        # No file is opened for WRITING — reads of the subject pages are the whole point.
        # The leg's only write is the attention card, posted through `attention-ask.py` as a
        # subprocess, so nothing here touches a state path.
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func) in ("open", "io.open")):
                continue
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for keyword in node.keywords:
                if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                    mode = str(keyword.value.value)
            self.assertFalse(
                any(flag in mode for flag in ("w", "a", "x", "+")),
                "the leg opened a file for writing (mode %r) at line %s" % (mode, node.lineno),
            )
        # And the emitted line carries no kill either.
        line = M.reap.subject_reap_line("A Goal", "goal", "completed", LIVE_ID, "live")
        self.assertNotIn("kill", line)
        self.assertIn("SendMessage", line)
        # The moot render is shared with the task form: only `live` names a recipient.
        self.assertEqual(
            M.reap.subject_reap_line("A Goal", "goal", "completed", LIVE_ID, "absent"),
            M.reap.MOOT,
        )


if __name__ == "__main__":
    unittest.main()
