#!/usr/bin/env python3
"""Tests for scripts/manager-predispatch.py.

The load-bearing properties, in the order the criteria grade them:

1. A no-change tree reports NOCHANGE and a change reports CHANGE — the saving itself.
2. **Liveness is in the digest**, so a worker dying moves it. Without this the gate
   would replay a stored table straight over a death.
3. **The negative control**: a LIVE worker whose heartbeat is older than the TTL must
   NOT read as dead. Property 2 alone is satisfied by a build that answers "dead" too
   eagerly and mis-reports live workers — positive without negative is a probe that
   cannot fail, so both are asserted here.
4. Fail-open: a missing, unreadable or table-less state file, and an unresolvable
   subject, all report CHANGE rather than "no change".
5. The stored table is written link-free and 0600 — the rendered frame's OSC 8 jump
   links carry the jump token, and this file lives on disk to be copied and pasted.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import stat
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "manager-predispatch.py")

TASK = """---
status: in_progress
phase: execution
claude_session_id: {sid}
goals:
    - '[[AGoal]]'
---
Tags: [[Task]]

---

# Tasks

- [ ] one
- [x] two

# Progress

{progress}
"""

TOPIC = """---
page_type: topic
---
Tags: [[Topic]]

---

## Goals

- [[AGoal]]
- [[ATask]]
"""

GOAL = """---
page_type: goal
---
Tags: [[Goal]]

---

# Tasks

1. [[ATask]]
"""


def load(state_dir):
    os.environ["MANAGER_PREDISPATCH_STATE_DIR"] = state_dir
    spec = importlib.util.spec_from_file_location("manager_predispatch", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = os.path.join(self.tmp, "vault")
        for d in ("25 Tasks", "23 Topics", "24 Goals"):
            os.makedirs(os.path.join(self.vault, d))
        self.m = load(os.path.join(self.tmp, "state"))
        self.m.REGISTRY_DIR = os.path.join(self.tmp, "sessions")
        self.m.FEED_DIR = os.path.join(self.tmp, "attention")
        self.m.HEARTBEAT_DIR = os.path.join(self.tmp, "live")
        for d in (self.m.REGISTRY_DIR, self.m.FEED_DIR, self.m.HEARTBEAT_DIR):
            os.makedirs(d)
        self.write("23 Topics/ATopic.md", TOPIC)
        self.write("24 Goals/AGoal.md", GOAL)
        self.task("ATask", sid="")
        self.task("AGoalTask", sid="", goals="AGoal")

    def write(self, rel, text):
        with open(os.path.join(self.vault, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def task(self, name, sid="", progress="", goals=None):
        self.write(
            "25 Tasks/%s.md" % name,
            TASK.format(sid=sid, progress=progress).replace(
                "[[AGoal]]", "[[%s]]" % (goals or "AGoal")
            ),
        )

    def registry(self, sid, status="idle"):
        with open(os.path.join(self.m.REGISTRY_DIR, "1.json"), "w") as fh:
            json.dump({"sessionId": sid, "pid": os.getpid(), "status": status}, fh)

    def heartbeat(self, sid, age):
        p = os.path.join(self.m.HEARTBEAT_DIR, "%s.json" % sid)
        with open(p, "w") as fh:
            fh.write("{}")
        os.utime(p, (time.time() - age, time.time() - age))

    def run_gate(self, *argv, stdin=""):
        """-> (rc, stdout). stdin is fed only to --save."""
        import sys

        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(stdin), io.StringIO()
        try:
            rc = self.m.main(list(argv) + ["--vault", self.vault])
            return rc, sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out

    def save(self, subject, table="Subject: x\n+---+\n| a |\n+---+\n"):
        return self.run_gate("--subject", subject, "--save", stdin=table)

    def check(self, subject):
        return self.run_gate("--subject", subject, "--check")

    def prime(self, subject):
        self.save(subject)
        rc, out = self.check(subject)
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)

    def read_state(self, subject):
        with open(self.m.state_path(subject), encoding="utf-8") as fh:
            return fh.read()


class TestVerdict(Base):
    def test_first_run_is_change_not_nochange(self):
        """Fail-open: a gate with no state cannot claim 'no change'."""
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("first run", out)

    def test_unchanged_tree_is_nochange(self):
        self.prime("ATopic")
        rc, _ = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE)

    def test_status_flip_is_change(self):
        self.prime("ATopic")
        self.task("ATask", sid="", progress="")  # rewrite with a different status
        self.write(
            "25 Tasks/ATask.md",
            TASK.format(sid="", progress="").replace("in_progress", "completed"),
        )
        rc, _ = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)

    def test_progress_write_is_change(self):
        """The Progress hash is the signal that status/phase alone would miss."""
        self.prime("ATopic")
        self.task("ATask", sid="", progress="- 2026-09-26: moved.")
        rc, _ = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)

    def test_print_replays_stored_table(self):
        self.save("ATopic", "Subject: x\n+---+\n| a |\n+---+\n")
        rc, out = self.run_gate("--subject", "ATopic", "--print")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE)
        self.assertIn(self.m.NO_CHANGE_MARKER, out)
        self.assertIn("| a |", out)

    def test_goal_and_topic_both_resolve(self):
        for subject in ("ATopic", "AGoal"):
            rc, out = self.check(subject)
            self.assertEqual(rc, self.m.EXIT_CHANGE, subject)
            self.assertNotIn("unresolvable", out)

    def test_unknown_subject_fails_open(self):
        rc, out = self.check("No Such Subject")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("fail-open", out)


class TestLiveness(Base):
    """Property 2 and its negative control — the pair SC3 is graded on."""

    def digest_with(self, sid):
        self.task("ATask", sid=sid)
        _, _, payload, _ = self.m.evaluate(self.vault, "ATopic")
        return self.m.digest_of(payload["tracked"])

    def test_dead_worker_moves_the_digest(self):
        """A worker dying is a change — it must never be swallowed."""
        live = self.digest_with("s-live")
        self.registry("s-live")
        live_now = self.digest_with("s-live")
        dead = self.digest_with("s-dead")
        self.assertNotEqual(live, dead, "an absent session did not move the digest")
        self.assertNotEqual(live, live_now, "gaining a live session did not move the digest")

    def test_live_worker_with_stale_heartbeat_is_not_dead(self):
        """The NEGATIVE CONTROL.

        A live worker whose heartbeat is older than the TTL, but whose session is
        still in the registry against a running pid, must read live. A build that
        decides 'dead' by heartbeat age alone passes the positive test above and
        fails this one.
        """
        self.heartbeat("s-live", age=self.m.HEARTBEAT_TTL_SECONDS * 100)
        self.registry("s-live")
        tracked = [{"name": "T", "session": "s-live"}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_LIVE)

    def test_headless_worker_with_fresh_heartbeat_is_live(self):
        """The case the registry structurally cannot see."""
        self.heartbeat("s-headless", age=1)
        tracked = [{"name": "T", "session": "s-headless"}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_LIVE)

    def test_stale_heartbeat_without_registry_is_none(self):
        """A killed server never clears its stamps, so age — not existence — decides."""
        self.heartbeat("s-gone", age=self.m.HEARTBEAT_TTL_SECONDS * 100)
        tracked = [{"name": "T", "session": "s-gone"}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_NONE)

    def test_waiting_session_is_parked_not_live(self):
        """A newly-blocked worker is a change too."""
        self.registry("s-wait", status="waiting")
        tracked = [{"name": "T", "session": "s-wait"}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_PARKED)


class TestFailOpen(Base):
    def test_corrupt_state_is_change(self):
        self.save("ATopic")
        with open(self.m.state_path("ATopic"), "w") as fh:
            fh.write("{not json")
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("parse error", out)

    def test_state_without_a_table_is_change(self):
        """A snapshot with no table cannot be replayed, so it cannot make a run free."""
        self.save("ATopic")
        data = json.loads(self.read_state("ATopic"))
        data["table"] = ""
        with open(self.m.state_path("ATopic"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("no stored table", out)

    def test_empty_table_is_refused_and_state_untouched(self):
        self.save("ATopic", "good table\n")
        before = self.read_state("ATopic")
        rc, _ = self.run_gate("--subject", "ATopic", "--save", stdin="   \n")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertEqual(self.read_state("ATopic"), before)


class TestStorage(Base):
    def test_table_is_stored_link_free(self):
        """The OSC 8 URI carries the jump token; the on-disk copy must not."""
        link = "\x1b]8;;http://127.0.0.1:1337/jump?pane=9&t=SECRET\x07Session\x1b]8;;\x07"
        self.save("ATopic", "Subject: x\n%s\n" % link)
        stored = json.loads(self.read_state("ATopic"))["table"]
        self.assertNotIn("SECRET", stored)
        self.assertIn("Session", stored)

    def test_state_file_is_0600(self):
        self.save("ATopic")
        mode = stat.S_IMODE(os.stat(self.m.state_path("ATopic")).st_mode)
        self.assertEqual(mode, 0o600, "state file is %o, want 600" % mode)

    def test_save_reports_change_on_success(self):
        """Exit 10 from --save means 'this run saw a change', not a failure."""
        rc, out = self.save("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("SAVED", out)


class TestVaultRootBoundary(Base):
    """A bad `--vault` is a usage error, never a missing subject page.

    Given a vault NAME the gate joined it as a relative path, both `os.path.exists`
    calls were False, and `resolve_subject` blamed the SUBJECT — reporting a page that
    is present on disk as absent. Two things have to hold: the argument is named as the
    culprit, and the subject message stays reserved for a real root.
    """

    def run_gate_raw(self, *argv, stdin=""):
        """-> (rc, stdout+stderr) with no `--vault` appended by the caller."""
        import sys

        old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = (
            io.StringIO(stdin),
            io.StringIO(),
            io.StringIO(),
        )
        try:
            rc = self.m.main(list(argv))
            return rc, sys.stdout.getvalue() + sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err

    def test_vault_name_is_a_usage_error_not_a_missing_page(self):
        rc, out = self.run_gate_raw("--vault", "vault", "--subject", "ATopic", "--check")
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertIn("--vault", out)
        self.assertNotIn("no subject page", out)

    def test_vault_name_is_rejected_from_the_vaults_own_parent(self):
        """The regression: from the vault's PARENT a name resolved to a real vault and
        gated it silently, so the same call meant different trees in different cwds."""
        old = os.getcwd()
        os.chdir(os.path.dirname(self.vault))
        try:
            rc, out = self.run_gate_raw(
                "--vault",
                os.path.basename(self.vault),
                "--subject",
                "ATopic",
                "--check",
            )
        finally:
            os.chdir(old)
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertNotIn("no subject page", out)

    def test_non_vault_directory_is_a_usage_error(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        rc, out = self.run_gate_raw("--vault", plain, "--subject", "ATopic", "--check")
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertIn("not a vault root", out)
        self.assertNotIn("no subject page", out)

    def test_missing_absolute_path_is_a_usage_error(self):
        rc, out = self.run_gate_raw(
            "--vault", os.path.join(self.tmp, "nope"), "--subject", "ATopic", "--check"
        )
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertIn("not a directory", out)

    def test_absent_subject_under_a_real_root_still_fails_open(self):
        """Negative control: the boundary guard must not swallow the fail-open path a
        genuinely absent subject relies on, or the fix would trade one blind gate for
        another."""
        rc, out = self.run_gate_raw(
            "--vault", self.vault, "--subject", "No Such Subject", "--check"
        )
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertIn("no subject page", out)


if __name__ == "__main__":
    unittest.main()
