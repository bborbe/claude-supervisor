"""check-acting-legs.py: the delegate rule's carve-out, and each acting leg's own statement.

The round's delegate rule sends everything that is not the sweep to a sub-agent, and rows 5b
(keep alive) and 5c (flagged open) are exactly that — while being the only two rows whose
purpose is to act. With no carve-out a round delegates them read-only and prints a count it was
told to print: measured 2026-11-01 13:27 CET, a round instructed its sub-agent to print
`opened=0` and resumed none of the 8 `resume` rows it read.

`test_flagged_leg_without_its_statement_fails` is the case this file exists for: a tree whose
router carries the carve-out and whose 5c file never says the leg acts is exactly the tree that
shipped the defect — the leg is delegated, the delegate runs read-only, and the round reports
zero. The neighbouring cases cover the single-home half and the pointer half.

Same shape as `test_check_round_overlap.py`.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-acting-legs.py"
ROUTER = "commands/fleet-drive.md"
KEEPALIVE = "commands/steps/keepalive.md"
FLAGGED = "commands/steps/flagged-open.md"

CARVEOUT = "5b and 5c are acting legs"
MARKER = "a read-only execution is not a run of it"
WHEN = "every tick"


def router(with_carveout: bool = True, rows: tuple[tuple[str, str], ...] = (("5b", KEEPALIVE), ("5c", FLAGGED))) -> str:
    lines = [
        "# Fleet drive",
        "",
        "⚠️ **Delegate; keep this session for the round.** `Task` is available — anything not the",
        "sweep goes to a sub-agent, or the round stalls.",
        "",
    ]
    if with_carveout:
        lines += [
            f"⚠️ **Carve-out: {CARVEOUT}.** They run here, or in a sub-agent granted",
            "write tools (`Bash`, `Edit`, `Write`); a read-only delegate is not a run of them.",
            "",
        ]
    lines += ["| # | Step | When | File |", "|---|---|---|---|"]
    for n, path in rows:
        lines.append(f"| {n} | Step {n} | {WHEN} | `${{CLAUDE_PLUGIN_ROOT}}/{path}` |")
    return "\n".join(lines) + "\n"


def step(title: str, with_marker: bool = True, with_pointer: bool = True) -> str:
    body = [
        "---",
        "user-invocable: false",
        "---",
        "",
        f"# {title}",
        "",
        f"When: {WHEN}",
        "",
    ]
    if with_marker:
        body.append(f"⚠️ **This leg acts, and {MARKER}.** It restarts and resumes.")
    if with_pointer:
        body.append(f"See `{ROUTER}` § the delegate rule.")
    return "\n".join(body) + "\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        root = pathlib.Path(self.dir)
        (root / "scripts").mkdir()
        (root / "commands" / "steps").mkdir(parents=True)
        shutil.copy(SCRIPT, root / "scripts" / "check-acting-legs.py")
        self.write(ROUTER, router())
        self.write(KEEPALIVE, step("Keep alive"))
        self.write(FLAGGED, step("Flagged open"))

    def write(self, rel: str, text: str) -> None:
        (pathlib.Path(self.dir) / rel).write_text(text, encoding="utf-8")

    def guard(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(pathlib.Path(self.dir) / "scripts" / "check-acting-legs.py")],
            capture_output=True,
            text=True,
        )


class TestGuard(Base):
    def test_intact_tree_passes(self) -> None:
        r = self.guard()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_pre_fix_tree_without_carveout_fails(self) -> None:
        # The rule as it shipped before the fix: delegate everything, no exception.
        self.write(ROUTER, router(with_carveout=False))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("carries the carve-out", r.stderr)

    def test_flagged_leg_without_its_statement_fails(self) -> None:
        # 5c delegated read-only: the router carves it out, but the leg's own file never says
        # it acts, so a sub-agent that runs it as written opens nothing and reports zero.
        self.write(FLAGGED, step("Flagged open", with_marker=False))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("commands/steps/flagged-open.md", r.stderr)
        self.assertIn("carries no acting statement", r.stderr)

    def test_keepalive_without_its_statement_fails(self) -> None:
        self.write(KEEPALIVE, step("Keep alive", with_marker=False))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("commands/steps/keepalive.md", r.stderr)

    def test_carveout_moved_off_the_router_fails(self) -> None:
        # One home, and that home is beside the rule it excepts.
        self.write(ROUTER, router(with_carveout=False))
        self.write(KEEPALIVE, step("Keep alive") + f"\n<!-- {CARVEOUT} -->\n")
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("home must be", r.stderr)

    def test_carveout_restated_in_a_step_file_fails(self) -> None:
        # Two copies is two counters a grep cannot tell apart.
        self.write(FLAGGED, step("Flagged open") + f"\n<!-- {CARVEOUT} -->\n")
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("appears 2 times", r.stderr)

    def test_carveout_without_write_tools_fails(self) -> None:
        # "Delegate it" without "grant it write tools" is the read-only delegate again.
        self.write(ROUTER, router().replace("write tools", "the tools it needs"))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not state 'write tools'", r.stderr)

    def test_carveout_without_the_read_only_case_fails(self) -> None:
        self.write(ROUTER, router().replace("a read-only delegate is not a run of them", "so delegate it"))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not state 'read-only'", r.stderr)

    def test_leg_without_pointer_fails(self) -> None:
        self.write(FLAGGED, step("Flagged open", with_pointer=False))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not point at", r.stderr)

    def test_router_row_dropped_fails(self) -> None:
        # A carve-out for a row the router no longer dispatches governs nothing.
        self.write(ROUTER, router(rows=(("5b", KEEPALIVE),)))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("row 5c does not name", r.stderr)


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
