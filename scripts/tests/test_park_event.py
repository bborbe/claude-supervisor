"""park-event.py: the park threshold and the one-watch-per-event store.

The defect this guards is not a crash. It is a worker that keeps its fleet slot
for hours because two rules disagreed about how long a wait may be lived
through, and a pair of workers that each armed a watcher for the same event
because nothing recorded that the events were the same. So the fixtures here are
about *agreement and identity*, not about parsing:

- **The threshold has one home.** A doc that restates a number is the way the
  permissive rule won last time, so the number is asserted here once and every
  other surface is expected to read it from this script.
- **The re-check is a TIME.** Date-only input is refused rather than defaulted
  to midnight — a silent midnight default is exactly the truncation the park
  work removes, reintroduced on the write side where it would be invisible.
- **Two sessions on one event are one watch**, and the earliest wake wins, so
  joining never delays a peer who is already parked.
- **Distinct keys stay distinct.** A slug that collided two keys would merge two
  unrelated watches silently, which is worse than no dedup at all.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "park-event.py"


class ParkEventTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = pathlib.Path(self._tmp.name) / "state"
        self.state.mkdir(parents=True)

    def run_script(self, *args, extra_env=None):
        env = dict(os.environ)
        env["PARK_EVENT_STATE_DIR"] = str(self.state)
        env.pop("PARK_EVENT_NOW", None)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, env=env, check=False,
        )

    # --- the threshold ------------------------------------------------------

    def test_threshold_has_one_home_and_prints_15m(self):
        human = self.run_script("threshold")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(human.stdout.strip(), "15m")

        raw = self.run_script("threshold", "--seconds")
        self.assertEqual(raw.returncode, 0, raw.stderr)
        self.assertEqual(raw.stdout.strip(), "900")

    # --- the re-check is an instant ----------------------------------------

    def test_date_only_recheck_is_refused(self):
        proc = self.run_script(
            "register", "--event", "a metric", "--recheck", "2026-11-02",
            "--session", "s1",
        )
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("date-only", proc.stderr)
        self.assertEqual(list(self.state.glob("*.json")), [])

    def test_recheck_without_offset_is_refused(self):
        proc = self.run_script(
            "register", "--event", "a metric", "--recheck", "2026-11-02T02:00:00",
            "--session", "s1",
        )
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("no offset", proc.stderr)

    # --- one watch per event ------------------------------------------------

    def test_two_sessions_on_one_event_share_one_watch(self):
        first = self.run_script(
            "register", "--event", "executor_config_accepted metric",
            "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(first.stdout.startswith("created "), first.stdout)

        second = self.run_script(
            "register", "--event", "executor_config_accepted metric",
            "--recheck", "2026-11-02T03:00:00+01:00", "--session", "bbbbbbbb",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(second.stdout.startswith("joined "), second.stdout)

        watches = list(self.state.glob("*.json"))
        self.assertEqual(len(watches), 1, watches)
        record = json.loads(watches[0].read_text())
        self.assertEqual(sorted(record["sessions"]), ["aaaaaaaa", "bbbbbbbb"])
        # The earliest wake wins, so joining never delays the peer already parked.
        self.assertEqual(record["recheck_at"], "2026-11-02T02:00:00+01:00")

    def test_join_on_a_record_missing_its_instant_takes_the_new_one(self):
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")
        path = next(iter(self.state.glob("*.json")))
        record = json.loads(path.read_text())
        del record["recheck_at"]
        path.write_text(json.dumps(record))

        proc = self.run_script(
            "register", "--event", "a metric",
            "--recheck", "2026-11-02T04:00:00+01:00", "--session", "bbbbbbbb",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        joined = json.loads(path.read_text())
        self.assertEqual(joined["recheck_at"], "2026-11-02T04:00:00+01:00")
        self.assertEqual(sorted(joined["sessions"]), ["aaaaaaaa", "bbbbbbbb"])

    def test_distinct_keys_with_similar_names_do_not_collide(self):
        self.run_script("register", "--event", "metric a/b",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "s1")
        self.run_script("register", "--event", "metric a-b",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "s2")
        self.assertEqual(len(list(self.state.glob("*.json"))), 2)

    def test_an_earlier_recheck_shortens_the_watch(self):
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T04:00:00+01:00", "--session", "aaaaaaaa")
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "bbbbbbbb")
        record = json.loads(next(iter(self.state.glob("*.json"))).read_text())
        self.assertEqual(record["recheck_at"], "2026-11-02T02:00:00+01:00")

    def test_register_without_a_session_is_refused(self):
        proc = self.run_script("register", "--event", "a metric",
                               "--recheck", "2026-11-02T02:00:00+01:00")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("no session id", proc.stderr)
        self.assertEqual(list(self.state.glob("*.json")), [])

    def test_a_corrupt_watch_is_refused_not_replaced(self):
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")
        path = next(iter(self.state.glob("*.json")))
        path.write_text("{ not json")

        proc = self.run_script("register", "--event", "a metric",
                               "--recheck", "2026-11-02T05:00:00+01:00", "--session", "bbbbbbbb")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("unreadable", proc.stderr)
        # The live membership must survive: a corrupt record is not an absent one.
        self.assertEqual(path.read_text(), "{ not json")

    def test_release_of_a_non_member_is_refused(self):
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")
        path = next(iter(self.state.glob("*.json")))
        before = path.read_text()

        proc = self.run_script("release", "--event", "a metric", "--session", "zzzzzzzz")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("not a member", proc.stderr)
        self.assertEqual(path.read_text(), before)

    def test_release_of_an_unknown_event_exits_3(self):
        proc = self.run_script("release", "--event", "nope", "--session", "aaaaaaaa")
        self.assertEqual(proc.returncode, 3, proc.stdout)

    def test_concurrent_registers_keep_every_member(self):
        sessions = [f"session{i:04d}" for i in range(8)]
        env = dict(os.environ)
        env["PARK_EVENT_STATE_DIR"] = str(self.state)
        env.pop("PARK_EVENT_NOW", None)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        procs = [
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "register", "--event", "same event",
                 "--recheck", "2026-11-02T02:00:00+01:00", "--session", s],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            for s in sessions
        ]
        for proc in procs:
            proc.communicate(timeout=30)
        record = json.loads(next(iter(self.state.glob("*.json"))).read_text())
        self.assertEqual(sorted(record["sessions"]), sessions)

    def test_list_reports_one_line_per_watch(self):
        self.run_script("register", "--event", "first", "--session", "aaaaaaaa",
                        "--recheck", "2026-11-02T02:00:00+01:00")
        self.run_script("register", "--event", "second", "--session", "bbbbbbbb",
                        "--recheck", "2026-11-02T03:00:00+01:00")
        proc = self.run_script("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 2)

    # --- due ----------------------------------------------------------------

    def test_due_fires_only_after_the_recheck_instant(self):
        self.run_script(
            "register", "--event", "a metric",
            "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa",
        )
        before = self.run_script(
            "due", extra_env={"PARK_EVENT_NOW": "2026-11-02T00:30:00+01:00"})
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertIn("no park watches due", before.stdout)

        after = self.run_script(
            "due", extra_env={"PARK_EVENT_NOW": "2026-11-02T02:30:00+01:00"})
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertIn("resume: aaaaaaaa", after.stdout)

    # --- release ------------------------------------------------------------

    def test_release_removes_the_watch_when_the_last_session_leaves(self):
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "bbbbbbbb")

        one = self.run_script("release", "--event", "a metric", "--session", "aaaaaaaa")
        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertIn("keeps 1 session", one.stdout)
        self.assertEqual(len(list(self.state.glob("*.json"))), 1)

        last = self.run_script("release", "--event", "a metric", "--session", "bbbbbbbb")
        self.assertEqual(last.returncode, 0, last.stderr)
        self.assertEqual(list(self.state.glob("*.json")), [])

    # --- the key lands on the task -----------------------------------------

    def test_register_writes_the_event_key_onto_the_task(self):
        bin_dir = pathlib.Path(self._tmp.name) / "bin"
        bin_dir.mkdir()
        calls = pathlib.Path(self._tmp.name) / "calls.txt"
        stub = bin_dir / "vault-cli"
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{calls}"\n'
            "exit 0\n"
        )
        stub.chmod(0o755)

        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        proc = self.run_script(
            "register", "--task", "Some Task", "--event", "a metric",
            "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa",
            extra_env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = calls.read_text()
        self.assertIn("task set Some Task park_event a metric", written)
        self.assertIn("task set Some Task park_recheck 2026-11-02T02:00:00+01:00", written)

    def test_a_refused_task_write_is_reported_not_swallowed(self):
        bin_dir = pathlib.Path(self._tmp.name) / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "vault-cli"
        stub.write_text("#!/bin/sh\necho 'unknown key' >&2\nexit 1\n")
        stub.chmod(0o755)

        proc = self.run_script(
            "register", "--task", "Some Task", "--event", "a metric",
            "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa",
            extra_env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
        )
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("unknown key", proc.stderr)


if __name__ == "__main__":
    unittest.main()
