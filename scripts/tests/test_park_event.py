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
- **One identifier per watch.** `list` and `due` print the slug and a caller
  copies it straight back into `--event`, so `register` and `release` resolve it
  to the stored description rather than keying on it directly. A refusal for an
  unknown key names the keys that DO exist — a bare "no watch" reads as "nothing
  is parked", which is the one answer that sends the caller away.
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

    def stub_vault_cli(self, vault_dir, tasks_dir=None):
        """A `vault-cli` that answers `task show` / `config list` and logs calls.

        The real CLI is the only thing that can name a task's file, so a stub
        that prints nothing makes `register` refuse — which is the behaviour
        under test in `test_a_refused_task_write_is_reported_not_swallowed`,
        and noise everywhere else.

        `vault_dir` is what `config list` reports as the vault path;
        `tasks_dir` is where `task show` resolves pages, defaulting to the same
        directory so a register fixture can keep its pages flat.
        """
        bin_dir = pathlib.Path(self._tmp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        calls = pathlib.Path(self._tmp.name) / "calls.txt"
        stub = bin_dir / "vault-cli"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            'with open(os.environ["STUB_CALLS"], "a") as fh:\n'
            '    fh.write(" ".join(args) + "\\n")\n'
            'if args[:2] == ["task", "show"]:\n'
            '    print(json.dumps({"file_path": os.path.join(\n'
            '        os.environ["STUB_TASKS"], args[2] + ".md")}))\n'
            "    sys.exit(0)\n"
            'if args[:2] == ["config", "list"]:\n'
            "    print(json.dumps([{\"name\": \"stubvault\",\n"
            '        "path": os.environ["STUB_VAULT"], "tasks_dir": "25 Tasks"}]))\n'
            "    sys.exit(0)\n"
            "sys.exit(0)\n"
        )
        stub.chmod(0o755)
        return {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_CALLS": str(calls),
            "STUB_TASKS": str(tasks_dir or vault_dir),
            "STUB_VAULT": str(vault_dir),
            "PARK_EVENT_NOW": "2026-11-01T00:00:00+01:00",
        }

    def task_page(self, name, body="# Progress\n\n- 2026-11-01 — filed\n"):
        tasks = pathlib.Path(self._tmp.name) / "tasks"
        tasks.mkdir(exist_ok=True)
        page = tasks / f"{name}.md"
        page.write_text(
            "---\nstatus: in_progress\ndefer_date: 2026-11-02\n---\n\n"
            f"# Impact\n\nsomething\n\n{body}"
        )
        return tasks, page

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

    def test_due_names_the_task_so_the_round_can_open_it(self):
        self.run_script("register", "--task", "Some Task", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")
        proc = self.run_script("due", extra_env={"PARK_EVENT_NOW": "2026-11-02T03:00:00+01:00"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Some Task [aaaaaaaa]", proc.stdout)

    def test_repark_counter_persists_and_increments(self):
        self.run_script("register", "--task", "Some Task", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")
        for _ in range(2):
            self.run_script("register", "--task", "Some Task", "--event", "a metric",
                            "--recheck", "2026-11-03T02:00:00+01:00",
                            "--session", "aaaaaaaa", "--repark")
        record = json.loads(next(iter(self.state.glob("*.json"))).read_text())
        self.assertEqual(record["repark_count"], 2)
        # Re-parking must not add the round as a member.
        self.assertEqual(record["sessions"], ["aaaaaaaa"])
        # And it must MOVE the instant forward: the earliest-wake rule applies to
        # joins, not re-parks. A stale past instant would leave the watch due and
        # re-fire it every round, tripping the escalation in consecutive ticks.
        self.assertEqual(record["recheck_at"], "2026-11-03T02:00:00+01:00")

    def test_repark_advances_every_member_task_not_just_the_named_one(self):
        calls = pathlib.Path(self._tmp.name) / "calls.txt"
        bin_dir = pathlib.Path(self._tmp.name) / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "vault-cli"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit 0\n')
        stub.chmod(0o755)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

        for session, task in (("aaaaaaaa", "Task A"), ("bbbbbbbb", "Task B")):
            self.run_script("register", "--task", task, "--event", "a metric",
                            "--recheck", "2026-11-02T02:00:00+01:00",
                            "--session", session, extra_env=env)
        calls.write_text("")

        proc = self.run_script(
            "register", "--task", "Task A", "--event", "a metric",
            "--recheck", "2026-11-05T02:00:00+01:00",
            "--session", "aaaaaaaa", "--repark", extra_env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = calls.read_text()
        # The peer must move too: a past defer is not "deferred" to the reader,
        # so leaving Task B behind resumes it into a wait that has not fired.
        self.assertIn("task set Task A defer_date 2026-11-05T02:00:00+01:00", written)
        self.assertIn("task set Task B defer_date 2026-11-05T02:00:00+01:00", written)
        self.assertIn("advanced: Task B", proc.stdout)
        # ...and the watch is still counted once, not once per member.
        record = json.loads(next(iter(self.state.glob("*.json"))).read_text())
        self.assertEqual(record["repark_count"], 1)

    def test_a_corrupt_watch_is_surfaced_by_due_not_hidden(self):
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")
        next(iter(self.state.glob("*.json"))).write_text("{ not json")
        proc = self.run_script("due", extra_env={"PARK_EVENT_NOW": "2026-11-02T03:00:00+01:00"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CORRUPT", proc.stdout)

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

    # --- the printed slug is a valid key ------------------------------------

    def slug_of(self, event):
        """The token `list`/`due` print first — what a caller copies verbatim."""
        proc = self.run_script("list")
        for line in proc.stdout.splitlines():
            if f"event: {event} ·" in line:
                return line.split(" · ")[0]
        self.fail(f"no watch line for {event!r}: {proc.stdout!r}")

    def test_release_accepts_the_slug_that_list_prints(self):
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")
        slug = self.slug_of("a metric")

        proc = self.run_script("release", "--event", slug, "--session", "aaaaaaaa")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(list(self.state.glob("*.json")), [])

    def test_register_by_the_printed_slug_joins_the_one_watch(self):
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")
        slug = self.slug_of("a metric")

        joined = self.run_script("register", "--event", slug,
                                 "--recheck", "2026-11-02T02:00:00+01:00",
                                 "--session", "bbbbbbbb")
        self.assertEqual(joined.returncode, 0, joined.stderr)
        # One watch carrying both members: a second file here is the defect, not
        # a join. `slugify` appends a digest, so re-slugifying the printed slug
        # would land on a different path if the resolver were not there.
        self.assertEqual(len(list(self.state.glob("*.json"))), 1)
        listing = self.run_script("list")
        self.assertEqual(len(listing.stdout.strip().splitlines()), 1, listing.stdout)
        self.assertIn("sessions: 2", listing.stdout)

    def test_release_of_an_unknown_event_names_the_keys_that_exist(self):
        self.run_script("register", "--event", "a metric",
                        "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa")

        proc = self.run_script("release", "--event", "no-such-event", "--session", "aaaaaaaa")
        self.assertEqual(proc.returncode, 3, proc.stdout)
        self.assertIn("no-such-event", proc.stderr)
        self.assertIn("a metric", proc.stderr)

    def test_release_with_no_watches_at_all_says_none(self):
        proc = self.run_script("release", "--event", "no-such-event", "--session", "aaaaaaaa")
        self.assertEqual(proc.returncode, 3, proc.stdout)
        self.assertIn("(known: none)", proc.stderr)

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
        self.assertIn("task set Some Task defer_date 2026-11-02T02:00:00+01:00", written)

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


    # --- the park record the round reads ------------------------------------

    def test_register_writes_the_later_on_record_into_progress(self):
        tasks, page = self.task_page("Some Task")
        proc = self.run_script(
            "register", "--task", "Some Task", "--event", "a metric",
            "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa",
            extra_env=self.stub_vault_cli(tasks),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = page.read_text()
        # The round greps for `later (on `; without it a correctly deferred row
        # reads `unverifiable` and its slot is never freed.
        self.assertIn("later (on a metric):", text)
        self.assertIn("2026-11-02T02:00:00+01:00", text)
        # Inside `# Progress`, after the entry already there — not appended to
        # whatever section the page happens to end with.
        self.assertGreater(text.index("later (on a metric):"), text.index("— filed"))

    def test_register_creates_a_progress_section_when_the_page_has_none(self):
        tasks, page = self.task_page("Bare Task", body="")
        proc = self.run_script(
            "register", "--task", "Bare Task", "--event", "a metric",
            "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa",
            extra_env=self.stub_vault_cli(tasks),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = page.read_text()
        self.assertIn("# Progress", text)
        self.assertIn("later (on a metric):", text)

    def test_a_repark_rewrites_the_record_instead_of_stacking_lines(self):
        tasks, page = self.task_page("Some Task")
        env = self.stub_vault_cli(tasks)
        for recheck, extra in (
            ("2026-11-02T02:00:00+01:00", ()),
            ("2026-11-05T05:00:00+01:00", ("--repark",)),
        ):
            proc = self.run_script(
                "register", "--task", "Some Task", "--event", "a metric",
                "--recheck", recheck, "--session", "aaaaaaaa", *extra,
                extra_env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        text = page.read_text()
        self.assertEqual(text.count("later (on a metric):"), 1, text)
        # The line names the CURRENT re-check: a stale instant would tell the
        # round the wait is still running against a date that has passed.
        self.assertIn("2026-11-05T05:00:00+01:00", text)
        self.assertNotIn("2026-11-02T02:00:00+01:00", text)

    def test_a_repark_rewrites_the_peers_record_too(self):
        tasks, _page_a = self.task_page("Task A")
        _tasks, page_b = self.task_page("Task B")
        env = self.stub_vault_cli(tasks)
        for session, task in (("aaaaaaaa", "Task A"), ("bbbbbbbb", "Task B")):
            self.run_script(
                "register", "--task", task, "--event", "a metric",
                "--recheck", "2026-11-02T02:00:00+01:00", "--session", session,
                extra_env=env,
            )
        proc = self.run_script(
            "register", "--task", "Task A", "--event", "a metric",
            "--recheck", "2026-11-05T05:00:00+01:00", "--session", "aaaaaaaa",
            "--repark", extra_env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The peer's clock moves, so its record must move with it.
        self.assertIn("2026-11-05T05:00:00+01:00", page_b.read_text())
        self.assertNotIn("2026-11-02T02:00:00+01:00", page_b.read_text())

    def test_a_level_two_progress_section_is_used_rather_than_duplicated(self):
        tasks, page = self.task_page("Level Two", body="## Progress\n\n- 2026-11-01 — filed\n")
        proc = self.run_script(
            "register", "--task", "Level Two", "--event", "a metric",
            "--recheck", "2026-11-02T02:00:00+01:00", "--session", "aaaaaaaa",
            extra_env=self.stub_vault_cli(tasks),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = page.read_text()
        # The vault is not uniform — 53 task pages carry `## Progress`. Writing a
        # second `# Progress` beside it is the page being made wrong, not fixed.
        self.assertEqual(text.count("Progress"), 1, text)
        self.assertGreater(text.index("later (on a metric):"), text.index("## Progress"))

    def test_no_record_is_written_for_a_past_recheck(self):
        tasks, page = self.task_page("Some Task")
        proc = self.run_script(
            "register", "--task", "Some Task", "--event", "a metric",
            "--recheck", "2026-10-01T02:00:00+01:00", "--session", "aaaaaaaa",
            extra_env=self.stub_vault_cli(tasks),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # A past instant is not a park: the row is due, so a `later (on …)` line
        # would describe a wait that is not running.
        self.assertNotIn("later (on ", page.read_text())

    def test_backfill_writes_the_record_only_where_it_is_missing(self):
        vault = pathlib.Path(self._tmp.name) / "vault"
        tasks = vault / "25 Tasks"
        tasks.mkdir(parents=True)

        def page(name, status="in_progress", defer="2026-11-20", body=""):
            (tasks / f"{name}.md").write_text(
                f"---\nstatus: {status}\ndefer_date: {defer}\n---\n\n"
                f"# Progress\n\n{body}"
            )

        page("Deferred No Record")
        page("Another Deferred")
        page("Deferred With Record", body="- later (on something): the round re-opens it\n")
        page("Deferred Past", defer="2026-10-01")
        page("Done", status="completed")

        env = self.stub_vault_cli(vault)
        dry = self.run_script("backfill", "--vault", "stubvault", extra_env=env)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("3 future-dated in_progress | 2 missing later(on) | 0 written", dry.stdout)
        self.assertNotIn("later (on ", (tasks / "Deferred No Record.md").read_text())

        applied = self.run_script("backfill", "--vault", "stubvault", "--apply", extra_env=env)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("3 future-dated in_progress | 2 missing later(on) | 2 written", applied.stdout)
        self.assertIn("later (on ", (tasks / "Deferred No Record.md").read_text())

        again = self.run_script("backfill", "--vault", "stubvault", "--apply", extra_env=env)
        self.assertIn("0 missing later(on) | 0 written", again.stdout)


if __name__ == "__main__":
    unittest.main()
