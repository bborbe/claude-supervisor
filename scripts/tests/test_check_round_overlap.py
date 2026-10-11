"""check-round-overlap.py: the reach assertion must be pinned to the TICK router.

The guard's headline claim is that the rule's home is *dispatched every tick*. Its first
version read that reach from the step file's own `When:` line — a restatement, not the
normative trigger — so deleting the row naming the file from every router left it green.
The second version read the router tables but accepted **any** router, and the loop has
two: `commands/manager-loop.md` is the arm, invoked once to start the loop and never
re-invoked, and it carries the same step table as `commands/manager-loop-tick.md`, which
is what a wakeup names. So deleting the row from the **tick** router alone still exited 0
while the success message read *"named by a router row that fires every tick"* — an
instrument reporting success on a weaker condition than the one it names, which is the
defect class the guard exists to catch, reproduced inside the guard.

`test_tick_router_row_deleted_fails` is the case that keeps that gap closed, and it is why
this file exists rather than being skipped as boilerplate. The arm-only and gated-`When`
cases are its neighbours, and the marker cases cover the single-copy half.

Same shape as `test_check_inflight_limb.py`.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-round-overlap.py"
ARM = "commands/manager-loop.md"
TICK = "commands/manager-loop-tick.md"
STEP = "commands/steps/manager-readings.md"

MARKER = "is ABSORBED, never run"
WHEN = "every tick, **before** the sweep dispatch"
GATED = "the round has parked rows"

STEP_BODY = f"""---
user-invocable: false
---

# Readings

When: {WHEN}

⚠️ A firing that lands while the previous round is still in flight {MARKER} — and that is a
precondition on this whole step, not a reading within it.
"""


def router(rows: list[tuple[str, str, str]]) -> str:
    lines = ["# Router", "", "| # | Step | When | File |", "|---|---|---|---|"]
    for n, when, path in rows:
        lines.append(f"| {n} | Step {n} | {when} | `${{CLAUDE_PLUGIN_ROOT}}/{path}` |")
    return "\n".join(lines) + "\n"


ROW = ("2", WHEN, STEP)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        root = pathlib.Path(self.dir)
        (root / "scripts").mkdir()
        (root / "commands" / "steps").mkdir(parents=True)
        shutil.copy(SCRIPT, root / "scripts" / "check-round-overlap.py")
        self.write(ARM, router([ROW]))
        self.write(TICK, router([ROW]))
        self.write(STEP, STEP_BODY)

    def write(self, rel: str, text: str) -> None:
        (pathlib.Path(self.dir) / rel).write_text(text, encoding="utf-8")

    def guard(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(pathlib.Path(self.dir) / "scripts" / "check-round-overlap.py")],
            capture_output=True,
            text=True,
        )


class TestGuard(Base):
    def test_intact_tree_passes(self) -> None:
        r = self.guard()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_tick_router_row_deleted_fails(self) -> None:
        # The case the second version of the guard could not detect: the arm's
        # duplicate row stood in for the tick router's, so the rule stopped being
        # read every round while the guard printed success.
        self.write(TICK, router([]))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only a non-tick router names it", r.stderr)

    def test_arm_row_deleted_still_passes(self) -> None:
        # The arm is not what fires; losing its copy costs nothing.
        self.write(ARM, router([]))
        self.assertEqual(self.guard().returncode, 0)

    def test_both_router_rows_deleted_fails(self) -> None:
        self.write(ARM, router([]))
        self.write(TICK, router([]))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no router row names this file", r.stderr)

    def test_tick_row_gated_fails(self) -> None:
        self.write(TICK, router([("2", GATED, STEP)]))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no row's `When` is every-tick", r.stderr)

    def test_marker_missing_fails(self) -> None:
        self.write(STEP, STEP_BODY.replace(MARKER, "is absorbed, never run"))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("carries the rule marker", r.stderr)

    def test_marker_twice_in_one_file_fails(self) -> None:
        # Counting files is not counting copies.
        self.write(STEP, STEP_BODY + f"\n<!-- {MARKER} -->\n")
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("appears 2 times", r.stderr)

    def test_marker_in_the_arm_fails(self) -> None:
        self.write(STEP, STEP_BODY.replace(MARKER, "is absorbed, never run"))
        self.write(ARM, router([ROW]) + f"\n<!-- {MARKER} -->\n")
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must be a step file", r.stderr)


class TestAgainstTheRealRepo(unittest.TestCase):
    """The guard must pass on the tree it ships in — a temp fixture cannot show that."""

    def test_repo_guard_passes(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(SCRIPT.parent.parent),
        )
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
