"""check-handback-keys.py: a consumer must never test a key nobody emits.

Two families are covered, and both are silent when broken:

  (1) the fleet tick's router `When` cells against `agents/fleet-sweep-reader.md`'s
      hand-back, and
  (2) the delegated legs — `commands/steps/round-readings.md` → `agents/round-readings-reader.md`,
      `commands/steps/goal-spawn.md` → `agents/goal-spawn-reader.md`, and the fleet round's two
      acting legs, `commands/steps/keepalive.md` → `agents/fleet-keepalive-actor.md` and
      `commands/steps/flagged-open.md` → `agents/fleet-flagged-open-actor.md`.

The load-bearing cases are the PRE-CHANGE ones. Both reader agents shipped on 2026-11-06
with a hand-back that omitted values its own contract requires — the readings leg collapsed
three families' `unread` into one shared `unread=` (the feed's), and the spawn leg never
emitted `task=`, which the a2a branch's server requires. Reverting either agent's fence to
that text must fail this check; the fixtures below are built from the checker's own `LEGS`
registry so they cannot drift from it, and then falsified one key at a time.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-handback-keys.py"


def legs():
    """The checker's own `LEGS`, loaded from it rather than copied.

    ⚠️ **Derived, never hand-listed.** A second copy of a required set is exactly the drift
    this checker exists to catch, so the fixture must agree with it by construction.
    """
    spec = importlib.util.spec_from_file_location("check_handback_keys", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LEGS


#: The fleet router cell and a sweep hand-back that satisfies it.
ROUTER = "the hand-back reports `orphan`, `collision` or `unmanaged` > 0\n"
SWEEP = (
    "**Hand-back**\n\n```text\n"
    "orphan=<n> · collision=<n> · unmanaged=<n> · blocked=<n> · context70=<n>\n"
    "```\n</output_format>\n"
)


def fence(keys, drop=()):
    """A `**Hand-back**` block emitting every key in `keys`, minus `drop`."""
    kept = [k for k in keys if k not in drop]
    return (
        "**Hand-back**\n\n```text\n"
        + " · ".join(f"{k}=<n>" for k in kept)
        + "\n```\n</output_format>\n"
    )


class CheckHandbackKeysTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-handback-keys.py")
        self.write("commands/fleet-drive.md", ROUTER)
        self.write("agents/fleet-sweep-reader.md", SWEEP)
        self.legs = legs()
        for name, leg in self.legs.items():
            for site in leg["sites"]:
                self.write(site, f'Task(subagent_type: "{name}", prompt: <inputs>)\n')
            self.write(leg["agent"], fence(leg["required"]))

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run([sys.executable, "scripts/check-handback-keys.py"], cwd=self.dir,
                              capture_output=True, text=True)

    def test_wired_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_step_reverted_to_pre_split_text_fails(self):
        """The split's own guard: a step that stops dispatching its reader is its old text."""
        site = self.legs["supervisor:round-readings-reader"]["sites"][0]
        self.write(site, "# Readings\n\n- Take all four verdict families with one command.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(site, result.stderr)
        self.assertIn("no longer dispatches", result.stderr)

    def test_pre_change_readings_fence_fails(self):
        """The pre-change readings hand-back: one shared `unread=`, no per-family keys."""
        self.write(
            "agents/round-readings-reader.md",
            "**Hand-back**\n\n```text\n"
            "readings=written · rows=<n> · payload=<path>\n"
            "live=<n> · absent=<n> · unknown=<n> · inflight=<n> · quiet=<n>\n"
            "gated=<n> · clear=<n> · unread=<n> · external=<n> · none=<n> · stuck=<n>\n"
            "```\n</output_format>\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("inflight-unread", result.stderr)
        self.assertIn("external-unread", result.stderr)

    def test_pre_change_spawn_fence_fails(self):
        """The pre-change spawn hand-back: no `task=`, which the a2a branch requires."""
        self.write(
            "agents/goal-spawn-reader.md",
            "**Hand-back**\n\n```text\n"
            "rows=<n> · open=<n> · resume=<n> · held=<n> · refused=<n> · truncated=<n> · "
            "workers=<n> · target=<n> · cap=open · payload=<path>\n"
            "open: <task> · role=<role> · mode=<headless> · mode_source=<field> · "
            "target=<value> · shipping=<true> · vault=<path> · cwd=<path> · prompt=<command>\n"
            "```\n</output_format>\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("`task=`", result.stderr)

    def test_missing_agent_file_fails(self):
        (pathlib.Path(self.dir) / "agents" / "goal-spawn-reader.md").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stderr)
        self.assertIn("supervisor:goal-spawn-reader", result.stderr)

    def test_fleet_key_with_no_emitter_still_fails(self):
        """The original defect class, unchanged by the widening."""
        self.write("agents/fleet-sweep-reader.md", fence(["orphan", "collision"]))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("`unmanaged`", result.stderr)

    def test_hyphenated_key_is_matched_whole(self):
        """`inflight-unread=` must not be read as its tail `unread=`."""
        self.write(
            "agents/round-readings-reader.md",
            fence(self.legs["supervisor:round-readings-reader"]["required"],
                  drop=("inflight-unread",)),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("inflight-unread", result.stderr)

    def test_pre_change_keepalive_fence_fails(self):
        """The keep-alive leg's hand-back without `legs=` — the digest-write verdict.

        The counts are not the whole hand-back: the caller branches on whether the digest
        write landed, and a fence that drops that key leaves the failed-write render
        unreachable — the round would print numbers no digest carries.
        """
        self.write(
            "agents/fleet-keepalive-actor.md",
            fence(self.legs["supervisor:fleet-keepalive-actor"]["required"], drop=("legs",)),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("`legs=`", result.stderr)

    def test_pre_change_keepalive_fence_fails_without_per_list_dropped_counts(self):
        """The pre-change keep-alive hand-back: one shared `truncated=`, no per-list counts.

        Two truncatable row lists behind a single count cannot say which list lost rows, so
        the caller's per-list reconciliation (`printed + that list's dropped == that list's
        count`) is arithmetically unsatisfiable. The fixture is the exact shape this change
        replaced, and both keys must be named.
        """
        self.write(
            "agents/fleet-keepalive-actor.md",
            fence(
                self.legs["supervisor:fleet-keepalive-actor"]["required"],
                drop=("crash-loop-truncated", "cannot-tell-truncated"),
            ).replace("crash-loop=<n>", "crash-loop=<n> · truncated=<n>"),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("crash-loop-truncated", result.stderr)
        self.assertIn("cannot-tell-truncated", result.stderr)

    def test_pre_change_keepalive_fence_fails_without_dead_scan(self):
        """`dead-scan` is the label the digest write used — `—` when `dead-workers=not-run`.

        A caller that had to infer the label from the count would read a floor as an exact
        scan, which is the "a number with no label reads as a measurement" defect. A fence
        that drops the key leaves that cell permanently unreadable, so it must fail here.
        """
        self.write(
            "agents/fleet-keepalive-actor.md",
            fence(
                self.legs["supervisor:fleet-keepalive-actor"]["required"],
                drop=("dead-scan",),
            ),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("`dead-scan=`", result.stderr)

    def test_pre_change_flagged_open_fence_fails(self):
        """The pre-change flagged-open hand-back: no `legs-reason`, no truncation channel.

        The caller renders `legs: UNKNOWN — <reason>` from `legs-reason=` and reads the
        refusals that did not fit from `payload=`, so a fence carrying counts alone leaves a
        failed digest write and a lost refusal both unreportable.
        """
        self.write(
            "agents/fleet-flagged-open-actor.md",
            "**Hand-back**\n\n```text\n"
            "operator-flagged=<n> · opened=<n> · refused=<n>\n"
            "```\n</output_format>\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        for key in ("legs", "legs-reason", "truncated", "payload"):
            self.assertIn(f"`{key}=`", result.stderr)

    def test_flagged_open_fence_without_opened_fails(self):
        """`opened` is the batch's own footer count — the one number that separates a leg
        which opened nothing from a leg that was told to print nothing."""
        self.write(
            "agents/fleet-flagged-open-actor.md",
            fence(
                self.legs["supervisor:fleet-flagged-open-actor"]["required"],
                drop=("opened",),
            ),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("`opened=`", result.stderr)

    def test_a_fleet_acting_leg_that_stops_dispatching_fails(self):
        """The same split guard, on the fleet side: a reverted 5c is its pre-split text."""
        site = self.legs["supervisor:fleet-flagged-open-actor"]["sites"][0]
        self.write(site, "# Flagged open\n\n- Open what the operator marked, if any.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(site, result.stderr)
        self.assertIn("no longer dispatches", result.stderr)


if __name__ == "__main__":
    unittest.main()
