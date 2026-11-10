"""spawn-ledger.py's `--json` CLI — the surface that stops the silent no-op.

Before the CLI existed, `python3 spawn-ledger.py` printed nothing and exited 0, because the file
is a library module with only `read_ledger()`. A caller reading that empty stdout saw "no rows",
which is the exact silent-no-op class `agents/fleet-drive.md` step 1a exists to remove — and the
first attempt at fixing step 1a was written against that broken invocation.

The contract these cases pin:
  * a readable ledger prints one JSON object (session_id -> row) and exits 0;
  * an unreadable ledger exits **2** and says so — `None` must never render as `{}`, because an
    empty store and an unreachable one are different answers (`read_ledger`'s own docstring);
  * any argument other than `--json` exits 2 with the usage line.
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "spawn-ledger.py"


def load_module():
    spec = importlib.util.spec_from_file_location("spawn_ledger_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SpawnLedgerCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def run_main(self, argv, ledger):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.module, "read_ledger", return_value=ledger):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.module._main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_readable_ledger_prints_one_json_object(self) -> None:
        rows = {
            "aaaaaaaa-1111-2222-3333-444444444444": {
                "session_id": "aaaaaaaa-1111-2222-3333-444444444444",
                "mode": "headless",
                "spawned_at": "2026-11-10T10:00:00Z",
                "ended_at": "2026-11-10T10:30:00Z",
                "status": "done",
            }
        }
        code, out, err = self.run_main(["--json"], rows)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), rows)

    def test_no_argument_still_prints_the_ledger(self) -> None:
        code, out, _ = self.run_main([], {"x": {"session_id": "x"}})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"x": {"session_id": "x"}})

    def test_unreadable_ledger_exits_two_and_never_prints_an_empty_object(self) -> None:
        code, out, err = self.run_main(["--json"], None)
        self.assertEqual(code, 2)
        self.assertEqual(out, "", "an unreadable ledger must not render as an empty object")
        self.assertIn("spawn-ledger: unreadable", err)

    def test_unexpected_argument_exits_two_with_usage(self) -> None:
        code, out, err = self.run_main(["--rows"], {"x": {"session_id": "x"}})
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("usage: spawn-ledger.py --json", err)

    def test_trailing_argument_is_refused_not_ignored(self) -> None:
        """A silently-ignored extra arg hides a caller that thinks it asked for something else."""
        code, out, err = self.run_main(["--json", "--mode", "headless"], {"x": {"session_id": "x"}})
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("usage: spawn-ledger.py --json", err)


if __name__ == "__main__":
    unittest.main()
