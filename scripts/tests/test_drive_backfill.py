"""The `task_file` backfill: resolve from a declared id, never from a name.

The load-bearing test here is `test_an_ambiguous_prefix_is_left_empty`. The whole
point of the never-guess rule this backfill exists to satisfy is that an unresolved
row stays unresolved -- a migration that resolves the ambiguous cases "helpfully"
would be the guess the rule forbids, and it would write a wrong path into the
ledger, where step 0 would then re-probe the wrong task and report a confident
verdict about it.
"""
import importlib.util
import os
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "drive-backfill.py"

_spec = importlib.util.spec_from_file_location("drive_backfill", SCRIPT)
drive_backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drive_backfill)

#: A full id and the 8-char prefix the ledger carries.
FULL = "55907437-f717-43c9-a5e1-9ab415bfffa2"
SID = "55907437"


def row(sid, verdict="unverifiable", **over):
    entry = {
        "session_id": sid,
        "name": "Task %s" % sid,
        "verdict": verdict,
        "open_boxes": 3,
        "probe": "no task file recorded",
        "nudge": "none",
        "suppression": "none",
        "task_file": "",
        "reprobe_verdict": "",
        "reprobe_probe": "",
    }
    entry.update(over)
    return entry


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))

    def write_task(self, name, session_id=None, body="- [ ] open\n"):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        front = "---\nstatus: in_progress\nphase: execution\n"
        if session_id is not None:
            front += "claude_session_id: %s\n" % session_id
        front += "---\n"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(front + body)
        return path


class TestResolve(Base):
    def test_a_unique_prefix_resolves(self):
        path = self.write_task("25 Tasks/Alpha.md", FULL)
        resolved, skipped = drive_backfill.plan(
            {"entries": [row(SID)]}, drive_backfill.task_index(self.dir)
        )
        self.assertEqual(len(resolved), 1)
        # Absolute: drive-retained.py opens task_file as given, from any cwd.
        self.assertEqual(resolved[0]["task_file"], path)
        self.assertEqual(skipped, [])

    def test_an_ambiguous_prefix_is_left_empty(self):
        """Two tasks declaring ids that share the prefix: neither may be chosen."""
        self.write_task("25 Tasks/Alpha.md", FULL)
        self.write_task("25 Tasks/Beta.md", SID + "0000-0000-0000-000000000000")
        resolved, skipped = drive_backfill.plan(
            {"entries": [row(SID)]}, drive_backfill.task_index(self.dir)
        )
        self.assertEqual(resolved, [])
        self.assertEqual(skipped, [(SID, "ambiguous")])

    def test_an_unmatched_row_is_left_empty(self):
        self.write_task("25 Tasks/Alpha.md", FULL)
        resolved, skipped = drive_backfill.plan(
            {"entries": [row("deadbeef")]}, drive_backfill.task_index(self.dir)
        )
        self.assertEqual(resolved, [])
        self.assertEqual(skipped, [("deadbeef", "unmatched")])

    def test_a_name_match_is_not_a_resolution(self):
        """The ledger name and the task name disagree; only the id may join them."""
        path = self.write_task("25 Tasks/A Vault Write Re-Mints.md", FULL)
        resolved, _ = drive_backfill.plan(
            {"entries": [row(SID, name="Renaming Task Re-Mints id")]},
            drive_backfill.task_index(self.dir),
        )
        self.assertEqual(resolved[0]["task_file"], path)

    def test_an_empty_declared_id_is_ignored(self):
        self.write_task("25 Tasks/Alpha.md", "")
        self.assertEqual(drive_backfill.task_index(self.dir), {})

    def test_a_quoted_declared_id_is_read(self):
        path = self.write_task("25 Tasks/Alpha.md", '"%s"' % FULL)
        self.assertEqual(drive_backfill.task_index(self.dir), {FULL: [path]})


class TestScope(Base):
    def test_a_row_that_already_has_a_path_is_untouched(self):
        self.write_task("25 Tasks/Alpha.md", FULL)
        kept = row(SID, task_file="25 Tasks/Already.md")
        resolved, skipped = drive_backfill.plan(
            {"entries": [kept]}, drive_backfill.task_index(self.dir)
        )
        self.assertEqual((resolved, skipped), ([], []))

    def test_non_retained_rows_are_untouched(self):
        """`revive`/`finished` are not re-probed by step 0, so a path changes nothing."""
        self.write_task("25 Tasks/Alpha.md", FULL)
        for verdict in ("revive", "finished"):
            resolved, _ = drive_backfill.plan(
                {"entries": [row(SID, verdict=verdict)]},
                drive_backfill.task_index(self.dir),
            )
            self.assertEqual(resolved, [], verdict)

    def test_only_the_task_file_changes(self):
        path = self.write_task("25 Tasks/Alpha.md", FULL)
        before = row(SID)
        resolved, _ = drive_backfill.plan(
            {"entries": [before]}, drive_backfill.task_index(self.dir)
        )
        after = resolved[0]
        self.assertEqual(after["task_file"], path)
        for key in ("verdict", "probe", "name", "open_boxes", "nudge",
                    "suppression", "reprobe_verdict", "reprobe_probe"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(before["task_file"], "", "input row must not be mutated")


if __name__ == "__main__":
    unittest.main()
