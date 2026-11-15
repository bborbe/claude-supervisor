"""`scripts/goal-open-gate.py` — the OPEN gate `/supervisor:open --goal` reads before a spawn.

Covers the three verdicts, both goals-folder spellings, and the remedy line. The remedy is
asserted because the obvious CLI line is wrong: `vault-cli goal set` has no `--by`, so a
`goal set … flag true` writes a flag with no `flag_set_by`, which the shared predicate
refuses — a printed command that leaves the goal refused is worse than no remedy at all.

Run: python3 -m unittest discover -s scripts/tests -p 'test_goal_open_gate.py'
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load():
    spec = importlib.util.spec_from_file_location(
        "goal_open_gate", os.path.join(_SCRIPTS, "goal-open-gate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()


def _page(vault, dirname, title, frontmatter):
    os.makedirs(os.path.join(vault, dirname), exist_ok=True)
    with open(os.path.join(vault, dirname, title + ".md"), "w", encoding="utf-8") as handle:
        handle.write("---\n%s---\nTags: [[Goal]]\n" % frontmatter)


def _run(vault, goal):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = gate.main(["--vault", vault, "--goal", goal])
    return rc, out.getvalue().splitlines()


class GoalOpenGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_operator_flag_opens(self):
        _page(self.vault, "24 Goals", "G", "flag: true\nflag_set_by: operator\n")
        rc, lines = _run(self.vault, "G")
        self.assertEqual((rc, lines[0]), (0, "open-ok"))

    def test_legacy_flag_opens(self):
        _page(self.vault, "24 Goals", "G", "flag: true\nflag_set_by: legacy\n")
        self.assertEqual(_run(self.vault, "G")[0], 0)

    def test_agent_set_flag_refuses_with_its_reason(self):
        _page(self.vault, "24 Goals", "G", "flag: true\nflag_set_by: agent\n")
        rc, lines = _run(self.vault, "G")
        self.assertEqual(rc, 1)
        self.assertEqual(lines[0], "goal-unflagged: agent-set-flag")

    def test_bare_flag_without_setter_refuses(self):
        """The shape `vault-cli goal set <g> flag true` writes — refused, by design."""
        _page(self.vault, "24 Goals", "G", "flag: true\n")
        rc, lines = _run(self.vault, "G")
        self.assertEqual(rc, 1)
        self.assertTrue(lines[0].startswith("goal-unflagged"))

    def test_unflagged_refuses(self):
        _page(self.vault, "24 Goals", "G", "status: in_progress\n")
        rc, lines = _run(self.vault, "G")
        self.assertEqual((rc, lines[0]), (1, "goal-unflagged: not flagged"))

    def test_older_goals_folder_is_read(self):
        """`private-family` and `private-starcitizen` still carry `23 Goals`."""
        _page(self.vault, "23 Goals", "G", "flag: true\nflag_set_by: operator\n")
        self.assertEqual(_run(self.vault, "G"), (0, ["open-ok"]))

    def test_newer_goals_folder_wins_when_both_exist(self):
        _page(self.vault, "24 Goals", "G", "status: in_progress\n")
        _page(self.vault, "23 Goals", "G", "flag: true\nflag_set_by: operator\n")
        self.assertEqual(_run(self.vault, "G")[0], 1)

    def test_missing_page_is_not_a_flag_verdict(self):
        rc, lines = _run(self.vault, "No Such Goal")
        self.assertEqual((rc, lines[0]), (1, "goal-not-found"))

    def test_remedy_never_prints_an_unsupported_flag(self):
        """`goal set` has no `--by`; the remedy must point at the operator's own surface."""
        _page(self.vault, "24 Goals", "G", "flag: true\nflag_set_by: agent\n")
        _, lines = _run(self.vault, "G")
        remedy = "\n".join(lines[1:])
        self.assertIn("flag it first", remedy)
        self.assertIn("Vault UI", remedy)
        self.assertNotIn("--by", remedy)


if __name__ == "__main__":
    unittest.main()
