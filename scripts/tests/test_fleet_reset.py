#!/usr/bin/env python3
"""Tests for scripts/fleet-reset.py.

Covers the three properties the verb is built around, each in both directions:

  * the confirm gate   — no token refuses and changes nothing; the token acts.
  * the dry-run        — inert (no mtime moves under any store) yet names every target.
  * the strip opt-in   — a state-only run leaves task files byte-identical, while a strip
                         run removes the links from NON-terminal tasks and from no terminal
                         one, and its count equals the dry-run's prediction.

Plus the ordering rule: sessions are stopped before their registry entries are removed, so a
killed pid is gone and the registry is empty afterwards.

The negative controls matter here as much as the positive ones. A strip that removes
everything passes "the non-terminal task lost its links" exactly as a correct one does, so
the terminal-unchanged assertion is what makes the first meaningful. Likewise a state clear
that deletes task files would pass "the store is empty"; the byte-identical assertion is the
other half.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "fleet-reset.py")

_spec = importlib.util.spec_from_file_location("fleet_reset", _SCRIPT)
fr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fr)

NON_TERMINAL = ("in_progress", "next")
TERMINAL = ("completed", "aborted", "hold")

DEFAULT_SID = "11111111-1111-1111-1111-111111111111"
LIVE_SID = "22222222-2222-2222-2222-222222222222"
DEAD_SID = "33333333-3333-3333-3333-333333333333"


def task_page(status, sid=DEFAULT_SID):
    """A task file carrying both link keys, in the shape the vault writes them."""
    return (
        "---\n"
        f"status: {status}\n"
        "page_type: task\n"
        f"claude_session_id: {sid}\n"
        "metrics_sessions:\n"
        f"    - session_id: {sid}\n"
        "      started_at: 2026-11-10T10:00:00+01:00\n"
        "      completed_at: 2026-11-10T11:00:00+01:00\n"
        "---\n"
        "Tags: [[Task]]\n"
        "\n"
        "---\n"
        "\n"
        "Body text that must survive.\n"
    )


def task_page_multi(status, primary=None, metrics=()):
    """A task file whose id set is built explicitly, for the multi-id cases."""
    body = "---\n" f"status: {status}\n" "page_type: task\n"
    if primary:
        body += f"claude_session_id: {primary}\n"
    if metrics:
        body += "metrics_sessions:\n"
        for sid in metrics:
            body += f"    - session_id: {sid}\n"
            body += "      started_at: 2026-11-10T10:00:00+01:00\n"
    body += "---\nTags: [[Task]]\n\n---\n\nBody text that must survive.\n"
    return body


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class FleetResetTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.stores = fr.Stores(self.root)
        os.makedirs(self.stores.sessions)
        os.makedirs(self.stores.worker_manager)
        os.makedirs(self.stores.open_items)
        self.tasks = os.path.join(self.root, "tasks")
        os.makedirs(self.tasks)

    # -- helpers -----------------------------------------------------------

    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fr.main(argv)
        return rc, out.getvalue()

    def seed_store(self):
        for i in range(3):
            with open(os.path.join(self.stores.worker_manager, f"m{i}.json"), "w") as fh:
                json.dump({"subject": f"s{i}"}, fh)
        for i in range(2):
            with open(os.path.join(self.stores.open_items, f"o{i}.json"), "w") as fh:
                json.dump({"items": []}, fh)
        with open(self.stores.holds, "w") as fh:
            json.dump({"version": 1, "holds": {"a": {}, "b": {}}}, fh)
        with open(self.stores.snapshot, "w") as fh:
            json.dump({"sessions": {}}, fh)

    def seed_tasks(self, sid_by_status=None):
        paths = {}
        for status in NON_TERMINAL + TERMINAL:
            sid = (sid_by_status or {}).get(status, DEFAULT_SID)
            path = os.path.join(self.tasks, f"{status}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(task_page(status, sid))
            paths[status] = path
        return paths

    @contextlib.contextmanager
    def verdicts(self, mapping, alive=()):
        """Stub the liveness instrument and the registry — the DECISION, not the probes.

        `crashed-managers`' tests take the same stance: each instrument has its own tests, so
        re-testing them here would only make this file flaky against the live endpoint.
        `alive` names the ids whose registry entry still backs a running pid.
        """
        orig_verdict, orig_alive = fr.session_verdict, fr.identity_alive
        backed = set(alive)
        fr.session_verdict = lambda sid: mapping.get(sid, "UNKNOWN")
        fr.identity_alive = lambda sid, sessions_dir=None: sid in backed
        try:
            yield
        finally:
            fr.session_verdict, fr.identity_alive = orig_verdict, orig_alive

    def mtimes(self):
        rows = {}
        for directory in (self.stores.sessions, self.stores.worker_manager, self.stores.open_items):
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                rows[path] = os.stat(path).st_mtime_ns
        for path in (self.stores.holds, self.stores.snapshot):
            if os.path.exists(path):
                rows[path] = os.stat(path).st_mtime_ns
        return rows

    # -- confirm gate ------------------------------------------------------

    def test_refuses_without_token_and_changes_nothing(self):
        self.seed_store()
        before = self.mtimes()
        rc, out = self.run_main(["--root", self.root])
        self.assertEqual(rc, 2)
        self.assertIn("--confirm fleet-reset", out)
        self.assertEqual(self.mtimes(), before)
        self.assertEqual(fr.count_jsons(self.stores.worker_manager), 3)

    def test_refuses_a_wrong_token(self):
        self.seed_store()
        rc, _out = self.run_main(["--root", self.root, "--confirm", "yes"])
        self.assertEqual(rc, 2)
        self.assertEqual(fr.count_jsons(self.stores.worker_manager), 3)

    # -- dry-run inertness -------------------------------------------------

    def test_dry_run_names_every_store_and_touches_nothing(self):
        self.seed_store()
        self.seed_tasks()
        before = self.mtimes()
        with self.verdicts({DEFAULT_SID: "ABSENT"}):
            rc, out = self.run_main(["--root", self.root, "--dry-run", "--strip-task-links",
                                     "--task-dir", self.tasks])
        self.assertEqual(rc, 0)
        self.assertEqual(self.mtimes(), before)
        for label in ("sessions", "worker-manager", "open-items", "session-holds",
                      "fleet-snapshot"):
            self.assertIn(label, out)
        # The predicted strip count is the number of non-terminal tasks carrying links.
        self.assertIn(f"{len(NON_TERMINAL)} candidate(s)", out)

    # -- state-only run ----------------------------------------------------

    def test_state_only_run_clears_the_five_stores(self):
        self.seed_store()
        rc, out = self.run_main(["--root", self.root, "--confirm", "fleet-reset"])
        self.assertEqual(rc, 0)
        self.assertEqual(fr.count_jsons(self.stores.worker_manager), 0)
        self.assertEqual(fr.count_jsons(self.stores.open_items), 0)
        self.assertFalse(os.path.exists(self.stores.holds))
        self.assertFalse(os.path.exists(self.stores.snapshot))
        self.assertIn("worker-manager   3 → 0", out)
        self.assertIn("open-items       2 → 0", out)
        self.assertIn("session-holds    2 → 0", out)
        self.assertIn("fleet-snapshot   1 → 0", out)

    def test_state_only_run_leaves_task_files_byte_identical(self):
        self.seed_store()
        paths = self.seed_tasks()
        before = {p: sha(p) for p in paths.values()}
        rc, _out = self.run_main(["--root", self.root, "--confirm", "fleet-reset"])
        self.assertEqual(rc, 0)
        self.assertEqual({p: sha(p) for p in paths.values()}, before)
        self.assertIn("NOT requested", _out)

    # -- strip opt-in ------------------------------------------------------

    def test_strip_removes_links_from_non_terminal_only(self):
        paths = self.seed_tasks()
        terminal_before = {paths[s]: sha(paths[s]) for s in TERMINAL}
        with self.verdicts({DEFAULT_SID: "ABSENT"}):
            rc, out = self.run_main([
                "--root", self.root, "--confirm", "fleet-reset",
                "--strip-task-links", "--task-dir", self.tasks,
            ])
        self.assertEqual(rc, 0)
        for status in NON_TERMINAL:
            with open(paths[status], encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("claude_session_id", text, status)
            self.assertNotIn("metrics_sessions", text, status)
            self.assertIn("status: %s" % status, text)
            self.assertIn("Body text that must survive.", text)
        # The too-loose direction: a strip that removes everything also passes the loop
        # above, so the terminal files must be proven untouched.
        self.assertEqual({paths[s]: sha(paths[s]) for s in TERMINAL}, terminal_before)
        self.assertIn(f"{len(NON_TERMINAL)} candidate(s) → {len(NON_TERMINAL)} stripped", out)

    def test_strip_count_equals_the_dry_run_prediction(self):
        self.seed_tasks()
        predicted = len(NON_TERMINAL)
        with self.verdicts({DEFAULT_SID: "ABSENT"}):
            _rc, dry = self.run_main(["--root", self.root, "--dry-run", "--strip-task-links",
                                      "--task-dir", self.tasks])
            self.assertIn(f"{predicted} candidate(s)", dry)
            _rc, real = self.run_main([
                "--root", self.root, "--confirm", "fleet-reset",
                "--strip-task-links", "--task-dir", self.tasks,
            ])
        self.assertIn(f"{predicted} candidate(s) → {predicted} stripped", real)

    # -- strip is liveness-gated -------------------------------------------

    def test_strip_keeps_a_live_session_and_strips_a_dead_one(self):
        paths = self.seed_tasks({"in_progress": LIVE_SID, "next": DEAD_SID})
        live_before = sha(paths["in_progress"])
        with self.verdicts({LIVE_SID: "LIVE", DEAD_SID: "ABSENT"}):
            rc, out = self.run_main([
                "--root", self.root, "--confirm", "fleet-reset",
                "--strip-task-links", "--task-dir", self.tasks,
            ])
        self.assertEqual(rc, 0)
        # The too-loose direction: a status-only strip removes both, so the live file must
        # be proven byte-identical and the dead one proven stripped.
        self.assertEqual(sha(paths["in_progress"]), live_before)
        with open(paths["in_progress"], encoding="utf-8") as fh:
            self.assertIn(f"claude_session_id: {LIVE_SID}", fh.read())
        with open(paths["next"], encoding="utf-8") as fh:
            self.assertNotIn("claude_session_id", fh.read())
        self.assertIn("1 candidate(s) → 1 stripped", out)
        self.assertIn("kept             1 task(s)", out)

    def test_strip_keeps_an_unreadable_probe(self):
        paths = self.seed_tasks({"in_progress": LIVE_SID})
        before = {p: sha(p) for p in paths.values()}
        # Every id answers UNKNOWN — the probe could not run. That is not evidence of death.
        with self.verdicts({}):
            rc, out = self.run_main([
                "--root", self.root, "--confirm", "fleet-reset",
                "--strip-task-links", "--task-dir", self.tasks,
            ])
        self.assertEqual(rc, 0)
        self.assertEqual({p: sha(p) for p in paths.values()}, before)
        self.assertIn("0 candidate(s) → 0 stripped", out)
        self.assertIn(f"kept             {len(NON_TERMINAL)} task(s)", out)

    def test_strip_keeps_a_session_the_registry_still_backs(self):
        """`ABSENT` alone is not death — measured 2026-11-10, where `--check 4295ddac`
        answered `ABSENT` for a session whose pid 26515 was running. The registry
        corroboration is the half that keeps the link."""
        paths = self.seed_tasks({"in_progress": LIVE_SID})
        before = sha(paths["in_progress"])
        with self.verdicts({LIVE_SID: "ABSENT"}, alive=[LIVE_SID]):
            rc, out = self.run_main([
                "--root", self.root, "--confirm", "fleet-reset",
                "--strip-task-links", "--task-dir", self.tasks,
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(sha(paths["in_progress"]), before)
        self.assertIn("0 candidate(s) → 0 stripped", out)

    def test_strip_keeps_a_task_whose_metrics_session_is_live(self):
        """The strip removes the whole `metrics_sessions` block, and that block can name a
        different, live session (`server/task-binding.mjs` appends a second worker and
        leaves `claude_session_id` intact). Gating on the frontmatter id alone unlinks it."""
        path = os.path.join(self.tasks, "multi.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(task_page_multi("in_progress", primary=DEAD_SID, metrics=[LIVE_SID]))
        before = sha(path)
        with self.verdicts({DEAD_SID: "ABSENT", LIVE_SID: "LIVE"}):
            rc, out = self.run_main([
                "--root", self.root, "--confirm", "fleet-reset",
                "--strip-task-links", "--task-dir", self.tasks,
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(sha(path), before)
        self.assertIn("0 candidate(s) → 0 stripped", out)

    def test_strip_removes_a_metrics_only_task_when_its_id_is_dead(self):
        """A task carrying no `claude_session_id` but a dead `metrics_sessions` id is still
        a strip candidate — 64 of 124 such tasks measured in the Brogrammers vault."""
        path = os.path.join(self.tasks, "metrics-only.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(task_page_multi("in_progress", metrics=[DEAD_SID]))
        with self.verdicts({DEAD_SID: "ABSENT"}):
            rc, out = self.run_main([
                "--root", self.root, "--confirm", "fleet-reset",
                "--strip-task-links", "--task-dir", self.tasks,
            ])
        self.assertEqual(rc, 0)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("metrics_sessions", text)
        self.assertIn("1 candidate(s) → 1 stripped", out)

    # -- the fail-closed machinery itself (never stubbed by `verdicts`) -----

    def test_a_bare_exit_1_is_unknown_not_absent(self):
        """`1` is both the instrument's ABSENT code and Python's uncaught-exception code,
        and ABSENT is the one verdict that authorises the destructive unlink."""
        self.assertEqual(fr._verdict_from(1, ""), "UNKNOWN")
        self.assertEqual(fr._verdict_from(1, "Traceback (most recent call last):"), "UNKNOWN")
        self.assertEqual(fr._verdict_from(1, "ABSENT — 4295ddac-ba83"), "ABSENT")
        self.assertEqual(fr._verdict_from(0, "LIVE 4295ddac"), "LIVE")
        self.assertEqual(fr._verdict_from(2, "UNKNOWN"), "UNKNOWN")

    def test_an_absent_registry_answers_alive(self):
        """`glob` on a missing directory returns `[]`, so without the isdir guard an absent
        registry would read as "nothing is live" and permit the strip."""
        missing = os.path.join(self.root, "no-such-registry")
        empty = os.path.join(self.root, "empty-registry")
        os.makedirs(empty)
        self.assertTrue(fr.identity_alive(DEAD_SID, missing))
        self.assertFalse(fr.identity_alive(DEAD_SID, empty))

    def test_dry_run_prediction_counts_only_strippable(self):
        self.seed_tasks({"in_progress": LIVE_SID, "next": DEAD_SID})
        with self.verdicts({LIVE_SID: "LIVE", DEAD_SID: "ABSENT"}):
            _rc, dry = self.run_main(["--root", self.root, "--dry-run", "--strip-task-links",
                                      "--task-dir", self.tasks])
            _rc, real = self.run_main([
                "--root", self.root, "--confirm", "fleet-reset",
                "--strip-task-links", "--task-dir", self.tasks,
            ])
        # The prediction is the strip's own count, not every linked task in sight.
        self.assertIn("1 candidate(s)", dry)
        self.assertIn("1 candidate(s) → 1 stripped", real)

    def test_strip_with_no_task_dir_refuses(self):
        # An empty vault config is the "no task dir resolves" state — without it the
        # fallback reads the machine's real vault-cli config and finds directories.
        config = os.path.join(self.root, "vaults.json")
        with open(config, "w") as fh:
            json.dump([], fh)
        os.environ["SUPERVISOR_VAULT_CONFIG"] = config
        try:
            rc, out = self.run_main([
                "--root", self.root, "--confirm", "fleet-reset", "--strip-task-links",
            ])
        finally:
            os.environ.pop("SUPERVISOR_VAULT_CONFIG", None)
        self.assertEqual(rc, 2)
        self.assertIn("no task directory", out)

    # -- ordering: stop before registry clear ------------------------------

    def test_sessions_are_stopped_before_the_registry_is_cleared(self):
        child = subprocess.Popen(["sleep", "30"])
        try:
            path = os.path.join(self.stores.sessions, f"{child.pid}.json")
            with open(path, "w") as fh:
                json.dump({"pid": child.pid, "sessionId": "22222222-2222-2222-2222-222222222222",
                           "name": "test-session"}, fh)
            os.environ["SUPERVISOR_FLEET_RESET_PROC_PATTERN"] = "sleep"
            try:
                rc, out = self.run_main(["--root", self.root, "--confirm", "fleet-reset",
                                         "--grace", "2"])
            finally:
                os.environ.pop("SUPERVISOR_FLEET_RESET_PROC_PATTERN", None)
            self.assertEqual(rc, 0, out)
            self.assertIsNotNone(child.poll(), "the pid must be gone")
            self.assertEqual(fr.count_jsons(self.stores.sessions), 0)
            self.assertIn("stopped", out)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_a_pid_of_the_wrong_shape_is_kept_and_reported(self):
        child = subprocess.Popen(["sleep", "30"])
        try:
            path = os.path.join(self.stores.sessions, f"{child.pid}.json")
            with open(path, "w") as fh:
                json.dump({"pid": child.pid, "sessionId": "33333333-3333-3333-3333-333333333333",
                           "name": "not-claude"}, fh)
            os.environ["SUPERVISOR_FLEET_RESET_PROC_PATTERN"] = "claude"
            try:
                rc, out = self.run_main(["--root", self.root, "--confirm", "fleet-reset",
                                         "--grace", "1"])
            finally:
                os.environ.pop("SUPERVISOR_FLEET_RESET_PROC_PATTERN", None)
            self.assertEqual(rc, 3, out)
            self.assertTrue(os.path.exists(path), "a refused entry must be kept")
            self.assertIsNone(child.poll(), "a refused process must not be signalled")
            self.assertIn("survived", out)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


if __name__ == "__main__":
    unittest.main()
