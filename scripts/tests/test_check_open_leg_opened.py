"""check-open-leg-opened.py: an all-refused round fails loudly, a healthy one does not.

The criterion has two directions and the second is the one that catches an over-eager check:

- **fails** when every selected row was refused — round `1795012825`, 2026-11-18: 7 selected,
  7 `SESSION UNREADABLE` refusals, rendered as `opened 0` with no other signal.
- **passes** on a round that reached live sessions — including round `1795020429`, whose real
  shape is `opened 0 · resumed 3 · refused 1`. A check keyed on `opened` alone would fail that
  healthy round, so `resumed` and `jumped` count as reach.

⚠️ **The counts are the hand-back's, not the payload's.** Measured 2026-11-18: only 1 of 90 `.open`
payloads carries an `opened: …` line, so a payload-parsing check would answer "not a verdict" on
every real round — the defect a review caught in the first cut of this file.

`test_missing_count_is_not_a_pass` pins the third state: unreadable input is neither pass nor fail.
"""
import pathlib
import subprocess
import sys
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-open-leg-opened.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


class CheckOpenLegOpenedTest(unittest.TestCase):
    def test_all_refused_fails(self):
        result = run("--selected", "7", "--opened", "0", "--resumed", "0", "--jumped", "0",
                     "--refused", "7")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("EVERY selected row was refused", result.stderr)

    def test_one_opened_passes(self):
        result = run("--selected", "4", "--opened", "1", "--resumed", "2", "--jumped", "0",
                     "--refused", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_resumed_only_passes(self):
        # The real shape of round 1795020429: no new worker, but three rows re-entered a session.
        result = run("--selected", "4", "--opened", "0", "--resumed", "3", "--jumped", "0",
                     "--refused", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nothing_selected_passes(self):
        result = run("--selected", "0", "--opened", "0", "--resumed", "0", "--jumped", "0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_count_is_not_a_pass(self):
        result = run("--selected", "4", "--opened", "0")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("EVERY selected row was refused", result.stderr)

    def test_unparseable_count_is_not_a_pass(self):
        result = run("--selected", "four", "--opened", "0", "--resumed", "0", "--jumped", "0")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_negative_count_is_not_a_pass(self):
        result = run("--selected", "4", "--opened", "-1", "--resumed", "0", "--jumped", "0")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
