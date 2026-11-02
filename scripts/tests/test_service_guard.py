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


def runner_for(fp=FOOTPRINT, cpu="3.0\n", status="completed\n"):
    return FakeRunner({
        ("launchctl", "list"): LAUNCHCTL,
        ("footprint",): fp,
        ("ps",): cpu,
        ("vault-cli", "--vault", "v", "task", "get"): status,
        ("vault-cli", "--vault", "v", "task", "set"): "",
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
        runner = runner_for(status="completed\n")
        rc, out = run_main([SERVICE], runner, "--apply")
        self.assertEqual(rc, 0)
        self.assertIn('REOPENED "Some Task" (was completed)', out)
        sets = [c for c in runner.calls if c[:5] == ["vault-cli", "--vault", "v", "task", "set"]]
        self.assertEqual(sets, [
            ["vault-cli", "--vault", "v", "task", "set", "Some Task", "status", "in_progress"],
            ["vault-cli", "--vault", "v", "task", "set", "Some Task", "phase", "execution"],
        ])

    def test_apply_leaves_open_row_alone(self):
        runner = runner_for(status="in_progress\n")
        _, out = run_main([SERVICE], runner, "--apply")
        self.assertIn("already open", out)
        self.assertFalse(any(c[4:5] == ["set"] for c in runner.calls))

    def test_apply_does_nothing_when_ok(self):
        runner = runner_for(fp="phys_footprint: 100 MB", cpu="1.0")
        run_main([SERVICE], runner, "--apply")
        self.assertFalse(any(c[0] == "vault-cli" for c in runner.calls))

    def test_unknown_exits_non_zero(self):
        rc, out = run_main([SERVICE], runner_for(fp="garbage"))
        self.assertEqual(rc, 1)
        self.assertIn("UNKNOWN", out)


if __name__ == "__main__":
    unittest.main()
