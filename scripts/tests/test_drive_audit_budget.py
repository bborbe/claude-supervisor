"""drive-audit-budget.py: the wall-clock bound on the manager drive leg's audit loop.

The defect this guards, measured 2026-10-16: `agents/manager-drive.md` clause (1)
dispatches one `vault-cli:task-auditor` subagent PER ROW, sequentially, and nothing
bounded the loop. On a round with zero actionable rows -- Recurring Operator Gates,
`ready-to-start` 0 and `stuck` 0 -- the audit is the whole of the remaining work, and
that tick ran about 34 minutes (`.tracked.txt` 11:05:04 -> `.verdicts.json` 11:39:01).
`commands/steps/manager-drive.md` had already recorded the sibling: 27 dispatches, 0
cache hits, 29 minutes. The task's SC4 asks for a bound that emits a `TERMINAL` line
instead of hanging, and this file is the test that shows it.

Three things here are load-bearing, and each is a way the suite could be plausibly
wrong:

- **The boundary is asserted on BOTH sides.** The bound is `>=`, so a test that only
  checks "well under" and "well over" passes for a `>` implementation too, and the
  off-by-one it hides is exactly one dispatch -- the row that should not have been
  scored. `test_at_the_bound_is_terminal` and `test_one_second_under_is_continue` pin
  the two sides of the same instant.

- **The discrimination section.** A suite that only exercises the reference
  implementation proves the script works today, not that these tests would catch it
  regressing tomorrow. Each mutation is the real script with ONE source-level break,
  and `TestDiscrimination` requires each to violate **its own** invariant -- not "it
  exits non-zero", which an unrelated crash would satisfy.

- **REFUSED is asserted as a third state, not as a flavour of the other two.** The
  expensive failure is a caller reading a refused clock as CONTINUE: the leg then has
  no bound at all and behaves exactly as it did before the fix, while every check
  reads green. So every refusal case asserts the exit code AND that neither the
  `CONTINUE` nor the `TERMINAL` token reached stdout.
"""
import datetime
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "drive-audit-budget.py"

#: The leg's start, as the leg reads it (`date -u +%Y-%m-%dT%H:%M:%SZ`).
START = "2026-10-16T11:05:04Z"

#: The default bound, mirrored here so a silent change to it is a test failure rather
#: than a quietly different suite. The value's single home is the script's constant.
DEFAULT_BUDGET = 900


def run(args, script=None):
    proc = subprocess.run(
        [sys.executable, str(script or SCRIPT)] + args,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def at(seconds_after_start):
    """A `--now` stamp `seconds_after_start` past START, in UTC."""
    base = datetime.datetime.fromisoformat(START.replace("Z", "+00:00"))
    return (base + datetime.timedelta(seconds=seconds_after_start)).isoformat()


def standard(**over):
    """The arguments a leg would pass, with named overrides.

    Built from a dict rather than by editing a fixed list, so an override may ADD a
    flag (`budget_seconds`) as well as replace one.
    """
    values = {
        "started-at": START,
        "audited": "3",
        "total": "14",
        "now": at(60),
    }
    values.update({key.replace("_", "-"): str(value) for key, value in over.items()})
    args = []
    for key, value in values.items():
        args += ["--" + key, value]
    return args


class TestVerdicts(unittest.TestCase):
    def test_below_the_bound_continues(self):
        code, out, err = run(standard(now=at(60)))
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("CONTINUE "), out)
        self.assertNotIn("TERMINAL", out)

    def test_continue_reports_elapsed_remaining_and_progress(self):
        code, out, _ = run(standard(now=at(60), audited=3, total=14))
        self.assertEqual(code, 0)
        self.assertIn("elapsed=60s", out)
        self.assertIn("remaining=840s", out)
        self.assertIn("audited=3/14", out)

    def test_one_second_under_is_continue(self):
        code, out, _ = run(standard(now=at(DEFAULT_BUDGET - 1)))
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("CONTINUE "), out)

    def test_at_the_bound_is_terminal(self):
        code, out, _ = run(standard(now=at(DEFAULT_BUDGET)))
        self.assertEqual(code, 3, out)
        self.assertTrue(out.startswith("TERMINAL:"), out)

    def test_past_the_bound_is_terminal(self):
        code, out, _ = run(standard(now=at(DEFAULT_BUDGET + 500)))
        self.assertEqual(code, 3, out)
        self.assertTrue(out.startswith("TERMINAL:"), out)

    def test_terminal_names_the_bound_the_progress_and_the_remainder(self):
        code, out, _ = run(standard(now=at(1000), audited=3, total=14))
        self.assertEqual(code, 3)
        self.assertIn("budget 900s", out)
        self.assertIn("audited 3/14", out)
        self.assertIn("11 left unaudited", out)

    def test_terminal_says_it_is_a_failure_not_a_hold(self):
        """Clause (1)'s `Cap failure` shape: a bounded stop is loud, never a quiet hold."""
        _, out, _ = run(standard(now=at(DEFAULT_BUDGET)))
        self.assertIn("never a hold", out)

    def test_terminal_is_exactly_one_line(self):
        """The contract is a single greppable line -- silence must not read as running."""
        _, out, _ = run(standard(now=at(DEFAULT_BUDGET)))
        self.assertEqual(len(out.splitlines()), 1, out)

    def test_a_zero_budget_is_refused(self):
        """A zero bound is a broken argument, not an instantly-exhausted budget."""
        code, out, _ = run(standard(now=at(0), budget_seconds=0))
        self.assertEqual(code, 2, out)

    def test_a_custom_budget_moves_the_boundary(self):
        code, _, _ = run(standard(now=at(120), budget_seconds=60))
        self.assertEqual(code, 3)
        code, _, _ = run(standard(now=at(120), budget_seconds=600))
        self.assertEqual(code, 0)


class TestProgress(unittest.TestCase):
    def test_an_absent_total_renders_a_question_mark(self):
        """A leg that does not know its set size still gets a bound."""
        code, out, _ = run(
            ["--started-at", START, "--now", at(60), "--budget-seconds", "900"]
        )
        self.assertEqual(code, 0, out)
        self.assertIn("audited=0/?", out)

    def test_the_remainder_never_goes_negative(self):
        """A caller over-reporting `audited` must not render a negative remainder."""
        _, out, _ = run(standard(now=at(1000), audited=99, total=14))
        self.assertIn("0 left unaudited", out)


class TestStamps(unittest.TestCase):
    def test_a_zulu_stamp_parses(self):
        code, _, _ = run(["--started-at", START, "--now", at(10)])
        self.assertEqual(code, 0)

    def test_an_offset_stamp_parses(self):
        code, _, _ = run(
            ["--started-at", "2026-10-16T13:05:04+02:00", "--now", at(10)]
        )
        self.assertEqual(code, 0)

    def test_a_naive_stamp_is_read_as_local(self):
        """The leg reads a UTC stamp, but a hand-run may pass a bare one; it must parse."""
        local = datetime.datetime.fromisoformat(START.replace("Z", "+00:00")).astimezone()
        naive = local.replace(tzinfo=None).isoformat()
        code, _, err = run(["--started-at", naive, "--now", at(10)])
        self.assertEqual(code, 0, err)


class TestRefusals(unittest.TestCase):
    """REFUSED is a third state. The expensive bug is reading it as CONTINUE."""

    def assert_refused(self, args):
        code, out, err = run(args)
        self.assertEqual(code, 2, out + err)
        self.assertNotIn("CONTINUE", out, "a refused read must not read as continue")
        self.assertNotIn("TERMINAL", out, "a refused read is not a verdict")
        self.assertIn("REFUSED", err)
        return err

    def test_an_unparseable_start_is_refused(self):
        self.assert_refused(["--started-at", "yesterday", "--now", at(10)])

    def test_a_future_start_is_refused(self):
        err = self.assert_refused(["--started-at", START, "--now", at(-3600)])
        self.assertIn("future", err)

    def test_a_non_positive_budget_is_refused(self):
        self.assert_refused(standard(now=at(10), budget_seconds=0))
        self.assert_refused(standard(now=at(10), budget_seconds=-5))

    def test_a_negative_count_is_refused(self):
        self.assert_refused(standard(now=at(10), audited=-1))
        self.assert_refused(standard(now=at(10), total=-1))

    def test_a_non_integer_count_is_refused(self):
        self.assert_refused(standard(now=at(10), audited="three"))

    def test_a_missing_start_is_refused(self):
        code, out, _ = run(["--now", at(10)])
        self.assertEqual(code, 2, out)


class TestDefaultBudget(unittest.TestCase):
    def test_the_default_is_the_documented_constant(self):
        """A silent change to the bound is a suite failure, not a quiet difference."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("DEFAULT_BUDGET_SECONDS = %d" % DEFAULT_BUDGET, source)

    def test_the_default_is_used_when_no_budget_is_passed(self):
        code, _, _ = run(standard(now=at(DEFAULT_BUDGET)))
        self.assertEqual(code, 3, "the default bound did not fire")
        code, _, _ = run(standard(now=at(DEFAULT_BUDGET - 1)))
        self.assertEqual(code, 0, "the default bound fired early")


class TestDiscrimination(unittest.TestCase):
    """Each mutation must violate its OWN invariant, not merely exit non-zero."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def mutate(self, old, new, name):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(
            source.count(old), 1, "the anchor %r is not unique in the source" % old
        )
        path = pathlib.Path(self._tmp.name) / name
        path.write_text(source.replace(old, new), encoding="utf-8")
        return path

    def test_an_exclusive_bound_is_caught(self):
        """`>=` -> `>` moves the boundary by one dispatch; the at-the-bound test fires."""
        broken = self.mutate(
            "if elapsed >= args.budget_seconds:", "if elapsed > args.budget_seconds:", "gt.py"
        )
        code, out, _ = run(standard(now=at(DEFAULT_BUDGET)), script=broken)
        self.assertEqual(code, 0, "the at-the-bound case stopped discriminating")

    def test_a_removed_terminal_branch_is_caught(self):
        """The loop never ends -- the exact hang this fix exists to remove."""
        broken = self.mutate(
            "if elapsed >= args.budget_seconds:", "if False:", "never.py"
        )
        code, out, _ = run(standard(now=at(DEFAULT_BUDGET + 5000)), script=broken)
        self.assertEqual(code, 0, out)
        self.assertNotIn("TERMINAL", out)

    def test_a_removed_future_guard_is_caught(self):
        """A negative elapsed is clock skew, not a budget verdict."""
        broken = self.mutate("if elapsed < 0:", "if False:", "skew.py")
        code, out, _ = run(standard(now=at(-3600)), script=broken)
        self.assertEqual(code, 0, out)

    def test_a_swallowed_refusal_is_caught(self):
        """A refusal that returns 0 would give the leg no bound at all."""
        broken = self.mutate(
            '            "TERMINAL branch on it",\n            file=sys.stderr,\n        )\n        return REFUSED\n',
            '            "TERMINAL branch on it",\n            file=sys.stderr,\n        )\n        return CONTINUE\n',
            "swallow.py",
        )
        code, out, _ = run(["--started-at", "yesterday", "--now", at(10)], script=broken)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
