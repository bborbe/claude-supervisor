"""`scripts/check-open-hand-run.py` — each guard fires on the one mutation it exists to catch.

Every fixture starts from the real `commands/open.md` and changes exactly one thing, so a
guard that cannot fail (the defect a first revision of guard (d) had: it matched a backtick
placement the prose never uses) turns this file red.

Run: python3 -m unittest discover -s scripts/tests -p 'test_check_open_hand_run.py'
"""

import contextlib
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

_NONE_CALL = '`topic-spawn.py --vault "<vault-root>" --topic "<topic>" --topic-open --hand-run`'
_UNKNOWN_ARM = "drop `--hand-run`: `topic-unflagged` (1) → print `⏸ held — no flag` and STOP"


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
        self.assertEqual(1, self.real.count(old), "fixture anchor not unique: %r" % old)
        return self.real.replace(old, new)

    def test_the_real_command_passes(self):
        self.assertEqual(0, self._run(self.real)[0])

    def test_hand_run_on_the_unknown_arm_fires(self):
        """Guard (d): `--hand-run` moved onto the resume arm opens an unflagged topic fresh."""
        text = self._mutate(_UNKNOWN_ARM, "run " + _NONE_CALL + ": `topic-unflagged` (1) → STOP")
        rc, out = self._run(text)
        self.assertEqual(1, rc)
        self.assertIn("outside the `None` arm", out)

    def test_unknown_arm_without_the_flag_check_fires(self):
        text = self._mutate(_UNKNOWN_ARM, "keep the call as is")
        rc, out = self._run(text)
        self.assertEqual(1, rc)
        self.assertIn("UNKNOWN/AMBIGUOUS arm does not keep the flag check", out)

    def test_no_hand_run_anywhere_fires(self):
        text = self._mutate(_NONE_CALL, _NONE_CALL.replace(" --hand-run", ""))
        rc, out = self._run(text)
        self.assertEqual(1, rc)
        self.assertIn("never passes --hand-run", out)

    def test_a_goal_flag_stop_fires(self):
        anchor = "## Step 2C — Topic branch"
        text = self._mutate(anchor, "On `goal-unflagged` print it and STOP.\n\n" + anchor)
        rc, out = self._run(text)
        self.assertEqual(1, rc)
        self.assertIn("STOPs on goal-unflagged", out)


if __name__ == "__main__":
    unittest.main()
