#!/usr/bin/env python3
"""Tests for the fleet pusher cadence — scripts/fleet-pusher-tick.sh and -watch.sh.

Each case guards a defect that was measured on this machine, not imagined:

  * **The resolution must read the newest installed version.** A version-pinned cache path is
    the failure the plists' own headers name — "a cache path would pin the agent to one release
    and silently run stale code after every upgrade". `test_a_pinned_version_is_observably_stale`
    runs the MUTATION as well as the real script, so the assertion cannot pass on a hardcoded
    path: the same test that demands 0.10.0 shows the pinned copy producing 0.1.0.
  * **A pane-wake `{"error": ...}` line is a broken environment, not a skipped session.** Measured
    2026-11-12: with `wezterm` missing from the launchd PATH every tick exited 0 and logged a
    single error line, so `launchctl list` showed a healthy agent that woke nothing. The tick must
    exit non-zero there.
  * **A fresh heartbeat is not a success signal.** The heartbeat is written before any work, so a
    tick that resolves zero sessions produces a fresh heartbeat and exit 0. Pinned here so nobody
    "fixes" the heartbeat into a delivery probe.
  * **The watchdog must not cry wolf on a valid timestamp.** Measured 2026-11-12: BSD
    `date -j -f "%Y-%m-%dT%H:%M:%SZ"` does not accept a literal trailing `Z`, so an eight-minute
    old heartbeat was reported as unparseable. A watchdog that false-positives gets ignored,
    which is the same silent failure it exists to catch.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_TICK = os.path.join(_SCRIPTS, "fleet-pusher-tick.sh")
_WATCH = os.path.join(_SCRIPTS, "fleet-pusher-watch.sh")

_TICK_SOURCE = open(_TICK, encoding="utf-8").read()
_WATCH_SOURCE = open(_WATCH, encoding="utf-8").read()


def _write(path, body, executable=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    if executable:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _version(root, version, *, sessions='[{"sessionId": "s1", "alive": true}]',
             wake='print(\'{"sessionId": "s1", "outcome": "delivered"}\')',
             liveness_body=None):
    """Lay down one installed supervisor version, with a stub for each callee."""
    scripts = os.path.join(root, version, "scripts")
    _write(
        os.path.join(scripts, "session-liveness.py"),
        liveness_body if liveness_body is not None else
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--list' in sys.argv:\n"
        "    print(%r)\n" % sessions,
    )
    _write(
        os.path.join(scripts, "pane-wake.py"),
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "marker = os.environ.get('TICK_MARKER')\n"
        "if marker:\n"
        "    open(marker, 'w').write(%r)\n" % version
        + wake + "\n",
    )
    return scripts


class TickSyntax(unittest.TestCase):
    def test_tick_parses(self):
        self.assertEqual(subprocess.run(["bash", "-n", _TICK]).returncode, 0)

    def test_watch_parses(self):
        self.assertEqual(subprocess.run(["bash", "-n", _WATCH]).returncode, 0)

    def test_tick_invokes_pane_wake_with_send(self):
        """A diagnostic echo mentioning pane-wake.py is not an invocation of it: require the
        interpreter, the resolved path and the send flag on one non-comment line."""
        import re
        live = [
            line for line in _TICK_SOURCE.splitlines()
            if not line.lstrip().startswith("#")
            and re.search(r'python3\s+"\$S/pane-wake\.py".*--send', line)
        ]
        self.assertTrue(live, "no executable line invokes pane-wake.py with --send")


class TickResolution(unittest.TestCase):
    """The tick resolves the NEWEST installed supervisor, and a pinned path is observably
    stale. The mutation runs in the same test as the real script, so a hardcoded path cannot
    satisfy it."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.root = os.path.join(
            self.home, ".claude", "plugins", "cache", "claude-supervisor", "supervisor"
        )
        # `sort -V` orders 0.1.0 BEFORE 0.10.0 because it compares 1 with 10 as numbers. A plain
        # byte-wise sort puts them in the same order here ('.' < '0'), which is why the version
        # fixture also carries a 0.9.0 — under `sort -V` 0.9.0 < 0.10.0, byte-wise "0.9.0" >
        # "0.10.0". That difference is what makes this a test of `sort -V` rather than of luck.
        _version(self.root, "0.1.0")
        _version(self.root, "0.9.0")
        _version(self.root, "0.10.0")
        self.marker = os.path.join(self.home, "marker")

    def _run(self, script):
        env = dict(os.environ, HOME=self.home, TICK_MARKER=self.marker)
        return subprocess.run(
            ["bash", script], env=env, capture_output=True, text=True, timeout=120
        )

    def _ran(self):
        with open(self.marker, encoding="utf-8") as handle:
            return handle.read().strip()

    def test_resolves_the_newest_installed_version(self):
        result = self._run(_TICK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plugin 0.10.0", result.stdout)
        self.assertEqual(self._ran(), "0.10.0")

    def test_a_pinned_version_is_observably_stale(self):
        """THE MUTATION. Replace the resolution with a hardcoded cache path and the same
        assertion the test above makes now fails — which is what makes that assertion a test of
        the resolution rather than of the stub."""
        mutated = os.path.join(self.home, "tick-pinned.sh")
        pinned = _TICK_SOURCE.replace(
            'LATEST="$(ls -1 "$SUP_ROOT" 2>/dev/null | grep -E \'^[0-9]+\\.[0-9]+\\.[0-9]+$\' '
            '| sort -V | tail -1)"',
            'LATEST="0.1.0"',
        )
        self.assertNotEqual(mutated, _TICK_SOURCE, "mutation did not apply — the resolution line moved")
        _write(mutated, pinned, executable=True)
        result = self._run(mutated)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._ran(), "0.1.0",
            "the pinned copy must run the stale version — otherwise this test cannot fail",
        )

    def test_no_hardcoded_version_path_in_the_script(self):
        import re
        hardcoded = re.findall(
            r"claude-supervisor/supervisor/\d+\.\d+\.\d+", _TICK_SOURCE
        )
        self.assertEqual(hardcoded, [])

    def test_missing_install_is_terminal(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        env = dict(os.environ, HOME=empty)
        result = subprocess.run(
            ["bash", _TICK], env=env, capture_output=True, text=True, timeout=120
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TERMINAL:", result.stdout)


class TickEnvironment(unittest.TestCase):
    """The 2026-11-12 green-but-idle incident: an error line is not a skipped session."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.root = os.path.join(
            self.home, ".claude", "plugins", "cache", "claude-supervisor", "supervisor"
        )

    def _run(self):
        env = dict(os.environ, HOME=self.home)
        return subprocess.run(
            ["bash", _TICK], env=env, capture_output=True, text=True, timeout=120
        )

    def test_an_error_line_exits_non_zero(self):
        _version(
            self.root, "1.0.0",
            wake='print(\'{"error": "wezterm cli list failed"}\')',
        )
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("environment is broken", result.stdout)

    def test_zero_sessions_still_writes_the_heartbeat_and_exits_zero(self):
        """Documents the trap rather than guarding against it: a fresh heartbeat is NOT proof
        of a delivery. The pusher's acceptance criterion reads a `delivered=N` log line."""
        _version(self.root, "1.0.0", sessions="[]")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        heartbeat = os.path.join(self.home, ".claude", "state", "fleet-pusher", "heartbeat")
        self.assertTrue(os.path.exists(heartbeat))
        self.assertIn("0 live session(s)", result.stdout)


class TickLiveness(unittest.TestCase):
    """A failed liveness read must never render as an empty fleet.

    This was a blocking review finding, and it is the same class of defect the cadence exists to
    remove: `session-liveness.py` failing collapsed into "0 live session(s)", the tick exited 0
    with a fresh heartbeat, and the watchdog — which reads only the heartbeat — stayed silent. A
    broken read and an idle fleet must not look alike.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.root = os.path.join(
            self.home, ".claude", "plugins", "cache", "claude-supervisor", "supervisor"
        )

    def _run(self):
        env = dict(os.environ, HOME=self.home)
        return subprocess.run(
            ["bash", _TICK], env=env, capture_output=True, text=True, timeout=120
        )

    def test_a_non_zero_liveness_exit_is_terminal(self):
        _version(
            self.root, "1.0.0",
            liveness_body="#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n",
        )
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("session-liveness.py unreadable", result.stdout)

    def test_unparseable_liveness_output_is_terminal(self):
        _version(
            self.root, "1.0.0",
            liveness_body="#!/usr/bin/env python3\nprint('not json')\n",
        )
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("session-liveness.py unreadable", result.stdout)

    def test_an_empty_but_valid_read_is_not_terminal(self):
        """The negative control: a genuinely idle fleet must still exit 0."""
        _version(self.root, "1.0.0", sessions="[]")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("TERMINAL:", result.stdout)


class Watchdog(unittest.TestCase):
    """Both directions, against a stub poster so the real board is never touched."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.script_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.script_dir, ignore_errors=True)
        self.calls = os.path.join(self.home, "calls")
        shutil.copy(_WATCH, os.path.join(self.script_dir, "fleet-pusher-watch.sh"))
        _write(
            os.path.join(self.script_dir, "attention-ask.py"),
            # The real tool refuses a post with no producer id when CLAUDE_CODE_SESSION_ID is
            # unset — which is exactly the launchd environment. Modelling that here makes the
            # producer-id assertion behavioural: a watchdog that drops the flag records no call.
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if '--producer-id' not in sys.argv:\n"
            "    print('REFUSED: no --producer-id and CLAUDE_CODE_SESSION_ID is unset', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "open(os.environ['WATCH_CALLS'], 'a').write(' '.join(sys.argv[1:]) + '\\n')\n",
            executable=True,
        )
        self.heartbeat = os.path.join(
            self.home, ".claude", "state", "fleet-pusher", "heartbeat"
        )
        os.makedirs(os.path.dirname(self.heartbeat), exist_ok=True)

    def _run(self):
        env = dict(os.environ, HOME=self.home, WATCH_CALLS=self.calls,
                   FLEET_PUSHER_BOOT_EPOCH="0")  # pin the boot grace out of the way
        return subprocess.run(
            ["bash", os.path.join(self.script_dir, "fleet-pusher-watch.sh")],
            env=env, capture_output=True, text=True, timeout=120,
        )

    def _calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if line.strip()]

    def _stamp(self, seconds_ago):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))

    def test_a_fresh_heartbeat_posts_nothing(self):
        _write(self.heartbeat, self._stamp(30))
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("ok age=", result.stdout)
        self.assertEqual(self._calls(), [])

    def test_an_eight_minute_old_heartbeat_is_not_unparseable(self):
        """THE REGRESSION. BSD `date -j -f "%Y-%m-%dT%H:%M:%SZ"` rejected the trailing `Z` and
        posted a false 'unparseable' card for a heartbeat that was eight minutes old."""
        _write(self.heartbeat, self._stamp(8 * 60))
        result = self._run()
        self.assertIn("ok age=", result.stdout)
        self.assertNotIn("unparseable", result.stdout)
        self.assertEqual(self._calls(), [])

    def test_a_stale_heartbeat_posts_exactly_one_stale_card(self):
        _write(self.heartbeat, self._stamp(1800 + 60))
        result = self._run()
        self.assertEqual(result.returncode, 0)
        calls = self._calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("--dedup-key fleet-pusher-heartbeat-stale", calls[0])
        self.assertIn("old", calls[0])
        # The post is REFUSED without this flag under launchd (no CLAUDE_CODE_SESSION_ID), so a
        # watchdog that omits it detects a dead cadence and tells nobody.
        self.assertIn("--producer-id fleet-pusher-watch", calls[0])

    def test_the_boot_grace_suppresses_a_card(self):
        """A reboot is not a dead cadence: right after boot the tick has had no chance to run."""
        _write(self.heartbeat, self._stamp(1800 + 60))
        env = dict(os.environ, HOME=self.home, WATCH_CALLS=self.calls,
                   FLEET_PUSHER_BOOT_EPOCH=str(int(time.time()) - 60))
        result = subprocess.run(
            ["bash", os.path.join(self.script_dir, "fleet-pusher-watch.sh")],
            env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("startup grace", result.stdout)
        self.assertEqual(self._calls(), [])

    def test_a_missing_heartbeat_posts_the_missing_key(self):
        result = self._run()
        self.assertEqual(result.returncode, 0)
        calls = self._calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("--dedup-key fleet-pusher-heartbeat-missing", calls[0])

    def test_an_unparseable_heartbeat_posts_the_unparseable_key(self):
        _write(self.heartbeat, "not-a-timestamp")
        result = self._run()
        self.assertEqual(result.returncode, 0)
        calls = self._calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("--dedup-key fleet-pusher-heartbeat-unparseable", calls[0])

    def test_the_poster_is_resolved_from_the_scripts_own_directory(self):
        """A hardcoded marketplace-clone path would break for any other clone location."""
        self.assertNotIn("marketplaces/claude-supervisor/scripts/attention-ask.py", _WATCH_SOURCE)
        self.assertIn("BASH_SOURCE", _WATCH_SOURCE)


class Plists(unittest.TestCase):
    """The plists are the install artifact; a malformed one fails at bootstrap, not at review."""

    def setUp(self):
        import plistlib
        self.plistlib = plistlib

    def _load(self, name):
        with open(os.path.join(_SCRIPTS, "..", "launchd", name), "rb") as handle:
            return self.plistlib.load(handle)

    def test_pusher_plist(self):
        d = self._load("com.bborbe.fleet-pusher.plist")
        self.assertEqual(d["Label"], "com.bborbe.fleet-pusher")
        self.assertEqual(d["StartInterval"], 900)
        self.assertTrue(d["ProgramArguments"][-1].endswith("fleet-pusher-tick.sh"))
        self.assertIn("marketplaces/claude-supervisor/scripts/", d["ProgramArguments"][-1])
        self.assertTrue(
            d["EnvironmentVariables"]["PATH"].endswith(
                "/Applications/WezTerm.app/Contents/MacOS"
            ),
            "the WezTerm directory must be LAST so nothing there shadows /usr/bin",
        )
        self.assertIn("StandardOutPath", d)

    def test_watch_plist(self):
        d = self._load("com.bborbe.fleet-pusher-watch.plist")
        self.assertEqual(d["Label"], "com.bborbe.fleet-pusher-watch")
        self.assertEqual(d["StartInterval"], 300)
        self.assertTrue(d["ProgramArguments"][-1].endswith("fleet-pusher-watch.sh"))


if __name__ == "__main__":
    unittest.main()
