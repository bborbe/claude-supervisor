#!/usr/bin/env python3
"""Tests for scripts/answer-reach.py — the wrapper-leg reach recorder.

The load-bearing property is that it writes ONLY on a status that means "arrived and was
refused there" (409 no open question / already answered, 404 unknown session). The origin case
this task exists for is a manager whose answer command was denied by the permission
classifier: the command never reaches the wrapper, so there is no status at all, and a
recorder that wrote unconditionally would certify exactly the spawner the probe must refuse.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "answer-reach.py")


def load(reach_dir):
    """Import the hyphenated module with its reach dir pointed at `reach_dir`.

    ⚠️ The directory is resolved from the environment AT IMPORT, so the established override is
    set (and any ambient one cleared) before the module is executed — otherwise a test would
    write into the operator's real reach store.
    """
    os.environ.pop("SUPERVISOR_ANSWER_REACH_DIR", None)
    os.environ["SUPERVISOR_ANSWER_REACH_DIR"] = reach_dir
    spec = importlib.util.spec_from_file_location("answer_reach", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AnswerReachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reach = os.path.join(self.tmp, "reach")
        self.mod = load(self.reach)

    def written(self, session_id="sid", leg="wrapper"):
        path = os.path.join(self.reach, f"{session_id}.{leg}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    # --- what it accepts -------------------------------------------------------

    def test_409_writes_a_reach_with_the_documented_schema(self):
        ok, detail = self.mod.record("sid", "wrapper", "409")
        self.assertTrue(ok, detail)
        record = self.written()
        self.assertEqual(record["session_id"], "sid")
        self.assertEqual(record["leg"], "wrapper")
        self.assertEqual(record["outcome"], "http-409")
        self.assertIsInstance(record["ts"], str)

    def test_404_writes_a_reach(self):
        ok, _ = self.mod.record("sid", "wrapper", "404")
        self.assertTrue(ok)
        self.assertEqual(self.written()["outcome"], "http-404")

    def test_an_integer_status_is_accepted_as_well_as_a_string(self):
        ok, _ = self.mod.record("sid", "wrapper", 409)
        self.assertTrue(ok)

    def test_the_file_is_named_per_session_and_leg_so_the_two_writers_cannot_erase_each_other(self):
        self.mod.record("sid", "wrapper", "409")
        # A second session's record must not touch the first.
        self.mod.record("other", "wrapper", "409")
        self.assertIsNotNone(self.written("sid"))
        self.assertIsNotNone(self.written("other"))

    # --- what it refuses, and the origin case in particular --------------------

    def test_200_refuses_and_writes_nothing(self):
        ok, detail = self.mod.record("sid", "wrapper", "200")
        self.assertFalse(ok)
        self.assertIn("404/409", detail)
        self.assertIsNone(self.written())

    def test_5xx_refuses_and_writes_nothing(self):
        ok, _ = self.mod.record("sid", "wrapper", "503")
        self.assertFalse(ok)
        self.assertIsNone(self.written())

    def test_a_missing_status_refuses(self):
        # The origin case: a command the classifier denied produces no status at all, so the
        # shell substitution is empty and the recorder must refuse rather than default.
        ok, detail = self.mod.record("sid", "wrapper", "")
        self.assertFalse(ok)
        self.assertIn("not an integer", detail)
        self.assertIsNone(self.written())

    def test_an_empty_session_id_refuses(self):
        ok, detail = self.mod.record("", "wrapper", "409")
        self.assertFalse(ok)
        self.assertIn("no session id", detail)
        self.assertEqual(os.listdir(self.reach) if os.path.isdir(self.reach) else [], [])

    def test_a_session_id_that_is_not_a_bare_filename_refuses(self):
        for bad in ("..", ".", "a/b", "a\\b"):
            ok, detail = self.mod.record(bad, "wrapper", "409")
            self.assertFalse(ok, f"{bad!r} was accepted")
            self.assertIn("not a bare filename", detail)

    # --- the CLI ---------------------------------------------------------------

    def run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.mod.main()
        return code, buf.getvalue()

    def test_cli_accepts_the_wrapper_leg_and_exits_zero(self):
        import sys

        saved = sys.argv
        sys.argv = ["answer-reach.py", "--session-id", "sid", "--status", "409", "--json"]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.mod.main()
        finally:
            sys.argv = saved
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buf.getvalue())["ok"])

    def test_cli_refuses_the_mcp_leg_because_the_server_owns_it(self):
        import sys

        saved = sys.argv
        sys.argv = ["answer-reach.py", "--session-id", "sid", "--leg", "mcp", "--status", "409", "--json"]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.mod.main()
        finally:
            sys.argv = saved
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("not this script's to write", payload["detail"])
        self.assertIsNone(self.written())

    def test_cli_refusal_without_json_is_plain_text(self):
        import sys

        saved = sys.argv
        sys.argv = ["answer-reach.py", "--session-id", "sid", "--status", "200"]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.mod.main()
        finally:
            sys.argv = saved
        self.assertEqual(code, 1)
        self.assertTrue(buf.getvalue().startswith("REFUSED\t"))


if __name__ == "__main__":
    unittest.main()
