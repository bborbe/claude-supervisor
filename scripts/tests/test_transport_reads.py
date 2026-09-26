"""The transport-read check runs clean on this tree.

Wires `scripts/transport-read-check.py` into `make test`, so a site that
re-collapses a failed read into an empty result fails CI rather than waiting for
someone to remember to run the check by hand. A check nothing runs is a check
that never fired — and this whole class of defect is one where the failing
behaviour is indistinguishable from the healthy behaviour, so nothing else in
the suite would notice.

The other half of the criterion — that the check *fails* on a pre-fix revision —
needs a `git worktree` of an old commit and is run by hand:

    git worktree add /tmp/pre-audit <sha>
    python3 scripts/transport-read-check.py /tmp/pre-audit   # must exit 1

See `docs/pane-reads.md` for the rule the check enforces.
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
CHECK = os.path.join(SCRIPTS, "transport-read-check.py")


class TransportReadCheckPasses(unittest.TestCase):
    def test_check_passes_on_this_tree(self):
        r = subprocess.run(
            [sys.executable, CHECK],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        self.assertEqual(
            0,
            r.returncode,
            f"transport-read-check failed:\n{r.stdout}\n{r.stderr}",
        )
        self.assertIn("checks passed", r.stdout)


if __name__ == "__main__":
    unittest.main()
