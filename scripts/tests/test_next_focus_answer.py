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

def weekly_text(blocks=1, picked=1):
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
        return answer.consume_items(
            items, str(self.vault), "25 Tasks",
            str(self.dismissed), str(self.consumed), NOW,
        )

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

    def test_missing_weekly_note_is_refused_not_quiet(self):
        self.weekly.unlink()
        with self.assertRaises(answer.Refused):
            self.run_consume([self.card("add it to the plan")])

    def test_empty_weekly_note_refuses_rather_than_guessing_a_number(self):
        self.weekly.write_text("# Weekly\n\nno blocks here\n", encoding="utf-8")
        with self.assertRaises(answer.Refused):
            self.run_consume([self.card("add it to the plan")])


if __name__ == "__main__":
    unittest.main()
