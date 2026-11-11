"""The unclaimable sweep: what it reports, what it refuses to report, and how it counts.

The load-bearing tests are `test_reports_empty_field_and_ignores_clean_and_untyped`
(the positive control plus both negative controls), `test_undated_is_subtracted_not_double_counted`
(the three counts must sum to the total), `test_mtime_never_opens_the_window`, and
`test_unreadable_file_is_counted_not_silently_skipped` (a file the sweep cannot read is
evidence, not an absence).

The defect this change removes is a task no controller claims that nothing reports. The
defect it could easily introduce is the opposite: a sweep that reports a clean `none` for
a pass that scanned nothing, which reads exactly like a healthy fleet.
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "unclaimable-tasks.py"

_spec = importlib.util.spec_from_file_location("unclaimable_tasks", SCRIPT)
unclaimable = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(unclaimable)

EMPTY = """---
assignee: claude
status: in_progress
task_identifier: 11111111-1111-4111-8111-111111111111
task_type: pr-review
created_date: "2026-11-11"
---
body
"""

CLEAN = """---
assignee: claude
status: in_progress
target_vault: personal
task_identifier: 22222222-2222-4222-8222-222222222222
task_type: pr-review
created_date: "2026-11-11"
---
body
"""

UNTYPED = """---
assignee: bborbe
status: in_progress
task_identifier: 33333333-3333-4333-8333-333333333333
---
body
"""

UNDATED = """---
assignee: claude
status: in_progress
task_identifier: 44444444-4444-4444-8444-444444444444
task_type: llm
---
body
"""


def write(root, name, text):
    with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
        handle.write(text)


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        unclaimable.UNREADABLE.clear()

    def sweep(self, since="2026-11-01"):
        return unclaimable.rows(since, [self.tmp])

    def test_reports_empty_field_and_ignores_clean_and_untyped(self):
        write(self.tmp, "empty.md", EMPTY)
        write(self.tmp, "clean.md", CLEAN)
        write(self.tmp, "untyped.md", UNTYPED)
        found, recent, _ = self.sweep()
        self.assertEqual(
            [row["file"] for row in found], ["empty.md"],
            "only the agent-minted empty field is a defect",
        )
        self.assertEqual([row["file"] for row in recent], ["empty.md"])

    def test_undated_is_subtracted_not_double_counted(self):
        write(self.tmp, "empty.md", EMPTY)
        write(self.tmp, "undated.md", UNDATED)
        found, recent, undated = self.sweep()
        historical = len(found) - len(recent) - len(undated)
        self.assertEqual(len(found), 2)
        self.assertEqual(len(recent), 1)
        self.assertEqual(len(undated), 1)
        self.assertEqual(historical, 0, "undated must not also count as historical")
        self.assertEqual(len(recent) + historical + len(undated), len(found))

    def test_mtime_never_opens_the_window(self):
        # A file with no created_date and no job_started_at is UNDATED even though its
        # mtime is now. Measured 2026-11-11: an mtime window read 9,624 "recent"
        # emissions against a true zero, because mtime is the clone date here.
        write(self.tmp, "undated.md", UNDATED)
        _, recent, undated = self.sweep()
        self.assertEqual(recent, [], "mtime must not place a file in the window")
        self.assertEqual(len(undated), 1)

    def test_unreadable_file_is_counted_not_silently_skipped(self):
        path = os.path.join(self.tmp, "unreadable.md")
        write(self.tmp, "unreadable.md", EMPTY)
        os.chmod(path, 0o000)
        try:
            self.sweep()
        finally:
            os.chmod(path, 0o644)
        self.assertEqual(
            unclaimable.UNREADABLE, [path],
            "an unreadable file is recorded, never swallowed",
        )


    def test_literal_empty_string_is_the_same_defect(self):
        # `target_vault: ""` is what the wire format actually carries when a producer
        # omits the field; treating only an absent key as empty would miss it.
        write(self.tmp, "quoted-empty.md", EMPTY.replace(
            'task_type: pr-review', 'target_vault: ""\ntask_type: pr-review'))
        found, recent, _ = self.sweep()
        self.assertEqual([row["file"] for row in found], ["quoted-empty.md"])
        self.assertEqual([row["file"] for row in recent], ["quoted-empty.md"])

    def test_clean_render_is_unknown_when_a_file_could_not_be_read(self):
        # The failure class this change exists to remove: a partial scan that renders
        # like a healthy fleet. `none` may only be printed when everything was read.
        path = os.path.join(self.tmp, "unreadable.md")
        write(self.tmp, "unreadable.md", EMPTY)
        os.chmod(path, 0o000)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), \
                    unittest.mock.patch.object(sys, "argv", [
                        "unclaimable-tasks.py", "--root", self.tmp,
                        "--since", "2026-11-01"]):
                unclaimable.main()
        finally:
            os.chmod(path, 0o644)
        self.assertIn("UNKNOWN (partial scan)", out.getvalue())
        self.assertNotIn("none —", out.getvalue())

    def test_no_runtime_vault_is_a_failure_not_a_clean_scan(self):
        # "scanned nothing" must never render as "found nothing". The guard lives on
        # the resolution path, so the stub is the vault-cli config coming back empty.
        class Empty:
            @staticmethod
            def vault_dirs_from_cli():
                return {}

        with unittest.mock.patch.object(unclaimable, "_load", lambda *a: Empty):
            with self.assertRaises(SystemExit) as raised:
                unclaimable.rows("2026-11-01")
        self.assertIn("no agent runtime vault", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
