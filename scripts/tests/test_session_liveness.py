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
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

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


def live_proc_start(pid):
    """`pid`'s real start time, formatted the way the registry writes `procStart`: ctime, UTC.

    A planted record has to carry this to be realistic. The probe compares it against the
    live holder's `ps -o lstart=` (which prints LOCAL, hence the conversion), and a record
    with no `procStart` is UNKNOWN by design — so a fixture that omits it exercises the
    missing-field path, not the live one, and would read every planted session as unproven.
    """
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    if not out:
        return None
    return (
        datetime.strptime(out, "%a %b %d %H:%M:%S %Y")
        .astimezone(timezone.utc)
        .strftime("%a %b %d %H:%M:%S %Y")
    )


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
        # The start-time cache, isolated for the same reason the heartbeat store is — and it
        # is not optional either. It is keyed by PID, so a suite reading the real one would
        # answer for whatever process held that number when the machine last wrote it, and
        # the result would depend on the host rather than on the code.
        self._prior_cache_env = os.environ.get("SUPERVISOR_START_CACHE")
        os.environ["SUPERVISOR_START_CACHE"] = os.path.join(self.tmp.name, "starts.json")

    def tearDown(self):
        # Restored, never popped: the sibling suites that load this module set one shared
        # isolated store at import, and popping here would send every later test back to the
        # real one — the leak this isolation exists to close, re-entered by the cleanup.
        if self._prior_cache_env is None:
            os.environ.pop("SUPERVISOR_START_CACHE", None)
        else:
            os.environ["SUPERVISOR_START_CACHE"] = self._prior_cache_env
        self.tmp.cleanup()

    def plant(self, session_id, pid, name="synth", status="running", proc_start=_DEFAULT):
        """Plant a registry record. `proc_start` defaults to the pid's REAL start time.

        Pass an explicit value to plant a mismatch on purpose, or `None` to omit the field.
        """
        rec = {"sessionId": session_id, "pid": pid, "name": name, "status": status}
        start = live_proc_start(pid) if proc_start is _DEFAULT else proc_start
        if start is not None:
            rec["procStart"] = start
        with open(os.path.join(self.dir, "%s.json" % pid), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)

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

    # ---- pid IDENTITY: occupancy is not liveness -----------------------------------------
    #
    # `os.kill(pid, 0)` asks whether SOME process holds the pid, never whether it is THIS
    # session's. These pin the residual `[[A Substring-Matched Liveness Probe Reports a Dead
    # Session as Live]]` left open: a record that outlives its session (a `kill -9` leaves it
    # behind) plus a pid the OS has since recycled. Before the identity check that pair read
    # LIVE, which is how a close-me line gets printed for a session that is already gone.

    def test_a_recycled_pid_is_absent_not_live(self):
        # The controlled fixture, in one assertion: a record for a session id that is gone,
        # against a pid held by an unrelated live process.
        self.plant(
            "rec1c1ed-1111-2222-3333-444455556666",
            os.getpid(),
            proc_start="Thu Jan  1 00:00:00 1970",
        )
        rc, out = self.check("rec1c1ed")
        self.assertEqual(rc, ABSENT)
        self.assertIn("not this session", out)

    def test_a_matching_proc_start_is_live(self):
        # The other arm, and the one that keeps the fix from being "always ABSENT": the
        # record's own start time agrees with the live holder's.
        self.plant("ma7ch1ng-1111-2222-3333-444455556666", os.getpid())
        self.assertEqual(self.check("ma7ch1ng")[0], LIVE)

    def test_an_occupied_pid_with_no_proc_start_is_unknown(self):
        # Occupied, but the record cannot say WHICH process — "cannot tell", never death.
        self.plant("n0s7ar75-1111-2222-3333-444455556666", os.getpid(), proc_start=None)
        rc, out = self.check("n0s7ar75")
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("no usable procStart", out)

    def test_an_occupied_pid_with_a_malformed_proc_start_is_unknown(self):
        self.plant("bad5tamp-1111-2222-3333-444455556666", os.getpid(), proc_start="not a date")
        self.assertEqual(self.check("bad5tamp")[0], UNKNOWN)

    def test_a_gone_pid_is_still_absent_without_a_proc_start(self):
        # The identity check must not swallow the stale-record arm: a pid that is GONE is
        # decisive on its own, because there is no holder to compare a start time against.
        self.plant("g0ne0000-1111-2222-3333-444455556666", dead_pid(), proc_start=None)
        self.assertEqual(self.check("g0ne0000")[0], ABSENT)

    def test_a_mismatched_proc_start_is_unknown_when_the_heartbeat_store_is_unreadable(self):
        # A mismatch is a NEGATIVE, so it needs both sources readable — the same rule the
        # stale-record branch applies.
        self.plant(
            "m15ma7ch-1111-2222-3333-444455556666",
            os.getpid(),
            proc_start="Thu Jan  1 00:00:00 1970",
        )
        rc, out = self.check("m15ma7ch", heartbeat_dir=self.unreadable())
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("heartbeat store is unreadable", out)

    def test_a_recycled_pid_is_omitted_from_the_listing(self):
        # `--list` is "one line per live session", so an unproven identity must not appear.
        self.plant(
            "rec1c1ed-1111-2222-3333-444455556666",
            os.getpid(),
            name="Recycled",
            proc_start="Thu Jan  1 00:00:00 1970",
        )
        _, out = self.listing()
        self.assertNotIn("Recycled", out)

    # ---- the start-time probe: the cache and the timeout ---------------------------------
    #
    # SC1 and SC2 of the 2026-10-04 task. Measured that day, and it is why the probe is
    # per-pid rather than batched: `ps -o pid=,lstart= -p <n>` costs 0.026 s at n=1 and
    # 3.7-7.4 s at EVERY n >= 2 (medians of 5), because macOS `ps` takes a fast single-pid
    # path and falls back to enumerating the whole process table for two or more. At ~3 probe
    # processes/s, each holding one ~45 s `ps`, that produced ~134 concurrent `ps` children —
    # 134 of them with ppid 1, their python parent having been killed by a caller timeout.

    # A ctime in `ps -o lstart=`'s format. Fixed, so an assertion never depends on the clock.
    _PS_LINE = "Sun Oct  4 11:18:04 2026"

    def _stub_probe(self, spawns, stdout_for=None, raises=None):
        """Replace the module's `subprocess` with a recorder of `run` calls.

        Only `run` is overridden; everything else delegates, so `subprocess.TimeoutExpired`
        still resolves by name — the module catches it by that name, and a bare object
        without it would turn a clean `return {}` into an `AttributeError`.
        """
        real = self.m.subprocess

        class Stub:
            def __getattr__(self, name):
                return getattr(real, name)

            def run(self, argv, **kwargs):
                spawns.append(list(argv))
                if raises is not None:
                    raise raises
                text = stdout_for(argv) if stdout_for else ""
                return real.CompletedProcess(argv, 0, text, "")

        self.m.subprocess = Stub()
        return lambda: setattr(self.m, "subprocess", real)

    def test_a_second_call_for_the_same_pids_spawns_no_ps(self):
        # SC1, verbatim: "a second call for the same pids spawns no `ps`". The first call has
        # to read the pid or the second proves nothing — a probe that never read it would
        # satisfy "no second spawn" by never spawning at all.
        spawns = []
        restore = self._stub_probe(spawns, lambda argv: "%s %s\n" % (argv[-1], self._PS_LINE))
        try:
            first = self.m._ps_starts([os.getpid()])
            reads_after_first = len(spawns)
            second = self.m._ps_starts([os.getpid()])
        finally:
            restore()
        self.assertEqual(reads_after_first, 1, "the first call must read the pid")
        self.assertEqual(len(spawns), 1, "a second call for the same pid must spawn no `ps`")
        self.assertEqual(first, second, "the cached answer must equal the read one")

    def test_the_ps_call_carries_a_timeout_and_a_timeout_answers_nothing(self):
        # SC2's first clause. `subprocess.run`'s timeout is what kills the child; without it
        # the probe blocks until `ps` finishes, a caller gives up, and `ps` is reparented to
        # launchd still running — the orphan this task exists to remove.
        spawns, seen = [], {}
        real = self.m.subprocess

        class Stub:
            def __getattr__(self, name):
                return getattr(real, name)

            def run(self, argv, **kwargs):
                spawns.append(list(argv))
                seen.update(kwargs)
                raise real.TimeoutExpired(argv, kwargs.get("timeout"))

        self.m.subprocess = Stub()
        try:
            out = self.m._ps_starts([os.getpid()])
        finally:
            self.m.subprocess = real
        self.assertEqual(len(spawns), 1, "the pid was never probed — the timeout never fired")
        self.assertEqual(seen.get("timeout"), self.m._PS_TIMEOUT, "the `ps` call must be bounded")
        self.assertEqual(out, {}, "a timed-out read answers nothing, never a guess")

    def test_a_timed_out_ps_is_killed_not_orphaned(self):
        """SC2's second clause — the child is reaped, not left running.

        ⚠️ **The proof that the child really ran is the `TimeoutExpired` itself, not a marker
        file.** `subprocess.run` raises it only while the child is still alive at the deadline,
        so a command that never started would return normally and this assertion would never be
        reached — which is what makes the absence check below evidence, rather than a probe that
        passes because it spawned nothing. An earlier draft had the child write a file first,
        and that made the test hostage to interpreter startup on a loaded host: miss the window
        and the child dies before the write lands. This has no such dependency, because the
        process exists from the moment `Popen` returns, whether or not it has finished exec'ing.

        ⚠️ **Only the COMMAND is swapped.** Every kwarg — `timeout` above all — comes from the
        code under test, so the kill under assertion is the real one and not a mock of it.
        """
        marker = "session-liveness-timeout-probe-%d" % os.getpid()
        real_run = self.m.subprocess.run
        real_subprocess = self.m.subprocess
        # A single process carrying the marker in its own argv, outliving the timeout — and one
        # that self-terminates in 30 s, so a regression in the kill leaves a stray sleeper for
        # half a minute rather than the five a `sleep 300` would have.
        probe_argv = [sys.executable, "-c", "import time; time.sleep(30)", marker]
        observed = {}

        class Stub:
            def __getattr__(self, name):
                return getattr(real_subprocess, name)

            def run(self, _argv, **kwargs):
                try:
                    return real_run(probe_argv, **kwargs)
                except real_subprocess.TimeoutExpired:
                    observed["timed_out"] = True
                    raise

        self.m.subprocess = Stub()
        original_timeout = self.m._PS_TIMEOUT
        self.m._PS_TIMEOUT = 2
        try:
            out = self.m._ps_starts([os.getpid()])
        finally:
            self.m.subprocess = real_subprocess
            self.m._PS_TIMEOUT = original_timeout

        self.assertTrue(
            observed.get("timed_out"),
            "the child never outlived the timeout — its absence would prove nothing",
        )
        self.assertEqual(out, {}, "a timed-out read answers nothing")
        survivors = real_run(["ps", "-eo", "pid=,args="], capture_output=True, text=True).stdout
        self.assertNotIn(marker, survivors, "the timed-out child survived — orphaned, not killed")

    def test_a_stale_cache_entry_is_read_again(self):
        # A pid's start time is immutable, but its MEANING is not: pids are recycled. The TTL
        # is what bounds that, so an expired entry must not be served.
        spawns = []
        restore = self._stub_probe(spawns, lambda argv: "%s %s\n" % (argv[-1], self._PS_LINE))
        try:
            self.m._ps_starts([os.getpid()])
            self.m._START_CACHE_TTL = 0  # every entry is now stale
            self.m._ps_starts([os.getpid()])
        finally:
            restore()
        self.assertEqual(len(spawns), 2, "a stale entry must be read again, not served")

    def test_a_corrupt_cache_file_is_ignored_not_fatal(self):
        # The cache only shortens the path to the answer; an unreadable one costs latency and
        # never correctness. It must not raise, and it must not answer from garbage.
        with open(os.environ["SUPERVISOR_START_CACHE"], "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        spawns = []
        restore = self._stub_probe(spawns, lambda argv: "%s %s\n" % (argv[-1], self._PS_LINE))
        try:
            out = self.m._ps_starts([os.getpid()])
        finally:
            restore()
        self.assertEqual(len(spawns), 1, "a corrupt cache must fall through to a real read")
        self.assertEqual(sorted(out), [os.getpid()], "the read answer must survive the garbage")
        self.assertIsInstance(out[os.getpid()], int)

    def test_the_whole_probe_is_bounded_by_its_own_budget(self):
        # ⚠️ A per-call timeout is not a bound on the CALL. The loop is strictly serial, so
        # `_PS_TIMEOUT` alone leaves a worst case of n x 5 s for n uncached pids — worse than
        # the single batched call this change replaced, in exactly the pathological case it
        # exists to fix. `_PS_BUDGET` caps the probe itself.
        spawns = []
        restore = self._stub_probe(spawns, lambda argv: "%s %s\n" % (argv[-1], self._PS_LINE))
        original = self.m._PS_BUDGET
        self.m._PS_BUDGET = 0  # exhausted before the first pid is even attempted
        try:
            out = self.m._ps_starts([os.getpid(), os.getppid()])
        finally:
            self.m._PS_BUDGET = original
            restore()
        self.assertEqual(spawns, [], "an exhausted budget must spawn no `ps` at all")
        self.assertEqual(out, {}, "unread pids stay unanswered — UNKNOWN, never death")

    def test_a_failed_cache_write_neither_raises_nor_strands_its_temp_file(self):
        # ⚠️ `_ps_starts` documents "Never raises" and calls this on its way out, so the guard
        # is widened past `OSError` — an unserialisable value raises `TypeError` from
        # `json.dump` — and the per-process temp file is unlinked. Nothing else ever cleans it:
        # the name is derived from the pid and is never reused, so one failed write would be
        # permanent litter in the state directory.
        self.m._save_start_cache({os.getpid(): [object(), 456.0]})  # must not raise
        parent = os.path.dirname(os.environ["SUPERVISOR_START_CACHE"])
        leftovers = [f for f in os.listdir(parent) if ".tmp." in f]
        self.assertEqual(leftovers, [], "a failed write must not strand its temp file")

    def test_a_malformed_tunable_does_not_break_import(self):
        # ⚠️ This module is imported by seven other scripts, so a ValueError raised at import
        # here disables the whole plugin, not just the probe — a typo'd
        # `SUPERVISOR_PS_TIMEOUT=5s` would take down who-needs-me, fleet-board, worker-sessions
        # and four more. The parse is tolerant for the same reason `_start_cache_path()` is.
        os.environ["SUPERVISOR_PS_TIMEOUT"] = "5s"
        try:
            fresh = load()  # the real assertion: this must not raise
        finally:
            os.environ.pop("SUPERVISOR_PS_TIMEOUT", None)
        self.assertEqual(fresh._PS_TIMEOUT, 5, "a malformed tunable falls back to its default")
        self.assertEqual(self.m._env_float("SUPERVISOR_PS_TIMEOUT", 5), 5)
        os.environ["SUPERVISOR_PS_TIMEOUT"] = "2.5"
        try:
            self.assertEqual(self.m._env_float("SUPERVISOR_PS_TIMEOUT", 5), 2.5)
        finally:
            os.environ.pop("SUPERVISOR_PS_TIMEOUT", None)

    def test_the_cache_file_is_written_owner_only(self):
        # The repo's other `~/.claude/state/` writers create at 0o600 rather than letting the
        # umask decide. This file is read as an identity assertion, so a store another user
        # could write is a poisoning surface for the TTL window.
        self.m._save_start_cache({os.getpid(): [123, 456.0]})
        mode = stat.S_IMODE(os.stat(os.environ["SUPERVISOR_START_CACHE"]).st_mode)
        self.assertEqual(mode, 0o600, "the cache must never be world-readable")

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


    # ---- --list --json: the shape `/supervisor:open` reads -------------------------------

    def json_listing(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(["--list", "--json", "--dir", self.dir, "--heartbeat-dir", self.hb])
        return rc, json.loads(out.getvalue())

    def test_json_listing_carries_former_names_and_cwd(self):
        # `/supervisor:open` resolves a topic's manager by matching the topic against the
        # current name AND every name the session has held, then spawns into `cwd`. Those two
        # fields are the whole reason it can use this reader instead of opening the registry
        # itself — a name-only match spawns a SECOND manager onto a live topic.
        with open(os.path.join(self.dir, "%s.json" % os.getpid()), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "sessionId": "f0rmer00-1111-2222-3333-444455556666",
                    "pid": os.getpid(),
                    "procStart": live_proc_start(os.getpid()),
                    "name": "Renamed Topic Manager",
                    "status": "busy",
                    "formerNames": [{"name": "Topic Manager", "at": "2026-09-18"}],
                    "cwd": "/Users/bborbe/Documents/workspaces/thing",
                    "nameSource": "user",
                },
                fh,
            )
        rc, rows = self.json_listing()
        self.assertEqual(rc, LIVE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["formerNames"], ["Topic Manager"])
        self.assertEqual(rows[0]["cwd"], "/Users/bborbe/Documents/workspaces/thing")
        self.assertEqual(rows[0]["nameSource"], "user")

    def test_json_listing_omits_a_dead_pid(self):
        self.plant("deadp1d0-1111-2222-3333-444455556666", dead_pid())
        _, rows = self.json_listing()
        self.assertEqual(rows, [])

    def test_json_listing_marks_a_heartbeat_row_by_source(self):
        self.beat("bea70001-1111-2222-3333-444455556666")
        _, rows = self.json_listing()
        self.assertEqual([r["source"] for r in rows], ["heartbeat"])

    def cluster_stamp(self, session_id, age_seconds=300, reachable=False):
        """A stale cluster stamp, and optionally a fresh mirror reachability marker."""
        path = os.path.join(self.hb, "%s.json" % session_id)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"source": "cluster", "pid": None}, fh)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        if reachable:
            with open(os.path.join(self.hb, "_cluster-reachability.json"), "w", encoding="utf-8") as fh:
                fh.write("{}")

    def test_a_stale_cluster_stamp_is_unknown_when_the_cluster_could_not_be_read(self):
        # SC4, and the whole reason the reachability marker exists. The mirror stops
        # refreshing a cluster worker's stamp when the cluster is unreachable, so the stamp
        # goes stale — and a stale stamp is indistinguishable from a dead worker without the
        # marker. Reporting STALE here would permit a resume onto a worker that may be alive
        # behind a network fault.
        self.cluster_stamp("c1u57e12-1111-2222-3333-444455556666", reachable=False)
        rc, out = self.check("c1u57e12")
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("cluster store could not be read", out)

    def test_a_stale_cluster_stamp_is_absent_once_the_mirror_is_reachable(self):
        # The marker is fresh, so the cluster WAS read and this worker simply stopped
        # refreshing — an ordinary death, which must stay ABSENT and not drift to UNKNOWN.
        self.cluster_stamp("c1u57e12-1111-2222-3333-444455556666", reachable=True)
        self.assertEqual(self.check("c1u57e12")[0], ABSENT)

    def test_a_stale_non_cluster_stamp_is_absent_even_with_the_cluster_down(self):
        # A headless worker's death must not be laundered into UNKNOWN by an unrelated cluster
        # outage — the rule keys on the stamp's own source, not on the marker alone.
        self.beat("11vea11f-1111-2222-3333-444455556666", age_seconds=300)
        self.assertEqual(self.check("11vea11f")[0], ABSENT)

    def test_a_stale_record_is_unknown_when_the_heartbeat_store_is_unreadable(self):
        # A stale record is a NEGATIVE, so it needs both sources readable — the same rule the
        # no-match branch applies. Returning ABSENT here would let a fresh stamp in the half we
        # could not read be outvoted, and ABSENT is the one answer that permits a resume.
        self.plant("57a1e57a-1111-2222-3333-444455556666", dead_pid())
        rc, out = self.check("57a1e57a", heartbeat_dir=self.unreadable())
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("heartbeat store is unreadable", out)


if __name__ == "__main__":
    unittest.main()
