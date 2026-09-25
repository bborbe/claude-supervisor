"""check-spawn-ledger.py: a recent new-worker spawn must record a decided mode.

The defect this guards against, measured 2026-09-25: `agent_163` and `agent_164` were
opened directly by `agents/manager-drive.md` and both reported `mode_source=config` —
the value a site produces when it never passed the argument. `scripts/check-spawn-mode.py`
stayed green throughout, because it reads what a file *says* and this defect was a
runtime call. This check reads the record the spawn actually produced.

Two cases are load-bearing, and each is the one way this check could be plausibly wrong:

- `test_resume_row_is_not_an_offence` — a resume answers to the auto-resume gate, not to
  the mode decision, so counting it would fail every fleet that resumed a worker.
- `test_out_of_window_config_row_is_not_an_offence` — the wiring landed incrementally, so
  the ledger's history is mostly `config` rows. Run without the window on 2026-09-25 it
  reported **241** offenders and could never pass; the window is what makes the verdict
  describe current behaviour instead of the ledger's past.
"""
import datetime
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-spawn-ledger.py"

#: A fixed "now" so the window assertions never depend on the wall clock.
NOW = "2026-09-25T12:00:00Z"


def at(hours_ago):
    return (
        datetime.datetime.fromisoformat(NOW.replace("Z", "+00:00"))
        - datetime.timedelta(hours=hours_ago)
    ).isoformat()


def record(dir, name, **fields):
    path = pathlib.Path(dir) / f"{name}.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def run(dir):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(dir), "--now", NOW],
        capture_output=True,
        text=True,
    )


class CheckSpawnLedgerTest(unittest.TestCase):
    def test_decided_new_worker_passes(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="interactive", mode_source="argument",
                   resumed_from=None, spawned_at=at(1))
            result = run(d)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_config_sourced_new_worker_fails(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="interactive", mode_source="config",
                   resumed_from=None, spawned_at=at(1))
            result = run(d)
        self.assertEqual(result.returncode, 1)
        self.assertIn("a.json", result.stdout)

    def test_resume_row_is_not_an_offence(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="headless", mode_source="config",
                   resumed_from="deadbeef", spawned_at=at(1))
            result = run(d)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_out_of_window_config_row_is_not_an_offence(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="interactive", mode_source="config",
                   resumed_from=None, spawned_at=at(72))
            result = run(d)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_mode_source_is_not_an_offence(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="interactive", resumed_from=None, spawned_at=at(1))
            result = run(d)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_undated_record_is_not_an_offence(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, "a", mode="interactive", mode_source="config", resumed_from=None)
            result = run(d)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no parseable spawned_at", result.stdout)

    def test_absent_ledger_is_not_a_failure(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--ledger", "/nonexistent/ledger"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
