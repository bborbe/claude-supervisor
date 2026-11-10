"""check-wrote-nothing.py: the class, its executable sources, and every property that fails silently.

The rule this guards was wrong three times before it was right, and each wrong version passed a
review of the prose alone:

1. the spawn time came from `metrics_sessions[].started_at`, which a headless spawn never binds;
2. the candidate set was `CLASSIFICATION` rows, and `agents/fleet-sweep-reader.md:122` says a
   headless worker is *"invisible to `ListAgents` by construction"*;
3. the replacement read ran `python3 spawn-ledger.py` (a library module — printed nothing, exited
   0) and `date -r <file> +%s` (GNU-only, errors on this fleet's macOS host).

`test_removing_any_marker_fails` is the case this file exists for: the guard asserts prose
substrings, so its only real power is that each one is separately load-bearing. If a marker can
be deleted without the guard failing, that property is decoration.

Same shape as `test_check_acting_legs.py`.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-wrote-nothing.py"

#: Every marker the guard requires, in the prose shape the real file carries it. Kept here
#: rather than imported so a marker deleted from BOTH files still fails the suite.
MARKERS = {
    "class": "wrote nothing since spawn",
    "source": "The spawn time is `spawned_at` on the row keyed by the worker's full session uuid",
    "refusal": "Never `claude_session_started` either",
    "headless": "A headless worker has no `CLASSIFICATION` row",
    "mtime": 'python3 -c "import os,sys;print(int(os.path.getmtime(sys.argv[1])))"',
    "ledger": "python3 $P/spawn-ledger.py --json",
    "join": "Join on the first 8 characters of `session_id`",
    "grace": "exceeds the 15-minute park window",
    "table": "| **wrote-nothing** |",
    "counts": "wrote-nothing=<W>",
    "control": "the worker wrote: keep the ordinary disposition",
    "evidence": "The mtime is evidence, not proof.",
}


def fleet_drive(**overrides: str) -> str:
    """The rule text carrying every marker, with any of them replaced for a negative case."""
    return "# Fleet drive\n\n1a. **Did the worker write at all?**\n\n" + "\n\n".join(
        overrides.get(name, text) for name, text in MARKERS.items()
    ) + "\n"


class CheckWroteNothingTest(unittest.TestCase):
    def run_guard(self, rule_text: str) -> subprocess.CompletedProcess:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "scripts").mkdir()
        (tmp / "agents").mkdir()
        shutil.copy(SCRIPT, tmp / "scripts" / SCRIPT.name)
        (tmp / "agents" / "fleet-drive.md").write_text(rule_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(tmp / "scripts" / SCRIPT.name)],
            capture_output=True,
            text=True,
        )

    def test_rule_with_every_marker_passes(self) -> None:
        result = self.run_guard(fleet_drive())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_removing_any_marker_fails(self) -> None:
        """One case per marker: delete it and the guard must fail naming it."""
        for name, text in MARKERS.items():
            with self.subTest(marker=name):
                result = self.run_guard(fleet_drive(**{name: "(removed)"}))
                self.assertEqual(
                    result.returncode,
                    1,
                    f"removing the {name!r} marker left the guard passing — that property is "
                    f"decoration, not a check",
                )
                self.assertIn("does not carry", result.stderr)

    def test_missing_rule_file_fails(self) -> None:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "scripts").mkdir()
        shutil.copy(SCRIPT, tmp / "scripts" / SCRIPT.name)
        result = subprocess.run(
            [sys.executable, str(tmp / "scripts" / SCRIPT.name)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
