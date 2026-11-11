#!/usr/bin/env python3
"""Tests for scripts/next-focus-answer.py — the answer's consumer.

Each case guards a claim the task file makes to the operator, and each is written
to fail on a plausible wrong implementation rather than on a broken one:

  * **`add it to the plan` writes BOTH weekly-note artifacts.** The Picked row is
    the one a human reads; the `### N.` block is the only one `parse_weekly`
    parses. Measured 2026-10-27: the row alone did NOT stop the re-post, the
    block did — so a build that writes only the row must fail here.
  * **a second answer adds neither a second block nor a second row.** Both
    keyings (by card key and by title) get this wrong in opposite directions.
  * **`leave it out` is durable AND visible.** The dismissal must drop the row
    from `proposed` *and* appear in `dismissed[]` — absence alone is also what
    the 3-per-round cap produces, so an absence-only assertion would pass on a
    build that never wrote the record.
  * **an unhandled answer is not consumed.** Silently mapping an unknown option
    to `leave it out` would dismiss work the operator never declined.
  * **an unreadable store is a REFUSAL, never "nothing to consume".** The two
    must not render the same.
"""
import importlib.util
import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
ANSWER = SCRIPTS / "next-focus-answer.py"
NEXT_FOCUS = SCRIPTS / "next-focus.py"

WEEK = "2026-W46"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


answer = load(ANSWER, "next_focus_answer")
nf = load(NEXT_FOCUS, "next_focus_under_test")

CARD_KEY = "next-focus:testvault:task:my-task-abcdef12"
ITEM_ID = "a" * 32
TASK_NAME = "My Task"

def weekly_text(blocks=1):
    lines = [
        "---", "page_type: weekly", "---", "",
        "# Weekly", "",
        "## ✅ Picked (1)", "",
        "| # | Item | Why it made the cut |",
        "| --- | --- | --- |",
        "| 1 | 🔧 Something else | P1, added earlier |",
        "", "# Tasks", "",
    ]
    for number in range(1, blocks + 1):
        lines += [f"### {number}. 🔧 Existing {number}", "",
                  f"**Task:** [[Existing {number}]]", "", "**What:** a thing.", ""]
    return "\n".join(lines) + "\n"


def make_vault(root, blocks=1):
    vault = pathlib.Path(root) / "vault"
    weekly = vault / "60 Periodic Notes" / "Weekly" / f"{WEEK}.md"
    weekly.parent.mkdir(parents=True, exist_ok=True)
    weekly.write_text(weekly_text(blocks), encoding="utf-8")
    tasks = vault / "25 Tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{TASK_NAME}.md").write_text(
        "---\nstatus: next\nphase: planning\npriority: \"1\"\n---\n\n# Impact\n\nbody\n",
        encoding="utf-8",
    )
    return vault, weekly


def card(answer_value, payload=None, state="answered", item_id=ITEM_ID, key=CARD_KEY):
    return {
        "item_id": item_id,
        "dedup_key": key,
        "state": state,
        "answered_at": "2026-11-09T10:00:00Z",
        "created_at": "2026-11-09T09:00:00Z",
        "answer": {"value": answer_value},
        "payload": payload if payload is not None else (
            f"{TASK_NAME} — priority: 1, in no current plan — /x/25 Tasks/My Task.md:1"
        ),
    }


class ConsumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault, self.weekly = make_vault(self.tmp.name)
        self.dismissed = pathlib.Path(self.tmp.name) / "dismissed"
        self.consumed = pathlib.Path(self.tmp.name) / "consumed"
        # The key `next-focus.py` would compute for this row — the consumer records the
        # card's own key, so a fixture key that differs from the reader's would test
        # nothing about the join between them.
        self.card_key = nf.card_key(str(self.vault), "task", TASK_NAME)

    def tearDown(self):
        self.tmp.cleanup()

    def run_consume(self, items):
        consumed, notes, _pending = answer.consume_items(
            items, str(self.vault), "25 Tasks",
            str(self.dismissed), str(self.consumed), NOW,
        )
        return consumed, notes

    def card(self, answer_value, **kwargs):
        kwargs.setdefault("key", self.card_key)
        return card(answer_value, **kwargs)

    # ---------------------------------------------------------------- add path

    def test_add_writes_both_artifacts(self):
        consumed, _ = self.run_consume([self.card("add it to the plan")])
        self.assertEqual(len(consumed), 1)
        self.assertEqual(consumed[0]["action"], "added")

        text = self.weekly.read_text(encoding="utf-8")
        # The block is what `parse_weekly` reads — the load-bearing artifact.
        parsed = {row["title"]: row for row in nf.parse_weekly(text)}
        self.assertTrue(
            any(TASK_NAME in row["names"] for row in parsed.values()),
            "no `### N.` block carries the task — the reader would still propose it",
        )
        # The row is what the operator reads.
        self.assertIn(f"| 2 | 📌 {TASK_NAME} |", text)
        self.assertIn("## ✅ Picked (2)", text)

    def test_add_is_idempotent_by_task_name(self):
        self.run_consume([self.card("add it to the plan")])
        first = self.weekly.read_text(encoding="utf-8")
        # A second, differently-keyed card for the SAME task must not duplicate it.
        second_card = self.card("add it to the plan", item_id="b" * 32)
        second_card["dedup_key"] = "next-focus:testvault:task:my-task-99999999"
        self.run_consume([second_card])
        second = self.weekly.read_text(encoding="utf-8")

        self.assertEqual(first.count(f"| 2 | 📌 {TASK_NAME} |"), 1)
        self.assertEqual(second.count(TASK_NAME), first.count(TASK_NAME))

    def test_same_card_is_not_consumed_twice(self):
        self.run_consume([self.card("add it to the plan")])
        consumed, _ = self.run_consume([self.card("add it to the plan")])
        self.assertEqual(consumed, [], "the same card was consumed twice")

    def test_leave_after_add_leaves_the_plan_row_intact(self):
        """A dismissal must not undo a plan entry already written.

        Both keyings get this wrong — a title-keyed dismissal deletes the row it
        matches, and a card-keyed one that rewrites the note can drop it by accident.
        """
        self.run_consume([self.card("add it to the plan")])
        before = self.weekly.read_text(encoding="utf-8")
        self.assertIn(f"| 2 | 📌 {TASK_NAME} |", before)

        later = self.card("leave it out", item_id="c" * 32)
        later["dedup_key"] = "next-focus:vault:task:my-task-99999999"
        consumed, _ = self.run_consume([later])

        self.assertEqual(consumed[0]["action"], "dismissed")
        after = self.weekly.read_text(encoding="utf-8")
        self.assertEqual(before, after, "the dismissal rewrote the weekly note")
        self.assertEqual(after.count(f"| 2 | 📌 {TASK_NAME} |"), 1)

    def test_row_numbering_comes_from_the_table_not_the_blocks(self):
        """The real weekly shape carries far more blocks than Picked rows.

        W44 has 72 `### N.` blocks and 42 Picked rows, so reusing the block number
        appends `| 73 |` after row 42 and rewrites the heading to `(73)`.
        """
        self.weekly.write_text(weekly_text(blocks=5), encoding="utf-8")
        self.run_consume([self.card("add it to the plan")])
        text = self.weekly.read_text(encoding="utf-8")

        self.assertIn("## ✅ Picked (2)", text)
        self.assertIn(f"| 2 | 📌 {TASK_NAME} |", text)
        self.assertNotIn("## ✅ Picked (6)", text)
        self.assertNotIn("| 6 |", text)

    def json_read(self):
        out = subprocess.run(
            [sys.executable, str(NEXT_FOCUS), "--vault", str(self.vault),
             "--now", "2026-11-09", "--live", "0", "--target", "1",
             "--state-dir", str(self.dismissed), "--json"],
            capture_output=True, text=True, check=True,
        )
        return json.loads(out.stdout)

    def test_add_removes_the_row_from_proposed(self):
        """The measured case: an added row must leave `proposed`, not merely gain a row."""
        self.assertIn(TASK_NAME, [row["name"] for row in self.json_read()["proposed"]])
        self.run_consume([self.card("add it to the plan")])
        self.assertNotIn(TASK_NAME, [row["name"] for row in self.json_read()["proposed"]])

    def test_an_alert_add_also_records_a_dismissal(self):
        """An alert name can never resolve to a page, so its plan block alone is inert.

        `plan_candidates` drops a name with no page behind it, so the row would stay
        unplanned and re-propose while the alert still fires — the operator's answer
        would be a silent no-op.
        """
        alert = self.card(
            "add it to the plan",
            payload=("prod alert firing — alert firing, in no current plan — "
                     "prometheus-alerts.sh:1"),
            key="next-focus:vault:alert:prod-alert-firing-12345678",
        )
        consumed, notes = self.run_consume([alert])
        self.assertEqual(consumed[0]["action"], "added")
        self.assertTrue(
            any(self.dismissed.glob("*.json")),
            "an alert `add` left no durable record — it will re-propose",
        )
        self.assertTrue(any("alert row" in note for note in notes))

    def test_an_empty_picked_table_gets_its_row_after_the_header(self):
        """Real notes put a BLANK line between the heading and the header.

        Skipping only `|` lines from `heading + 1` exits on that blank and inserts the
        row above the header, outside the table — and the misplaced row then matches
        `ROW_RE`, so every later add compounds it.
        """
        self.weekly.write_text(
            "# Weekly\n\n## ✅ Picked (0)\n\n"
            "| # | Item | Why |\n| --- | --- | --- |\n\n"
            "# Tasks\n\n### 1. 🔧 E\n\n**Task:** [[E]]\n\n",
            encoding="utf-8",
        )
        self.run_consume([self.card("add it to the plan")])
        text = self.weekly.read_text(encoding="utf-8")

        header = text.index("| # | Item | Why |")
        separator = text.index("| --- | --- | --- |")
        row = text.index(f"| 1 | 📌 {TASK_NAME} |")
        self.assertGreater(row, separator, "the row landed above the table header")
        self.assertGreater(separator, header)
        self.assertIn("## ✅ Picked (1)", text)

    def test_a_prefix_name_does_not_count_as_an_existing_row(self):
        """The row check is scoped to the table's item cell and compared whole."""
        self.weekly.write_text(
            weekly_text().replace(
                "| 1 | 🔧 Something else | P1, added earlier |",
                "| 1 | 🔧 My Task Extended | P1, added earlier |"),
            encoding="utf-8",
        )
        consumed, _ = self.run_consume([self.card("add it to the plan")])

        self.assertIsNotNone(consumed[0]["row"], "a prefix name suppressed the new row")
        self.assertIn(f"| 2 | 📌 {TASK_NAME} |", self.weekly.read_text(encoding="utf-8"))

    def test_an_alert_add_leaves_the_weekly_note_alone(self):
        """An alert's block can never resolve to a page — writing one is pure noise."""
        before = self.weekly.read_text(encoding="utf-8")
        alert = self.card(
            "add it to the plan",
            payload=("prod alert firing — alert firing, in no current plan — "
                     "prometheus-alerts.sh:1"),
            key="next-focus:vault:alert:prod-alert-firing-12345678",
        )
        self.run_consume([alert])

        self.assertEqual(before, self.weekly.read_text(encoding="utf-8"))
        self.assertTrue(any(self.dismissed.glob("*.json")))

    def test_render_never_prints_a_none_field(self):
        rendered = answer.render(
            [{"action": "added", "name": "X", "item_id": "a" * 32,
              "block": 5, "row": None}], [], 0, 0)
        self.assertIn("block 5", rendered)
        self.assertNotIn("None", rendered)

    def test_a_task_without_a_phase_has_no_empty_code_span(self):
        """`status: backlog` with no `phase:` is the vault's normal P1 shape."""
        (self.vault / "25 Tasks" / f"{TASK_NAME}.md").write_text(
            "---\nstatus: backlog\npriority: \"1\"\n---\n\nbody\n", encoding="utf-8")
        self.run_consume([self.card("add it to the plan")])
        text = self.weekly.read_text(encoding="utf-8")

        self.assertIn("`backlog`.", text)
        self.assertNotIn("`backlog` /", text)

    def test_a_goal_row_records_its_state(self):
        goals = self.vault / "24 Goals"
        goals.mkdir(parents=True, exist_ok=True)
        (goals / "Some Goal.md").write_text(
            "---\nstatus: hold\nblocked_by: bug\n---\n\nbody\n", encoding="utf-8")
        card = self.card(
            "add it to the plan",
            payload="Some Goal — blocked on bug, in no current plan — /x:1",
            key="next-focus:vault:goal:some-goal-abcdef12",
        )
        self.run_consume([card])

        self.assertIn("`hold`", self.weekly.read_text(encoding="utf-8"))

    def test_a_picked_heading_with_no_table_writes_no_row(self):
        self.weekly.write_text(
            "# Weekly\n\n## ✅ Picked (0)\n\nSome prose, no table here.\n\n"
            "# Tasks\n\n### 1. 🔧 E\n\n**Task:** [[E]]\n\n",
            encoding="utf-8",
        )
        consumed, notes = self.run_consume([self.card("add it to the plan")])
        text = self.weekly.read_text(encoding="utf-8")

        self.assertNotIn(f"| 1 | 📌 {TASK_NAME} |", text)
        self.assertTrue(any("row not written" in note for note in notes))
        self.assertIsNotNone(consumed[0]["block"], "the block should still be written")

    def test_a_shell_quoted_payload_yields_the_clean_name(self):
        """The store sometimes keeps the `shlex.quote` form of the payload.

        Measured 2026-11-10: 33 of 110 live blocks carried the leading quote or the
        `'"'"'` escape, named no page, and their rows kept re-proposing.
        """
        quoted = shlex.quote(f"{TASK_NAME} — priority: 1, in no current plan — /x:1")
        self.assertNotEqual(quoted, f"{TASK_NAME} — priority: 1, in no current plan — /x:1")

        consumed, _ = self.run_consume([self.card("add it to the plan", payload=quoted)])
        text = self.weekly.read_text(encoding="utf-8")

        self.assertEqual(consumed[0]["name"], TASK_NAME)
        self.assertIn(f"**Task:** [[{TASK_NAME}]]", text)
        self.assertNotIn(f"📌 '{TASK_NAME}", text)

    def test_an_unresolvable_name_writes_no_block(self):
        """A payload that is not `name — why — file` must never become a plan row."""
        consumed, notes = self.run_consume([
            self.card("add it to the plan",
                      payload="proposed for this week plan (see next-focus reading)"),
        ])

        self.assertEqual(consumed, [])
        self.assertTrue(any("refusing to write" in note for note in notes))
        self.assertNotIn("proposed for this week plan",
                         self.weekly.read_text(encoding="utf-8"))

    def test_unquote_handles_the_escape_form_and_a_raw_apostrophe(self):
        """The `'"'"'` form is what a naive `strip("'")` gets wrong."""
        raw = "A Headless Worker's Vault Write — priority: 1 — /x:1"
        escaped = shlex.quote(raw)
        self.assertIn("'\"'\"'", escaped, "the fixture did not produce the escape form")

        self.assertEqual(answer.unquote_payload(escaped), raw)
        # An UNQUOTED payload with an apostrophe makes shlex.split raise.
        self.assertEqual(answer.unquote_payload(raw), raw)

    def test_an_apostrophe_name_is_written_clean(self):
        """The live shape: 4 of 33 bad blocks carried this escape into the name."""
        name = "A Headless Worker's Vault Write"
        (self.vault / "25 Tasks" / f"{name}.md").write_text(
            "---\nstatus: next\nphase: planning\npriority: \"1\"\n---\n\nbody\n",
            encoding="utf-8",
        )
        quoted = shlex.quote(f"{name} — priority: 1, in no current plan — /x:1")
        self.run_consume([self.card("add it to the plan", payload=quoted)])

        text = self.weekly.read_text(encoding="utf-8")
        self.assertIn(f"**Task:** [[{name}]]", text)
        self.assertNotIn("\"'\"", text)

    def test_a_refusal_is_recorded_so_it_does_not_repeat(self):
        """A permanent refusal must drain `pending`, or it re-renders every tick."""
        card = self.card("add it to the plan",
                         payload="proposed for this week plan (see next-focus reading)")
        args = ([card], str(self.vault), "25 Tasks",
                str(self.dismissed), str(self.consumed), NOW)

        consumed1, notes1, pending1 = answer.consume_items(*args)
        self.assertEqual(consumed1, [])
        self.assertTrue(any("refusing to write" in note for note in notes1))
        # `pending` counts cards still awaiting consumption AFTER the run — the refusal
        # is a disposition, so it drains. Before the fix it stayed 1 forever.
        self.assertEqual(pending1, 0, "the refusal was not recorded — pending never drains")

        consumed2, notes2, pending2 = answer.consume_items(*args)
        self.assertEqual(consumed2, [])
        self.assertEqual(pending2, 0)
        self.assertEqual(notes2, [], "the refusal note repeated on the next tick")

    def test_a_transient_refusal_is_not_recorded(self):
        """A fresh week with no `### N.` blocks must not burn the operator's answer.

        Recording a transient failure would drop the `add it to the plan` answer for
        good, when the next tick could succeed.
        """
        self.weekly.write_text("# Weekly\n\nno blocks here\n", encoding="utf-8")
        args = ([self.card("add it to the plan")], str(self.vault), "25 Tasks",
                str(self.dismissed), str(self.consumed), NOW)

        _, notes1, pending1 = answer.consume_items(*args)
        self.assertTrue(any("refused" in note for note in notes1))
        self.assertEqual(pending1, 1, "a transient refusal must stay pending")

        _, notes2, pending2 = answer.consume_items(*args)
        self.assertTrue(any("refused" in note for note in notes2),
                        "the transient refusal was recorded — the answer is lost")
        self.assertEqual(pending2, 1)

    def test_a_name_containing_the_separator_is_not_truncated(self):
        """A task name may itself contain ` — ` — 4 of the 33 live bad blocks were this.

        The payload is built EXACTLY as `next-focus.py` builds it, `source.file`
        included: the path is `25 Tasks/<name>.md`, so it carries the separator too and
        no fixed split direction can find the boundary. A fixture path without the name
        would pass while the live shape still failed.
        """
        name = "Brogrammers Assistant Dies Mid-Request — claude process ended"
        page = self.vault / "25 Tasks" / f"{name}.md"
        page.write_text(
            "---\nstatus: next\nphase: planning\npriority: \"1\"\n---\n\nbody\n",
            encoding="utf-8",
        )
        payload = f"{name} — priority: 1, in no current plan — {page}:1"

        consumed, notes = self.run_consume([self.card("add it to the plan", payload=payload)])

        self.assertEqual(consumed[0]["name"], name, "the name was truncated")
        self.assertEqual(notes, [])
        self.assertIn(f"**Task:** [[{name}]]", self.weekly.read_text(encoding="utf-8"))

    def test_the_path_oracle_wins_over_a_longer_page_name(self):
        """`Foo — bar` may exist; a row genuinely named `Foo` must still resolve to Foo.

        The payload's own path is the exact oracle: `source.file` is the page that
        produced the row, so its stem is the name however many separators either side
        carries.
        """
        for page_name in ("Foo", "Foo — bar"):
            (self.vault / "25 Tasks" / f"{page_name}.md").write_text(
                "---\nstatus: next\n---\n\nbody\n", encoding="utf-8")
        foo = self.vault / "25 Tasks" / "Foo.md"

        name, why = answer._name_from_payload(
            f"Foo — bar — baz — {foo}:1", str(self.vault), "25 Tasks", "task")

        self.assertEqual(name, "Foo", "resolved to the longer page")
        self.assertEqual(why, "bar")

    def test_a_vanished_page_falls_back_and_is_refused(self):
        """A separator-carrying name whose page is gone must be refused, not guessed."""
        gone = self.vault / "25 Tasks" / "Gone — name.md"
        consumed, notes = self.run_consume([
            self.card("add it to the plan",
                      payload=f"Gone — name — priority: 1, in no current plan — {gone}:1"),
        ])

        self.assertEqual(consumed, [])
        self.assertTrue(any("refusing to write" in note for note in notes))
        self.assertNotIn("Gone", self.weekly.read_text(encoding="utf-8"))

    def test_an_alert_name_is_taken_verbatim(self):
        """An alert's name is a raw alert line — never a page, so never oracle-resolved."""
        (self.vault / "25 Tasks" / "Trading.md").write_text(
            "---\nstatus: next\n---\n\nbody\n", encoding="utf-8")

        name, _ = answer._name_from_payload(
            "Trading — High error rate — alert firing, in no current plan — "
            "prometheus-alerts.sh:1",
            str(self.vault), "25 Tasks", "alert")

        self.assertEqual(name, "Trading — High error rate")

    def test_the_path_oracle_survives_a_separator_in_the_path(self):
        """`parts[-1]` is only a full path when the name has no separator.

        With one, the path splits too, so a fragment can resolve to an unrelated page
        and a wrong row would be written. The `.md:<line>` tail is the exact oracle.
        """
        name = "Brogrammers Assistant Dies Mid-Request — claude process ended"
        # A page named after the path FRAGMENT the broken oracle would have produced.
        (self.vault / "25 Tasks" / "claude process ended.md").write_text(
            "---\nstatus: next\n---\n\nbody\n", encoding="utf-8")
        page = self.vault / "25 Tasks" / f"{name}.md"
        page.write_text("---\nstatus: next\n---\n\nbody\n", encoding="utf-8")

        got, _ = answer._name_from_payload(
            f"{name} — priority: 1, in no current plan — {page}:1",
            str(self.vault), "25 Tasks", "task")

        self.assertEqual(got, name, "resolved to the path fragment instead of the name")

    def test_an_escape_that_survived_the_shell_is_unwrapped(self):
        """The outer quoting is stripped but `shlex.quote`'s apostrophe escape stays.

        Measured 2026-11-12 on store item `1127462d`: that payload splits into 14
        tokens, so the single-token branch never fires and every apostrophe-carrying
        row was refused. The escape appears in the path too, exactly as here.
        """
        name = "A Worker's Gate Cannot Reach Its Spawner"
        page = self.vault / "25 Tasks" / f"{name}.md"
        page.write_text("---\nstatus: next\n---\n\nbody\n", encoding="utf-8")
        escaped = (f"{name} — priority: 1, in no current plan — {page}:1"
                   ).replace("'", "'\"'\"'")

        consumed, notes = self.run_consume([self.card("add it to the plan", payload=escaped)])

        self.assertEqual(consumed[0]["name"], name, "the escape was not unwrapped")
        self.assertEqual(notes, [])
        self.assertIn(f"**Task:** [[{name}]]", self.weekly.read_text(encoding="utf-8"))

    def test_the_escape_unwrap_round_trips_a_name_containing_the_escape(self):
        """The replace is safe only because `shlex.quote` DOUBLES a literal escape.

        A name legitimately containing `'"'"'` always gets quoted (the sequence holds
        a `"`), so the half-stripped shape carries it doubled and one pass restores it
        exactly. This pins that invariant against a "simplify the replace" refactor.
        """
        name = "A Worker's '\"'\"' literal Gate"
        page = self.vault / "25 Tasks" / f"{name}.md"
        page.write_text("---\nstatus: next\n---\n\nbody\n", encoding="utf-8")
        escaped = (f"{name} — priority: 1, in no current plan — {page}:1"
                   ).replace("'", "'\"'\"'")

        consumed, _ = self.run_consume([self.card("add it to the plan", payload=escaped)])

        self.assertEqual(consumed[0]["name"], name)

    def test_the_escape_unwrap_keeps_an_em_dash_name_intact(self):
        """The `stem.count(" — ")` branch must still see the separator after unwrapping."""
        name = "Brogrammers Dies Mid-Request — claude process ended"
        page = self.vault / "25 Tasks" / f"{name}.md"
        page.write_text("---\nstatus: next\n---\n\nbody\n", encoding="utf-8")
        escaped = (f"{name} — priority: 1, in no current plan — {page}:1"
                   ).replace("'", "'\"'\"'")

        got, _ = answer._name_from_payload(
            answer.unquote_payload(escaped), str(self.vault), "25 Tasks", "task")

        self.assertEqual(got, name)

    def test_notes_render_on_an_empty_round(self):
        """A round that could not look must not read like a round that found nothing."""
        rendered = answer.render([], ["k: unhandled answer 'x' — not consumed"], 2, 1)
        self.assertIn("unhandled answer", rendered)
        self.assertIn("1 answered card(s) awaiting", rendered)

    def test_consume_flag_is_required(self):
        """Without `--consume` the script must refuse, not quietly write."""
        out = subprocess.run(
            [sys.executable, str(ANSWER), "--vault", str(self.vault)],
            capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 2)
        self.assertIn("--consume", out.stderr)

    # -------------------------------------------------------------- leave path

    def test_leave_writes_a_dismissal_that_the_reader_reports(self):
        consumed, _ = self.run_consume([self.card("leave it out")])
        self.assertEqual(consumed[0]["action"], "dismissed")
        self.assertTrue(any(self.dismissed.glob("*.json")), "no dismissal record written")

        out = subprocess.run(
            [sys.executable, str(NEXT_FOCUS), "--vault", str(self.vault),
             "--now", "2026-11-09", "--live", "0", "--target", "1",
             "--state-dir", str(self.dismissed), "--json"],
            capture_output=True, text=True, check=True,
        )
        result = json.loads(out.stdout)
        keys = [record["card_key"] for record in result["dismissed"]]
        self.assertIn(self.card_key, keys, "the dismissal is not reported in `dismissed[]`")

    def test_a_dismissed_row_is_not_proposed(self):
        self.run_consume([self.card("leave it out")])
        out = subprocess.run(
            [sys.executable, str(NEXT_FOCUS), "--vault", str(self.vault),
             "--now", "2026-11-09", "--live", "0", "--target", "1",
             "--state-dir", str(self.dismissed), "--json"],
            capture_output=True, text=True, check=True,
        )
        result = json.loads(out.stdout)
        names = [row["name"] for row in result["proposed"]]
        self.assertNotIn(TASK_NAME, names, "a dismissed row is still proposed")

    # ------------------------------------------------------------ odd answers

    def test_an_unhandled_answer_is_not_consumed(self):
        consumed, notes = self.run_consume([self.card("something else entirely")])
        self.assertEqual(consumed, [])
        self.assertTrue(any("unhandled" in note for note in notes))
        self.assertFalse(any(self.dismissed.glob("*.json")))

    def test_an_open_card_is_not_consumed(self):
        consumed, _ = self.run_consume([self.card("", state="open")])
        self.assertEqual(consumed, [])

    # ------------------------------------------------------------- refusals

    def test_one_unaddable_card_does_not_starve_the_others(self):
        """A `leave it out` needs nothing from the weekly note, so it must still apply."""
        self.weekly.unlink()
        leave = self.card("leave it out", item_id="d" * 32,
                          key="next-focus:vault:task:other-task-abcdef12")
        consumed, notes = self.run_consume([self.card("add it to the plan"), leave])

        self.assertEqual([entry["action"] for entry in consumed], ["dismissed"])
        self.assertTrue(any("refused" in note for note in notes))
        self.assertTrue(any(self.dismissed.glob("*.json")), "the dismissal was skipped")

    def test_a_note_with_no_blocks_refuses_that_card_and_says_so(self):
        self.weekly.write_text("# Weekly\n\nno blocks here\n", encoding="utf-8")
        consumed, notes = self.run_consume([self.card("add it to the plan")])
        self.assertEqual(consumed, [])
        self.assertTrue(any("refused" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
