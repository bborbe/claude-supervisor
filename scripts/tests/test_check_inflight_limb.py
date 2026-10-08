"""check-inflight-limb.py: both limb assertions must be reachable.

The guard's whole value is that it fails when the drop line stops naming a limb. Its first
version could not fail on the exemplar at all: the exemplar anchor was
`in flight: limb 2 (tool.json state=open`, which already contains `limb 2 (` — the exact
token the assertion below it searched for — so the assertion matched by construction and
its failure branch was unreachable. The guard returned 0 on a tree whose exemplar named no
limb, which is an instrument reporting success on a weaker condition than the one it names,
i.e. the defect class the guard exists to catch, reproduced inside the guard.

`test_exemplar_without_a_limb_fails` is the case that keeps that branch reachable, and it is
why this file exists rather than being skipped as boilerplate. `test_rule_without_a_limb_fails`
is its rule-side twin: the rule anchor carries no limb token, so that assertion was always
reachable — the asymmetry is exactly what made the exemplar half invisible.

Same shape as `test_check_necessity_templates.py`.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-inflight-limb.py"
DRIVE = "agents/manager-drive.md"

RULE = (
    "⚠️ **An `in flight:` drop names the LIMB that answered, not only the value it read** — "
    "`limb 1 (registry: shell)`, `limb 2 (tool.json state=open 4 min, Bash)`, or "
    "`limb 3 (transcript 2 min)`."
)
EXEMPLAR = "  <task> — in flight: limb 2 (tool.json state=open 4 min, Bash) — working, not stuck"


def body(rule: str = RULE, exemplar: str | None = EXEMPLAR) -> str:
    lines = ["# head", "", rule, "", "Freshness / in-flight drops (2):"]
    if exemplar is not None:
        lines.append(exemplar)
    return "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        (pathlib.Path(self.dir) / "agents").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-inflight-limb.py")
        self.write(body())

    def write(self, text: str) -> None:
        (pathlib.Path(self.dir) / DRIVE).write_text(text, encoding="utf-8")

    def guard(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(pathlib.Path(self.dir) / "scripts" / "check-inflight-limb.py")],
            capture_output=True,
            text=True,
        )


class TestGuard(Base):
    def test_intact_tree_passes(self) -> None:
        r = self.guard()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_exemplar_without_a_limb_fails(self) -> None:
        # The case the first version of the guard could not detect.
        self.write(body(exemplar="  <task> — in flight: tool.json state=open — working, not stuck"))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no longer names a limb", r.stderr)

    def test_rule_without_a_limb_fails(self) -> None:
        self.write(body(rule="⚠️ **An `in flight:` drop names the LIMB that answered.**"))
        r = self.guard()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only detector", r.stderr)

    def test_missing_exemplar_fails(self) -> None:
        self.write(body(exemplar=None))
        self.assertNotEqual(self.guard().returncode, 0)

    def test_missing_rule_fails(self) -> None:
        self.write(body(rule="# something else entirely"))
        self.assertNotEqual(self.guard().returncode, 0)


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
