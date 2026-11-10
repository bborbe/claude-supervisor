#!/usr/bin/env python3
"""Tests for scripts/task-standdown.py.

The script's two write paths both shell out to `vault-cli`, so the writer is injected:
a test that ran the real CLI would mutate a real vault page, and a test that asserted
against a fake CLI's argv would assert the shape of the call rather than the verdict.

Each case guards one claim the task file makes:

  * **the stamp writes both keys** — `stopped` and `stop_date`. The marker alone is what
    the readers gate on, but the criterion names the pair.
  * **a handed-off session refuses** — the id appears only in `metrics_sessions`, so the
    task belongs to a successor. ⚠️ This is the case a `find_task`-is-not-None check would
    get wrong, and it would stop a task another session now owns.
  * **an unreadable vault list is `cannot-tell`, never `no-task`** — the third state.
  * **`--clear` removes both keys**, and a clear that cannot run is reported, not swallowed.
"""

import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "task-standdown.py")

_spec = importlib.util.spec_from_file_location("task_standdown", _SCRIPT)
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)


class TaskStanddownTest(unittest.TestCase):
    def setUp(self):
        self.writes = []
        self.removals = []
        self._orig = (ts.set_field, ts.remove_field, ts.vaults, ts.anchored_task, ts._load)
        # ⚠️ A realpath root, because `vault_name_for()` compares realpaths: on macOS
        # `/tmp` is a symlink to `/private/tmp`, so a literal root would never match and
        # the vault would resolve to None.
        ts.vaults = lambda: [("v", os.path.realpath("/tmp/v"))]
        ts.set_field = lambda task, key, value, vault: (
            self.writes.append((task, key, value, vault)) or None
        )
        ts.remove_field = lambda task, key, vault: (
            self.removals.append((task, key, vault)) or None
        )

    def tearDown(self):
        (ts.set_field, ts.remove_field, ts.vaults, ts.anchored_task, ts._load) = self._orig

    def stub_resolve(self, path, ambiguous=()):
        """Replace the `resolve-task-file.py` seam.

        ⚠️ **Patched on `_load`, not on a loaded module.** `clear()` calls `_load()` on
        every invocation, so it gets a freshly executed module object each time — a patch
        applied to a previously loaded instance is silently discarded and the real
        resolver runs against this machine's vaults.
        """
        class Resolver:
            @staticmethod
            def vault_dirs():
                return ([], "")

            @staticmethod
            def resolve(name, payload):
                return path, list(ambiguous)

        original = ts._load
        ts._load = lambda name, filename: (
            Resolver() if filename == "resolve-task-file.py" else original(name, filename)
        )

    def run_main(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = ts.main(argv)
        return code, out.getvalue().splitlines()

    # -- stamp ------------------------------------------------------------

    def test_the_stamp_writes_both_keys(self):
        ts.anchored_task = lambda sid, cwd, root: ("/tmp/v/25 Tasks/A Task.md", False, "")
        code, out = self.run_main(["--session", "sid-1"])

        self.assertEqual(0, code)
        self.assertEqual("stopped\t/tmp/v/25 Tasks/A Task.md", out[0])
        self.assertEqual([w[1] for w in self.writes], [ts.STOPPED, ts.STOP_DATE])
        self.assertEqual(self.writes[0][2], "true")
        self.assertEqual(self.writes[0][3], "v", "the vault must be resolved from the path")
        # ⚠️ The writer takes the task's NAME, never its path. `vault-cli task set` joins
        # its argument under `<vault>/<tasks_dir>`, so a path resolves to
        # `<tasks_dir>/<absolute path>` and fails — measured 2026-11-10, and the failure is
        # silent from this script's side because it only sees a non-zero exit.
        self.assertEqual(self.writes[0][0], "A Task")

    def test_an_unattributable_path_is_refused_not_written_to_a_default_vault(self):
        """⚠️ The wrong-target write.

        `vault_name_for()` answers None for a page under no configured root, and a bare
        stem handed to `vault-cli task set` resolves against its **configured default**
        vault — so the stop would land on a same-titled task in a different vault. The
        stem-plus-`--vault` rule exists to prevent exactly that; an unattributable path
        must refuse.
        """
        ts.anchored_task = lambda sid, cwd, root: ("/elsewhere/25 Tasks/A Task.md", False, "")
        code, out = self.run_main(["--session", "sid-1"])

        self.assertEqual(1, code)
        self.assertEqual("cannot-tell\t/elsewhere/25 Tasks/A Task.md", out[0])
        self.assertEqual([], self.writes, "a bare stem must never reach vault-cli")

    def test_an_explicit_vault_overrides_the_path_detection(self):
        """`--vault` is the escape hatch the refusal above names."""
        ts.anchored_task = lambda sid, cwd, root: ("/elsewhere/25 Tasks/A Task.md", False, "")
        code, out = self.run_main(["--session", "sid-1", "--vault", "explicit"])

        self.assertEqual(0, code)
        self.assertEqual("stopped\t/elsewhere/25 Tasks/A Task.md", out[0])
        self.assertEqual(self.writes[0][3], "explicit")

    def test_the_writer_gets_the_stem_not_the_path(self):
        self.assertEqual(ts.task_name("/v/25 Tasks/Some Long Title.md"), "Some Long Title")
        self.assertEqual(ts.task_name("Bare Name"), "Bare Name")

    def test_a_by_name_stamp_writes_the_same_keys(self):
        """The by-name form must reach the same page field.

        ⚠️ Without this path a task stopped by name writes only the
        `task--<base>.stopped` state file, which **no reader consumes** — so the row was
        still reopened by the open leg and resumed by the keepalive. That was the
        half-merge the by-name PR's own SC3 deferred to this change, and the local review
        caught it.
        """
        self.stub_resolve("/tmp/v/25 Tasks/A Task.md")
        code, out = self.run_main(["--subject", "A Task"])

        self.assertEqual(0, code)
        self.assertEqual("stopped\t/tmp/v/25 Tasks/A Task.md", out[0])
        self.assertEqual([w[1] for w in self.writes], [ts.STOPPED, ts.STOP_DATE])
        self.assertEqual(self.writes[0][0], "A Task")

    def test_a_by_name_stamp_of_an_unknown_name_writes_nothing(self):
        self.stub_resolve("")
        code, out = self.run_main(["--subject", "Nope"])

        self.assertEqual(1, code)
        self.assertEqual("no-task\t", out[0])
        self.assertEqual([], self.writes)

    def test_a_handed_off_task_refuses(self):
        """The id is only in `metrics_sessions` — the task belongs to a successor."""
        ts.anchored_task = lambda sid, cwd, root: ("/tmp/v/25 Tasks/A Task.md", True, "")
        code, out = self.run_main(["--session", "sid-1"])

        self.assertEqual(1, code)
        self.assertEqual("handed-off\t/tmp/v/25 Tasks/A Task.md", out[0])
        self.assertEqual([], self.writes, "a handed-off task must not be stamped")

    def test_no_anchored_task_is_a_refusal(self):
        ts.anchored_task = lambda sid, cwd, root: ("", False, "no task claims this session")
        code, out = self.run_main(["--session", "sid-1"])

        self.assertEqual(1, code)
        self.assertEqual("no-task\t", out[0])
        self.assertEqual([], self.writes)

    def test_an_unreadable_vault_list_is_cannot_tell(self):
        """The third state: 'I could not look' must never render as 'no task'."""
        ts.anchored_task = lambda sid, cwd, root: ("/tmp/v/25 Tasks/A Task.md", False, "")
        ts.vaults = lambda: None
        code, out = self.run_main(["--session", "sid-1"])

        self.assertEqual(1, code)
        self.assertEqual("cannot-tell\t/tmp/v/25 Tasks/A Task.md", out[0])
        self.assertEqual([], self.writes)

    def test_a_failed_stop_write_is_reported_not_swallowed(self):
        ts.anchored_task = lambda sid, cwd, root: ("/tmp/v/25 Tasks/A Task.md", False, "")
        ts.set_field = lambda task, key, value, vault: "boom"
        code, out = self.run_main(["--session", "sid-1"])

        self.assertEqual(1, code)
        self.assertEqual("cannot-tell\t/tmp/v/25 Tasks/A Task.md", out[0])

    def test_a_failed_date_write_keeps_the_stop(self):
        """`stopped` is the operative key; a missing `stop_date` must not undo it."""
        ts.anchored_task = lambda sid, cwd, root: ("/tmp/v/25 Tasks/A Task.md", False, "")

        def write(task, key, value, vault):
            self.writes.append((task, key, value, vault))
            return "boom" if key == ts.STOP_DATE else None

        ts.set_field = write
        code, out = self.run_main(["--session", "sid-1"])

        self.assertEqual(0, code)
        self.assertEqual("stopped\t/tmp/v/25 Tasks/A Task.md", out[0])

    # -- clear ------------------------------------------------------------

    def test_clear_removes_both_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "A Task.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("---\nstopped: true\nstop_date: 2026-11-10\n---\n")
            self.stub_resolve(path)
            # `--vault` named explicitly: the fixture page lives in a temp dir that is not
            # under the stubbed vault root, and an unattributable path is now refused.
            code, out = self.run_main(["--clear", "--subject", "A Task", "--vault", "v"])

        self.assertEqual(0, code)
        self.assertEqual("cleared\t%s" % path, out[0])
        self.assertEqual([r[1] for r in self.removals], [ts.STOPPED, ts.STOP_DATE])

    def test_an_ambiguous_name_writes_nothing(self):
        self.stub_resolve("", ambiguous=["/a.md", "/b.md"])
        code, out = self.run_main(["--clear", "--subject", "A Task"])

        self.assertEqual(1, code)
        self.assertEqual("ambiguous\t", out[0])
        self.assertEqual([], self.removals)

    def test_clear_needs_a_subject(self):
        with self.assertRaises(SystemExit):
            ts.main(["--clear"])


if __name__ == "__main__":
    unittest.main()
