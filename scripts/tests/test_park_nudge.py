"""park-nudge.py: the enforcement point for the 15-minute park rule.

The defect this guards is a rule that is loaded and ignored. A session sat
`watching` for 31 minutes after the rule landed while a 12-minute one beside it
was legal, and no fleet leg consumed the reading that tells them apart. So the
fixtures pin *discrimination*, not parsing:

- **The threshold discriminates.** Over 15 min emits, inside it does not. A run
  that emits for both, or for neither, fails.
- **The bucket is the gate.** Every non-`watching` row in the bucket fixture
  sits ABOVE the threshold, so an implementation that filters on the duration
  alone emits for them and fails here.
- **The duration is `watching_min`, not `quiet_min`.** A watcher re-arm writes to
  the transcript and resets the quiet clock, so a session parked 3 h 34 m read
  `quiet 12m` and was never nudged. The `divergent` fixture carries both fields
  disagreeing; the selection must follow `watching_min`.
- **Suppression is per episode, not per duration value.** `watching_min` grows
  every round, so a key carrying it never suppresses in a live fleet. Run 2
  advances the duration on purpose; run 4 proves re-entry into `watching` re-arms.
- **The nudge carries the park.** register, `defer_date` at a TIME, self-exit
  by a path that resolves, because the self-exit script ships in vault-cli.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "park-nudge.py"
FIXTURE = json.loads((HERE / "fixtures" / "park-nudge-activity.json").read_text())
OVER = "aaaaaaaa-0000-4000-8000-000000000031"
WITHIN = "bbbbbbbb-0000-4000-8000-000000000012"


def reading(sessions):
    return dict(window_min=15, live=len(sessions), sessions=sessions)


def row(sid=OVER, bucket="watching", quiet=31, watching=None, since=None):
    return dict(sid=sid, name="Synthetic", status="shell", bucket=bucket,
                calls=0, out_tok=0, quiet_min=quiet,
                watching_min=quiet if watching is None else watching,
                watching_since=since,
                waits_on="CI run 1111 completes")


class ParkNudgeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.state = self.tmp / "state" / "ledger.json"

    def run_script(self, data, *args, extra_env=None):
        env = dict(os.environ)
        env.pop("PARK_NUDGE_STATE", None)
        env.update(extra_env or {})
        payload = data if isinstance(data, str) else json.dumps(data)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--state", str(self.state), *args],
            input=payload, capture_output=True, text=True, env=env, check=False,
        )

    @staticmethod
    def nudges(proc):
        return [ln for ln in proc.stdout.splitlines() if ln.startswith("NUDGE ")]

    # --- SC1: the threshold discriminates -----------------------------------

    def test_over_threshold_emits_and_within_allowance_does_not(self):
        p = self.run_script(FIXTURE["threshold"])
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn(OVER, lines[0])
        self.assertIn("quiet 31m", lines[0])
        self.assertNotIn(WITHIN, p.stdout)
        self.assertIn("park-nudge: 1 nudged · 0 suppressed · 1 within 15m · 2 watching · 2 live", p.stdout)

    def test_a_re_arming_watcher_is_nudged_on_its_state_duration(self):
        """The defect: quiet 12m, state 214m. The re-arm must not hide the park."""
        p = self.run_script(FIXTURE["divergent"])
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn("quiet 214m", lines[0],
                      "the emitted number must be watching_min, not quiet_min")

    def test_a_row_without_a_state_duration_is_skipped_not_treated_as_zero(self):
        """An unmeasured row is not a legal short wait — it is not a candidate,
        but it IS a watching row and must still be counted."""
        unmeasured = row(quiet=99)
        unmeasured.pop("watching_min")
        p = self.run_script(reading([unmeasured]))
        self.assertEqual(self.nudges(p), [])
        self.assertIn("1 watching", p.stdout)

    def test_a_new_episode_re_nudges_even_when_the_duration_grew(self):
        """SC4's missed boundary: two parks with no reading in between.

        Keyed on the duration alone, the second park is suppressed for its whole
        length because the value never fell."""
        self.assertEqual(len(self.nudges(self.run_script(
            reading([row(quiet=16, since="2026-11-08T00:00:00Z")])))), 1)
        self.assertEqual(len(self.nudges(self.run_script(
            reading([row(quiet=40, since="2026-11-08T00:30:00Z")])))), 1,
            "a different episode timestamp must re-arm suppression")

    def test_an_old_transcript_does_not_nudge_a_young_watch(self):
        """SC2: quiet 99m but watching 12m — a stale transcript alone never nudges."""
        p = self.run_script(FIXTURE["stale_transcript"])
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn("quiet 214m", lines[0])
        self.assertNotIn("bbbbbbbb-0000-4000-8000-000000000013", p.stdout)
        self.assertIn("1 within 15m", p.stdout)

    def test_exactly_fifteen_is_inside_the_allowance(self):
        p = self.run_script(reading([row(quiet=15)]))
        self.assertEqual(self.nudges(p), [], p.stdout)
        p = self.run_script(reading([row(quiet=16)]))
        self.assertEqual(len(self.nudges(p)), 1, p.stdout)

    # --- SC2: the nudge carries the park ------------------------------------

    def test_nudge_text_carries_register_defer_time_and_self_exit_path(self):
        fake = self.tmp / "vcs"
        fake.mkdir()
        (fake / "session-self-exit.py").write_text("")
        p = self.run_script(FIXTURE["threshold"], extra_env={"VAULT_CLI_SCRIPTS": str(fake)})
        msg = self.nudges(p)[0].split(" :: ", 1)[1]
        self.assertIn(str(SCRIPT.parent / "park-event.py") + " register --task", msg)
        self.assertIn("--recheck", msg)
        self.assertIn("`defer_date`", msg)
        self.assertIn("ISO-8601 with offset", msg)
        self.assertIn("Never a bare date", msg)
        self.assertIn(f"python3 {fake / 'session-self-exit.py'}", msg)

    def test_self_exit_falls_back_to_vault_cli_not_this_plugin(self):
        home = self.tmp / "home"
        for ver in ("0.9.0", "0.10.0"):
            d = home / ".claude/plugins/cache/vault-cli/vault-cli" / ver / "scripts"
            d.mkdir(parents=True)
            (d / "session-self-exit.py").write_text("")
        env = {"HOME": str(home), "VAULT_CLI_SCRIPTS": ""}
        msg = self.nudges(self.run_script(FIXTURE["threshold"], extra_env=env))[0]
        self.assertIn("vault-cli/vault-cli/0.10.0/scripts/session-self-exit.py", msg)
        self.assertNotIn(str(SCRIPT.parent / "session-self-exit.py"), msg)

    # --- SC3: the bucket is the gate ----------------------------------------

    def test_only_the_watching_row_emits_though_every_row_is_over_threshold(self):
        watched = {r["bucket"]: r["watching_min"] for r in FIXTURE["buckets"]["sessions"]}
        self.assertEqual(watched, {"working": 40, "gated": 22, "idle": 60, "watching": 31})
        p = self.run_script(FIXTURE["buckets"])
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn(OVER, lines[0])

    # --- SC4: one nudge per watching episode --------------------------------

    def test_suppression_survives_growing_duration_and_rearms_on_reentry(self):
        runs = [("watching", 31, 1), ("watching", 35, 0), ("gated", 35, 0), ("watching", 35, 1)]
        for i, (bucket, quiet, want) in enumerate(runs, 1):
            p = self.run_script(reading([row(bucket=bucket, quiet=quiet)]))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(len(self.nudges(p)), want, f"run {i} ({bucket} {quiet}m): {p.stdout}")

    def test_a_duration_dropping_starts_a_new_episode(self):
        self.assertEqual(len(self.nudges(self.run_script(reading([row(quiet=31)])))), 1)
        # A fresh measurement (e.g. after a transcript rotation): nudge again.
        self.assertEqual(len(self.nudges(self.run_script(reading([row(quiet=20)])))), 1)

    def test_absence_from_the_reading_ends_the_episode(self):
        self.run_script(reading([row(quiet=31)]))
        self.run_script(reading([]))
        self.assertEqual(len(self.nudges(self.run_script(reading([row(quiet=40)])))), 1)

    def test_dry_run_never_writes_the_ledger(self):
        self.run_script(reading([row(quiet=31)]), "--dry-run")
        self.assertFalse(self.state.exists())
        self.assertEqual(len(self.nudges(self.run_script(reading([row(quiet=31)])))), 1)

    # --- an unread fleet is never a clean one -------------------------------

    def test_unreadable_reading_is_unknown_not_zero(self):
        for bad in ("", "not json", json.dumps({"counts": {}})):
            p = self.run_script(bad)
            self.assertEqual(p.returncode, 2, bad)
            self.assertTrue(p.stdout.startswith("park-nudge: unknown"), p.stdout)
            self.assertEqual(self.nudges(p), [])

    def test_corrupt_ledger_is_unknown(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("[]")
        p = self.run_script(reading([row(quiet=31)]))
        self.assertEqual(p.returncode, 2)
        self.assertIn("ledger unreadable", p.stdout)

    def test_threshold_is_read_from_park_event(self):
        src = SCRIPT.read_text()
        self.assertIn("PARK_THRESHOLD_SECONDS", src)
        self.assertNotIn("15 * 60", src)


if __name__ == "__main__":
    unittest.main()
