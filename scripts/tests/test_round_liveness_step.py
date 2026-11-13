#!/usr/bin/env python3
"""Tests for the shipped `case "$STATE"` block in commands/steps/round-liveness.md.

That block decides, every round, whether a manager arms its liveness record. An arm is not
read-only: `--arm` clears `.sleeping` and rewrites the cadence history (it no longer clears
`.stopped` — a round's reading must never resume a stand-down). So the
block arms on an allow-list and must never arm on a misread of the subject:

  * an EMPTY `STATE` — `manager-liveness.py --state` refused an unresolvable subject (exit 3,
    nothing on stdout), and
  * `WRONG-BRANCH` — the record lives on another tier.

The old `*) --arm` fallback armed on both. These tests run the block exactly as shipped,
against the real `manager-liveness.py` in an isolated state dir and vault, with only `--arm`
intercepted so nothing is written by the arm itself.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
STEP = os.path.join(REPO, "commands", "steps", "round-liveness.md")
LIVENESS = os.path.join(REPO, "scripts", "manager-liveness.py")

FULL = "Several Clusters and the Mac Write One Vault"
ARMED = "ARM-CALLED"


def shipped_block() -> str:
    """The one ```bash block in the step that holds `case "$STATE"`."""
    text = open(STEP, encoding="utf-8").read()
    blocks = [b for b in re.findall(r"```bash\n(.*?)```", text, re.S) if 'case "$STATE"' in b]
    assert len(blocks) == 1, f"expected one case block in {STEP}, found {len(blocks)}"
    return blocks[0]


class RoundLivenessStep(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.vault = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self.addCleanup(shutil.rmtree, self.vault, True)
        os.makedirs(os.path.join(self.vault, "24 Goals"))
        open(os.path.join(self.vault, "24 Goals", FULL + ".md"), "w").close()
        self.plugin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.plugin, True)
        os.makedirs(os.path.join(self.plugin, "scripts"))
        os.symlink(LIVENESS, os.path.join(self.plugin, "scripts", "manager-liveness.py"))

    def liveness(self, *argv):
        env = {**os.environ, "MANAGER_LIVENESS_STATE_DIR": self.state_dir}
        subprocess.run([sys.executable, LIVENESS, *argv], env=env, check=True,
                       capture_output=True)

    def run_block(self, subject: str, branch: str) -> str:
        block = (
            shipped_block()
            .replace("<subject>", subject)
            .replace("<goal|topic>", branch)
            .replace("<vault-root>", self.vault)
            .replace("<clamped-seconds>", "900")
        )
        # Intercept the arm only: `--state` runs the real script, so the STATE the block
        # reads is the one a real round would read.
        prelude = (
            f'python3() {{ case " $* " in *" --arm "*) echo {ARMED} ;; '
            f'*) command "{sys.executable}" "$@" ;; esac; }}\n'
        )
        env = {
            **os.environ,
            "MANAGER_LIVENESS_STATE_DIR": self.state_dir,
            "CLAUDE_PLUGIN_ROOT": self.plugin,
        }
        done = subprocess.run(["bash", "-c", prelude + block], env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout

    # --- never arms on a misread ----------------------------------------------

    def test_an_unresolvable_subject_never_arms(self):
        self.liveness("--arm", "--subject", FULL, "--branch", "goal", "--interval", "900")
        out = self.run_block("Several Clusters", "goal")
        self.assertNotIn(ARMED, out)
        self.assertIn("refused", out)

    def test_wrong_branch_never_arms(self):
        self.liveness("--arm", "--subject", FULL, "--branch", "goal", "--interval", "900")
        self.liveness("--stop", "--subject", FULL, "--branch", "goal")
        out = self.run_block(FULL, "topic")
        self.assertNotIn(ARMED, out)
        self.assertIn("WRONG-BRANCH", out)

    def test_stopped_never_arms(self):
        self.liveness("--arm", "--subject", FULL, "--branch", "goal", "--interval", "900")
        self.liveness("--stop", "--subject", FULL, "--branch", "goal")
        self.assertNotIn(ARMED, self.run_block(FULL, "goal"))

    def test_sleeping_never_arms(self):
        self.liveness("--sleep", "--subject", FULL, "--branch", "goal",
                      "--until", "2999-01-01T00:00:00+00:00")
        self.assertNotIn(ARMED, self.run_block(FULL, "goal"))

    # --- arms on the allow-list -----------------------------------------------

    def test_ok_arms(self):
        self.liveness("--arm", "--subject", FULL, "--branch", "goal", "--interval", "900")
        self.assertIn(ARMED, self.run_block(FULL, "goal"))

    def test_a_never_armed_subject_whose_page_resolves_arms(self):
        self.assertIn(ARMED, self.run_block(FULL, "goal"))

    def test_the_fallback_is_not_an_arm(self):
        # Pin the shape, not just the behaviour on today's states: any word the block does
        # not name must land on a non-arming branch.
        block = shipped_block()
        fallback = [ln for ln in block.splitlines() if ln.strip().startswith("*)")]
        self.assertEqual(1, len(fallback), block)
        self.assertNotIn("--arm", fallback[0])


if __name__ == "__main__":
    unittest.main()
