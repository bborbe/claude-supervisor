#!/usr/bin/env python3
"""Tests for scripts/session-identity.py — the session registry read (identity, not liveness).

These cases lived in `test_session_liveness.py` until 2026-10-08, when liveness moved to the
session-heartbeat endpoint and the registry machinery split out into `session-identity.py`.
The intent is unchanged and the load-bearing properties are the ones the 2026-09-26 near-miss
ranks them:

  * `read_registry` keeps `{}` (read, nothing there) and `None` (could not read) distinct — a
    caller that folds an I/O error into "no session" resumes onto a live conversation;
  * a registered id whose pid is gone, or whose pid belongs to a different process, is not
    this session's — occupancy is not identity;
  * the start-time probe's cache is bounded by the pid-reuse window, and a cached value never
    manufactures a negative.

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
from contextlib import redirect_stderr
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))

# Distinguishes "the caller said nothing, use the isolated store" from "the caller passed
# None", which is a real argument value in this suite.
_DEFAULT = object()


def load():
    spec = importlib.util.spec_from_file_location(
        "session_identity", os.path.join(os.path.dirname(_HERE), "session-identity.py")
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


class SessionIdentity(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        # The start-time cache, isolated for the same reason the heartbeat store was — and it
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

    def plant(self, session_id, pid, name="synth", status="running", proc_start=_DEFAULT, filename=None):
        """Plant a registry record. `proc_start` defaults to the pid's REAL start time.

        Pass an explicit value to plant a mismatch on purpose, or `None` to omit the field.
        """
        rec = {"sessionId": session_id, "pid": pid, "name": name, "status": status}
        start = live_proc_start(pid) if proc_start is _DEFAULT else proc_start
        if start is not None:
            rec["procStart"] = start
        with open(os.path.join(self.dir, filename or ("%s.json" % pid)), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)

    def alive(self, session_id, directory=None):
        """The three-state `alive` for one planted session, or `None` for an unreadable read."""
        records = self.m.read_registry(directory or self.dir)
        if records is None:
            return "UNREADABLE"
        return records.get(session_id, {}).get("alive", "ABSENT")

    # ---- the read's two answers: unreadable is not empty ------------------------------

    def test_unreadable_registry_is_none_not_empty(self):
        self.assertIsNone(self.m.read_registry(os.path.join(self.dir, "does-not-exist")))

    def test_empty_registry_is_an_empty_dict(self):
        self.assertEqual(self.m.read_registry(self.dir), {})

    def test_read_registry_entries_unreadable_is_none(self):
        self.assertIsNone(self.m.read_registry_entries(os.path.join(self.dir, "nope")))

    def test_read_registry_entries_is_empty_list_when_nothing_planted(self):
        self.assertEqual(self.m.read_registry_entries(self.dir), [])

    def test_read_registry_entries_preserves_a_collision_the_dict_collapses(self):
        """Two files claiming one id: the dict keeps one, the list keeps both.

        `restart-worker.py` refuses on that collision, so the list shape has to see it.
        """
        self.plant("c0111510-1111-2222-3333-444455556666", 4242, filename="a.json")
        self.plant("c0111510-1111-2222-3333-444455556666", 4243, filename="b.json")
        self.assertEqual(len(self.m.read_registry_entries(self.dir)), 2)
        self.assertEqual(len(self.m.read_registry(self.dir)), 1)

    # ---- pid liveness: occupancy is not identity --------------------------------------

    def test_registered_but_dead_pid_is_not_alive(self):
        # `kill -9` leaves the registry file behind; presence alone is not a verdict.
        self.plant("beefbeef-1111-2222-3333-444455556666", dead_pid())
        self.assertIs(self.alive("beefbeef-1111-2222-3333-444455556666"), False)

    def test_permission_error_on_the_pid_counts_as_occupied(self):
        # The process exists but is not ours — that is life, not death.
        self.plant("feedfeed-1111-2222-3333-444455556666", 1)
        real_kill = self.m.os.kill
        self.m.os.kill = lambda pid, sig: (_ for _ in ()).throw(PermissionError())
        try:
            self.assertIs(self.alive("feedfeed-1111-2222-3333-444455556666"), True)
        finally:
            self.m.os.kill = real_kill

    def test_a_recycled_pid_is_not_alive(self):
        # A record for a session id that is gone, against a pid held by an unrelated live
        # process: the record's own `procStart` disagrees with the live holder's.
        self.plant(
            "rec1c1ed-1111-2222-3333-444455556666",
            os.getpid(),
            proc_start="Thu Jan  1 00:00:00 1970",
        )
        self.assertIs(self.alive("rec1c1ed-1111-2222-3333-444455556666"), False)

    def test_a_matching_proc_start_is_alive(self):
        # The other arm, and the one that keeps the fix from being "always not-alive".
        self.plant("ma7ch1ng-1111-2222-3333-444455556666", os.getpid())
        self.assertIs(self.alive("ma7ch1ng-1111-2222-3333-444455556666"), True)

    def test_an_occupied_pid_with_no_proc_start_is_none(self):
        # Occupied, but the record cannot say WHICH process — "cannot tell", never death.
        self.plant("n0s7ar75-1111-2222-3333-444455556666", os.getpid(), proc_start=None)
        self.assertIsNone(self.alive("n0s7ar75-1111-2222-3333-444455556666"))

    def test_an_occupied_pid_with_a_malformed_proc_start_is_none(self):
        self.plant("bad5tamp-1111-2222-3333-444455556666", os.getpid(), proc_start="not a date")
        self.assertIsNone(self.alive("bad5tamp-1111-2222-3333-444455556666"))

    def test_a_gone_pid_is_not_alive_even_without_a_proc_start(self):
        # A pid that is GONE is decisive on its own: there is no holder to compare against.
        self.plant("g0ne0000-1111-2222-3333-444455556666", dead_pid(), proc_start=None)
        self.assertIs(self.alive("g0ne0000-1111-2222-3333-444455556666"), False)

    # ---- the start-time probe: the cache and the timeout ------------------------------
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
        marker = "session-identity-timeout-probe-%d" % os.getpid()
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

    def test_an_out_of_range_cache_value_cannot_make_a_live_session_absent(self):
        """⚠️ Valid JSON holding an out-of-range value must not poison the read.

        `json.load` accepts a bare `Infinity`, and `int(inf)` then raises `OverflowError` — a
        subclass of `ArithmeticError`, not of `TypeError` or `ValueError`. Uncaught it escapes
        `_ps_starts` (documented "Never raises") and out of a caller's `main()`, and an uncaught
        exception exits **1** — the module's documented ABSENT code. A corrupt cache would
        therefore report a LIVE session as ABSENT, the one answer that permits a resume onto a
        live conversation. End-to-end form: a planted live record plus a poisoned cache.
        """
        self.plant("0u70frange-1111-2222-3333-444455556666", os.getpid())
        with open(os.environ["SUPERVISOR_START_CACHE"], "w", encoding="utf-8") as fh:
            fh.write('{"%d": [Infinity, 1.0]}' % os.getpid())
        self.assertIs(
            self.alive("0u70frange-1111-2222-3333-444455556666"), True,
            "an out-of-range cache value must not turn a live session into a negative",
        )

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

    def test_a_cached_mismatch_is_reproved_uncached_before_it_reads_absent(self):
        """A stale CACHE entry must not manufacture a negative.

        Within the TTL a cached start time belongs to whatever process *last held* that pid, so
        a pid the OS recycled inside the window hands back the dead holder's start and disagrees
        with the live holder's record. `_pid_identity` reads a mismatch as `False`, which the
        old liveness path turned into ABSENT and a genuinely LIVE session became resumable. The
        pre-cache code could not do this — `ps` then always answered for the current holder — so
        this pins the regression the cache introduced, and the uncached re-read that closes it.
        """
        pid = os.getpid()
        self.plant("ca5heca5-1111-2222-3333-444455556666", pid)  # real procStart -> alive
        # A FRESH cache entry disagreeing with the record, exactly as a recycled pid produces.
        with open(os.environ["SUPERVISOR_START_CACHE"], "w", encoding="utf-8") as fh:
            json.dump({str(pid): [1, time.time()]}, fh)
        self.assertIs(
            self.alive("ca5heca5-1111-2222-3333-444455556666"), True,
            "a cache-served mismatch must be re-proved, not believed",
        )

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
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                out = self.m._ps_starts([os.getpid(), os.getppid()])
        finally:
            self.m._PS_BUDGET = original
            restore()
        self.assertEqual(spawns, [], "an exhausted budget must spawn no `ps` at all")
        self.assertEqual(out, {}, "unread pids stay unanswered — UNKNOWN, never death")
        # ⚠️ **A degraded read must not be quiet.** A caller cannot tell "probed and not held"
        # from "gave up halfway" by the return value alone, so the break says so on stderr.
        self.assertIn("UNKNOWN", err.getvalue(), "a budget break must say so on stderr")
        self.assertIn("2 pid(s) unanswered", err.getvalue(), "and name how many were left")

    def test_an_exhausted_threaded_deadline_spawns_nothing(self):
        # ⚠️ The identity pass makes TWO calls over one probe. A second derived deadline would
        # let one `read_registry` spend twice the bound the module note advertises — the
        # unbounded-fan-out shape this change exists to remove, re-entered on the mismatch arm.
        spawns = []
        restore = self._stub_probe(spawns, lambda argv: "%s %s\n" % (argv[-1], self._PS_LINE))
        try:
            out = self.m._ps_starts([os.getpid()], deadline=time.monotonic() - 1)
        finally:
            restore()
        self.assertEqual(spawns, [], "a threaded deadline already in the past spawns no `ps`")
        self.assertEqual(out, {}, "and answers nothing, which is UNKNOWN rather than death")

    def test_a_future_dated_entry_is_pruned_not_kept_forever(self):
        # ⚠️ `now - read_at >= TTL` misses a FUTURE-dated `read_at` — clock skew, or a
        # hand-edited file — and that is the one input that lets the map grow without bound,
        # since the read guard's `0 <=` lower bound already refuses to serve such an entry.
        never_occupied = 999999  # nothing will re-read it, so only the prune can remove it
        self.m._START_CACHE = {never_occupied: [123, time.time() + 10_000]}
        spawns = []
        restore = self._stub_probe(spawns, lambda argv: "%s %s\n" % (argv[-1], self._PS_LINE))
        try:
            self.m._ps_starts([os.getpid()])  # learning one pid is what triggers the prune
        finally:
            restore()
        self.assertNotIn(
            never_occupied, self.m._START_CACHE, "a future-dated entry must be pruned, not kept"
        )

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

    def test_a_non_finite_or_non_positive_tunable_falls_back_to_its_default(self):
        # ⚠️ `float()` accepts `nan` and `inf`, and a `nan` timeout is not a bound at all:
        # `min(nan, remaining)` is `nan`, so `subprocess`'s deadline test never fires and the
        # child is never killed — the original orphan, reachable through a single typo. A value
        # that cannot bound anything must not be allowed to *unbound* something.
        for bad in ("nan", "inf", "-inf", "0", "-1", "5s"):
            os.environ["SUPERVISOR_PS_TIMEOUT"] = bad
            try:
                self.assertEqual(
                    self.m._env_float("SUPERVISOR_PS_TIMEOUT", 5),
                    5,
                    "%r must fall back to the default rather than becoming the timeout" % bad,
                )
            finally:
                os.environ.pop("SUPERVISOR_PS_TIMEOUT", None)
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


if __name__ == "__main__":
    unittest.main()
