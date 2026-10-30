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

import contextlib
import importlib.util
import io
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


def _page(sc_boxes, dod_boxes, status="in_progress", created=None):
    front = "---\n" if status is None else f"---\nstatus: {status}\n"
    if created is not None:
        front += f'created: "{created}"\n'
    body = f"{front}---\n\n# Success Criteria\n\n{sc_boxes}\n\n# Definition of Done\n\n{dod_boxes}\n"
    return body


def _page_no_dod(sc_boxes, status="in_progress", created=None):
    """A page with Success Criteria only — no `# Definition of Done` heading at all."""
    front = "---\n" if status is None else f"---\nstatus: {status}\n"
    if created is not None:
        front += f'created: "{created}"\n'
    return f"{front}---\n\n# Success Criteria\n\n{sc_boxes}\n"


class VerdictTest(unittest.TestCase):
    def _verdict(self, page, kind="goal"):
        names = [n for n, _ in closure.SECTIONS[kind]]
        return closure.verdict_of(
            closure.read_status(page),
            {n: closure.read_section(page, n) for n in names},
            closure.dod_exempt(kind, closure.read_created(page)),
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
        # Post-cutoff: the DoD is required, so its absence still refuses the close.
        page = _page_no_dod("- [x] a", created="2026-10-19")
        self.assertEqual(self._verdict(page), "UNMEASURABLE")

    def test_grandfathered_goal_without_a_dod_closes(self):
        # The vault's grandfathering decision: a goal created before
        # DOD_REQUIRED_AS_OF carries no DoD and is not in violation. Refusing to close
        # it made every grandfathered goal permanently unclosable — closable by every
        # other reader in the vault, unclosable by the one that actually closes it.
        page = _page_no_dod("- [x] a\n- [x] b", created="2026-01-15")
        self.assertEqual(self._verdict(page), "CLOSE")

    def test_goal_without_a_created_date_is_grandfathered(self):
        # Absent metadata is grandfathered too: the rule trusts the document's own
        # `created`, never the filesystem.
        page = _page_no_dod("- [x] a")
        self.assertEqual(self._verdict(page), "CLOSE")

    def test_post_cutoff_goal_without_a_dod_is_unmeasurable(self):
        # The negative control: the cutoff widens nothing for new goals.
        page = _page_no_dod("- [x] a\n- [x] b", created="2026-07-01")
        self.assertEqual(self._verdict(page), "UNMEASURABLE")

    def test_created_on_the_cutoff_date_is_not_grandfathered(self):
        page = _page_no_dod("- [x] a", created="2026-06-26")
        self.assertEqual(self._verdict(page), "UNMEASURABLE")

    def test_grandfathered_goal_with_an_empty_dod_is_unmeasurable(self):
        # Grandfathering covers ABSENCE, never an empty section.
        page = _page("- [x] a", "", created="2026-01-15")
        self.assertEqual(self._verdict(page), "UNMEASURABLE")

    def test_grandfathered_goal_with_open_criteria_never_closes(self):
        page = _page_no_dod("- [x] a\n- [ ] b", created="2026-01-15")
        self.assertEqual(self._verdict(page), "OPEN")

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


def _topic_page(sc_boxes, gate_boxes, status="in_progress"):
    return (
        f"---\nstatus: {status}\n---\n\n# Success Criteria\n\n{sc_boxes}\n\n"
        f"# Completion Gate\n\n{gate_boxes}\n"
    )


class TopicKindTest(unittest.TestCase):
    """A topic closes on SC + `# Completion Gate`, never on a Definition of Done."""

    def _verdict(self, page):
        body = closure.strip_fences(page)
        sections = {n: closure.section_of(body, n, strict) for n, strict in closure.SECTIONS["topic"]}
        return closure.verdict_of(
            closure.read_status(page), sections, closure.dod_exempt("topic", closure.read_created(page))
        )

    def test_grandfathered_topic_without_a_gate_is_still_unmeasurable(self):
        # The grandfathering decision names goals. A topic closes on its
        # `# Completion Gate`, which has no cutoff at any age.
        page = '---\nstatus: in_progress\ncreated: "2020-01-01"\n---\n\n# Success Criteria\n\n- [x] a\n'
        self.assertEqual(self._verdict(page), "UNMEASURABLE")

    def test_topic_closes_on_sc_plus_gate(self):
        self.assertEqual(self._verdict(_topic_page("- [x] a", "- [x] G1\n- [x] G2")), "CLOSE")

    def test_topic_with_open_gate_item_refuses(self):
        self.assertEqual(self._verdict(_topic_page("- [x] a", "- [x] G1\n- [ ] G2")), "OPEN")

    def test_topic_with_no_gate_section_is_unmeasurable(self):
        page = "---\nstatus: in_progress\n---\n\n# Success Criteria\n\n- [x] a\n"
        self.assertEqual(self._verdict(page), "UNMEASURABLE")

    def test_mixed_gate_is_out_of_compliance(self):
        # docs/sweep-output.md:96 — a gate mixing checkboxes with an ordered list is
        # out of compliance, not a second shape; counting the boxes alone would drop
        # the numbered items from the denominator.
        self.assertEqual(self._verdict(_topic_page("- [x] a", "- [x] G1\n1. not a box")), "UNMEASURABLE")

    def test_topic_never_reads_definition_of_done(self):
        # A topic page carrying a DoD must not be closed on it.
        page = _topic_page("- [x] a", "- [x] G1") + "\n# Definition of Done\n\n- [ ] never\n"
        self.assertEqual(self._verdict(page), "CLOSE")


class TaskKindTest(unittest.TestCase):
    def test_task_without_a_dod_is_unmeasurable_at_every_cutoff(self):
        # A task's DoD requirement is not part of the grandfathering decision, so an
        # ancient task with no DoD still refuses the close.
        page = '---\nstatus: in_progress\ncreated: "2020-01-01"\n---\n\n# Success Criteria\n\n- [x] a\n'
        body = closure.strip_fences(page)
        sections = {n: closure.section_of(body, n, s) for n, s in closure.SECTIONS["task"]}
        self.assertEqual(
            closure.verdict_of(
                closure.read_status(page), sections, closure.dod_exempt("task", closure.read_created(page))
            ),
            "UNMEASURABLE",
        )


class CreatedFieldTest(unittest.TestCase):
    """`created` is read as a raw scalar, and the cutoff compares it lexicographically."""

    def test_quoted_date(self):
        self.assertEqual(closure.read_created('---\ncreated: "2026-01-15"\n---\n'), "2026-01-15")

    def test_unquoted_date(self):
        self.assertEqual(closure.read_created("---\ncreated: 2026-01-15\n---\n"), "2026-01-15")

    def test_timestamp_suffix(self):
        self.assertEqual(
            closure.read_created("---\ncreated: 2026-09-20T00:00:00Z\n---\n"), "2026-09-20T00:00:00Z"
        )

    def test_absent_is_none(self):
        self.assertIsNone(closure.read_created("---\nstatus: in_progress\n---\n"))

    def test_created_date_key_is_not_created(self):
        # Tasks carry `created_date:`; that key must not satisfy the `created` read.
        self.assertIsNone(closure.read_created('---\ncreated_date: "2026-01-15"\n---\n'))

    def test_a_non_padded_date_fails_closed(self):
        # Lexicographic comparison is exact only for zero-padded ISO-8601. A
        # non-padded value is refused rather than compared — the strict direction,
        # which can refuse a close but can never permit one.
        self.assertFalse(
            closure.dod_exempt("goal", closure.read_created("---\ncreated: 2026-6-1\n---\n"))
        )

    def test_a_non_iso_date_fails_closed(self):
        # `15.01.2026` and a bare `2026` both sort BELOW the cutoff lexicographically,
        # so an unguarded comparison would exempt them — a false CLOSE, the expensive
        # direction. The shape guard refuses them instead.
        for raw in ("15.01.2026", "2026", "01/15/2026", "Jan 2026", ""):
            self.assertFalse(closure.dod_exempt("goal", raw), raw)

    def test_a_well_shaped_but_invalid_or_trailing_date_fails_closed(self):
        # The guard must match its own contract: an anchored, range-checked ISO date.
        # `2026-00-00` sorts below the cutoff, and `2026-01-15garbage` is not a date at
        # all — both would exempt a malformed page if the shape went unchecked.
        for raw in ("2026-00-00", "2026-13-01", "2026-01-32", "2026-01-15garbage", "2026-01-15 note"):
            self.assertFalse(closure.dod_exempt("goal", raw), raw)

    def test_a_time_suffix_still_parses(self):
        for raw in ("2026-01-15T00:00:00Z", "2026-01-15 12:30", "2026-01-15"):
            self.assertTrue(closure.dod_exempt("goal", raw), raw)

    def test_the_cutoff_boundary_is_exact_to_the_day(self):
        # The lexicographic claim the whole cutoff rests on: a timestamp on the day
        # BEFORE the cutoff is grandfathered, and one ON the cutoff date is not.
        self.assertTrue(closure.dod_exempt("goal", "2026-06-25T23:59:59Z"))
        self.assertFalse(closure.dod_exempt("goal", "2026-06-26T00:00:00Z"))
        self.assertTrue(closure.dod_exempt("goal", "2026-06-25"))
        self.assertFalse(closure.dod_exempt("goal", "2026-06-26"))


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

    def test_h2_heading_is_not_the_section(self):
        # The tree's readers require an exact H1 (`agents/goal-verify.md:56`,
        # `scripts/goal-condition.py:93`), so an H2 fails closed rather than closing.
        page = "---\nstatus: in_progress\n---\n\n## Success Criteria\n\n- [x] a\n\n# Definition of Done\n\n- [x] c\n"
        self.assertIsNone(closure.read_section(page, "Success Criteria"))

    def test_inline_fence_at_line_start_is_unmeasurable(self):
        page = "---\nstatus: in_progress\n---\n\n# Success Criteria\n\n```\n- [x] a\n\n# Definition of Done\n\n- [x] c\n"
        self.assertIsNone(closure.strip_fences(page))

    def test_mismatched_fence_markers_are_unmeasurable(self):
        page = "---\nstatus: in_progress\n---\n\n# Success Criteria\n\n~~~\n- [x] a\n```\n\n- [x] b\n"
        self.assertIsNone(closure.strip_fences(page))


class AbsentStatusTest(unittest.TestCase):
    def test_a_page_with_no_status_is_closable_on_evidence(self):
        # Pinned intent: closure follows the evidence, and the verb re-checks its own
        # gate. A page that never carried a `status:` is closable when both sections
        # are measurable and fully ticked.
        page = "---\nphase: execution\n---\n\n# Success Criteria\n\n- [x] a\n\n# Definition of Done\n\n- [x] c\n"
        sections = {n: closure.read_section(page, n) for n in ("Success Criteria", "Definition of Done")}
        self.assertEqual(closure.verdict_of(closure.read_status(page), sections), "CLOSE")


class CliTest(unittest.TestCase):
    def _run(self, page):
        return self._run_capture(page)[0]

    def _run_capture(self, page):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write(page)
            path = handle.name
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = closure.main(["goal-closure-read.py", path])
        finally:
            os.unlink(path)
        return code, buf.getvalue()

    def test_grandfathered_goal_prints_the_absent_line(self):
        # The line shape is load-bearing: it separates a close the rule now PERMITS
        # from a section the reader failed to find. Collapsing the two — or dropping
        # the exemption branch in main — leaves every verdict test above green.
        page = _page_no_dod("- [x] a", created="2026-01-15")
        code, out = self._run_capture(page)
        self.assertEqual(code, 0)
        self.assertIn("ABSENT — grandfathered", out)
        self.assertNotIn("MISSING", out)
        self.assertIn("VERDICT: CLOSE", out)

    def test_post_cutoff_goal_prints_missing(self):
        page = _page_no_dod("- [x] a", created="2026-10-19")
        code, out = self._run_capture(page)
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)
        self.assertNotIn("grandfathered", out)
        self.assertIn("VERDICT: UNMEASURABLE", out)

    def test_an_absent_created_prints_the_absent_arm(self):
        # The `created or 'absent'` arm is the line a page with no `created` actually
        # prints; without this it is covered only at the verdict_of level.
        page = _page_no_dod("- [x] a")
        code, out = self._run_capture(page)
        self.assertEqual(code, 0)
        self.assertIn("created absent", out)

    def test_a_page_with_unreadable_frontmatter_is_not_grandfathered(self):
        # `read_created` returns None for an absent field AND for a page whose
        # frontmatter never parsed. Without the parse guard the second reads as
        # "no created" and closes a page the reader could not read at all.
        page = "\n# Success Criteria\n\n- [x] a\n"
        code, out = self._run_capture(page)
        self.assertEqual(code, 1)
        self.assertIn("VERDICT: UNMEASURABLE", out)

    def test_a_bom_page_is_read_and_still_honours_the_cutoff(self):
        page = "﻿" + _page_no_dod("- [x] a", created="2026-01-15")
        code, out = self._run_capture(page)
        self.assertEqual(code, 0)
        self.assertIn("VERDICT: CLOSE", out)

    def test_an_h2_dod_heading_is_not_grandfathered(self):
        # The section reader wants an exact H1. An h2 DoD holding an OPEN box must not
        # become a CLOSE merely because the heading shape was not recognised.
        page = (
            '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
            "# Success Criteria\n\n- [x] a\n\n## Definition of Done\n\n- [ ] never\n"
        )
        code, out = self._run_capture(page)
        self.assertEqual(code, 1)
        self.assertIn("VERDICT: UNMEASURABLE", out)

    def test_a_suffixed_dod_heading_is_not_grandfathered(self):
        page = (
            '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
            "# Success Criteria\n\n- [x] a\n\n# Definition of Done (optional)\n\n- [ ] never\n"
        )
        code, out = self._run_capture(page)
        self.assertEqual(code, 1)
        self.assertIn("VERDICT: UNMEASURABLE", out)

    def test_an_indented_atx_dod_heading_is_not_grandfathered(self):
        # CommonMark/Obsidian render an ATX heading indented by up to three spaces.
        page = (
            '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
            "# Success Criteria\n\n- [x] a\n\n  # Definition of Done\n\n- [ ] never\n"
        )
        code, out = self._run_capture(page)
        self.assertEqual(code, 1)
        self.assertIn("VERDICT: UNMEASURABLE", out)

    def test_a_setext_dod_heading_is_not_grandfathered(self):
        # A setext heading is a heading to Obsidian and invisible to `section_of`.
        page = (
            '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
            "# Success Criteria\n\n- [x] a\n\nDefinition of Done\n------------------\n\n- [ ] never\n"
        )
        code, out = self._run_capture(page)
        self.assertEqual(code, 1)
        self.assertIn("VERDICT: UNMEASURABLE", out)

    def test_a_suffixed_setext_dod_after_tasks_is_not_grandfathered(self):
        # The layout that slipped through every earlier guard: the DoD sits AFTER
        # `# Tasks`, so its boxes are not absorbed into the SC tally, and its heading is
        # a suffixed setext — invisible to `section_of` and to a heading regex.
        page = (
            '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
            "# Success Criteria\n\n- [x] a\n\n# Tasks\n\n- [x] b\n\n"
            "Definition of Done (optional)\n-----------------------------\n\n- [ ] never\n"
        )
        code, out = self._run_capture(page)
        self.assertEqual(code, 1)
        self.assertNotIn("grandfathered", out)

    def test_a_blockquoted_or_bold_dod_heading_is_not_grandfathered(self):
        for heading in ("> # Definition of Done", "# **Definition of Done**", "Definition of Done"):
            page = (
                '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
                f"# Success Criteria\n\n- [x] a\n\n# Tasks\n\n- [x] b\n\n{heading}\n\n- [ ] never\n"
            )
            code, out = self._run_capture(page)
            self.assertEqual(code, 1, heading)
            self.assertNotIn("grandfathered", out, heading)

    def test_a_whitespace_variant_dod_heading_is_not_grandfathered(self):
        # `# Definition  of  Done` and a non-breaking space are the same heading to a
        # renderer, and `section_of` treats the line as a boundary either way.
        for heading in ("# Definition  of  Done", "# Definition of Done"):
            page = (
                '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
                f"# Success Criteria\n\n- [x] a\n\n# Tasks\n\n- [x] b\n\n{heading}\n\n- [ ] never\n"
            )
            code, out = self._run_capture(page)
            self.assertEqual(code, 1, heading)
            self.assertNotIn("grandfathered", out, heading)

    def test_a_list_marker_dod_heading_is_not_grandfathered(self):
        # A bulleted or numbered DoD label is one more spelling of the heading.
        for heading in ("- Definition of Done", "1. Definition of Done", "- **Definition of Done**"):
            page = (
                '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
                f"# Success Criteria\n\n- [x] a\n\n# Tasks\n\n- [x] b\n\n{heading}\n\n- [ ] never\n"
            )
            code, out = self._run_capture(page)
            self.assertEqual(code, 1, heading)
            self.assertNotIn("grandfathered", out, heading)

    def test_a_grandfathered_goal_with_an_unterminated_fence_is_unmeasurable(self):
        # The absence guard is vacuous when the body cannot be read, so this path must
        # fail closed on the Success Criteria section alone — and never print the
        # grandfathered line for a page whose fences are broken.
        page = (
            '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
            "# Success Criteria\n\n- [x] a\n\n```\n- [x] example\n"
        )
        code, out = self._run_capture(page)
        self.assertEqual(code, 1)
        self.assertIn("VERDICT: UNMEASURABLE", out)
        self.assertNotIn("grandfathered", out)

    def test_a_prose_mention_of_the_section_does_not_block_a_grandfathered_close(self):
        # The guard matches a line that BEGINS with the section name, so a sentence
        # about it does not over-refuse a goal that genuinely has no DoD.
        page = (
            '---\nstatus: in_progress\ncreated: "2026-01-15"\n---\n\n'
            "# Success Criteria\n\n- [x] a\n\nThis goal predates the Definition of Done requirement.\n"
        )
        code, out = self._run_capture(page)
        self.assertEqual(code, 0)
        self.assertIn("VERDICT: CLOSE", out)

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


class TaskVerbGateTest(unittest.TestCase):
    """A task's CLOSE must predict `vault-cli task complete`, which refuses page-wide."""

    def _run(self, page, kind):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write(page)
            path = handle.name
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = closure.main(["goal-closure-read.py", "--kind", kind, path])
        finally:
            os.unlink(path)
        return code, buf.getvalue()

    def test_open_tasks_box_keeps_a_task_open(self):
        # The 2026-10-30 case: SC + DoD all ticked, one open `# Tasks` box. The verb
        # refuses it, so the reader must not offer it.
        page = _page("- [x] a", "- [x] b") + "\n# Tasks\n\n- [ ] e2e round\n"
        code, out = self._run(page, "task")
        self.assertEqual(code, 1)
        self.assertIn("Whole page (task complete's gate): 1 open", out)
        self.assertIn("VERDICT: OPEN", out)

    def test_in_progress_box_anywhere_keeps_a_task_open(self):
        page = _page("- [x] a", "- [x] b") + "\n# Tasks\n\n- [/] half done\n"
        self.assertIn("VERDICT: OPEN", self._run(page, "task")[1])

    def test_fully_ticked_task_still_closes(self):
        page = _page("- [x] a", "- [x] b") + "\n# Tasks\n\n- [x] done\n"
        code, out = self._run(page, "task")
        self.assertEqual(code, 0)
        self.assertIn("Whole page (task complete's gate): 0 open", out)
        self.assertIn("VERDICT: CLOSE", out)

    def test_fenced_open_box_blocks_a_task_because_the_verb_counts_it(self):
        # The verb strips no fences, so neither may the mirror of its gate.
        page = _page("- [x] a", "- [x] b") + "\n# Notes\n\n```\n- [ ] example\n```\n"
        self.assertIn("VERDICT: OPEN", self._run(page, "task")[1])

    def test_goal_ignores_the_tasks_section(self):
        # `vault-cli goal complete` checks linked task status, never boxes.
        page = _page("- [x] a", "- [x] b") + "\n# Tasks\n\n- [ ] [[Some Task]]\n"
        code, out = self._run(page, "goal")
        self.assertEqual(code, 0)
        self.assertNotIn("Whole page", out)


class DivergenceClassTest(unittest.TestCase):
    """Every class where `_BOX` and `_VERB_BOX` disagree is pinned on BOTH sides.

    The reader's own `_BOX` and the verb gate's `_VERB_BOX` are deliberately different
    regexes, and that divergence is the whole reason `verb_open_boxes` exists. Each test
    below asserts the count on both sides, so an edit to either regex fails here instead
    of silently moving the verdict away from `vault-cli task complete`. Asserting only the
    verb's count — the first version of these tests — leaves an edit to `_BOX` unpinned.

    The classes are derived from the two literals rather than hand-picked: `_BOX` takes
    `[-*+]` with `[ \\t]*` before the box and any marker char, and needs no text after it;
    `_VERB_BOX` takes `[-*]` with exactly one space and `[ x/]`, and requires `(.+)`.

    The box sits under `# Tasks`, outside SC/DoD, on purpose. `_VERB_BOX`'s shapes are a
    strict subset of `_BOX`'s, so the reader is always at least as strict as the verb and
    can never report CLOSE for a page the verb refuses — the divergence is one-directional
    and safe. What these tests pin is therefore the two *regexes*, not a verdict flip; a
    fixture asserting a verdict would only re-state that subset property.
    """

    @staticmethod
    def _reader_open(text):
        """Open boxes by the reader's own `_BOX` — the counterpart of `verb_open_boxes`."""
        return sum(1 for marker in closure._BOX.findall(text) if marker not in closure._DONE)

    def _assert_diverges(self, box):
        page = _page("- [x] a", "- [x] b") + f"\n# Tasks\n\n{box}\n"
        self.assertEqual(self._reader_open(page), 1, f"{box!r} must read OPEN to _BOX")
        self.assertEqual(
            closure.verb_open_boxes(page), 0, f"{box!r} must NOT be counted by _VERB_BOX"
        )

    def test_plus_bullet(self):
        self._assert_diverges("+ [ ] plus")

    def test_separator_wider_than_one_space(self):
        self._assert_diverges("-  [ ] two spaces")

    def test_tab_separator(self):
        self._assert_diverges("-\t[ ] tab")

    def test_marker_outside_the_verb_class(self):
        # `_BOX` takes any marker char; `_VERB_BOX` takes only `[ x/]`.
        for box in ("- [?] question", "- [o] circle"):
            with self.subTest(box=box):
                self._assert_diverges(box)

    def test_no_text_after_the_box(self):
        # `_VERB_BOX` requires `(.+)` after the box; `_BOX` requires nothing.
        for box in ("- [ ]", "- [ ] ", "- [ ]glued"):
            with self.subTest(box=box):
                self._assert_diverges(box)


class CutoffDocumentationTest(unittest.TestCase):
    def test_the_closure_step_restates_the_cutoff_date(self):
        # DOD_REQUIRED_AS_OF is deliberately restated in the step doc. Nothing else
        # enforces the agreement, and a partial edit silently re-breaks closure —
        # the exact failure this change repairs.
        step = os.path.join(os.path.dirname(_SCRIPTS), "commands", "steps", "goal-closure.md")
        with open(step, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("DOD_REQUIRED_AS_OF", text)
        self.assertIn(closure.DOD_REQUIRED_AS_OF, text)


if __name__ == "__main__":
    unittest.main()
