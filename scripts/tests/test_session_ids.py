#!/usr/bin/env python3
"""Tests for scripts/session-ids.py.

The load-bearing property is the one the 2026-09-30 measurement names: a bare-uuid
SHAPE match over a task's frontmatter sweeps in `task_identifier` — a uuid of exactly the
same shape, carried by nearly every task file — giving a task with no session a PHANTOM
OWNER and silently dropping it out of `ready-to-start`. So the suite pins, in order:

  * the check REFUSES a `task_identifier` rather than merely omitting it — an omission
    reports a clean set for a file the extraction read wrongly, which is the silent
    direction the whole change exists to remove;
  * the shape mode is refused outright, so there is no path back to the sweep;
  * a frontmatter carrying BOTH a real `claude_session_id` and a `task_identifier`
    returns the session id ALONE (the positive control for the refusal above);
  * the counterfactual is pinned — a bare-uuid match over the SAME frontmatter really
    does return the identifier, so the refusal is guarding a reachable shape and not a
    straw man;
  * `metrics_sessions` entries are read UNANCHORED (they are indented, so a `^session_id:`
    match misses every one) and a file with no frontmatter is UNREADABLE, never an empty
    set.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)

EXIT_OK, EXIT_REJECTED, EXIT_UNREADABLE = 0, 1, 2

REAL = "11111111-2222-3333-4444-555566667777"
SECOND = "99999999-8888-7777-6666-555544443333"
IDENT = "38df293b-221d-4484-b308-02bb5c8152c7"

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def load():
    spec = importlib.util.spec_from_file_location(
        "session_ids", os.path.join(_SCRIPTS, "session-ids.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*argv):
    """Run the script as a subprocess: exit code + (stdout, stderr).

    A subprocess rather than an in-process `main()` call, because the refusals ARE the
    exit code and the stream they land on — asserting on a returned tuple would pin the
    internals and leave the contract the command actually reads untested.
    """
    p = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS, "session-ids.py"), *argv],
        capture_output=True,
        text=True,
    )
    return p.returncode, p.stdout, p.stderr


class Frontmatter(unittest.TestCase):
    def write(self, body):
        fh = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        fh.write(body)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name


class TestRefusal(Frontmatter):
    def test_task_identifier_is_never_returned_as_a_session_id(self):
        """The positive control: both fields present, only the real ids come back."""
        path = self.write(
            f"---\nclaude_session_id: {REAL}\ntask_identifier: {IDENT}\n"
            f"metrics_sessions:\n    - session_id: {SECOND}\n---\nbody\n"
        )
        code, out, err = run("--task", path)
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(out.split(), [REAL, SECOND])

    def test_a_poisoned_session_field_is_refused_loudly(self):
        """The session field carries the task_identifier — the phantom shape, reached.

        A field-scoped read cannot produce this from a well-formed file, so the guard
        firing is the signal that the extraction is not the one the script declares.
        Refusing beats filtering: a filtered set is one a caller believes was read right.
        """
        path = self.write(
            f"---\nclaude_session_id: {IDENT}\ntask_identifier: {IDENT}\n---\nbody\n"
        )
        code, out, err = run("--task", path)
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("REJECTED:", err)
        self.assertIn(IDENT, err)
        self.assertEqual(out, "", "a refused read must print no ids")

    def test_a_poisoned_metrics_session_entry_is_refused_too(self):
        """The refusal is over the whole set, not only the frontmatter scalar."""
        path = self.write(
            f"---\nclaude_session_id: {REAL}\ntask_identifier: {IDENT}\n"
            f"metrics_sessions:\n    - session_id: {IDENT}\n---\nbody\n"
        )
        code, out, err = run("--task", path)
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("REJECTED:", err)
        self.assertEqual(out, "")

    def test_the_shape_mode_is_refused(self):
        """There is no shape mode to reach for — the sweep is unreachable, not banned."""
        code, out, err = run("--shape", "anything")
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("REJECTED:", err)
        self.assertIn("task_identifier", err)
        self.assertEqual(out, "")

    def test_the_counterfactual_holds(self):
        """A bare-uuid match over the SAME frontmatter really does return the identifier.

        Without this the refusal could be guarding a shape the system never produces —
        the check would pass for reasons unrelated to the claim, which is worse than no
        positive control because the control is what the criterion leans on.
        """
        fm = (
            f"---\nclaude_session_id: {REAL}\ntask_identifier: {IDENT}\n"
            f"metrics_sessions:\n    - session_id: {SECOND}\n---\n"
        )
        swept = set(_UUID.findall(fm))
        self.assertIn(IDENT, swept, "the shape match must sweep the identifier in")
        self.assertGreater(len(swept), 2, "and it sweeps more than the real ids")

        path = self.write(fm + "body\n")
        code, out, _ = run("--task", path)
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn(IDENT, out.split(), "the check returns no phantom")


class TestReads(Frontmatter):
    def test_metrics_sessions_are_read_unanchored(self):
        """Entries are indented (`    - session_id: …`) — a `^session_id:` match misses
        every one, which is the single-field read this exists to replace."""
        path = self.write(
            f"---\nmetrics_sessions:\n    - session_id: {SECOND}\n---\nbody\n"
        )
        code, out, err = run("--task", path)
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(out.split(), [SECOND])

    def test_no_frontmatter_is_unreadable_never_an_empty_set(self):
        """An empty set and an unreadable file are different answers: folding the second
        into the first reports "this task has no session" for a file never read."""
        path = self.write("no frontmatter here\n")
        code, out, err = run("--task", path)
        self.assertEqual(code, EXIT_UNREADABLE)
        self.assertIn("UNREADABLE:", err)
        self.assertEqual(out, "")

    def test_a_missing_file_is_unreadable(self):
        code, out, err = run("--task", "/nonexistent/task.md")
        self.assertEqual(code, EXIT_UNREADABLE)
        self.assertIn("UNREADABLE:", err)
        self.assertEqual(out, "")

    def test_the_identifier_in_body_prose_is_not_a_phantom_id(self):
        """Only the FIELD can hand a task a phantom owner. A task_identifier quoted in
        `# Progress` is not one, and refusing on it would make the guard fire on files
        it read perfectly — the over-report that gets a check switched off."""
        path = self.write(
            f"---\nclaude_session_id: {REAL}\ntask_identifier: {IDENT}\n---\n"
            f"Progress mentions {IDENT} and {SECOND} in prose.\n"
        )
        code, out, err = run("--task", path)
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(out.split(), [REAL])


class TestDeclaredFieldSet(unittest.TestCase):
    def test_the_declared_field_set_names_both_read_fields(self):
        """`commands/manager-loop.md`'s guard bullet must name the same field set the
        script enforces — the constant is what makes that checkable rather than a
        restatement a `grep` cannot tell from a real one."""
        mod = load()
        self.assertEqual(
            mod.SESSION_ID_FIELDS,
            ("claude_session_id", "metrics_sessions[].session_id"),
        )

    def test_task_identifier_is_declared_a_phantom_field(self):
        mod = load()
        self.assertIn("task_identifier", mod.PHANTOM_FIELDS)
        for field in mod.PHANTOM_FIELDS:
            self.assertNotIn(
                field,
                mod.SESSION_ID_FIELDS,
                "a phantom field must never be in the session-id field set",
            )


if __name__ == "__main__":
    unittest.main()
