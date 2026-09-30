#!/usr/bin/env python3
"""Tests for scripts/cluster-heartbeat.py.

The load-bearing properties, in the order SC4 ranks them:

  * an unreachable cluster is UNKNOWN, never an empty list — the empty list is the
    dangerous direction, because it reads as "no cluster worker is live" for every
    worker in the half that failed;
  * an ABSENT ConfigMap is NOT an unreachable cluster — it is a readable-and-empty
    answer, and collapsing the two loses the distinction SC4 rests on;
  * the verdict is the stamp's AGE, never the key's existence — a pod killed with no
    chance to clean up leaves its entry behind;
  * a prefix resolves the same way it does in the local reader.

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
from contextlib import redirect_stderr, redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))

LIVE, STALE, UNKNOWN = 0, 1, 2


def load():
    spec = importlib.util.spec_from_file_location(
        "cluster_heartbeat", os.path.join(os.path.dirname(_HERE), "cluster-heartbeat.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def cm(stamps):
    """A ConfigMap object as `kubectl -o json` returns it."""
    return json.dumps({"data": {sid: json.dumps({"refreshedAt": at}) for sid, at in stamps.items()}})


def rfc3339(epoch):
    import datetime

    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class ClusterHeartbeat(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.now = time.time()

    # ---- the verdict is age, not existence ---------------------------------------------

    def test_a_fresh_stamp_is_live(self):
        stamps = self.m.read_store(now=self.now, runner=lambda a: FakeProc(stdout=cm({"a1b2c3d4-1111": rfc3339(self.now - 5)})))
        self.assertEqual([s["session_id"] for s in stamps], ["a1b2c3d4-1111"])

    def test_a_stale_stamp_is_dropped(self):
        # Age past the TTL. A pod killed with no chance to clean up leaves the key behind, so
        # existence must not be the verdict.
        stamps = self.m.read_store(now=self.now, runner=lambda a: FakeProc(stdout=cm({"a1b2c3d4-1111": rfc3339(self.now - 120)})))
        self.assertEqual(stamps, [])

    def test_a_record_without_refreshed_at_is_skipped(self):
        payload = json.dumps({"data": {"a1b2c3d4-1111": json.dumps({"pid": 1})}})
        self.assertEqual(self.m.read_store(now=self.now, runner=lambda a: FakeProc(stdout=payload)), [])

    # ---- UNKNOWN vs empty: the dangerous direction -------------------------------------

    def test_an_unreachable_cluster_is_none_not_empty(self):
        # `None` and `[]` are different answers; collapsing them is the whole bug class.
        self.assertIsNone(self.m.read_store(now=self.now, runner=lambda a: FakeProc(returncode=1, stderr="connection refused")))

    def test_an_absent_configmap_is_empty_not_unknown(self):
        # `--ignore-not-found` makes a missing ConfigMap exit 0 with no output. That is a
        # readable-and-empty answer, and it must NOT read as an unreachable cluster.
        self.assertEqual(self.m.read_store(now=self.now, runner=lambda a: FakeProc(returncode=0, stdout="")), [])

    def test_an_unparseable_payload_is_unknown(self):
        self.assertIsNone(self.m.read_store(now=self.now, runner=lambda a: FakeProc(stdout="not json")))

    def test_a_raising_runner_is_unknown(self):
        def boom(_argv):
            raise OSError("no such file")

        self.assertIsNone(self.m.read_store(now=self.now, runner=boom))

    def test_the_read_uses_ignore_not_found(self):
        # Without the flag, "no ConfigMap yet" and "cluster unreachable" are both a non-zero
        # exit and the distinction is unrecoverable.
        seen = {}

        def capture(argv):
            seen["argv"] = argv
            return FakeProc(stdout="")

        self.m.read_store(now=self.now, runner=capture)
        self.assertIn("--ignore-not-found", seen["argv"])
        self.assertIn("-n", seen["argv"])

    # ---- --check ------------------------------------------------------------------------

    def check(self, session_id, stamps, ttl=None):
        argv = ["--check", session_id, "--cmd", self.fake, "--ttl", str(ttl or 60)]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(argv)
        return rc, out.getvalue() + err.getvalue()

    def setUp_fake(self, payload):
        """A real executable on PATH, so the `--cmd` path is exercised end to end."""
        d = tempfile.mkdtemp()
        path = os.path.join(d, "fake-kubectl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\ncat <<'JSON'\n%s\nJSON\n" % payload)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def test_check_resolves_a_prefix(self):
        self.fake = self.setUp_fake(cm({"a1b2c3d4-1111-2222": rfc3339(time.time() - 3)}))
        rc, out = self.check("a1b2c3d4", None)
        self.assertEqual(rc, LIVE)
        self.assertIn("LIVE", out)

    def test_check_on_a_stale_stamp_is_stale_not_live(self):
        self.fake = self.setUp_fake(cm({"a1b2c3d4-1111-2222": rfc3339(time.time() - 300)}))
        self.assertEqual(self.check("a1b2c3d4", None)[0], STALE)

    def test_check_with_two_matching_stamps_refuses(self):
        self.fake = self.setUp_fake(
            cm({"a1b2c3d4-1111-2222": rfc3339(time.time() - 3), "a1b2c3d4-9999-8888": rfc3339(time.time() - 3)})
        )
        rc, out = self.check("a1b2c3d4", None)
        self.assertEqual(rc, STALE)
        self.assertIn("none uniquely", out)

    def test_cli_unknown_when_the_wrapper_is_missing(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(["--list", "--cmd", "/nonexistent/kubectl-wrapper"])
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
