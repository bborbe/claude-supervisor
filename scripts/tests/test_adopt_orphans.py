#!/usr/bin/env python3
"""Tests for scripts/adopt-orphans.py.

Each case guards a way the adoption can silently do the wrong thing:

  * a worker whose manager is GONE is adopted; one whose manager is still live is not --
    the negative control for the whole script, since adopting a live manager's workers
    would take their gates away from the manager that exists to route them.
  * a spawner that is itself a worker is not a manager. `is_manager` is imported from the
    filter rather than reimplemented, and this pins the import: a spawner with a ledger
    record and no `role` reads as a worker, so its workers are not stranded.
  * a DEAD worker is not adopted. It holds no gate, and counting it would inflate the
    tick's adoption line with sessions nothing routes to.
  * the successor branch is resolved by SUBJECT, and every ambiguous shape falls back
    rather than picking: exactly one match adopts, two matches fall back to the tick's own
    session. Ambiguity is the case a "pick the first" implementation gets wrong, and it
    gets it wrong silently by silencing one of the two managers.
  * an unreadable registry is UNKNOWN, not empty. Reading it as empty makes every manager
    look exited and hands the whole fleet's workers to the fallback on no evidence.
  * a claim HELD by a live manager is the single-owner guarantee, not a failure -- it is
    reported apart from the adoption count and never taken over.
  * `--dry-run` writes nothing.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "adopt-orphans.py")

_spec = importlib.util.spec_from_file_location("adopt_orphans", _SCRIPT)
ao = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ao)

# The tick's own session -- the fallback adopter.
ME = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
# An exited manager: in the ledger as a manager (no record of its own), absent from the registry.
EXITED = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
# A live same-subject successor.
SUCCESSOR = "cccccccc-3333-4333-8333-cccccccccccc"
# A second live session recording the SAME subject -- makes the successor ambiguous.
RIVAL = "dddddddd-4444-4444-8444-dddddddddddd"
# A live manager serving a different subject.
PEER = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"
# A live worker spawned by EXITED.
WORKER = "ffffffff-6666-4666-8666-ffffffffffff"
# A live worker spawned by PEER.
PEER_WORKER = "99999999-7777-4777-8777-999999999999"

SUBJECT = "Manager Layer"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adopt-orphans-test-")
        self.ledger_dir = os.path.join(self.tmp, "sessions")
        self.registry_dir = os.path.join(self.tmp, "registry")
        self.heartbeat_dir = os.path.join(self.tmp, "heartbeat")
        self.subjects_dir = os.path.join(self.tmp, "worker-manager")
        for path in (self.ledger_dir, self.registry_dir, self.heartbeat_dir, self.subjects_dir):
            os.makedirs(path)
        self.claims_file = os.path.join(self.tmp, "ownership-claims.json")
        self._pids = 0

    def spawn(self, session_id, parent=None, role=None):
        """A spawn ledger record. `role` is written only when given."""
        record = {"session_id": session_id, "parent_session": parent, "label": session_id[:8]}
        if role:
            record["role"] = role
        with open(os.path.join(self.ledger_dir, session_id + ".json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh)

    def live(self, session_id):
        """A registry entry -- the liveness channel that answers for an interactive session."""
        self._pids += 1
        with open(
            os.path.join(self.registry_dir, "%d.json" % self._pids), "w", encoding="utf-8"
        ) as fh:
            json.dump({"sessionId": session_id, "pid": self._pids, "status": "running"}, fh)

    def subject(self, session_id, text):
        """A subject-resolution record, as `docs/subject-resolution.md` § Recording writes it."""
        with open(
            os.path.join(self.subjects_dir, session_id + ".json"), "w", encoding="utf-8"
        ) as fh:
            json.dump(
                {"subject": text, "vault": "personal", "branch": "topic", "resolved_at": "x"}, fh
            )

    def run_script(self, *extra):
        return subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "--self",
                ME,
                "--ledger-dir",
                self.ledger_dir,
                "--registry-dir",
                self.registry_dir,
                "--heartbeat-dir",
                self.heartbeat_dir,
                "--subjects-dir",
                self.subjects_dir,
                "--claims-file",
                self.claims_file,
            ]
            + list(extra),
            capture_output=True,
            text=True,
        )

    def holders(self):
        try:
            with open(self.claims_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return {sid: entry.get("manager") for sid, entry in data.get("claims", {}).items()}

    def note(self, session_id):
        with open(self.claims_file, encoding="utf-8") as fh:
            data = json.load(fh)
        return data["claims"][session_id].get("note")


class Stranding(Base):
    """Who counts as stranded. Adopt a live worker whose manager is gone -- nobody else."""

    def test_live_worker_under_exited_manager_is_adopted(self):
        self.spawn(WORKER, parent=EXITED)
        self.live(WORKER)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("manager %s exited" % EXITED[:8], proc.stdout)
        self.assertIn("1 workers adopted from exited manager %s" % EXITED[:8], proc.stdout)
        self.assertEqual(self.holders().get(WORKER), ME)
        self.assertEqual(self.note(WORKER), "adopted from exited manager %s" % EXITED[:8])

    def test_worker_under_live_manager_is_untouched(self):
        """The negative control: a live manager keeps its own workers' gates."""
        self.spawn(PEER, role="manager")
        self.spawn(PEER_WORKER, parent=PEER)
        self.live(PEER)
        self.live(PEER_WORKER)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("exited", proc.stdout)
        self.assertEqual(self.holders(), {})

    def test_worker_whose_spawner_is_a_worker_is_untouched(self):
        """A record with no `role` reads as a worker, so its children are not stranded."""
        self.spawn(PEER)  # has its own record, no role -> worker
        self.spawn(PEER_WORKER, parent=PEER)
        self.live(PEER)
        self.live(PEER_WORKER)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.holders(), {})

    def test_dead_worker_is_not_adopted(self):
        """A worker that also exited holds no gate; adopting it would inflate the count."""
        self.spawn(WORKER, parent=EXITED)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.holders(), {})

    def test_worker_with_no_spawner_is_not_adopted(self):
        self.spawn(WORKER)
        self.live(WORKER)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.holders(), {})


class AdopterResolution(Base):
    """Successor when exactly one matches, fallback otherwise -- never a guess."""

    def _stranded(self):
        self.spawn(WORKER, parent=EXITED)
        self.live(WORKER)

    def test_unique_same_subject_successor_adopts(self):
        self._stranded()
        self.subject(EXITED, SUBJECT)
        self.subject(SUCCESSOR, SUBJECT)
        self.live(SUCCESSOR)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.holders().get(WORKER), SUCCESSOR)

    def test_subject_match_is_case_and_whitespace_folded(self):
        self._stranded()
        self.subject(EXITED, "  manager   layer ")
        self.subject(SUCCESSOR, "Manager Layer")
        self.live(SUCCESSOR)
        self.run_script()
        self.assertEqual(self.holders().get(WORKER), SUCCESSOR)

    def test_ambiguous_successor_falls_back(self):
        """Two same-subject managers are indistinguishable -- picking one silences the other."""
        self._stranded()
        self.subject(EXITED, SUBJECT)
        self.subject(SUCCESSOR, SUBJECT)
        self.subject(RIVAL, SUBJECT)
        self.live(SUCCESSOR)
        self.live(RIVAL)
        self.run_script()
        self.assertEqual(self.holders().get(WORKER), ME)

    def test_missing_subject_file_falls_back(self):
        """The common case: a manager that armed no loop writes no subject record."""
        self._stranded()
        self.subject(SUCCESSOR, SUBJECT)
        self.live(SUCCESSOR)
        self.run_script()
        self.assertEqual(self.holders().get(WORKER), ME)

    def test_different_subject_does_not_adopt(self):
        self._stranded()
        self.subject(EXITED, "Manager Layer")
        self.subject(SUCCESSOR, "Attention Routing")
        self.live(SUCCESSOR)
        self.run_script()
        self.assertEqual(self.holders().get(WORKER), ME)


class Refusal(Base):
    """Unknown is not empty. Never move ownership on evidence nobody has."""

    def test_unreadable_registry_refuses(self):
        self.spawn(WORKER, parent=EXITED)
        self.live(WORKER)
        proc = self.run_script("--registry-dir", os.path.join(self.tmp, "does-not-exist"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("liveness is unknown", proc.stderr)
        self.assertEqual(self.holders(), {})

    def test_missing_self_id_refuses(self):
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CLAUDE_SESSION_ID", None)
        proc = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "--ledger-dir",
                self.ledger_dir,
                "--registry-dir",
                self.registry_dir,
                "--heartbeat-dir",
                self.heartbeat_dir,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no tick session id", proc.stderr)


class Claiming(Base):
    """The claim store is the record, and the single-owner guarantee is never overridden."""

    def setUp(self):
        super().setUp()
        self.spawn(WORKER, parent=EXITED)
        self.live(WORKER)

    def test_held_claim_is_reported_apart_and_not_taken_over(self):
        """The exit-3 branch, driven in-process.

        It cannot be reached through the CLI against a fixture: `ownership-claim.py`
        resolves the holder's liveness against the REAL registry and offers no override,
        so a fixture holder always reads as gone and is taken over. Patching `claim` keeps
        the test on this script's handling of exit 3 -- which is what is under test -- and
        leaves the sibling's own decision to its own suite.
        """
        calls = []

        def held(session_id, manager, note, claims_file=None):
            calls.append((session_id, manager))
            return ao.HELD, "held by eeeeeeee"

        original = ao.claim
        ao.claim = held
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = ao.main(
                    [
                        "--self",
                        ME,
                        "--ledger-dir",
                        self.ledger_dir,
                        "--registry-dir",
                        self.registry_dir,
                        "--heartbeat-dir",
                        self.heartbeat_dir,
                        "--subjects-dir",
                        self.subjects_dir,
                        "--claims-file",
                        self.claims_file,
                    ]
                )
        finally:
            ao.claim = original
        self.assertEqual(code, 0)
        self.assertIn("1 workers already held by a live manager", out.getvalue())
        self.assertIn("0 workers adopted from exited manager", out.getvalue())
        self.assertEqual(calls, [(WORKER, ME)])
        self.assertEqual(self.holders(), {})

    def test_dry_run_claims_nothing(self):
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 workers adopted from exited manager %s" % EXITED[:8], proc.stdout)
        self.assertEqual(self.holders(), {})

    def test_parent_session_is_never_rewritten(self):
        """Adoption is a claim, not a re-parent -- the filter documents re-parenting as
        falsifying provenance, so the ledger record must survive the adoption intact."""
        self.run_script()
        with open(
            os.path.join(self.ledger_dir, WORKER + ".json"), encoding="utf-8"
        ) as fh:
            self.assertEqual(json.load(fh)["parent_session"], EXITED)


class Counting(Base):
    def test_two_workers_one_manager_counts_twice_on_one_exit_line(self):
        for worker in (WORKER, PEER_WORKER):
            self.spawn(worker, parent=EXITED)
            self.live(worker)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("manager %s exited" % EXITED[:8]), 1)
        self.assertIn("2 workers adopted from exited manager %s" % EXITED[:8], proc.stdout)


if __name__ == "__main__":
    unittest.main()
