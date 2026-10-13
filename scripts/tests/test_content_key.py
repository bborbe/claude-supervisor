#!/usr/bin/env python3
"""Tests for the verdicts cache's content-key derivation (`manager-predispatch.py`).

`content_key` is the single home of a derivation that used to live only as **prose**, in
`agents/manager-drive.md` clause (1). The drive leg is an agent, so every leg re-implemented
the formula and they disagreed — a leg's own `Audit` block on **2026-10-12T15:49:30Z**
reported that its stored keys reproduced under a `mode:`-only exclusion while the clause
named four fields. Writer and reader therefore keyed the same row differently, and the row
read as cold on every tick. `scripts/check-content-key-formula.py` says in its own docstring
that a prose-shape check *"cannot prove the leg computes the key this way"*. These cases are
that missing half.

Each case guards a defect the derivation has actually shipped or was filed against:

  * **a field the audit does not read must NOT move the key** — `mode:`,
    `last_auto_resume`, `claude_session_id`, `metrics_sessions`. A key that moves on a
    tick-written field re-audits the whole set on every tick, which is the cost this cache
    exists to remove.
  * **`status:` and `phase:` must move it** — excluding them freezes a row's verdict label
    across an approval, since `phase` moving `todo -> planning` is precisely what turns
    `Ready for approval` into `Ready`.
  * **the exclusion reaches the field's CONTINUATION BLOCK, not just its key line** —
    `metrics_sessions` is written as a multi-line YAML block, and its `- session_id:`
    entries carry a uuid that changes on every worker run. A key-line-only deletion leaves
    those entries in the key and the exclusion buys nothing.
  * **the hash is over RAW BYTES** — a parse-and-re-serialise route agrees with the byte
    route on the fixtures everyone writes and disagrees on the ones that matter (CRLF, a
    trailing newline, a quoted value), so one case asserts a body-whitespace change moves
    the key.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "manager-predispatch.py")

_spec = importlib.util.spec_from_file_location("manager_predispatch", _SCRIPT)
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)

#: A row file carrying all four excluded fields, one of them as a multi-line block, plus the
#: two fields the clause deliberately keeps. Deliberately *not* minimal: a fixture without a
#: continuation block cannot fail the block-extent case, and a fixture without `phase:`
#: cannot fail the phase case.
ROW = """---
task_identifier: 11111111-2222-3333-4444-555555555555
status: in_progress
phase: planning
page_type: task
mode: interactive
last_auto_resume: null
claude_session_id: aaaa-bbbb-cccc-dddd
metrics_sessions:
    - session_id: 11111111-1111-1111-1111-111111111111
      started_at: 2026-10-01T00:00:00+02:00
priority: 2
---

Tags: [[Task]]

---

Body line one.
Body line two.
"""


class ContentKeyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # ⚠️ `--vault` is validated as a real vault root (it must hold `24 Goals` or
        # `23 Topics`), so the CLI cases need one. An empty temp dir is refused with exit 2
        # *before* the mode runs, which would make both CLI cases pass or fail for a reason
        # unrelated to what they assert.
        os.makedirs(os.path.join(self._tmp.name, "24 Goals"), exist_ok=True)

    def write(self, text, name="row.md"):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return path

    def key_of(self, text):
        return mp.content_key(self.write(text))

    # -- the key is a function of the bytes, and nothing else ----------------------------

    def test_deterministic(self):
        """The same bytes key the same, twice — the cache's whole premise."""
        self.assertEqual(self.key_of(ROW), self.key_of(ROW))

    def test_truncated_to_sixteen_hex(self):
        """⚠️ Three truncation lengths were measured on disk 2026-10-06 (12, 16 and 64),
        each cache internally uniform — a derivation invented per caller rather than read.
        A cache written at one length can never match another."""
        self.assertRegex(self.key_of(ROW), r"^[0-9a-f]{16}$")

    # -- the four excluded fields do not move the key ------------------------------------

    def test_mode_does_not_move_the_key(self):
        """`mode:` is the field clause (6) writes to the row it is preparing, so a key taken
        over the raw file is orphaned by the leg's own next clause — the entry is written and
        immediately dead, and the row pays a cold dispatch on the next tick."""
        self.assertEqual(self.key_of(ROW), self.key_of(ROW.replace("mode: interactive", "mode: headless")))

    def test_last_auto_resume_does_not_move_the_key(self):
        self.assertEqual(self.key_of(ROW), self.key_of(ROW.replace("last_auto_resume: null", "last_auto_resume: 2026-10-13T00:00:00Z")))

    def test_claude_session_id_does_not_move_the_key(self):
        self.assertEqual(self.key_of(ROW), self.key_of(ROW.replace("claude_session_id: aaaa-bbbb-cccc-dddd", "claude_session_id: eeee-ffff-0000-1111")))

    def test_metrics_sessions_block_does_not_move_the_key(self):
        """⚠️ **The block half is load-bearing, not tidiness.** `metrics_sessions` is written
        as a multi-line YAML block whose `- session_id:` entries change on every worker run.
        A deletion that drops only the `metrics_sessions:` key line leaves those entries in
        the key, so the key churns per tick and the exclusion buys nothing."""
        mutated = ROW.replace(
            "    - session_id: 11111111-1111-1111-1111-111111111111",
            "    - session_id: 99999999-9999-9999-9999-999999999999",
        )
        self.assertNotEqual(ROW, mutated, "fixture did not mutate — the case would pass vacuously")
        self.assertEqual(self.key_of(ROW), self.key_of(mutated))

    # -- the two fields the clause deliberately KEEPS ------------------------------------

    def test_phase_moves_the_key(self):
        """Excluding `phase:` freezes a row's verdict label across an approval: `phase`
        moving `todo -> planning` is what turns `Ready for approval` into `Ready`."""
        self.assertNotEqual(self.key_of(ROW), self.key_of(ROW.replace("phase: planning", "phase: execution")))

    def test_status_moves_the_key(self):
        self.assertNotEqual(self.key_of(ROW), self.key_of(ROW.replace("status: in_progress", "status: completed")))

    # -- raw bytes, not a YAML round-trip ------------------------------------------------

    def test_body_whitespace_moves_the_key(self):
        """A parse-and-re-serialise route normalises this away; the clause forbids it, and
        the two routes disagree on CRLF, on a trailing newline, and on quoted values."""
        self.assertNotEqual(self.key_of(ROW), self.key_of(ROW.replace("Body line one.", "Body line one. ")))

    # -- the CLI mode the drive leg calls ------------------------------------------------

    def test_cli_prints_key_tab_path_in_order(self):
        """The leg calls this once per tick with its row paths. ⚠️ **In the order given** —
        a set that comes back reordered is one the caller cannot zip against its own list."""
        a = self.write(ROW, "a.md")
        b = self.write(ROW.replace("phase: planning", "phase: execution"), "b.md")
        out = subprocess.run(
            [sys.executable, _SCRIPT, "--vault", self._tmp.name, "--subject", "smoke", "--content-keys"],
            input=f"{b}\n{a}\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        self.assertEqual([ln.split("\t", 1)[1] for ln in lines], [b, a])
        self.assertEqual(lines[0].split("\t", 1)[0], mp.content_key(b))

    def test_cli_reports_an_unreadable_path_rather_than_skipping_it(self):
        """⚠️ **A silently dropped row is indistinguishable, to the caller, from one that was
        never passed** — which is the omission this mode exists to remove."""
        missing = os.path.join(self._tmp.name, "nope.md")
        out = subprocess.run(
            [sys.executable, _SCRIPT, "--vault", self._tmp.name, "--subject", "smoke", "--content-keys"],
            input=f"{missing}\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(out.returncode, 2)
        self.assertIn("UNREADABLE:", out.stderr)
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
