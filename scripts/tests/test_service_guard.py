"""Tests for scripts/service-guard.py."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "service_guard", os.path.join(_HERE, "..", "service-guard.py"))
sg = importlib.util.module_from_spec(_spec)
sys.modules["service_guard"] = sg  # dataclass resolution looks the module up by name
_spec.loader.exec_module(sg)

LAUNCHCTL = '{\n\t"Label" = "x";\n\t"PID" = 56681;\n};\n'
FOOTPRINT = "attention-controller [56681]: 64-bit    Footprint: 865 MB\n    phys_footprint: 865 MB\n"
SERVICE = {"label": "com.example.svc", "max_footprint_mb": 150, "max_cpu_pct": 5,
           "task": "Some Task", "vault": "v"}


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        for key, out in self.outputs.items():
            if cmd[: len(key)] == list(key):
                if isinstance(out, Exception):
                    raise out
                return out
        raise AssertionError(f"unexpected command {cmd}")


def status_json(value):
    return json.dumps({"key": "status", "name": "Some Task", "value": value})


def runner_for(fp=FOOTPRINT, cpu="3.0\n", status=None, set_out=""):
    return FakeRunner({
        ("launchctl", "list"): LAUNCHCTL,
        ("footprint",): fp,
        ("ps",): cpu,
        ("vault-cli", "--vault", "v", "task", "get"): status if status is not None else status_json("completed"),
        ("vault-cli", "--vault", "v", "task", "set"): set_out,
        ("vault-cli", "--vault", "v", "task", "clear"): "",
    })


def run_main(services, runner, *args):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(services, fh)
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            rc = sg.main(["--config", fh.name, *args], runner=runner)
    finally:
        os.unlink(fh.name)
    return rc, out.getvalue()


class ParseTest(unittest.TestCase):
    def test_footprint_units(self):
        self.assertEqual(sg.parse_footprint_mb("phys_footprint: 865 MB"), 865.0)
        self.assertEqual(sg.parse_footprint_mb("phys_footprint: 2 GB"), 2048.0)
        self.assertEqual(sg.parse_footprint_mb("phys_footprint: 2048 KB"), 2.0)
        self.assertEqual(sg.parse_footprint_mb("phys_footprint: 5,522 MB"), 5522.0)
        self.assertIsNone(sg.parse_footprint_mb("nothing here"))

    def test_launchctl_pid(self):
        self.assertEqual(sg.parse_launchctl_pid(LAUNCHCTL), 56681)
        self.assertIsNone(sg.parse_launchctl_pid('{\n\t"Label" = "x";\n};'))


class MeasureTest(unittest.TestCase):
    def test_breach_on_footprint(self):
        r = sg.measure(SERVICE, runner_for())
        self.assertEqual(r.state, "BREACH")
        self.assertIn("footprint 865 MB > 150 MB", r.reason)

    def test_breach_on_cpu(self):
        r = sg.measure(SERVICE, runner_for(fp="phys_footprint: 100 MB", cpu="12.9"))
        self.assertEqual(r.state, "BREACH")
        self.assertIn("cpu 12.9% > 5%", r.reason)

    def test_ok_within_bar(self):
        r = sg.measure(SERVICE, runner_for(fp="phys_footprint: 100 MB", cpu="1.0"))
        self.assertEqual(r.state, "OK")

    def test_unknown_when_not_running(self):
        runner = FakeRunner({("launchctl", "list"): '{\n\t"Label" = "x";\n};'})
        self.assertEqual(sg.measure(SERVICE, runner).state, "UNKNOWN")

    def test_unknown_when_footprint_unreadable(self):
        self.assertEqual(sg.measure(SERVICE, runner_for(fp="garbage")).state, "UNKNOWN")


class MainTest(unittest.TestCase):
    def test_missing_config_is_not_an_error(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = sg.main(["--config", "/nonexistent/service-guard.json"], runner=runner_for())
        self.assertEqual(rc, 0)
        self.assertIn("no config", out.getvalue())

    def test_breach_without_apply_reports_only(self):
        runner = runner_for()
        rc, out = run_main([SERVICE], runner)
        self.assertEqual(rc, 0)
        self.assertIn("BREACH", out)
        self.assertFalse(any(c[0] == "vault-cli" for c in runner.calls))

    def test_apply_reopens_terminal_row(self):
        runner = runner_for(status=status_json("completed"))
        rc, out = run_main([SERVICE], runner, "--apply")
        self.assertEqual(rc, 0)
        self.assertIn('REOPENED "Some Task" (was completed)', out)
        writes = [c for c in runner.calls if c[:4] == ["vault-cli", "--vault", "v", "task"] and c[4] != "get"]
        self.assertEqual(writes, [
            ["vault-cli", "--vault", "v", "task", "set", "Some Task", "phase", "execution"],
            ["vault-cli", "--vault", "v", "task", "set", "Some Task", "status", "in_progress"],
            ["vault-cli", "--vault", "v", "task", "clear", "Some Task", "completed_date"],
        ])

    def test_apply_reopens_aborted_row(self):
        _, out = run_main([SERVICE], runner_for(status=status_json("aborted")), "--apply")
        self.assertIn("(was aborted)", out)

    def test_title_containing_failed_does_not_fail_a_clean_reopen(self):
        svc = dict(SERVICE, task="Fix FAILED nightly build")
        rc, out = run_main([svc], runner_for(), "--apply")
        self.assertEqual(rc, 0)
        self.assertIn("REOPENED", out)

    def test_status_read_error_is_reopen_failed_not_already_open(self):
        err = json.dumps({"error": "find task: file not found", "success": False})
        rc, out = run_main([SERVICE], runner_for(status=err), "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("REOPEN FAILED", out)
        self.assertNotIn("already open", out)

    def test_write_failure_is_reopen_failed(self):
        rc, out = run_main([SERVICE], runner_for(set_out=RuntimeError("boom")), "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("REOPEN FAILED", out)

    def test_apply_leaves_open_row_alone(self):
        runner = runner_for(status=status_json("in_progress"))
        _, out = run_main([SERVICE], runner, "--apply")
        self.assertIn("already open", out)
        self.assertFalse(any(c[4:5] == ["set"] for c in runner.calls))

    def test_apply_does_nothing_when_ok(self):
        runner = runner_for(fp="phys_footprint: 100 MB", cpu="1.0")
        run_main([SERVICE], runner, "--apply")
        self.assertFalse(any(c[0] == "vault-cli" for c in runner.calls))

    def test_malformed_config_is_refused_not_a_traceback(self):
        rc, out = run_main([{"label": "x"}], runner_for())
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", out)
        self.assertIn("missing max_footprint_mb", out)

    def test_non_numeric_bar_is_refused(self):
        rc, out = run_main([dict(SERVICE, max_footprint_mb="150")], runner_for())
        self.assertEqual(rc, 1)
        self.assertIn("not a number", out)

    def test_entry_without_cpu_bar_skips_ps(self):
        svc = {k: v for k, v in SERVICE.items() if k != "max_cpu_pct"}
        runner = runner_for(fp="phys_footprint: 100 MB")
        rc, out = run_main([svc], runner)
        self.assertEqual(rc, 0)
        self.assertIn("OK", out)
        self.assertFalse(any(c[0] == "ps" for c in runner.calls))

    def test_json_output(self):
        _, out = run_main([SERVICE], runner_for(), "--json")
        payload = json.loads(out)
        self.assertEqual(payload["readings"][0]["state"], "BREACH")
        self.assertEqual(payload["outcomes"], [])

    def test_unknown_exits_non_zero(self):
        rc, out = run_main([SERVICE], runner_for(fp="garbage"))
        self.assertEqual(rc, 1)
        self.assertIn("UNKNOWN", out)


if __name__ == "__main__":
    unittest.main()
