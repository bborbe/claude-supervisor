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

⚠️ **The rows come from a fixture ENDPOINT now, not a directory of files.** This check read
`~/.local/state/claude-supervisor/sessions/` until 2026-10-14, and a case that still planted
`<name>.json` would check an empty store — on which **every** case below reports OK,
including `test_config_sourced_new_worker_fails`, the one that must FAIL. The fixture is
load-bearing here rather than decoration, and that case is the evidence: it failed before
these fixtures were added.
"""
import datetime
import pathlib
import subprocess
import sys
import unittest

import endpoint_fixture  # noqa: E402
from endpoint_fixture import FixtureEndpoint, dead_url, ledger_row  # noqa: E402

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-spawn-ledger.py"

#: A fixed "now" so the window assertions never depend on the wall clock.
NOW = "2026-09-25T12:00:00Z"

#: Distinctive, because one case asserts the offending row is NAMED on stdout and the store
#: keys a row by its own `session_id` — a one-character name would match almost any line.
SID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def at(hours_ago):
    return (
        datetime.datetime.fromisoformat(NOW.replace("Z", "+00:00"))
        - datetime.timedelta(hours=hours_ago)
    ).isoformat()


def row(session_id, **fields):
    """One ledger row, keyed by its own `session_id` — the only sound join key."""
    return ledger_row(session_id, **fields)


def run(endpoint):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--endpoint", endpoint, "--now", NOW],
        capture_output=True,
        text=True,
    )


class CheckSpawnLedgerTest(unittest.TestCase):
    def test_decided_new_worker_passes(self):
        with FixtureEndpoint(ledger_rows=[row(SID, mode="interactive", mode_source="argument",
                                              resumed_from=None, spawned_at=at(1))]) as fx:
            result = run(fx.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_config_sourced_new_worker_fails(self):
        with FixtureEndpoint(ledger_rows=[row(SID, mode="interactive", mode_source="config",
                                              resumed_from=None, spawned_at=at(1))]) as fx:
            result = run(fx.url)
        self.assertEqual(result.returncode, 1)
        self.assertIn(SID, result.stdout)

    def test_resume_row_is_not_an_offence(self):
        with FixtureEndpoint(ledger_rows=[row(SID, mode="headless", mode_source="config",
                                              resumed_from="deadbeef", spawned_at=at(1))]) as fx:
            result = run(fx.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_out_of_window_config_row_is_not_an_offence(self):
        with FixtureEndpoint(ledger_rows=[row(SID, mode="interactive", mode_source="config",
                                              resumed_from=None, spawned_at=at(72))]) as fx:
            result = run(fx.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_mode_source_is_not_an_offence(self):
        with FixtureEndpoint(ledger_rows=[row(SID, mode="interactive",
                                              resumed_from=None, spawned_at=at(1))]) as fx:
            result = run(fx.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_undated_record_is_not_an_offence(self):
        with FixtureEndpoint(ledger_rows=[row(SID, mode="interactive", mode_source="config",
                                              resumed_from=None, spawned_at=None)]) as fx:
            result = run(fx.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no parseable spawned_at", result.stdout)

    def test_a_non_object_row_does_not_abort_the_check(self):
        """The store can serve a row that is not an object; it must be skipped, not raised on."""
        with FixtureEndpoint(ledger_rows=[None, row(SID, mode="interactive",
                                                     mode_source="argument",
                                                     resumed_from=None, spawned_at=at(1))]) as fx:
            result = run(fx.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_unreadable_store_is_not_a_failure(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--endpoint", dead_url()],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
