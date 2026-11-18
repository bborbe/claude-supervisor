#!/usr/bin/env python3
"""Tests for scripts/deliver-message.py — the session-id-addressed delivery path.

Two defects shipped silently from v0.335.0 and both lived in the MCP call this suite pins:

  * the tool name was `send_message`, which no daemon serves (`unknown tool`);
  * the arguments were assumed, not read — `agent_id` is a roster handle that equals the
    session uuid on only some rows, and the body key is `text`, which the live
    `tools/list` inputSchema marks required.

So the properties under test are the ones that would have caught them: the handle is
resolved from the roster rather than assumed, the tool name and argument keys are pinned
against a recorder, and an unresolvable handle refuses instead of calling with a value that
reaches nobody.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = "11111111-2222-3333-4444-555555555555"
# The foreign/rehydrated handle shape measured on 16 of 74 live roster rows (2026-11-18).
FOREIGN_HANDLE = SESSION + "~11111111"


def load():
    spec = importlib.util.spec_from_file_location(
        "deliver_message", os.path.join(os.path.dirname(_HERE), "deliver-message.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def roster_payload(rows):
    """The MCP result shape `list_agents` answers: one content block holding JSON."""
    return {"content": [{"type": "text", "text": json.dumps(rows)}]}


class ResolveAgentHandleTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()

    def test_foreign_handle_is_returned_verbatim(self):
        """A `<uuid>~<sid8>` row must yield that handle, never the bare uuid."""
        rows = [{"agent_id": FOREIGN_HANDLE, "session_id": SESSION}]
        with mock.patch.object(self.mod, "mcp_call",
                               return_value=(roster_payload(rows), None, False)) as call:
            handle, why = self.mod.resolve_agent_handle(SESSION)
        self.assertEqual(handle, FOREIGN_HANDLE)
        self.assertIsNone(why)
        self.assertEqual(call.call_args[0][0], "list_agents")

    def test_identical_handle_and_session_id_are_returned(self):
        """The daemon-created shape, where the two values coincide."""
        rows = [{"agent_id": SESSION, "session_id": SESSION}]
        with mock.patch.object(self.mod, "mcp_call",
                               return_value=(roster_payload(rows), None, False)):
            handle, why = self.mod.resolve_agent_handle(SESSION)
        self.assertEqual(handle, SESSION)
        self.assertIsNone(why)

    def test_absent_row_refuses_and_names_the_id(self):
        """No row means no delivery — the call must not be made with the raw uuid."""
        with mock.patch.object(self.mod, "mcp_call",
                               return_value=(roster_payload([]), None, False)):
            handle, why = self.mod.resolve_agent_handle(SESSION)
        self.assertIsNone(handle)
        self.assertIn(SESSION, why)

    def test_transport_failure_refuses(self):
        with mock.patch.object(self.mod, "mcp_call", return_value=(None, None, True)):
            handle, why = self.mod.resolve_agent_handle(SESSION)
        self.assertIsNone(handle)
        self.assertTrue(why)

    def test_unreadable_roster_refuses(self):
        broken = {"content": [{"type": "text", "text": "not json"}]}
        with mock.patch.object(self.mod, "mcp_call", return_value=(broken, None, False)):
            handle, why = self.mod.resolve_agent_handle(SESSION)
        self.assertIsNone(handle)
        self.assertTrue(why)


class McpDeliveryVerdictTest(unittest.TestCase):
    """A non-error hand-back is not a delivery — the shape that printed `delivered: true`
    for a message that reached nobody."""

    def setUp(self):
        self.mod = load()

    def _verdict(self, payload):
        text = json.dumps(payload) if not isinstance(payload, str) else payload
        return self.mod._mcp_delivery_verdict({"content": [{"type": "text", "text": text}]})

    def test_explicit_sent_true_is_delivered(self):
        self.assertEqual(self._verdict({"sent": True}), "delivered")

    def test_explicit_delivered_true_is_delivered(self):
        self.assertEqual(self._verdict({"delivered": True}), "delivered")

    def test_sent_false_is_a_refusal_not_a_delivery(self):
        """The headless hand-back: `sent: false` plus the registry name."""
        self.assertEqual(self._verdict({"sent": False, "name": "agent-x"}), "refused")

    def test_delivered_false_wins_over_sent_true(self):
        """The pane hand-back: `sent: true` beside `delivered: false` is a stranding."""
        self.assertEqual(self._verdict({"sent": True, "delivered": False}), "refused")

    def test_delivered_true_wins_over_sent_false(self):
        self.assertEqual(self._verdict({"sent": False, "delivered": True}), "delivered")

    def test_unverified_string_beside_sent_true_is_not_a_delivery(self):
        """`delivered: 'unverified'` is the wrapper's documented unconfirmed-submit state.

        It must not fall through to `sent: true` — reading it as delivered is the false
        success this function exists to remove, pointing the wrong way.
        """
        self.assertEqual(
            self._verdict({"sent": True, "delivered": "unverified"}), "unknown"
        )

    def test_delivered_string_true_is_delivered(self):
        self.assertEqual(self._verdict({"delivered": "true"}), "delivered")

    def test_delivered_string_refused_is_a_refusal(self):
        self.assertEqual(self._verdict({"delivered": "refused"}), "refused")

    def test_hand_back_without_a_signal_is_unknown(self):
        self.assertEqual(self._verdict({"name": "agent-x"}), "unknown")

    def test_unparseable_hand_back_is_unknown(self):
        self.assertEqual(self._verdict("not json"), "unknown")


class DeliveryCallShapeTest(unittest.TestCase):
    """The exact JSON-RPC arguments, pinned — this is the shape both shipped defects sat in."""

    def setUp(self):
        self.mod = load()
        self.calls = []

    def _record(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "list_agents":
            rows = [{"agent_id": FOREIGN_HANDLE, "session_id": SESSION}]
            return roster_payload(rows), None, False
        return {"content": [{"type": "text", "text": json.dumps({"sent": True})}]}, None, False

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.mod, "mcp_call", side_effect=self._record), \
                mock.patch.object(self.mod, "post",
                                  return_value=(202, {"status": "sent"}, False)), \
                mock.patch.object(self.mod, "liveness", return_value=("LIVE", "stub")), \
                mock.patch.object(self.mod, "roster_row",
                                  return_value=({"controls": ["message"]}, None)), \
                mock.patch.object(sys, "argv", ["deliver-message.py"] + argv), \
                redirect_stdout(out), redirect_stderr(err):
            code = self.mod.main()
        return code, out.getvalue(), err.getvalue()

    def test_delivers_over_send_agent_message_with_agent_id_and_text(self):
        code, out, _ = self._run(["--session-id", SESSION, "--text", "hello"])
        self.assertEqual(code, 0, out)
        names = [c[0] for c in self.calls]
        self.assertIn("send_agent_message", names)
        self.assertNotIn("send_message", names)
        args = [c[1] for c in self.calls if c[0] == "send_agent_message"][0]
        self.assertEqual(args["agent_id"], FOREIGN_HANDLE)
        self.assertEqual(args["text"], "hello")
        self.assertNotIn("message", args)

    def test_headless_hand_back_reports_not_delivered(self):
        """`sent: false` from the tool must exit non-zero and print `delivered: false`."""

        def record(name, arguments):
            if name == "list_agents":
                rows = [{"agent_id": FOREIGN_HANDLE, "session_id": SESSION}]
                return roster_payload(rows), None, False
            return {"content": [{"type": "text",
                                 "text": json.dumps({"sent": False, "name": "agent-x"})}]}, None, False

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.mod, "mcp_call", side_effect=record), \
                mock.patch.object(self.mod, "liveness", return_value=("LIVE", "stub")), \
                mock.patch.object(self.mod, "roster_row",
                                  return_value=({"controls": ["message"]}, None)), \
                mock.patch.object(sys, "argv",
                                  ["deliver-message.py", "--session-id", SESSION,
                                   "--text", "hi"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = self.mod.main()
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn('"delivered": false', out.getvalue())

    def test_nudge_keeps_the_rest_route_and_calls_no_tool(self):
        code, out, _ = self._run(["--session-id", SESSION, "--text", "hello", "--nudge"])
        self.assertEqual(code, 0, out)
        self.assertEqual([c[0] for c in self.calls], [])


if __name__ == "__main__":
    unittest.main()
