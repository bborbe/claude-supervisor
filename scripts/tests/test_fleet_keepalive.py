#!/usr/bin/env python3
"""Tests for scripts/fleet-keepalive.py — the fleet tier's missing keeper.

The defect this pins: `manager-liveness.py` reports `OK` for a fleet manager whose model has
not run in hours, because the heartbeat is written by the model-free gate loop. So every test
here reads the DIGEST ledger, and none of them would pass if the script probed liveness.
"""

import importlib.util
import io
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("fleet_keepalive", str(_HERE.parent / "fleet-keepalive.py"))
fk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fk)


class FleetKeepAliveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = pathlib.Path(self.tmp.name) / "ledger.txt"

    def write(self, *lines):
        self.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_check(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = fk.main(["--ledger", str(self.ledger)])
        return rc, out.getvalue().splitlines()

    def line(self, age, limit, digest="2026-10-30T11:38:21Z"):
        return (f"2026-10-30T14:08:50Z\t0\tSUPPRESSED digest={digest} age={age}s "
                f"limit={limit}s round=2026-10-30T11-32-00Z.digest quality=parsed")

    # --- the three verdicts ---------------------------------------------------

    def test_a_fresh_round_is_ok(self):
        self.write(self.line(300, 1080))
        rc, out = self.run_check()
        self.assertEqual(0, rc)
        self.assertEqual("ok", out[0])

    def test_a_lapsed_round_is_stale(self):
        """The measured 2026-10-30 outage: age 9030s against an 1080s limit."""
        self.write(self.line(9030, 1080))
        rc, out = self.run_check()
        self.assertEqual(10, rc)
        self.assertEqual("stale", out[0])

    def test_the_boundary_is_inclusive(self):
        """age == limit is still inside: a round that lands exactly on its limit is not late."""
        self.write(self.line(1080, 1080))
        rc, out = self.run_check()
        self.assertEqual(0, rc)
        self.assertEqual("ok", out[0])

    def test_an_absent_ledger_is_unknown_never_ok(self):
        rc, out = self.run_check()
        self.assertEqual(2, rc)
        self.assertEqual("unknown", out[0])

    def test_an_unparseable_line_is_unknown_never_ok(self):
        """A ledger that carries no age/limit must never read as 'fine' — the fail-closed
        direction matters here, because 'ok' is what stops a keeper from acting."""
        self.write("2026-10-30T14:08:50Z\t0\tOK\t-")
        rc, out = self.run_check()
        self.assertEqual(2, rc)
        self.assertEqual("unknown", out[0])

    # --- it reads the TAIL, which is the whole point --------------------------

    def test_the_last_line_wins_over_an_earlier_fresh_one(self):
        """The ledger is append-only, one line a minute. Reading any line but the last would
        report a round as fresh forever, because some earlier line always was."""
        self.write(self.line(60, 1080), self.line(9030, 1080))
        rc, out = self.run_check()
        self.assertEqual(10, rc)
        self.assertEqual("stale", out[0])

    def test_a_ledger_larger_than_the_tail_window_still_reads_the_last_line(self):
        """The ledger is append-only and grows one line a minute forever, so the read is
        bounded to a 256 KB tail. A stale line INSIDE the window must still win over the
        thousands of fresh-looking ones before it — a bounded read that fell back to the
        window's first line would report `ok` here."""
        self.write(*([self.line(60, 1080, digest="2026-10-30T01:00:00Z")] * 4000),
                   self.line(9030, 1080))
        self.assertGreater(self.ledger.stat().st_size, fk.TAIL_BYTES)
        rc, out = self.run_check()
        self.assertEqual(10, rc)
        self.assertEqual("stale", out[0])
        self.assertIn("2026-10-30T11:38:21Z", out[1])

    def test_the_stale_message_names_the_digest_and_forbids_a_liveness_probe(self):
        self.write(self.line(9030, 1080))
        rc, out = self.run_check()
        self.assertIn("2026-10-30T11:38:21Z", out[1])
        self.assertIn("heartbeat will read OK", out[1])


if __name__ == "__main__":
    unittest.main()
