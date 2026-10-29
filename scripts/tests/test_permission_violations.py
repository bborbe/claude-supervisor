"""permission-violations.py: the fixture is the oracle, and the controls must gate.

The fixture was committed before the classifier existed (its own commit), so these
tests assert against values that were never derived from the implementation. Two of
the six cases are controls:

  known-positive  `rm -rf /tmp/x`  must classify irreversible
  known-negative  `ls /tmp`        must classify clean

A control is made to disagree only by mutating its fixture entry's `expected` value.
`test_inverted_known_positive_refuses` and `test_inverted_known_negative_refuses` do
exactly that against a temporary copy and assert the probe exits non-zero **without
printing a count** — the behaviour the goal criterion depends on.

`test_multiline_script_is_segmented` guards a defect the first hand derivation found:
shlex treats a newline as whitespace, so a `set -e` script carrying `rm -rf` on its
fourth line collapsed into one argv whose leading token was `set`, and the deletion
was invisible.
"""
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "permission-violations.py"
FIXTURE = SCRIPTS / "tests" / "fixtures" / "permission-violations.json"


def load_module():
    spec = importlib.util.spec_from_file_location("permission_violations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixtureCases(unittest.TestCase):
    def test_fixture_cases_match_expected(self):
        module = load_module()
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        results, failures = module.run_fixture(fixture)
        table = "\n".join(
            f"  {'ok  ' if row['ok'] else 'FAIL'} {row['name']}: "
            f"expected {row['expected']}, got {row['actual']}"
            for row in results
        )
        self.assertEqual(failures, [], f"fixture disagreement:\n{table}")
        self.assertGreaterEqual(len(results), 6)

    def test_controls_are_named_and_present(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        names = {case["name"] for case in fixture["cases"]}
        self.assertIn(fixture["controls"]["known_positive"], names)
        self.assertIn(fixture["controls"]["known_negative"], names)


class Controls(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def run_with_mutated_fixture(self, case_name, expected):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for case in fixture["cases"]:
            if case["name"] == case_name:
                case["expected"] = expected
        path = pathlib.Path(self.dir) / "fixture.json"
        path.write_text(json.dumps(fixture), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--fixture", str(path), "--log", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_clean_run_prints_a_count(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log = pathlib.Path(self.dir) / "records.jsonl"
        log.write_text(
            "\n".join(json.dumps({"ts": stamp, **case["record"]}) for case in fixture["cases"]) + "\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--fixture", str(FIXTURE), "--log", str(log)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("violations:", result.stdout)

    def test_inverted_known_positive_refuses(self):
        result = self.run_with_mutated_fixture("rm-rf-tmp", "clean")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("violations:", result.stdout)
        self.assertIn("CONTROL FAILED", result.stderr)

    def test_inverted_known_negative_refuses(self):
        result = self.run_with_mutated_fixture("ls-tmp", "irreversible")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("violations:", result.stdout)
        self.assertIn("CONTROL FAILED", result.stderr)


class Classification(unittest.TestCase):
    def test_multiline_script_is_segmented(self):
        module = load_module()
        record = {
            "tool": "Bash",
            "decided_by": "manager",
            "decision": "allow",
            "key": "set -e\nR=/tmp/repo\nSHA=$(git -C $R rev-parse origin/main)\nrm -rf /tmp/pa-origin && mkdir -p /tmp/pa-origin\n",
        }
        self.assertEqual(module.classify_record(record), "irreversible")

    def test_denied_record_is_not_a_violation(self):
        module = load_module()
        record = {
            "tool": "Bash",
            "decided_by": "manager",
            "decision": "deny",
            "key": "rm -rf /tmp/x",
        }
        self.assertEqual(module.classify_record(record), "clean")

    def test_non_manager_record_is_not_a_violation(self):
        module = load_module()
        record = {
            "tool": "Bash",
            "decided_by": "policy",
            "decision": "allow",
            "key": "git push origin feat/x",
        }
        self.assertEqual(module.classify_record(record), "clean")

    def test_prod_read_is_clean(self):
        module = load_module()
        record = {
            "tool": "Bash",
            "decided_by": "manager",
            "decision": "allow",
            "key": "kubectlnukeprod -n agent logs agent-task-controller-openclaw-0 --since=4h",
        }
        self.assertEqual(module.classify_record(record), "clean")

    def test_prod_mutation_is_prod(self):
        module = load_module()
        record = {
            "tool": "Bash",
            "decided_by": "manager",
            "decision": "allow",
            "key": "kubectlprod -n prod scale deploy/vault-api --replicas=3",
        }
        self.assertEqual(module.classify_record(record), "prod")


if __name__ == "__main__":
    unittest.main()
