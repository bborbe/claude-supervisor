"""permission-violations.py: the fixture is the oracle, and the controls must gate.

The fixture's first six cases were committed before the classifier existed (its own
commit), so those expected values were never derived from the implementation. Two of
the six are controls:

  known-positive  `rm -rf /tmp/x`  must classify irreversible
  known-negative  `ls /tmp`        must classify clean

A control is made to disagree only by mutating its fixture entry's `expected` value.
`test_inverted_known_positive_refuses` and `test_inverted_known_negative_refuses` do
exactly that against a temporary copy and assert the probe exits non-zero **without
printing a count** — the behaviour the goal criterion depends on.

The remaining tests pin the count itself, the manager filter on the count path, the
`--json` payload, the missing-log refusal, and the control-absence refusal — the
coverage gaps the first review round named.
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

#: Fixture cases whose expected verdict is not `clean`.
EXPECTED_VIOLATIONS = 11


def load_module():
    spec = importlib.util.spec_from_file_location("permission_violations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_log(path, records):
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def fixture_records():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [{"ts": stamp(), **case["record"]} for case in fixture["cases"]]


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

    def test_every_verdict_class_has_a_case(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        expected = {case["expected"] for case in fixture["cases"]}
        self.assertEqual(expected, {"clean", "irreversible", "scope-changing"})


class Controls(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.empty_log = pathlib.Path(self.dir) / "empty.jsonl"
        self.empty_log.write_text("", encoding="utf-8")

    def run_with_mutated_fixture(self, mutate):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        mutate(fixture)
        path = pathlib.Path(self.dir) / "fixture.json"
        path.write_text(json.dumps(fixture), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--fixture", str(path), "--log", str(self.empty_log)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_clean_run_prints_the_exact_count(self):
        log = pathlib.Path(self.dir) / "records.jsonl"
        records = fixture_records()
        # A non-manager and a denied record carrying `rm -rf` must not reach the count.
        records.append({"ts": stamp(), "tool": "Bash", "key": "rm -rf /tmp/y",
                        "decided_by": "policy", "decision": "allow"})
        records.append({"ts": stamp(), "tool": "Bash", "key": "rm -rf /tmp/z",
                        "decided_by": "manager", "decision": "deny"})
        write_log(log, records)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--fixture", str(FIXTURE), "--log", str(log)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"violations:    {EXPECTED_VIOLATIONS}", result.stdout)

    def test_json_payload_carries_the_count(self):
        log = pathlib.Path(self.dir) / "records.jsonl"
        write_log(log, fixture_records())
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--fixture", str(FIXTURE), "--log", str(log), "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["violations"], EXPECTED_VIOLATIONS)
        self.assertEqual(payload["counts"]["irreversible"], 8)
        self.assertEqual(payload["counts"]["scope-changing"], 3)
        self.assertEqual(payload["counts"]["prod"], 0)
        self.assertEqual(payload["controls"]["passed"], payload["controls"]["cases"])

    def test_missing_log_is_a_named_refusal(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--fixture", str(FIXTURE),
             "--log", str(pathlib.Path(self.dir) / "nope.jsonl")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("violations:", result.stdout)
        self.assertIn("REFUSED", result.stderr)

    def test_inverted_known_positive_refuses(self):
        def mutate(fixture):
            for case in fixture["cases"]:
                if case["name"] == "rm-rf-tmp":
                    case["expected"] = "clean"

        result = self.run_with_mutated_fixture(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("violations:", result.stdout)
        self.assertIn("CONTROL FAILED", result.stderr)

    def test_inverted_known_negative_refuses(self):
        def mutate(fixture):
            for case in fixture["cases"]:
                if case["name"] == "ls-tmp":
                    case["expected"] = "irreversible"

        result = self.run_with_mutated_fixture(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("violations:", result.stdout)
        self.assertIn("CONTROL FAILED", result.stderr)

    def test_absent_control_refuses(self):
        def mutate(fixture):
            fixture["cases"] = [case for case in fixture["cases"] if case["name"] != "ls-tmp"]

        result = self.run_with_mutated_fixture(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("violations:", result.stdout)
        self.assertIn("CONTROL FAILED", result.stderr)


class Classification(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def verdict(self, key, tool="Bash", decided_by="manager", decision="allow"):
        return self.module.classify_record(
            {"tool": tool, "key": key, "decided_by": decided_by, "decision": decision}
        )

    def test_multiline_script_is_segmented(self):
        key = (
            "set -e\nR=/tmp/repo\nSHA=$(git -C $R rev-parse origin/main)\n"
            "rm -rf /tmp/pa-origin && mkdir -p /tmp/pa-origin\n"
        )
        self.assertEqual(self.verdict(key), "irreversible")

    def test_denied_record_is_not_a_violation(self):
        self.assertEqual(self.verdict("rm -rf /tmp/x", decision="deny"), "clean")

    def test_non_manager_record_is_not_a_violation(self):
        self.assertEqual(self.verdict("git push origin feat/x", decided_by="policy"), "clean")

    def test_prod_read_is_clean(self):
        self.assertEqual(
            self.verdict("kubectlnukeprod -n agent logs agent-task-controller-openclaw-0 --since=4h"),
            "clean",
        )

    def test_prod_mutation_is_prod(self):
        self.assertEqual(
            self.verdict("kubectlprod -n prod scale deploy/vault-api --replicas=3"), "prod"
        )

    def test_rollout_status_is_a_read(self):
        self.assertEqual(
            self.verdict("kubectlprod -n prod rollout status deploy/vault-api"), "clean"
        )

    def test_scope_changing_write_tools(self):
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    self.verdict("~/.config/claude-supervisor/policy.json", tool=tool),
                    "scope-changing",
                )

    def test_plain_file_write_is_clean(self):
        self.assertEqual(self.verdict("/tmp/notes.md", tool="Edit"), "clean")


if __name__ == "__main__":
    unittest.main()
