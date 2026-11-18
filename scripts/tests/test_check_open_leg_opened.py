"""check-open-leg-opened.py: an all-refused round fails loudly, a healthy one does not.

The criterion has two directions and the second is the one that catches an over-eager check:

- **fails** when every selected row was refused — round `1795012825`, 2026-11-18: 7 selected,
  7 `SESSION UNREADABLE` refusals, rendered as `opened 0` with no other signal.
- **passes** on a round that reached live sessions — including round `1795020429`, whose real
  shape is `opened 0 · resumed 3 · refused 1`. A check keyed on `opened` alone would fail that
  healthy round, so `resumed` and `jumped` count as reach.

`test_mid_write_payload_is_not_a_verdict` pins the third state: a payload caught before its
trailing tally is neither pass nor fail. Existence is not completion — a manager read round 7
mid-write three times in one day and quoted `refused=0` from a half-written file.
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-open-leg-opened.py"

ALL_REFUSED = """open leg: selected 7 (workers 37/90)
  1. Reduce the bborbe Open PR Backlog Below 50 Again — flagged · priority 2
  2. Managers Are Opened Ungated, Dropped Without Reopening, and Reported OK While Dead — flagged · priority 2
  skipped: 180 — 13 blocked · 49 deferred
opened: 0 of 7 · resumed=0 · jumped=0
refused: ❓ SESSION UNREADABLE — Reduce the bborbe Open PR Backlog Below 50 Again · vault-cli exited 1
refused: ❓ SESSION UNREADABLE — Managers Are Opened Ungated · vault-cli exited 1
refused: ❓ SESSION UNREADABLE — The Fleet Round Runs Longer Than Its Own Interval · vault-cli exited 1
refused: ❓ SESSION UNREADABLE — Prove the Agent Chart's Keel Policy Value Survives a Prod Apply · vault-cli exited 1
refused: ❓ SESSION UNREADABLE — A Review Run That Dies Without Posting · vault-cli exited 1
refused: ❓ SESSION UNREADABLE — The Fleet Manager Runs All Day · vault-cli exited 1
refused: ❓ SESSION UNREADABLE — A Session Can Outlive Its Exit Observer · vault-cli exited 1
"""

ONE_OPENED = """open leg: selected 4 (workers 30/90)
  1. Reduce the bborbe Open PR Backlog Below 50 Again — flagged · priority 2
  skipped: 187 — 15 blocked · 50 deferred
opened: 1 of 4 · resumed=2 · jumped=0
refused: ⛔ NOT READY — SC1 Probe - Readiness Gate Fixture · held, not opened
"""

RESUMED_ONLY = """open leg: selected 4 (workers 30/90)
  1. Reduce the bborbe Open PR Backlog Below 50 Again — flagged · priority 2
  2. Managers Are Opened Ungated, Dropped Without Reopening, and Reported OK While Dead — flagged · priority 2
  skipped: 187 — 15 blocked · 50 deferred
opened: 0 of 4 · resumed=3 · jumped=0
refused: ⛔ NOT READY — SC1 Probe - Readiness Gate Fixture · # Success Criteria holds 1 checkbox · held, not opened
"""

MID_WRITE = """open leg: selected 4 (workers 30/90)
  1. Reduce the bborbe Open PR Backlog Below 50 Again — flagged · priority 2
  skipped: 187 — 15 blocked · 50 deferred"""


class CheckOpenLegOpenedTest(unittest.TestCase):
    def run_check(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "round.open"
            path.write_text(body, encoding="utf-8")
            return subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                  capture_output=True, text=True)

    def test_all_refused_fails(self):
        result = self.run_check(ALL_REFUSED)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("EVERY selected row was refused", result.stderr)

    def test_one_opened_passes(self):
        result = self.run_check(ONE_OPENED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_resumed_only_passes(self):
        # The real shape of round 1795020429: no new worker, but three rows re-entered a session.
        result = self.run_check(RESUMED_ONLY)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mid_write_payload_is_not_a_verdict(self):
        result = self.run_check(MID_WRITE)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("incomplete open-leg block", result.stderr)

    def test_unknown_block_is_not_a_pass(self):
        result = self.run_check("open leg: unknown\n")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_missing_payload_is_not_an_all_refused_verdict(self):
        # Exit 1 means "every selected row was refused"; an unreadable payload must never borrow
        # that verdict, so a missing file answers 2.
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(SCRIPT), str(pathlib.Path(tmp) / "nope.open")],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("EVERY selected row was refused", result.stderr)

    def test_older_payload_with_full_sibling_refusals_fails(self):
        # Payloads written before the tally existed: the refusals live in the truncation-overflow
        # sibling. A complete set there is the same all-refused condition.
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "round.open"
            path.write_text("open leg: selected 2 (workers 37/90)\n  skipped: 180\n", encoding="utf-8")
            pathlib.Path(str(path) + "-refusals").write_text(
                "refused: ❓ SESSION UNREADABLE — one\nrefused: ❓ SESSION UNREADABLE — two\n",
                encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("EVERY selected row was refused", result.stderr)

    def test_partial_sibling_refusals_is_not_a_verdict(self):
        # The real 2026-11-18 14:40 shape: 7 selected, only 4 refusals persisted (the payload is
        # the truncation overflow). Not enough on disk to conclude — exit 2, never a pass.
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "round.open"
            path.write_text("open leg: selected 7 (workers 37/90)\n  skipped: 180\n", encoding="utf-8")
            pathlib.Path(str(path) + "-refusals").write_text(
                "refused: ❓ SESSION UNREADABLE — one\nrefused: ❓ SESSION UNREADABLE — two\n"
                "refused: ❓ SESSION UNREADABLE — three\nrefused: ❓ SESSION UNREADABLE — four\n",
                encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
