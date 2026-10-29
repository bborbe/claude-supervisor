#!/usr/bin/env python3
"""Tests for scripts/goal-closure-read.py.

Four properties, each of which has been wrong in this tree in some form:

1. **An empty or unrecognised section is UNMEASURABLE, never CLOSE.** The predicate
   is "every box reads `[x]`", which a section holding no box satisfies vacuously —
   so the empty-`# Definition of Done` page used to reach the close verb.

2. **Every bullet and tick form is counted.** `-`/`*`/`+` and `[x]`/`[X]` are the
   same thing to a renderer; the first regex read a `* [ ]` page as "0 open, 0 done".

3. **An unrecognised marker is OPEN, not done.** `- [-]` is a box this reader does
   not understand, and guessing "done" closes a page someone left open.

4. **A terminal page is TERMINAL.** The close verb writes `status: completed` and
   leaves the boxes ticked, so without this guard the same page is re-closed on
   every later tick.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


closure = _load("goal_closure_read", "goal-closure-read.py")


def _page(sc_boxes, dod_boxes, status="in_progress"):
    front = "---\n" if status is None else f"---\nstatus: {status}\n"
    body = f"{front}---\n\n# Success Criteria\n\n{sc_boxes}\n\n# Definition of Done\n\n{dod_boxes}\n"
    return body


class VerdictTest(unittest.TestCase):
    def _verdict(self, page):
        return closure.verdict_of(
            closure.read_status(page),
            {n: closure.read_section(page, n) for n in ("Success Criteria", "Definition of Done")},
        )

    def test_all_boxes_ticked_closes(self):
        self.assertEqual(self._verdict(_page("- [x] a\n- [x] b", "- [x] c")), "CLOSE")

    def test_one_open_box_refuses(self):
        self.assertEqual(self._verdict(_page("- [x] a\n- [ ] b", "- [x] c")), "OPEN")

    def test_slash_marker_is_open(self):
        self.assertEqual(self._verdict(_page("- [x] a", "- [/] c")), "OPEN")

    def test_empty_section_is_unmeasurable_not_complete(self):
        self.assertEqual(self._verdict(_page("- [x] a", "")), "UNMEASURABLE")

    def test_missing_section_is_unmeasurable(self):
        page = "---\nstatus: in_progress\n---\n\n# Success Criteria\n\n- [x] a\n"
        self.assertEqual(self._verdict(page), "UNMEASURABLE")

    def test_star_and_plus_bullets_are_counted(self):
        self.assertEqual(self._verdict(_page("* [x] a\n+ [x] b", "* [x] c")), "CLOSE")

    def test_uppercase_x_is_done(self):
        self.assertEqual(self._verdict(_page("- [X] a", "- [X] c")), "CLOSE")

    def test_unrecognised_marker_is_open(self):
        self.assertEqual(self._verdict(_page("- [x] a", "- [-] c")), "OPEN")

    def test_fenced_example_does_not_make_a_section_measurable(self):
        # A fenced `- [x]` in an otherwise empty section is an EXAMPLE. Counting it
        # would turn (0, 0) into (1, 0) and let the page reach the close verb.
        page = _page("- [x] a", "```\n- [x] example only\n```")
        self.assertEqual(self._verdict(page), "UNMEASURABLE")

    def test_fenced_example_does_not_block_a_real_close(self):
        page = _page("- [x] a\n\n```\n- [ ] example only\n```", "- [x] c")
        self.assertEqual(self._verdict(page), "CLOSE")

    def test_heading_inside_a_fence_does_not_truncate_the_section(self):
        # The section used to be bounded on RAW text, so a `# ` line inside a fenced
        # example ended it early and every later box went uncounted — losing an OPEN
        # box and reporting CLOSE.
        page = _page("- [x] a\n\n```\n# shell comment\n```\n\n- [ ] b", "- [x] c")
        self.assertEqual(self._verdict(page), "OPEN")

    def test_unterminated_fence_is_unmeasurable(self):
        # A fence that never closes leaves its content in place, so a fenced `- [x]`
        # in an empty section would read (1, 0) and stop being UNMEASURABLE.
        page = _page("- [x] a", "```\n- [x] example only\n")
        self.assertEqual(self._verdict(page), "UNMEASURABLE")

    def test_crlf_page_keeps_the_terminal_guard(self):
        # A CRLF page that loses its frontmatter re-closes a finished page every tick.
        page = _page("- [x] a", "- [x] c", status="completed").replace("\n", "\r\n")
        self.assertEqual(self._verdict(page), "TERMINAL")

    def test_completed_page_is_terminal(self):
        self.assertEqual(self._verdict(_page("- [x] a", "- [x] c", status="completed")), "TERMINAL")

    def test_aborted_page_is_terminal(self):
        self.assertEqual(self._verdict(_page("- [x] a", "- [x] c", status="aborted")), "TERMINAL")


class SectionTest(unittest.TestCase):
    def test_absent_section_is_none(self):
        self.assertIsNone(closure.read_section("---\n---\n\n# Success Criteria\n\n- [x] a\n", "Definition of Done"))

    def test_present_empty_section_is_zero_zero(self):
        self.assertEqual(
            closure.read_section("---\n---\n\n# Definition of Done\n\n", "Definition of Done"), (0, 0)
        )

    def test_tasks_section_is_not_counted(self):
        page = "---\n---\n\n# Success Criteria\n\n- [x] a\n\n# Tasks\n\n- [ ] not mine\n"
        self.assertEqual(closure.read_section(page, "Success Criteria"), (1, 0))

    def test_quoted_status_is_unquoted(self):
        self.assertEqual(closure.read_status('---\nstatus: "next"\n---\n'), "next")

    def test_absent_status_is_none(self):
        self.assertIsNone(closure.read_status("---\nphase: execution\n---\n"))


class CliTest(unittest.TestCase):
    def _run(self, page):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write(page)
            path = handle.name
        try:
            return closure.main(["goal-closure-read.py", path])
        finally:
            os.unlink(path)

    def test_close_exits_zero(self):
        self.assertEqual(self._run(_page("- [x] a", "- [x] c")), 0)

    def test_open_exits_one(self):
        self.assertEqual(self._run(_page("- [ ] a", "- [x] c")), 1)

    def test_terminal_exits_one(self):
        # The step says the reader "exits 0 only on CLOSE", and it distinguishes
        # TERMINAL from CLOSE by the printed verdict. A regression returning 0 here
        # would still pass every verdict_of assertion above.
        self.assertEqual(self._run(_page("- [x] a", "- [x] c", status="completed")), 1)

    def test_unreadable_page_exits_two(self):
        self.assertEqual(closure.main(["goal-closure-read.py", "/nonexistent/page.md"]), 2)

    def test_wrong_arity_exits_two(self):
        self.assertEqual(closure.main(["goal-closure-read.py"]), 2)


if __name__ == "__main__":
    unittest.main()
