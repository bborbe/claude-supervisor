"""Unit tests for `scripts/check-open-hand-run.py`.

The check was rewritten on 2026-11-20 with `commands/open.md`: the command is a thin
caller of the wrapper's open endpoint now, so the guard is no longer "the spawn branches
carry the right flag" but "no spawn path comes back, and the endpoint call stays".

Each case mutates the REAL `commands/open.md` rather than a synthetic fixture, so a case
fails loudly when the anchor it depends on disappears from the file.
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_open_hand_run", os.path.join(_ROOT, "scripts", "check-open-hand-run.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CheckOpenHandRun(unittest.TestCase):
    def setUp(self):
        self.check = _load()
        with open(os.path.join(_ROOT, "commands", "open.md"), encoding="utf-8") as handle:
            self.real = handle.read()
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, text):
        page = pathlib.Path(self._tmp.name) / "open.md"
        page.write_text(text, encoding="utf-8")
        self.check.COMMAND = page
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.check.main()
        return rc, out.getvalue()

    def _mutate(self, old, new):
        self.assertGreaterEqual(self.real.count(old), 1, "fixture anchor absent: %r" % old)
        return self.real.replace(old, new)

    def test_the_real_command_passes(self):
        rc, out = self._run(self.real)
        self.assertEqual(0, rc, out)
        self.assertIn("thin caller", out)

    def test_a_reintroduced_tab_spawn_fires(self):
        rc, out = self._run(self._mutate("## ", "wezterm cli spawn --window-id 2 -- x\n\n## "))
        self.assertEqual(1, rc)
        self.assertIn("wezterm cli spawn", out)

    def test_a_reintroduced_headless_spawn_fires(self):
        rc, out = self._run(self._mutate("## ", "spawn_agent(prompt=row.name)\n\n## "))
        self.assertEqual(1, rc)
        self.assertIn("spawn_agent", out)

    def test_a_reintroduced_agents_window_fires(self):
        rc, out = self._run(self._mutate("## ", '{"agent": "Agents"}\n\n## '))
        self.assertEqual(1, rc)
        self.assertIn("Agents", out)

    def test_a_reintroduced_manager_recipe_fires(self):
        rc, out = self._run(self._mutate("## ", "manager-launcher.py --goal x\n\n## "))
        self.assertEqual(1, rc)
        self.assertIn("manager-launcher.py", out)

    def test_losing_the_endpoint_call_fires(self):
        text = self.real.replace("api/1.0/open", "some/other/path").replace("open_rows", "rows")
        rc, out = self._run(text)
        self.assertEqual(1, rc)
        self.assertIn("no route", out.replace("\n", " "))

    def test_a_missing_file_fails(self):
        self.check.COMMAND = pathlib.Path(self._tmp.name) / "absent.md"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.check.main()
        self.assertEqual(1, rc)
        self.assertIn("missing", out.getvalue())


if __name__ == "__main__":
    unittest.main()
