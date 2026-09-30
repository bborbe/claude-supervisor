#!/usr/bin/env python3
"""Tests for scripts/session-liveness.py.

The load-bearing properties, in the order the 2026-09-26 near-miss ranks them:

  * a prefix and the full id it abbreviates return the SAME verdict — the falsifier the
    task states, because a probe that answers differently for (b) than (a) is the defect;
  * an unreadable registry is UNKNOWN, never ABSENT — a caller that folds an I/O error
    into "not live" resumes onto a live conversation;
  * a readable-but-empty registry IS ABSENT — `{}` and `None` are different answers;
  * an ambiguous prefix refuses rather than guessing, and names candidates a caller can
    actually tell apart;
  * a registered id whose pid is gone is a stale record, not a live session.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))

LIVE, ABSENT, UNKNOWN, AMBIGUOUS = 0, 1, 2, 3

# Distinguishes "the caller said nothing, use the isolated store" from "the caller passed
# None", which is a real argument value in this suite.
_DEFAULT = object()


def load():
    spec = importlib.util.spec_from_file_location(
        "session_liveness", os.path.join(os.path.dirname(_HERE), "session-liveness.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dead_pid():
    """A pid that has certainly exited — reaped, so `os.kill` raises ProcessLookupError."""
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


class SessionLiveness(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        # An isolated heartbeat store, and it is not optional. Without it every case would
        # read the REAL store, so a headless worker running on this machine while the suite
        # runs would turn an ABSENT assertion into LIVE — a suite whose result depends on the
        # fleet's mood rather than on the code.
        self.hb = os.path.join(self.tmp.name, "heartbeats")
        os.makedirs(self.hb)
        # A path that cannot be listed (`NotADirectoryError`), for the unreadable-store cases.
        self.blocked = os.path.join(self.tmp.name, "blocked-file")
        with open(self.blocked, "w", encoding="utf-8") as fh:
            fh.write("not a directory\n")

    def tearDown(self):
        self.tmp.cleanup()

    def plant(self, session_id, pid, name="synth", status="running"):
        with open(os.path.join(self.dir, "%s.json" % pid), "w", encoding="utf-8") as fh:
            json.dump({"sessionId": session_id, "pid": pid, "name": name, "status": status}, fh)

    def beat(self, session_id, age_seconds=1):
        """Plant a heartbeat stamp aged `age_seconds`. The TTL is 60s, so the default is fresh."""
        path = os.path.join(self.hb, "%s.json" % session_id)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pid": None}, fh)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    def unreadable(self):
        """A heartbeat-dir path that raises `NotADirectoryError` on listdir -> `None`."""
        return os.path.join(self.blocked, "live")

    def check(self, session_id, directory=None, heartbeat_dir=_DEFAULT):
        out, err = io.StringIO(), io.StringIO()
        hb = self.hb if heartbeat_dir is _DEFAULT else heartbeat_dir
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(["--check", session_id, "--dir", directory or self.dir, "--heartbeat-dir", hb])
        return rc, out.getvalue() + err.getvalue()

    def listing(self, directory=None, heartbeat_dir=_DEFAULT):
        out, err = io.StringIO(), io.StringIO()
        hb = self.hb if heartbeat_dir is _DEFAULT else heartbeat_dir
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(["--list", "--dir", directory or self.dir, "--heartbeat-dir", hb])
        return rc, out.getvalue() + err.getvalue()

    # ---- the falsifier: prefix and full id agree -------------------------------------

    def test_prefix_and_full_id_return_the_same_verdict(self):
        # The 2026-09-26 near-miss: `3fd529af` was published as `registry ABSENT` while the
        # session was registered against a running pid. A unique prefix must resolve.
        self.plant("3fd529af-1111-2222-3333-444455556666", os.getpid())
        full = self.check("3fd529af-1111-2222-3333-444455556666")
        pref = self.check("3fd529af")
        self.assertEqual(full[0], LIVE)
        self.assertEqual(pref[0], LIVE)
        self.assertEqual(full[0], pref[0], "prefix verdict must equal full-id verdict")

    def test_absent_id_differs_from_a_resolving_prefix(self):
        # (b) and (c) must NOT agree, or the probe is a constant-return stub.
        self.plant("3fd529af-1111-2222-3333-444455556666", os.getpid())
        self.assertEqual(self.check("3fd529af")[0], LIVE)
        self.assertEqual(self.check("deadbeef")[0], ABSENT)

    def test_prefix_match_is_case_insensitive(self):
        self.plant("ABCDEF12-1111-2222-3333-444455556666", os.getpid())
        self.assertEqual(self.check("abcdef12")[0], LIVE)

    # ---- UNKNOWN vs ABSENT: the dangerous direction -----------------------------------

    def test_unreadable_registry_is_unknown_not_absent(self):
        rc, out = self.check("3fd529af", directory=os.path.join(self.dir, "does-not-exist"))
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_readable_but_empty_registry_is_absent_not_unknown(self):
        # `{}` and `None` are different answers; collapsing them is the whole bug class.
        self.assertEqual(self.check("3fd529af")[0], ABSENT)
        self.assertEqual(self.check("3fd529af", directory=os.path.join(self.dir, "nope"))[0], UNKNOWN)

    # ---- ambiguity ---------------------------------------------------------------------

    def plant_colliding_pair(self):
        """Two LIVE sessions sharing an 8-char prefix.

        The registry file is named by pid, so the pair needs two distinct live pids —
        this process and its parent. Planting both under one pid silently leaves a
        single file, and the ambiguity this asserts would never arise.
        """
        pids = (os.getpid(), os.getppid())
        self.assertNotEqual(pids[0], pids[1], "need two distinct live pids")
        self.plant("abc12345-1111-2222-3333-444455556666", pids[0])
        self.plant("abc12345-9999-8888-7777-666655554444", pids[1])

    def test_ambiguous_prefix_refuses_rather_than_guesses(self):
        self.plant_colliding_pair()
        rc, out = self.check("abc12345")
        self.assertEqual(rc, AMBIGUOUS)
        self.assertIn("pass a longer id", out)

    def test_ambiguous_candidates_are_distinguishable(self):
        # Echoing 8 chars back hands the caller two identical strings and no way to choose.
        self.plant_colliding_pair()
        _, out = self.check("abc12345")
        self.assertIn("abc12345-111", out)
        self.assertIn("abc12345-999", out)

    def test_a_longer_prefix_disambiguates(self):
        self.plant_colliding_pair()
        self.assertEqual(self.check("abc12345-1111")[0], LIVE)
        self.assertEqual(self.check("abc12345-9999")[0], LIVE)

    # ---- pid liveness -------------------------------------------------------------------

    def test_registered_but_dead_pid_is_a_stale_record(self):
        # `kill -9` leaves the registry file behind; presence alone is not the verdict.
        self.plant("beefbeef-1111-2222-3333-444455556666", dead_pid())
        rc, out = self.check("beefbeef")
        self.assertEqual(rc, ABSENT)
        self.assertIn("stale record", out)

    def test_permission_error_on_the_pid_counts_as_alive(self):
        # The process exists but is not ours — that is life, not death.
        self.plant("feedfeed-1111-2222-3333-444455556666", 1)
        real_kill = self.m.os.kill
        self.m.os.kill = lambda pid, sig: (_ for _ in ()).throw(PermissionError())
        try:
            self.assertEqual(self.check("feedfeed")[0], LIVE)
        finally:
            self.m.os.kill = real_kill

    # ---- --list -------------------------------------------------------------------------

    def test_list_shows_only_live_sessions(self):
        self.plant("aaaa1111-1111-2222-3333-444455556666", os.getpid(), name="Alive")
        self.plant("bbbb2222-1111-2222-3333-444455556666", dead_pid(), name="Dead")
        rc, out = self.listing()
        self.assertEqual(rc, LIVE)
        self.assertIn("Alive", out)
        self.assertNotIn("Dead", out)

    def test_list_on_an_unreadable_registry_is_unknown(self):
        rc, out = self.listing(directory=os.path.join(self.dir, "does-not-exist"))
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)


    # ---- the second source: heartbeats --------------------------------------------------
    #
    # The registry cannot answer for a session with no process of its own — a headless worker
    # or a cluster worker. These pin the half that can, and the composition rule that keeps
    # "could not tell" from collapsing into "not live".

    def test_a_fresh_heartbeat_is_live_with_no_registry_entry(self):
        # The cluster case, in one assertion: no pid, no registry entry, still live.
        self.beat("c1u57e12-1111-2222-3333-444455556666")
        rc, out = self.check("c1u57e12")
        self.assertEqual(rc, LIVE)
        self.assertIn("heartbeat", out)

    def test_a_stale_heartbeat_is_absent_not_live(self):
        # Age, not existence: a `kill -9`'d writer leaves the file behind. 120s is past the 60s
        # TTL, so the stamp is not evidence of life.
        self.beat("57a1e111-1111-2222-3333-444455556666", age_seconds=120)
        self.assertEqual(self.check("57a1e111")[0], ABSENT)

    def test_a_heartbeat_prefix_resolves_like_a_registry_prefix(self):
        # The prefix contract is a property of the ARGUMENT, not of the source it resolves in.
        self.beat("bea7bea7-1111-2222-3333-444455556666")
        self.assertEqual(self.check("bea7bea7")[0], LIVE)
        self.assertEqual(self.check("bea7bea7-1111-2222-3333-444455556666")[0], LIVE)

    def test_a_fresh_heartbeat_is_decisive_when_the_registry_is_unreadable(self):
        # A positive from one channel is an answer; "could not tell" from the other is not a
        # refutation of it.
        self.beat("dec151ve-1111-2222-3333-444455556666")
        rc, _ = self.check("dec151ve", directory=os.path.join(self.dir, "does-not-exist"))
        self.assertEqual(rc, LIVE)

    def test_unreadable_heartbeat_store_makes_an_unknown_id_unknown_not_absent(self):
        # The SC4 direction. With the second store unreadable, "no match" cannot rule out a
        # match in the half we could not read, so the answer is UNKNOWN — never ABSENT, which
        # is the one answer that permits a resume.
        rc, out = self.check("3fd529af", heartbeat_dir=self.unreadable())
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("heartbeat store unreadable", out)

    def test_list_includes_a_heartbeat_only_session(self):
        self.beat("11571e57-1111-2222-3333-444455556666")
        rc, out = self.listing()
        self.assertEqual(rc, LIVE)
        self.assertIn("11571e57", out)

    def test_list_on_an_unreadable_heartbeat_store_is_unknown(self):
        # A partial list presented as complete reads as "not live" for every session in the
        # half that failed, which is the dangerous direction.
        rc, out = self.listing(heartbeat_dir=self.unreadable())
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)


if __name__ == "__main__":
    unittest.main()
