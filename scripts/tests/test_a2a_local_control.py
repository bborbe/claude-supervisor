#!/usr/bin/env python3
"""`scripts/a2a-local-control.py` — the endpoint it calls, and the reply it reads.

The defect this file exists for, measured 2026-10-17 on the first live SC3 probe: the script's
default endpoint was the controller's ORIGIN (`http://localhost:8092`), not its JSON-RPC endpoint.
The controller mounts that handler at `pkg/a2a.go`'s `A2APath` (`/a2a`), so a POST to the origin
answered `404 page not found` and **every local row failed as shipped** — while the manual checks
that preceded the release (an override in both flag positions, an empty `--task` refused) all
passed, because each one supplied its own endpoint and so never exercised the default. The first
test below is the one that would have caught it.

⚠️ **Assert the URL that is actually sent, not just the constant.** A test that only pinned
`ENDPOINT_DEFAULT` would pass again the moment something downstream rewrote the URL, which is the
same class of failure one layer along.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "a2a-local-control.py")

_spec = importlib.util.spec_from_file_location("a2a_local_control", _SCRIPT)
ctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctl)

#: A fake token. Its only job is to be recognisable in stdout if redaction ever stops working —
#: no real credential belongs in a test, and a realistic-looking one would be worse than useless.
FAKE_TOKEN = "tok-NOT-A-REAL-SECRET-0000"

#: The controller's reply, in the shape `pkg/a2a-executor.go` actually produces: the
#: `ControlResponse` arrives as a **data part inside the task's `artifacts[]`**, reached through
#: `result.task` — the SendMessageResponse ONEOF — never at a flat `result.create.*`.
REPLY = {
    "jsonrpc": "2.0",
    "id": "1",
    "result": {
        "task": {
            "id": "task-1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {
                    "artifactId": "artifact-1",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "operation": "create",
                                "create": {
                                    "worker": {
                                        "worker_id": "27eebbea290ccc19",
                                        "task": "Open-A2A Local Backend Probe",
                                        "backend": "local",
                                        "state": "running",
                                        "pane_id": "81",
                                    },
                                    "token": FAKE_TOKEN,
                                },
                            },
                        }
                    ],
                }
            ],
        }
    },
}


class EndpointTest(unittest.TestCase):
    """Which URL the script calls — the defect's home."""

    def setUp(self):
        self._saved = os.environ.pop(ctl.ENDPOINT_ENV, None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(ctl.ENDPOINT_ENV, None)
        else:
            os.environ[ctl.ENDPOINT_ENV] = self._saved

    def test_default_endpoint_carries_the_a2a_path(self):
        """THE REGRESSION GUARD. The controller serves JSON-RPC at `/a2a`; the origin answers 404.

        A default without the path is not a cosmetic slip — it fails every local row, and it does
        so only on the default path, so a manual check that passes `--endpoint` cannot see it.
        """
        self.assertTrue(
            ctl.ENDPOINT_DEFAULT.endswith("/a2a"),
            f"ENDPOINT_DEFAULT must end in /a2a, got {ctl.ENDPOINT_DEFAULT!r} — the controller "
            f"mounts its handler at pkg/a2a.go's A2APath and a POST to the origin returns 404",
        )

    def test_the_default_is_what_gets_sent(self):
        """The constant reaches the wire. Pins the URL actually requested, not just the value."""
        endpoint = ctl.endpoint_for(None)
        call = ctl.send_request_for(endpoint, FAKE_TOKEN, ctl.control_request("list"))
        self.assertEqual(call["url"], ctl.ENDPOINT_DEFAULT)
        self.assertTrue(call["url"].endswith("/a2a"), call["url"])

    def test_env_wins_over_the_default(self):
        os.environ[ctl.ENDPOINT_ENV] = "http://example.test:9000/a2a"
        self.assertEqual(ctl.endpoint_for(None), "http://example.test:9000/a2a")

    def test_explicit_wins_over_env(self):
        os.environ[ctl.ENDPOINT_ENV] = "http://example.test:9000/a2a"
        self.assertEqual(ctl.endpoint_for("http://other.test/a2a"), "http://other.test/a2a")

    def test_a_trailing_slash_is_stripped_and_the_path_survives(self):
        """`.rstrip("/")` normalises the separator — it must not eat the path with it."""
        self.assertEqual(ctl.endpoint_for("http://example.test:9000/a2a/"), "http://example.test:9000/a2a")

    def test_a_caller_supplied_url_is_not_rewritten(self):
        """⚠️ The path is the CALLER's contract, so nothing appends one.

        Every sibling spells the endpoint in full (`a2a-spawn.mjs` documents
        `http://127.0.0.1:8080/a2a`; the k8s builder returns `"https://" + host + "/a2a"`), so an
        append here would turn a correct `…/a2a` into `…/a2a/a2a` — a new defect traded for the old.
        """
        given = "http://example.test:9000/a2a"
        self.assertEqual(ctl.endpoint_for(given), given)
        self.assertEqual(ctl.endpoint_for(given + "/"), given)


class ReplyTest(unittest.TestCase):
    """What the script reads out of the reply — and what it refuses to print."""

    def parse(self, payload):
        task, frames, error = ctl.control_reply(json.dumps(payload))
        self.assertIsNone(error, error)
        return ctl.control_response(task, frames, "create")

    def test_the_artifact_data_part_is_read(self):
        """The reply shape `pkg/a2a-executor.go` produces, end to end."""
        response, error, failed = self.parse(REPLY)
        self.assertIsNone(error, error)
        self.assertFalse(failed)
        worker = response["create"]["worker"]
        self.assertEqual(worker["worker_id"], "27eebbea290ccc19")
        self.assertEqual(worker["backend"], "local")

    def test_print_created_surfaces_the_worker_id_and_never_the_token(self):
        """The credential half. `CreateResponse.Token` is the worker's own bearer credential and
        the controller's own doc comment forbids logging, echoing or writing it — so a printer that
        echoed the reply verbatim would put it on stdout and into the caller's transcript."""
        response, _, _ = self.parse(REPLY)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            readable = ctl.print_created(response)
        printed = buffer.getvalue()
        self.assertTrue(readable)
        self.assertIn("worker_id: 27eebbea290ccc19", printed)
        self.assertNotIn(FAKE_TOKEN, printed)
        self.assertIn("<redacted>", printed)

    def test_print_created_reports_a_missing_worker_id_rather_than_guessing(self):
        """A create reply with no id is not a success. It must return False so the caller can exit
        non-zero, and it must still redact what it echoes to stderr."""
        response, _, _ = self.parse(
            {"result": {"task": {"id": "t", "artifacts": [
                {"parts": [{"kind": "data", "data": {"operation": "create", "create": {"worker": {}, "token": FAKE_TOKEN}}}]}
            ]}}}
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            readable = ctl.print_created(response)
        self.assertFalse(readable)

    def test_redact_is_recursive_and_keyed_on_the_name(self):
        """Depth and lists included — a token nested one level deeper is still a token."""
        self.assertEqual(
            ctl.redact({"a": {"token": FAKE_TOKEN}, "b": [{"secret": "s"}, {"password": "p"}]}),
            {"a": {"token": "<redacted>"}, "b": [{"secret": "<redacted>"}, {"password": "<redacted>"}]},
        )

    def test_redact_keeps_ordinary_fields(self):
        """`worker_id` is an identifier, not a credential — redacting it would break the caller."""
        self.assertEqual(ctl.redact({"worker_id": "w-1", "backend": "local"}),
                         {"worker_id": "w-1", "backend": "local"})

    def test_a_jsonrpc_error_is_a_refusal_not_a_failed_task(self):
        """A refusal never reached a backend, so it must not be reported as a controller failure."""
        task, frames, error = ctl.control_reply(json.dumps({"error": {"code": -32602, "message": "invalid params"}}))
        self.assertIsNotNone(error)
        self.assertIsNone(task)

    def test_a_reply_naming_another_operation_is_rejected(self):
        """The only client-side observable that the answer does not belong to the call that asked."""
        response, error, _ = self.parse(
            {"result": {"task": {"id": "t", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [
                {"parts": [{"kind": "data", "data": {"operation": "list", "list": {"workers": []}}}]}
            ]}}}
        )
        self.assertIsNotNone(error)



class CreateParityTest(unittest.TestCase):
    """`--vault` / `--first-step` reach the request; `bind` and `session_id` reach stdout."""

    def test_create_request_carries_vault_and_first_step(self):
        self.assertEqual(
            ctl.control_request("create", "T", "private-personal", "do X first"),
            {"operation": "create", "create": {"task": "T", "vault": "private-personal", "first_step": "do X first"}},
        )

    def test_create_request_omits_empty_options(self):
        self.assertEqual(ctl.control_request("create", "T"), {"operation": "create", "create": {"task": "T"}})
        self.assertEqual(ctl.control_request("create", "T", "", ""), {"operation": "create", "create": {"task": "T"}})

    def test_bind_line_covers_every_outcome(self):
        self.assertEqual(ctl.bind_line({"action": "stamped"}), "bind: stamped")
        self.assertEqual(ctl.bind_line({"action": "appended"}), "bind: appended")
        self.assertEqual(ctl.bind_line({"error": "vault-cli failed"}), "bind: error vault-cli failed")
        self.assertEqual(ctl.bind_line(None), "bind: none")

    def test_print_created_reports_bind_and_session_and_still_redacts(self):
        reply = json.loads(json.dumps(REPLY))
        data = reply["result"]["task"]["artifacts"][0]["parts"][0]["data"]
        data["create"]["worker"]["session_id"] = "11111111-2222-3333-4444-555555555555"
        data["create"]["bind"] = {"action": "stamped"}
        task, frames, _ = ctl.control_reply(json.dumps(reply))
        response, _, _ = ctl.control_response(task, frames, "create")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertTrue(ctl.print_created(response))
        printed = buffer.getvalue()
        self.assertIn("session_id: 11111111-2222-3333-4444-555555555555", printed)
        self.assertIn("bind: stamped", printed)
        self.assertNotIn(FAKE_TOKEN, printed)

    def test_print_created_without_bind_says_none(self):
        task, frames, _ = ctl.control_reply(json.dumps(REPLY))
        response, _, _ = ctl.control_response(task, frames, "create")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ctl.print_created(response)
        self.assertIn("bind: none", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
