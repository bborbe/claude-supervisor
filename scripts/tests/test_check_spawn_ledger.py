"""check-spawn-ledger.py: a new-worker spawn must record a decided mode.

The defect this guards against, measured 2026-09-25: `agent_163` and `agent_164` were
opened directly by `agents/manager-drive.md` and both reported `mode_source=config` —
the value a site produces when it never passed the argument. `scripts/check-spawn-mode.py`
stayed green throughout, because it reads what a file *says* and this defect was a
runtime call. This check reads the record the spawn actually produced.

The load-bearing case is `test_resume_row_is_not_an_offence`. A resume answers to the
auto-resume gate, not to the mode decision, and counting it here would fail every fleet
that resumed a worker. That is the one way this check could be plausibly wrong.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-spawn-ledger.py"


def record(dir, name, **fields):
    path = pathlib.Path(dir) / f"{name}.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def run(dir):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(dir)],
        capture_output=True,
        text=True,
    )


class CheckSpawnLedgerTest(unittest.TestCase):
    def test_decided_new_worker_passes(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="interactive", mode_source="argument", resumed_from=None)
            result = run(d)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_config_sourced_new_worker_fails(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="interactive", mode_source="config", resumed_from=None)
            result = run(d)
        self.assertEqual(result.returncode, 1)
        self.assertIn("a.json", result.stdout)

    def test_resume_row_is_not_an_offence(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="headless", mode_source="config", resumed_from="deadbeef")
            result = run(d)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_mode_source_is_not_an_offence(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="interactive", resumed_from=None)
            result = run(d)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_absent_ledger_is_not_a_failure(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--ledger", "/nonexistent/ledger"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
