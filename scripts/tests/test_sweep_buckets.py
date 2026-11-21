"""sweep-buckets.py: the round's page-bucket read, pinned.

The defect this guards against is measured. The wrapper serves
`GET /api/1.0/sweep?subject=<name>` — the gate's rows already classified — and nothing in
`claude-supervisor` called it, so the fleet round kept re-deriving the same classification
from the same inputs. Two derivations of one fact can disagree, and a round that disagrees
with the gate it is supposed to consume is the whole reason the route exists.

The load-bearing cases are `test_unknown_bucket_is_preserved_verbatim` (an output that folds
an unrecognised value into `other` looks identical to a round that re-derived, which is the
one thing this output must be able to disprove) and `test_partial_failure_is_reported_not_swallowed`
(a subject that failed must appear in `failed=`, because a partial read that reads as a
complete one is a false-quiet — the same class of defect reached from the other side).

Every case runs the real script against a real HTTP server on an ephemeral port, so the
`urllib` path, the JSON parse and the exit codes are all exercised rather than mocked.
"""
import http.server
import json
import pathlib
import socketserver
import subprocess
import sys
import threading
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "sweep-buckets.py"


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers from `responses` keyed on the `subject` query parameter."""

    responses = {}
    seen = []

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        from urllib.parse import urlparse, parse_qs

        query = parse_qs(urlparse(self.path).query)
        subject = (query.get("subject") or [""])[0]
        type(self).seen.append(subject)
        status, body = type(self).responses.get(subject, (404, {"error": "no fixture"}))
        payload = body if isinstance(body, str) else json.dumps(body)
        raw = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # keep the test output clean
        pass


class _Server:
    """A one-thread HTTP server on an ephemeral port, torn down by the context manager."""

    def __init__(self, responses):
        self.responses = responses

    def __enter__(self):
        handler = type("BoundHandler", (_Handler,), {"responses": self.responses, "seen": []})
        self._httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = "http://127.0.0.1:{}".format(self._httpd.server_address[1])
        self.seen = handler.seen
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def run(args):
    """Run the script and return `(exit_code, stdout, stderr)`."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def rows(*buckets):
    """A wrapper-shaped row list carrying one row per bucket."""
    return [{"name": "t{}".format(i), "session": "", "bucket": b, "label": "", "status": "", "phase": ""}
            for i, b in enumerate(buckets)]


class TestSweepBuckets(unittest.TestCase):
    def test_subjects_merge_into_one_pages_line(self):
        with _Server({"Deploy": (200, rows("waiting", "done")),
                      "Fleet": (200, rows("done", "hold"))}) as srv:
            code, out, err = run(["--subject", "Deploy", "--subject", "Fleet",
                                  "--base-url", srv.base_url])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "pages=done=2 hold=1 waiting=1")

    def test_unknown_bucket_is_preserved_verbatim(self):
        # A value the script has never seen must survive the count. Folding it into
        # `other` would make this output indistinguishable from a local re-derivation.
        with _Server({"S": (200, rows("probe-a"))}) as srv:
            code, out, _ = run(["--subject", "S", "--base-url", srv.base_url])
        self.assertEqual(code, 0)
        self.assertIn("probe-a=1", out)

    def test_partial_failure_is_reported_not_swallowed(self):
        with _Server({"Good": (200, rows("done")), "Dead": (503, {"error": "no cached rows"})}) as srv:
            code, out, _ = run(["--subject", "Good", "--subject", "Dead",
                                "--base-url", srv.base_url])
        self.assertEqual(code, 0, "one answered subject is a usable read")
        self.assertIn("pages=done=1", out)
        self.assertIn("failed=Dead", out)
        self.assertIn("failed=1", out)

    def test_every_subject_failing_exits_one(self):
        with _Server({}) as srv:
            code, out, _ = run(["--subject", "A", "--subject", "B", "--base-url", srv.base_url])
        self.assertEqual(code, 1)
        self.assertIn("failed=2", out)

    def test_row_without_a_bucket_fails_that_subject(self):
        broken = [{"name": "t", "session": "", "label": "", "status": "", "phase": ""}]
        with _Server({"S": (200, broken)}) as srv:
            code, out, _ = run(["--subject", "S", "--base-url", srv.base_url])
        self.assertEqual(code, 1)
        self.assertIn("failed=S", out)
        self.assertIn("without a bucket", out)

    def test_no_subject_is_a_usage_error(self):
        code, _, err = run([])
        self.assertEqual(code, 2)
        self.assertIn("no-subject", err)

    def test_unsafe_subject_is_refused_locally(self):
        code, _, err = run(["--subject", "../etc", "--base-url", "http://127.0.0.1:1"])
        self.assertEqual(code, 2)
        self.assertIn("bad-subject", err)

    def test_json_shape_carries_pages_and_failures(self):
        with _Server({"S": (200, rows("probe-b"))}) as srv:
            code, out, _ = run(["--subject", "S", "--base-url", srv.base_url, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["pages"], {"probe-b": 1})
        self.assertEqual(payload["failed"], {})

    def test_base_url_env_var_is_honoured(self):
        # The convention the sibling scripts already follow; a probe relies on it to point
        # at a second daemon without editing the caller.
        with _Server({"S": (200, rows("hold"))}) as srv:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--subject", "S"],
                capture_output=True, text=True, timeout=30,
                env={"SUPERVISOR_WRAPPER_URL": srv.base_url, "PATH": "/usr/bin:/bin"},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hold=1", proc.stdout)


if __name__ == "__main__":
    unittest.main()
