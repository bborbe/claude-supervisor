"""manager-wake.py — resolution, prompt validation, and the dry-run path.

Exercises the pure logic without a live pane: the manager-state resolution (which
must pick the newest registration and honour `--tier auto`), the `--interval`
validation that guards the typed prompt, and the dry-run branch that must never
call `wezterm`. The submit-confirmation path needs a live pane and is not driven
here — the blocking review finding it fixes is covered by the code's own comment
and by `scripts/send-delivery-drill.py`.
"""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
PATH = os.path.join(SCRIPTS, "manager-wake.py")


def load():
    spec = importlib.util.spec_from_file_location("manager_wake_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dead_pid():
    """A pid that provably does not exist: a child that has already been reaped."""
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class _Registries(unittest.TestCase):
    """Isolates all three stores the resolver reads: worker-manager, `.session`, registry."""

    A = "11111111-1111-1111-1111-111111111111"
    B = "22222222-2222-2222-2222-222222222222"
    C = "33333333-3333-3333-3333-333333333333"

    def setUp(self):
        self.mod = load()
        self.state = tempfile.mkdtemp(prefix="mw-state-")
        self.markers = tempfile.mkdtemp(prefix="mw-markers-")
        self.registry = tempfile.mkdtemp(prefix="mw-registry-")
        self.mod.MANAGER_STATE = self.state
        self.mod.SESSION_MARKER_DIR = self.markers
        self.mod.SESSIONS_DIR = self.registry

    def _write(self, sid, subject, branch, resolved_at):
        with open(os.path.join(self.state, f"{sid}.json"), "w", encoding="utf-8") as fh:
            json.dump({"subject": subject, "branch": branch, "resolved_at": resolved_at}, fh)

    def _marker(self, branch, slug, sid, mtime=None):
        p = os.path.join(self.markers, f"{branch}--{slug}.session")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(sid + "\n")
        if mtime is not None:
            os.utime(p, (mtime, mtime))

    def _live(self, *sids):
        for i, sid in enumerate(sids):
            with open(os.path.join(self.registry, f"{90000 + i}.json"), "w", encoding="utf-8") as fh:
                json.dump({"sessionId": sid, "pid": os.getpid()}, fh)

    def _stale_registry_entry(self, sid):
        with open(os.path.join(self.registry, "99999.json"), "w", encoding="utf-8") as fh:
            json.dump({"sessionId": sid, "pid": _dead_pid()}, fh)


class ResolveManagerSession(_Registries):
    def test_newest_live_wins(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._write(self.B, "Goal X", "goal", "2026-02-01T00:00:00Z")
        self._live(self.A, self.B)
        self.assertEqual((self.B, "goal", True), self.mod.resolve_manager_session("Goal X", "goal"))

    def test_dead_registration_newer_than_live_one_loses(self):
        # The measured defect: the newest registration names a session dead for days,
        # while an older one is the live manager. Recency must not outrank liveness.
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._write(self.B, "Goal X", "goal", "2026-02-01T00:00:00Z")
        self._live(self.A)
        self.assertEqual((self.A, "goal", True), self.mod.resolve_manager_session("Goal X", "goal"))

    def test_session_marker_alone_resolves_a_live_manager(self):
        # The live Development Pipeline Throughput shape: no worker-manager record for the
        # live session, only the `.session` marker `--arm` wrote.
        self._write(self.A, "Development Pipeline Throughput", "topic", "2026-11-09T20:37:36+01:00")
        self._marker("topic", "development-pipeline-throughput", self.B)
        self._live(self.B)
        self.assertEqual(
            (self.B, "topic", True),
            self.mod.resolve_manager_session("Development Pipeline Throughput", "auto"),
        )

    def test_marker_branch_honours_the_tier(self):
        self._marker("topic", "goal-x", self.A)
        self._live(self.A)
        self.assertEqual((None, None, False), self.mod.resolve_manager_session("Goal X", "goal"))
        self.assertEqual((self.A, "topic", True), self.mod.resolve_manager_session("Goal X", "topic"))

    def test_all_dead_names_the_newest_dead_session(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._write(self.B, "Goal X", "goal", "2026-02-01T00:00:00Z")
        self._live(self.C)
        self.assertEqual((self.B, "goal", False), self.mod.resolve_manager_session("Goal X", "auto"))

    def test_registry_entry_with_a_dead_pid_is_not_live(self):
        # A session killed hard leaves its `<pid>.json` behind.
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._stale_registry_entry(self.A)
        self.assertEqual((self.A, "goal", False), self.mod.resolve_manager_session("Goal X", "auto"))

    def test_unreadable_registry_falls_back_to_newest_with_unknown_liveness(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._write(self.B, "Goal X", "goal", "2026-02-01T00:00:00Z")
        self.mod.SESSIONS_DIR = os.path.join(self.registry, "missing")
        self.assertEqual((self.B, "goal", None), self.mod.resolve_manager_session("Goal X", "auto"))

    def test_auto_matches_any_branch(self):
        self._write(self.A, "Topic Y", "topic", "2026-01-01T00:00:00Z")
        self._live(self.A)
        self.assertEqual((self.A, "topic", True), self.mod.resolve_manager_session("Topic Y", "auto"))

    def test_tier_mismatch_is_none(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._live(self.A)
        self.assertEqual((None, None, False), self.mod.resolve_manager_session("Goal X", "topic"))

    def test_subject_match_is_case_insensitive(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._live(self.A)
        self.assertEqual((self.A, "goal", True), self.mod.resolve_manager_session("goal x", "auto"))

    def test_no_match_is_none(self):
        self.assertEqual((None, None, False), self.mod.resolve_manager_session("Nope", "auto"))

    def test_offsets_are_compared_as_instants(self):
        # 05:00Z vs 04:00-02:00 (= 06:00Z). A raw-string compare picks A; an instant
        # compare picks B, which is the newer registration.
        self._write(self.A, "Goal X", "goal", "2026-01-01T05:00:00+00:00")
        self._write(self.B, "Goal X", "goal", "2026-01-01T04:00:00-02:00")
        self._live(self.A, self.B)
        self.assertEqual((self.B, "goal", True), self.mod.resolve_manager_session("Goal X", "auto"))

    def test_naive_stamp_compares_with_a_marker_mtime(self):
        # A naive `resolved_at` must not raise against the aware marker mtime.
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00")
        self._marker("goal", "goal-x", self.B, mtime=1798761600)  # 2027-01-01T00:00:00Z
        self._live(self.A, self.B)
        self.assertEqual((self.B, "goal", True), self.mod.resolve_manager_session("Goal X", "auto"))


class MainValidation(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.mod.MANAGER_STATE = tempfile.mkdtemp(prefix="mw-empty-")
        self.mod.SESSION_MARKER_DIR = tempfile.mkdtemp(prefix="mw-empty-markers-")
        self.mod.SESSIONS_DIR = tempfile.mkdtemp(prefix="mw-empty-registry-")

    def test_non_numeric_interval_is_usage_error(self):
        self.assertEqual(
            2, self.mod.main(["--subject", "X", "--interval", "15m; rm -rf /", "--dry-run"])
        )

    def test_zero_interval_is_usage_error(self):
        self.assertEqual(2, self.mod.main(["--subject", "X", "--interval", "0", "--dry-run"]))

    def test_no_manager_exits_zero(self):
        self.assertEqual(0, self.mod.main(["--subject", "__nope__", "--dry-run"]))


class MainResultLines(_Registries):
    """Every result line names the resolved session and its liveness."""

    def _run(self, *argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.mod.main(["--subject", "Goal X", "--dry-run", *argv])
        self.assertEqual(0, rc)
        return buf.getvalue().strip()

    def test_no_registration_is_no_manager(self):
        self.assertEqual("NO-MANAGER subject='Goal X' tier=auto session=- live=no", self._run())

    def test_only_dead_registrations_is_no_manager_not_no_pane(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self.mod.resolve_pane = lambda sid: self.fail("a dead manager must not reach the pane lookup")
        self.assertEqual(
            "NO-MANAGER subject='Goal X' tier=auto session=11111111 live=no", self._run()
        )

    def test_live_manager_without_a_pane_is_no_pane_live_yes(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._live(self.A)
        self.mod.resolve_pane = lambda sid: (None, "tty-not-a-pane:ttys009")
        self.assertEqual(
            "NO-PANE reason=tty-not-a-pane:ttys009 subject='Goal X' session=11111111 live=yes",
            self._run(),
        )

    def _entry(self, sid, entrypoint):
        with open(os.path.join(self.registry, "90000.json"), "w", encoding="utf-8") as fh:
            json.dump({"sessionId": sid, "pid": os.getpid(), "entrypoint": entrypoint}, fh)

    def test_live_headless_manager_is_headless_not_no_pane(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._entry(self.A, "sdk-cli")
        self.mod.resolve_pane = lambda sid: (None, "no-tty")
        self.mod._pid_tty = lambda pid: ""
        self.assertEqual("HEADLESS subject='Goal X' session=11111111 live=yes", self._run())

    def test_sdk_cli_entrypoint_with_a_tty_stays_no_pane(self):
        # A tab opened from a headless leg registers `sdk-cli` but owns a terminal: a
        # missed pane lookup on it is NO-PANE, never excused as HEADLESS.
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._entry(self.A, "sdk-cli")
        self.mod.resolve_pane = lambda sid: (None, "tty-not-a-pane:ttys012")
        self.mod._pid_tty = lambda pid: "ttys012"
        self.assertEqual(
            "NO-PANE reason=tty-not-a-pane:ttys012 subject='Goal X' session=11111111 live=yes",
            self._run(),
        )

    def test_unreadable_tty_is_not_headless(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._entry(self.A, "sdk-cli")
        self.mod.resolve_pane = lambda sid: (None, "tty-unreadable")
        self.mod._pid_tty = lambda pid: None
        self.assertEqual(
            "NO-PANE reason=tty-unreadable subject='Goal X' session=11111111 live=yes",
            self._run(),
        )

    def test_skip_carries_the_session(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._live(self.A)
        self.mod.resolve_pane = lambda sid: ("7", None)
        self.mod.read_state = lambda subject, branch: "OK"
        self.mod.guard = lambda pane, state=None: "skip:working"
        self.assertEqual("SKIP skip:working pane=7 session=11111111 live=yes", self._run())

    def test_wake_carries_the_session(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._live(self.A)
        self.mod.resolve_pane = lambda sid: ("7", None)
        self.mod.read_state = lambda subject, branch: "OK"
        self.mod.guard = lambda pane, state=None: "wake"
        out = self._run()
        self.assertTrue(out.startswith("WOULD-WAKE pane=7 "), out)
        self.assertTrue(out.endswith(" session=11111111 live=yes"), out)


class WakeDryRun(unittest.TestCase):
    def test_dry_run_types_nothing(self):
        out = load().wake(123, "/supervisor:goal-drive --interval 15m", dry_run=True)
        self.assertIn("WOULD-WAKE", out)
        self.assertIn("pane=123", out)


class NonSessionFilesIgnored(_Registries):
    """`last-<vault>.json` sits in the same dir; it must never be read as a session id."""

    def test_last_vault_file_is_ignored(self):
        # Newer, but not a session id — must lose to the UUID even on recency.
        sid = "11111111-2222-3333-4444-555555555555"
        self._write("last-private-personal", "Goal X", "goal", "2026-03-01T00:00:00Z")
        self._write(sid, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._live(sid)
        self.assertEqual((sid, "goal", True), self.mod.resolve_manager_session("Goal X", "auto"))

    def test_only_last_vault_file_is_none(self):
        self._write("last-private-personal", "Goal X", "goal", "2026-03-01T00:00:00Z")
        self.assertEqual((None, None, False), self.mod.resolve_manager_session("Goal X", "auto"))

    def test_malformed_marker_is_ignored(self):
        self._marker("goal", "goal-x", "not-a-session-id")
        self.assertEqual((None, None, False), self.mod.resolve_manager_session("Goal X", "auto"))


class WakeConfirmation(unittest.TestCase):
    """The submit-confirmation loop, driven with fake pane reads and a no-op send."""

    def setUp(self):
        import time as _time
        self.mod = load()
        self._real_sleep = _time.sleep
        self.mod.time.sleep = lambda _s: None

        class _R:
            returncode = 0
            stderr = ""
        self._real_run = self.mod.subprocess.run
        self.mod.subprocess.run = lambda *a, **k: _R()

    def tearDown(self):
        import time as _time
        _time.sleep = self._real_sleep
        self.mod.subprocess.run = self._real_run

    def _wake_with(self, reads):
        it = iter(reads)
        self.mod._pane_text = lambda _pane: next(it, reads[-1])
        return self.mod.wake(1, "/supervisor:goal-drive --interval 15m", dry_run=False)

    def test_swallowed_then_cleared_is_woken(self):
        out = self._wake_with([
            "❯ /supervisor:goal-drive --interval 15m",
            "❯ \n> /supervisor:goal-drive --interval 15m",
        ])
        self.assertIn("WOKEN", out)

    def test_never_landed_is_unverified(self):
        out = self._wake_with(["❯ ", "❯ "])
        self.assertIn("UNVERIFIED", out)

    def test_queued_is_woken(self):
        out = self._wake_with(["❯ press up to edit queued messages"])
        self.assertIn("WOKEN", out)
        self.assertIn("queued", out)

    def test_always_swallowed_is_failed(self):
        out = self._wake_with(["❯ /supervisor:goal-drive --interval 15m"] * 20)
        self.assertIn("FAILED", out)


class Guard(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self._real_run = self.mod.subprocess.run

    def tearDown(self):
        self.mod.subprocess.run = self._real_run

    def _run_returning(self, stdout):
        class R:
            returncode = 0
            stderr = ""
            def __init__(self, out):
                self.stdout = out
        self.mod.subprocess.run = lambda *a, **k: R(stdout)

    def test_wake_verdict_passes_through(self):
        self._run_returning("wake\n")
        self.assertEqual("wake", self.mod.guard(1))

    def test_empty_stdout_is_error(self):
        self._run_returning("")
        self.assertEqual("error:no-verdict", self.mod.guard(1))

    def test_subject_state_is_passed_to_the_guard(self):
        seen = {}

        class R:
            returncode = 0
            stdout = "skip:sleeping\n"
            stderr = ""

        def run(argv, **kw):
            seen["argv"] = argv
            return R()

        self.mod.subprocess.run = run
        self.assertEqual("skip:sleeping", self.mod.guard(1, "SLEEPING"))
        self.assertIn("--subject-state", seen["argv"])
        self.assertEqual(seen["argv"][-1], "SLEEPING")

    def test_no_state_omits_the_flag(self):
        seen = {}

        class R:
            returncode = 0
            stdout = "wake\n"
            stderr = ""

        def run(argv, **kw):
            seen["argv"] = argv
            return R()

        self.mod.subprocess.run = run
        self.assertEqual("wake", self.mod.guard(1, None))
        self.assertNotIn("--subject-state", seen["argv"])


class ReadState(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self._real_run = self.mod.subprocess.run

    def tearDown(self):
        self.mod.subprocess.run = self._real_run

    def _run_returning(self, stdout, returncode=0):
        class R:
            def __init__(self, out, rc):
                self.stdout = out
                self.returncode = rc
                self.stderr = ""

        self.mod.subprocess.run = lambda *a, **k: R(stdout, returncode)

    def test_first_field_is_the_state(self):
        self._run_returning("SLEEPING\tgoal--x\tuntil 2026-11-12T01:22:59+01:00\n")
        self.assertEqual("SLEEPING", self.mod.read_state("X", "goal"))

    def test_non_zero_exit_is_the_unreadable_sentinel_not_a_state(self):
        # This caller always knows its subject, so a failed read fails CLOSED.
        self._run_returning("SLEEPING\tgoal--x\tdetail\n", returncode=3)
        self.assertEqual(self.mod.read_state("X", "goal"), self.mod._guard_mod().STATE_UNREADABLE)

    def test_empty_stdout_is_the_unreadable_sentinel(self):
        self._run_returning("")
        self.assertEqual(self.mod.read_state("X", "goal"), self.mod._guard_mod().STATE_UNREADABLE)

    def test_leading_tab_is_stripped_before_the_first_field(self):
        # `.strip()` removes the leading tab, so an empty first field cannot arise from
        # a tab-led line — the `or` fallback is defensive, not reachable here. Pinned so
        # a future reader does not "fix" it into the sentinel by mistake.
        self._run_returning("\tgoal--x\tdetail\n")
        self.assertEqual(self.mod.read_state("X", "goal"), "goal--x")


class ResolvePane(_Registries):
    """The join runs from the target session: registry -> pid -> tty -> pane."""

    def setUp(self):
        super().setUp()
        self.mod.PANE_RETRY_DELAY_S = 0
        self.ttys = {}
        self.mod._pid_tty = lambda pid: self.ttys.get(str(pid))
        self.panes = {"6": {"tty_name": "/dev/ttys012"}, "7": {"tty_name": "/dev/ttys013"}}
        self.calls = 0
        outer = self

        class J:
            @staticmethod
            def wezterm_panes():
                outer.calls += 1
                return outer.panes() if callable(outer.panes) else outer.panes
        self.mod._jump = lambda: J

    def _reg(self, name, sid, pid, tty):
        with open(os.path.join(self.registry, f"{name}.json"), "w", encoding="utf-8") as fh:
            json.dump({"sessionId": sid, "pid": pid}, fh)
        self.ttys[str(pid)] = tty

    def test_match(self):
        self._reg("100", self.A, 100, "ttys012")
        self.assertEqual(("6", None), self.mod.resolve_pane(self.A))

    def test_a_second_session_on_the_same_tty_does_not_hide_the_manager(self):
        # The `{tty: session}` map in `jump.pane_sessions` keeps ONE session per tty, so a
        # second registered session on the manager's tty could take the pane. Joining from
        # the target makes the other session irrelevant.
        self._reg("100", self.A, 100, "ttys012")
        self._reg("200", self.B, 200, "ttys012")
        self.assertEqual(("6", None), self.mod.resolve_pane(self.A))
        self.assertEqual(("6", None), self.mod.resolve_pane(self.B))

    def test_a_stale_twin_record_does_not_hide_the_live_one(self):
        self._reg("100", self.A, 100, None)  # a dead pid's leftover file: tty unreadable
        self._reg("101", self.A, 101, "ttys013")
        self.assertEqual(("7", None), self.mod.resolve_pane(self.A))

    def test_no_registry_entry(self):
        self.assertEqual((None, "no-registry-entry"), self.mod.resolve_pane(self.A))

    def test_no_tty(self):
        self._reg("100", self.A, 100, "")
        self.assertEqual((None, "no-tty"), self.mod.resolve_pane(self.A))

    def test_tty_not_a_pane(self):
        self._reg("100", self.A, 100, "ttys099")
        self.assertEqual((None, "tty-not-a-pane:ttys099"), self.mod.resolve_pane(self.A))

    def test_unreadable_pane_list_is_retried_once(self):
        self._reg("100", self.A, 100, "ttys012")
        answers = [None, {"6": {"tty_name": "/dev/ttys012"}}]
        self.panes = lambda: answers.pop(0)
        self.assertEqual(("6", None), self.mod.resolve_pane(self.A))
        self.assertEqual(2, self.calls)

    def test_persistently_unreadable_pane_list_names_the_read(self):
        self.panes = lambda: None
        self.assertEqual((None, "wezterm-unreadable"), self.mod.resolve_pane(self.A))
        self.assertEqual(self.mod.PANE_ATTEMPTS, self.calls)

    def test_a_flaky_pane_list_resolving_on_the_third_read_wakes(self):
        """The 2026-11-16 shape: `wezterm cli list` fails per call, flakily, so wakes
        minutes either side of a `NO-PANE` succeeded. One retry was not enough."""
        self._reg("100", self.A, 100, "ttys012")
        answers = [None, None, {"6": {"tty_name": "/dev/ttys012"}}]
        self.panes = lambda: answers.pop(0)
        self.assertEqual(("6", None), self.mod.resolve_pane(self.A))
        self.assertEqual(3, self.calls)

    def test_a_standing_miss_is_not_retried(self):
        # `no-tty` is structural: a headless session has no terminal by construction, so a
        # second read cannot clear it and the tick must not pay 2 s to learn that.
        self._reg("100", self.A, 100, "")
        self.mod.resolve_pane(self.A)
        self.assertEqual(1, self.calls)


if __name__ == "__main__":
    unittest.main()
