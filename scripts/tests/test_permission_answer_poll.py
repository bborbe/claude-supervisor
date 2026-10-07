#!/usr/bin/env python3
"""Tests for scripts/permission-answer-poll.py.

Each case pins a property measured on a live gate on 2026-09-27:

  * verdict -- only an answered card carrying allow/deny AND resolved_by counts.
  * resolver -- matches only THIS gate's permission card. A stale card left open
    by an earlier, already-answered gate of the same session must be refused;
    answering it would release the current gate under another's subject.
  * gate race -- attention-log.py runs in parallel, so a gate not yet in the
    feed is waited for, never read as closed.
  * fail-open -- a deadline, an error or the kill switch emits nothing on stdout.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import http.server
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "..", "permission-answer-poll.py")


def load():
    spec = importlib.util.spec_from_file_location("permission_answer_poll", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def card(item_id, payload="Write: /tmp/x", mech="permission", state="open", created=None, producer="S"):
    return {"item_id": item_id, "producer_id": producer, "answer_mechanism": mech,
            "state": state, "payload": payload, "created_at": iso(created or time.time())}


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_only_an_arm_answer_counts(self):
        cases = [
            ({"state": "open", "decision": "allow", "resolved_by": "s"}, False),
            ({"state": "answered", "decision": None, "resolved_by": "s"}, False),
            ({"state": "answered", "decision": "maybe", "resolved_by": "s"}, False),
            ({"state": "answered", "decision": "allow"}, False),
            ({"state": "answered", "decision": "deny", "resolved_by": ""}, False),
            ({"state": "answered", "decision": "allow", "resolved_by": "s"}, True),
            ({"state": "answered", "decision": "deny", "resolved_by": "s"}, True),
            (None, False),
        ]
        for item, want in cases:
            with self.subTest(item=item):
                self.assertEqual(self.m.verdict(item)[0], want)

    def test_emit_is_the_documented_shape(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.m.emit("allow")
        self.assertEqual(json.loads(out.getvalue()), {
            "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}})


class TransportTest(unittest.TestCase):
    """The transport is the caller's connection, not one built per request.

    Measured 2026-10-07: `urllib.request.AbstractHTTPHandler.do_open` hardcodes
    `headers["Connection"] = "close"`, so every 2 s pass opened a TCP connection and
    asked the server to tear it down — ~1-2 setups/s across two instances, and the
    board's idle CPU was `syscall`/`kevent`-dominated rather than handler-dominated.
    These pin the replacement, which the loop tests above cannot: they patch
    `get_json` itself and so never reach a transport.
    """

    def setUp(self):
        self.m = load()

    def _conn(self, status=200, body=b"{}"):
        seen = {}

        class FakeConn:
            def request(self, method, path, headers=None):
                seen["method"], seen["path"], seen["headers"] = method, path, headers

            def getresponse(self):
                return mock.Mock(status=status, read=lambda: body)

            def close(self):
                seen["closed"] = True

        return FakeConn(), seen

    def test_no_connection_close_is_sent(self):
        conn, seen = self._conn()
        self.m.get_json(conn, "http://localhost:18080/api/1.0/attention")
        self.assertEqual(seen["headers"], {"Accept": "application/json"})

    def test_the_same_connection_carries_every_request(self):
        conn, seen = self._conn()
        self.m.get_json(conn, "http://localhost:18080/api/1.0/attention")
        self.m.get_json(conn, "http://localhost:18080/api/1.0/attention/p1")
        self.assertEqual(seen["path"], "/api/1.0/attention/p1")
        # A good response must NOT tear the socket down, or the next pass pays a setup.
        self.assertNotIn("closed", seen)

    def test_a_transport_failure_drops_the_pooled_socket(self):
        conn, seen = self._conn()

        def boom(method, path, headers=None):
            raise OSError("socket dropped by the server")

        conn.request = boom
        ok, reason = self.m.get_json(conn, "http://localhost:18080/api/1.0/attention")
        self.assertFalse(ok)
        self.assertIn("OSError", reason)
        self.assertTrue(seen.get("closed"), "a dead pooled socket must not be reused")

    def test_a_non_200_is_reported_not_raised(self):
        conn, _ = self._conn(status=503, body=b"nope")
        ok, reason = self.m.get_json(conn, "http://localhost:18080/api/1.0/attention")
        self.assertFalse(ok)
        self.assertIn("503", reason)

    def test_store_connection_closes_on_every_path(self):
        with mock.patch.object(self.m.http.client, "HTTPConnection") as HC:
            with self.m.store_connection("http://localhost:18080"):
                pass
            HC.return_value.close.assert_called_once()

    def test_store_connection_closes_on_the_exception_path(self):
        # The name claims "every path"; the normal exit alone does not pin that.
        with mock.patch.object(self.m.http.client, "HTTPConnection") as HC:
            with self.assertRaises(RuntimeError):
                with self.m.store_connection("http://localhost:18080"):
                    raise RuntimeError("boom")
            HC.return_value.close.assert_called_once()

    def test_https_gets_an_https_connection(self):
        # `urlopen` honoured the scheme; a plain HTTPConnection would silently
        # downgrade an https store to plaintext on port 80.
        with mock.patch.object(self.m.http.client, "HTTPSConnection") as HS:
            with self.m.store_connection("https://store.example:8443"):
                pass
            HS.assert_called_once_with("store.example", 8443, timeout=self.m.HTTP_TIMEOUT)

    def test_http_defaults_to_port_80(self):
        with mock.patch.object(self.m.http.client, "HTTPConnection") as HC:
            with self.m.store_connection("http://store.example"):
                pass
            HC.assert_called_once_with("store.example", 80, timeout=self.m.HTTP_TIMEOUT)

    def test_an_unknown_scheme_is_refused_not_downgraded(self):
        with self.assertRaises(ValueError):
            with self.m.store_connection("ftp://store.example"):
                pass

    def test_a_malformed_body_does_not_drop_the_socket(self):
        # A payload problem on a healthy 200 leaves the connection reusable; only a
        # transport failure should pay the reconnect.
        conn, seen = self._conn(status=200, body=b"not json")
        ok, _ = self.m.get_json(conn, "http://localhost:18080/api/1.0/attention")
        self.assertFalse(ok)
        self.assertNotIn("closed", seen)

    def test_one_socket_carries_the_whole_poll(self):
        """The PR's central claim, driven against a real server.

        A hand-rolled FakeConn can only show the client does not *ask* for a close.
        This runs a real `HTTPConnection` against an in-process HTTP/1.1 server and
        asserts every request arrived from the same client port — which is what
        connection reuse actually means.
        """
        ports = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"   # 1.0 would close after every response

            def do_GET(self):
                ports.append(self.client_address[1])
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            store = f"http://127.0.0.1:{srv.server_address[1]}"
            with self.m.store_connection(store) as conn:
                for _ in range(3):
                    ok, _ = self.m.get_json(conn, f"{store}/api/1.0/attention")
                    self.assertTrue(ok)
        finally:
            srv.shutdown()
            srv.server_close()

        self.assertEqual(len(ports), 3)
        self.assertEqual(len(set(ports)), 1, "the whole poll must ride one client socket")


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def resolve(self, items, expect=None, not_before=None):
        self.m.get_json = lambda conn, url: (True, items)
        return self.m.resolve_item_for_session(None, "store", "S", expect, not_before)

    def test_message_card_is_never_a_permission_answer(self):
        self.assertFalse(self.resolve([card("m1", mech="message")])[0])

    def test_single_permission_card_matches(self):
        self.assertEqual(self.resolve([card("p1"), card("m1", mech="message")]), (True, "p1"))

    def test_two_permission_cards_are_ambiguous(self):
        self.assertFalse(self.resolve([card("p1"), card("p2")])[0])

    def test_stale_card_for_another_gate_is_refused(self):
        # Measured: an in-pane approval left its card `open` for 9+ minutes.
        stale = card("old", payload="Write: /tmp/z.txt")
        self.assertFalse(self.resolve([stale], expect="Write: /tmp/w.txt")[0])

    def test_card_older_than_the_hook_is_refused(self):
        now = time.time()
        old = card("old", payload="Write: /tmp/w.txt", created=now - 600)
        self.assertFalse(self.resolve([old], expect="Write: /tmp/w.txt", not_before=now)[0])
        # Control: the same card passes with an older floor, so the filter is not refuse-all.
        self.assertEqual(self.resolve([old], expect="Write: /tmp/w.txt", not_before=now - 3600), (True, "old"))

    def test_other_producers_are_ignored(self):
        self.assertFalse(self.resolve([card("p1", producer="OTHER")])[0])


class RunTest(unittest.TestCase):
    """Drive main() through its hook entry point (JSON on stdin)."""

    def setUp(self):
        self.m = load()
        self.dir = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"ATTENTION_STATE_DIR": self.dir}, clear=False)
        self.env.start()
        os.environ.pop("SUPERVISOR_PERMISSION_POLL", None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def feed(self, kind):
        with open(os.path.join(self.dir, "S.open.json"), "w") as fh:
            json.dump({"kind": kind}, fh)

    def run_main(self, argv, stdin="", store=None, sleep=None):
        self.m.get_json = store or (lambda conn, url: (True, []))
        if sleep:
            self.m.time.sleep = sleep
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["poll"] + argv), \
             mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
             redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main()
        return rc, out.getvalue(), err.getvalue()

    def test_kill_switch_exits_silently(self):
        os.environ["SUPERVISOR_PERMISSION_POLL"] = "off"
        rc, out, _ = self.run_main([], stdin='{"session_id":"S"}')
        self.assertEqual((rc, out), (0, ""))

    def test_headless_worker_is_left_to_its_own_park(self):
        # Blocking here would delay the server's canUseTool park by the whole deadline.
        self.feed("permission")
        with mock.patch.dict(os.environ, {"SUPERVISOR_WORKER_MODE": "headless"}):
            t0 = time.monotonic()
            rc, out, _ = self.run_main(["--session-id", "S", "--timeout", "30"])
        self.assertEqual((rc, out), (0, ""))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_interactive_worker_still_polls(self):
        self.feed("permission")
        with mock.patch.dict(os.environ, {"SUPERVISOR_WORKER_MODE": "interactive"}):
            rc, out, err = self.run_main(["--session-id", "S", "--timeout", "0.2", "--interval", "0.05"])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("deadline", err)

    def test_deadline_is_fail_open(self):
        self.feed("permission")
        rc, out, err = self.run_main(["--session-id", "S", "--timeout", "0.3", "--interval", "0.05"])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("deadline", err)

    def test_answered_card_is_delivered(self):
        self.feed("permission")
        hook = json.dumps({"session_id": "S", "tool_name": "Write", "tool_input": {"file_path": "/tmp/w.txt"}})
        live = card("p1", payload="Write: /tmp/w.txt", created=time.time() + 1)

        def store(conn, url):
            if url.endswith("/attention"):
                return True, [live]
            return True, {"state": "answered", "decision": "allow", "resolved_by": "manager"}

        rc, out, _ = self.run_main(["--timeout", "2", "--interval", "0.05"], stdin=hook, store=store)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["decision"], {"behavior": "allow"})

    def test_gate_not_yet_in_feed_is_waited_for(self):
        # attention-log.py writes the feed item in parallel; appearing late is normal.
        path = os.path.join(self.dir, "S.open.json")
        calls = {"n": 0}

        def sleep(_):
            calls["n"] += 1
            if calls["n"] == 3:
                self.feed("permission")
            if calls["n"] == 6:
                os.remove(path)

        rc, out, err = self.run_main(["--session-id", "S", "--timeout", "30", "--interval", "0"], sleep=sleep)
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("gate closed", err)

    def test_closed_card_ends_the_poll(self):
        self.feed("permission")
        rc, out, err = self.run_main(["--item-id", "p1", "--timeout", "30", "--interval", "0"],
                                     store=lambda conn, url: (True, {"state": "closed"}))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("'closed'", err)

    def test_store_error_is_never_a_decision(self):
        self.feed("permission")
        rc, out, _ = self.run_main(["--item-id", "p1", "--timeout", "0.2", "--interval", "0.05"],
                                   store=lambda conn, url: (False, "boom"))
        self.assertEqual((rc, out), (0, ""))


if __name__ == "__main__":
    unittest.main()
