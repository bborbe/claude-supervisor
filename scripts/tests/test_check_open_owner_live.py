"""`scripts/check-open-owner-live.py` — each guard fires on the one mutation it exists to catch.

Every fixture starts from the real `commands/open.md` and changes exactly one thing, so a
guard that cannot fail turns this file red. The producer and schema fixtures work against a
temp tree holding only the literal each guard keys on, so a mutation there is also one line.

Run: python3 -m unittest discover -s scripts/tests -p 'test_check_open_owner_live.py'
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

_RENDER = '`⛔ HOLD — owner <kind> "<name>" has a live manager`'
_SAME_RUN = '`opened-recently.py --subject "<owner> Manager" --minutes <window>`'
_OWNER_VERDICT = (
    "⚠️ **The verdict is the owner's, not the row's.** `⏭️ LIVE HERE` and `🔒 LIVE ELSEWHERE` "
    "read the **task's own** session; `held-owner-live` reads the **owning subject's**. "
    "Never reuse either label — one row can carry both facts, and they must stay "
    "distinguishable in the table.")

_PRODUCER = 'return "held-owner-live", refused'
_SCHEMA = '"held-owner-live"'


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_open_owner_live", os.path.join(_ROOT, "scripts", "check-open-owner-live.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CheckOpenOwnerLive(unittest.TestCase):
    def setUp(self):
        self.check = _load()
        with open(os.path.join(_ROOT, "commands", "open.md"), encoding="utf-8") as handle:
            self.real = handle.read()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "commands").mkdir()
        (self.root / "scripts" / "tests" / "schema").mkdir(parents=True)
        self._write_sources(_PRODUCER, _SCHEMA)
        self.check.ROOT = self.root

    def tearDown(self):
        self._tmp.cleanup()

    def _write_sources(self, producer, schema):
        (self.root / "scripts" / "flagged-rows.py").write_text(
            "def resolve_branch():\n    %s\n" % producer, encoding="utf-8")
        (self.root / "scripts" / "tests" / "schema" /
         "flagged-rows.schema.json").write_text(
            '{"branch": {"enum": ["create", %s]}}\n' % schema, encoding="utf-8")

    def _run(self, text):
        page = self.root / "commands" / "open.md"
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

    def test_a_missing_render_fires(self):
        """Guard (a): a held row with no reason printed is indistinguishable from a bug."""
        rc, out = self._run(self._mutate(_RENDER, "`⛔ HOLD`"))
        self.assertEqual(1, rc)
        self.assertIn("no longer renders `⛔ HOLD — owner", out)

    def test_a_render_that_stops_naming_the_owner_fires(self):
        """Guard (b): `⛔ HOLD` alone reads exactly like a `blocked=True` hold."""
        rc, out = self._run(self._mutate(
            _RENDER, "`⛔ HOLD — owner <subject> has a live manager`"))
        self.assertEqual(1, rc)
        self.assertIn("no longer names the owning subject's kind and name", out)

    def test_a_missing_spawn_ledger_leg_fires(self):
        """Guard (c): a manager opened moments ago is not in the registry yet (SC3)."""
        rc, out = self._run(self._mutate(_SAME_RUN, "`scripts/flagged-rows.py`"))
        self.assertEqual(1, rc)
        self.assertIn("no longer consults the spawn ledger", out)

    def test_a_footer_that_folds_the_two_holds_together_fires(self):
        """Guard (d): `held K (blocked)` alone hides that a hold was a decision."""
        rc, out = self._run(self._mutate(
            "held K (blocked · owner live)", "held K (blocked)"))
        self.assertEqual(1, rc)
        self.assertIn("the batch footer no longer carries", out)

    def test_dropping_the_row_own_verdicts_fires(self):
        """The hold must stay distinguishable from `LIVE HERE` / `LIVE ELSEWHERE` (SC4)."""
        rc, out = self._run(self._mutate(
            _OWNER_VERDICT,
            "⚠️ **The verdict is the owner's, not the row's.** Never reuse the row's label."))
        self.assertEqual(1, rc)
        self.assertIn("no longer names `LIVE HERE` as the row-own verdict", out)
        self.assertIn("no longer names `LIVE ELSEWHERE` as the row-own verdict", out)

    def test_a_producer_that_stops_emitting_the_branch_fires(self):
        """The render keys on a branch name; a producer rename makes it dead prose."""
        self._write_sources('return "create", refused', _SCHEMA)
        rc, out = self._run(self.real)
        self.assertEqual(1, rc)
        self.assertIn("no longer returns 'held-owner-live'", out)

    def test_a_schema_that_drops_the_branch_fires(self):
        """An emitted row outside the enum does not validate."""
        self._write_sources(_PRODUCER, '"other"')
        rc, out = self._run(self.real)
        self.assertEqual(1, rc)
        self.assertIn("no longer carries 'held-owner-live' in the branch enum", out)


if __name__ == "__main__":
    unittest.main()
