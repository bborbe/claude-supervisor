"""progress-window.py: the measured progress window, one fixture per class.

The defect this guards: `/supervisor:worker-drive` took "last progress" from a
`session_digest` the session wrote about itself, so a session going in circles read
healthy. `docs/session-progress.md` replaces that account with artifacts on disk; this file pins
one fixture per class it defines -- each with its expected verdict, grade or claim
result.

Every fixture is built from real git repositories and a real transcript file in a temp
dir: commit timestamps are set through `GIT_COMMITTER_DATE`, so the window is decided by
the artifact's own time, exactly as in production. `--now` is pinned to `--until`, so the
working tree is read as the window's end state.

Three things are load-bearing, and each is a way the suite could be plausibly wrong:

- **Grade A is never inferred.** A tick is A only with a `measured` coverage row; the
  same tick with no coverage is B (`rating unavailable`), and with `asserted` it is C and
  not Moved. Each of the three is its own fixture.
- **The one live lever is pinned on both sides.** `short_path` is true only for a claim
  backed by grade A; every B-backed claim asserts `short_path: false`.
- **UNKNOWN is a third state.** An unknown session and an unreadable transcript both
  exit 2 -- never a verdict, never exit 0.
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "progress-window.py"

SID = "11111111-2222-3333-4444-555555555555"
SESSION_START = datetime.datetime(2026, 10, 18, 10, 0, tzinfo=datetime.timezone.utc)
SINCE = datetime.datetime(2026, 10, 18, 11, 45, tzinfo=datetime.timezone.utc)
UNTIL = datetime.datetime(2026, 10, 18, 12, 0, tzinfo=datetime.timezone.utc)


def at(minutes):
    """A stamp `minutes` after SINCE (negative is before the window)."""
    return SINCE + datetime.timedelta(minutes=minutes)


def iso(stamp):
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def anchor_text(sc=("[ ] SC1: first criterion", "[ ] SC2: second criterion"),
                dod=("[ ] make precommit green",), progress=(), summary="Summary line."):
    return "\n".join([
        "---", "goals:", "    - '[[The Goal]]'", "---", "Tags: [[Task]]", "", "---", "",
        summary, "", "# Success Criteria", "",
        *["- " + line for line in sc], "", "# Definition of Done", "",
        *["- " + line for line in dod], "", "# Tasks", "", "- [ ] do the work", "",
        "# Progress", "", *progress, "",
    ])


class Env:
    """One fixture world: a vault repo with the anchor, a code repo, a transcript, a registry."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.root = root
        self.projects = root / "projects"
        self.sessions = root / "sessions"
        self.state = root / "state"
        for d in (self.projects / "proj", self.sessions, self.state):
            d.mkdir(parents=True)
        self.vault = self.repo("vault")
        (self.vault / "25 Tasks").mkdir()
        (self.vault / "24 Goals").mkdir()
        (self.vault / "24 Goals" / "The Goal.md").write_text(
            "# Tasks\n\n- [ ] [[T]] — closes SC2: worker tier\n", encoding="utf-8")
        self.anchor = self.vault / "25 Tasks" / "T.md"
        self.code = self.repo("code")
        self.records = [{"type": "user", "timestamp": iso(SESSION_START),
                         "message": {"role": "user", "content": "start"}}]
        self.transcript = self.projects / "proj" / (SID + ".jsonl")
        self.registered(started=SESSION_START)
        self.base = self.commit_anchor(anchor_text(), at(-100), "initial")
        self._n = 0

    def close(self):
        if self.transcript.exists():
            os.chmod(self.transcript, 0o644)
        self._tmp.cleanup()

    # git ---------------------------------------------------------------------------

    def repo(self, name):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "-q", "-b", "master")
        return path

    def git(self, repo, *args, when=None):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        if when is not None:
            stamp = when.strftime("%Y-%m-%d %H:%M:%S +0000")
            env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        proc = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
            cwd=repo, env=env, capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def commit(self, repo, message, when, paths=(), allow_empty=False):
        for p in paths:
            self.git(repo, "add", "--", str(p))
        extra = ["--allow-empty"] if allow_empty else []
        self.git(repo, "commit", "-q", *extra, "-m", message, when=when)
        return self.git(repo, "rev-parse", "HEAD")

    def commit_anchor(self, text, when, message="auto"):
        self.anchor.write_text(text, encoding="utf-8")
        return self.commit(self.vault, message, when, [self.anchor])

    def commit_code(self, name, body, when, message):
        path = self.code / name
        path.write_text(body, encoding="utf-8")
        self.call("Write", {"file_path": str(path), "content": body}, when - datetime.timedelta(seconds=30))
        return self.commit(self.code, message, when, [path])

    # transcript --------------------------------------------------------------------

    def call(self, name, data, when, result="ok", took=5, pending=False):
        self._n += 1
        ident = "toolu_%04d" % self._n
        self.records.append({"type": "assistant", "timestamp": iso(when), "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": ident, "name": name, "input": data}]}})
        # api-request-blob repeats the call; it must never be counted.
        self.records.append({"type": "api-request-blob", "timestamp": iso(when), "message": {
            "content": [{"type": "tool_use", "id": ident + "x", "name": name, "input": data}]}})
        if not pending:
            done = when + datetime.timedelta(seconds=took)
            self.records.append({"type": "user", "timestamp": iso(done), "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": ident, "content": result}]}})

    def bash(self, command, when, result="ok", **kw):
        self.call("Bash", {"command": command}, when, result, **kw)

    def say(self, text, when):
        self.records.append({"type": "assistant", "timestamp": iso(when), "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}]}})

    def registered(self, started, status="busy", updated=None):
        stamp = lambda d: int(d.timestamp() * 1000)
        (self.sessions / "4242.json").write_text(json.dumps({
            "pid": 4242, "sessionId": SID, "cwd": str(self.vault), "status": status,
            "startedAt": stamp(started), "statusUpdatedAt": stamp(updated or started)}), encoding="utf-8")

    def coverage(self, **ratings):
        rows = ["SC1: first criterion | do the work | %s | —" % ratings.get("SC1", "none"),
                "SC2: second criterion | do the work | %s | —" % ratings.get("SC2", "none"),
                "make precommit green | none | none | create",
                "Coverage: 3 boxes · driving: 2 driven · 1 none · evidence: ..."]
        path = self.root / "coverage.txt"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return path

    # run ---------------------------------------------------------------------------

    def run(self, *extra, since=SINCE, until=UNTIL, session=SID, claim=None, coverage=None):
        with open(self.transcript, "w", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(json.dumps(record) + "\n")
        return self.invoke(*extra, since=since, until=until, session=session, claim=claim, coverage=coverage)

    def invoke(self, *extra, since=SINCE, until=UNTIL, session=SID, claim=None, coverage=None):
        args = ["--session", session, "--anchor", str(self.anchor), "--json", "--now", iso(until)]
        if since is not None:
            args += ["--since", iso(since)]
        if until is not None:
            args += ["--until", iso(until)]
        if claim is not None:
            args += ["--claim", claim if isinstance(claim, str) else json.dumps(claim)]
        if coverage is not None:
            args += ["--coverage", str(coverage)]
        env = dict(os.environ, SUPERVISOR_PROJECTS_DIR=str(self.projects),
                   SUPERVISOR_SESSIONS_DIR=str(self.sessions), SUPERVISOR_STATE_DIR=str(self.state))
        proc = subprocess.run([sys.executable, str(SCRIPT), *args, *extra],
                              capture_output=True, text=True, env=env)
        report = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc.returncode, report, proc.stderr


class Case(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def tick(self, when, sc=("[x] SC1: first criterion", "[ ] SC2: second criterion"), **kw):
        return self.env.commit_anchor(anchor_text(sc=sc, **kw), when)

    def ok(self, result):
        code, report, err = result
        self.assertEqual(code, 0, err)
        return report


class TestBusyNoMove(Case):
    def test_three_identical_polls_and_no_write_is_loop(self):
        for minute in (2, 6, 10):
            self.env.bash("gh pr checks 412", at(minute), result="pending")
        report = self.ok(self.env.run())
        self.assertEqual((report["verdict"], report["subtype"]), ("BUSY-NO-MOVE", "LOOP"))
        self.assertEqual(report["activity"]["top_repeat"]["count"], 3)
        self.assertEqual(report["moved"], [])

    def test_ten_reads_and_no_write_is_read_loop(self):
        for i in range(10):
            self.env.call("Read", {"file_path": "/x/%d.md" % i}, at(i + 1))
        report = self.ok(self.env.run())
        self.assertEqual((report["verdict"], report["subtype"]), ("BUSY-NO-MOVE", "READ-LOOP"))

    def test_an_edit_reverted_is_oscillate(self):
        path = str(self.env.root / "scratch.txt")
        self.env.call("Edit", {"file_path": path, "old_string": "a", "new_string": "b"}, at(2))
        self.env.call("Edit", {"file_path": path, "old_string": "b", "new_string": "a"}, at(4))
        report = self.ok(self.env.run())
        self.assertEqual((report["verdict"], report["subtype"]), ("BUSY-NO-MOVE", "OSCILLATE"))

    def test_misleading_self_account_with_only_reads(self):
        self.env.say("I made progress on SC2 — the parser is nearly done.", at(1))
        for i in range(3):
            self.env.call("Read", {"file_path": "/x/%d.md" % i}, at(i + 2))
        report = self.ok(self.env.run())
        self.assertEqual(report["verdict"], "BUSY-NO-MOVE")
        self.assertEqual(report["claim"]["result"], "none filed")
        self.assertFalse(report["short_path"])


class TestTicks(Case):
    def test_measured_tick_is_progressing_grade_a(self):
        self.tick(at(5))
        report = self.ok(self.env.run(coverage=self.env.coverage(SC1="measured(progress 2026-10-18)")))
        self.assertEqual(report["verdict"], "PROGRESSING")
        self.assertEqual([(o["grade"], o["boxes"]) for o in report["moved"]], [("A", ["SC1"])])
        self.assertEqual(report["boxes"]["open"], ["SC2", "DoD1"])

    def test_tick_without_coverage_is_b_rating_unavailable(self):
        sha = self.tick(at(5))
        report = self.ok(self.env.run(claim={"box": "SC1", "kind": "tick", "ref": sha}))
        self.assertEqual(report["moved"][0]["grade"], "B")
        self.assertIn("rating unavailable", report["moved"][0]["text"])
        self.assertEqual(report["claim"]["result"], "backed")
        self.assertFalse(report["short_path"])

    def test_measured_tick_claim_takes_the_short_path(self):
        sha = self.tick(at(5))
        report = self.ok(self.env.run(claim={"box": "SC1", "kind": "tick", "ref": sha},
                                      coverage=self.env.coverage(SC1="measured(PR #12)")))
        self.assertEqual(report["claim"]["result"], "backed")
        self.assertTrue(report["short_path"])

    def test_tick_claim_older_than_the_window_predates(self):
        sha = self.tick(at(-30))
        report = self.ok(self.env.run(claim={"box": "SC1", "kind": "tick", "ref": sha},
                                      coverage=self.env.coverage(SC1="measured(PR #12)")))
        self.assertEqual(report["claim"]["result"], "rejected:predates-window")
        self.assertFalse(report["short_path"])

    def test_claim_on_a_box_met_at_window_start_is_already_met(self):
        self.tick(at(-30))
        sha = self.tick(at(5), summary="Summary reworded.")
        report = self.ok(self.env.run(claim={"box": "SC1", "kind": "tick", "ref": sha}))
        self.assertEqual(report["claim"]["result"], "rejected:already-met")

    def test_tick_on_an_asserted_box_is_c_and_not_moved(self):
        sha = self.tick(at(5))
        report = self.ok(self.env.run(claim={"box": "SC1", "kind": "tick", "ref": sha},
                                      coverage=self.env.coverage(SC1="asserted")))
        self.assertEqual(report["claim"]["result"], "rejected:asserted")
        self.assertEqual(report["moved"], [])
        self.assertEqual(report["observations"][0]["grade"], "C")
        self.assertNotEqual(report["verdict"], "PROGRESSING")

    def test_uncommitted_tick_is_read_from_the_working_tree(self):
        self.env.anchor.write_text(anchor_text(sc=("[x] SC1: first criterion", "[ ] SC2: second criterion")),
                                   encoding="utf-8")
        stamp = (UNTIL - datetime.timedelta(minutes=1)).timestamp()
        os.utime(self.env.anchor, (stamp, stamp))
        report = self.ok(self.env.run())
        self.assertEqual([(o["kind"], o["ref"], o["boxes"]) for o in report["moved"]],
                         [("tick", "worktree", ["SC1"])])
        self.assertEqual(report["verdict"], "PROGRESSING")

    def test_coverage_row_with_a_backtick_and_a_pipe_in_the_box_still_rates_it(self):
        dod = "make precommit green, installed version from `ls -dt … | head -1`"
        self.env.commit_anchor(anchor_text(dod=("[ ] " + dod,)), at(-50))
        self.env.commit_anchor(anchor_text(dod=("[x] " + dod,)), at(5))
        path = self.env.root / "coverage-pipe.txt"
        path.write_text("- [x] %s | none | measured(PR #12) | create\n" % dod, encoding="utf-8")
        report = self.ok(self.env.run(coverage=path))
        self.assertEqual([(o["grade"], o["boxes"]) for o in report["moved"]], [("A", ["DoD1"])])

    def test_coverage_row_labelled_with_a_bracketed_box_id_rates_that_box(self):
        dod = "plugin updated locally and the installed version confirmed"
        self.env.commit_anchor(anchor_text(dod=("[ ] " + dod,)), at(-50))
        self.env.commit_anchor(anchor_text(dod=("[x] " + dod,)), at(5))
        path = self.env.root / "coverage-label.txt"
        path.write_text("[DoD1] %s | none | measured(PR #12) | —\n" % dod, encoding="utf-8")
        report = self.ok(self.env.run(coverage=path))
        self.assertEqual(report["sources"]["coverage"]["rows"], 1)
        self.assertEqual([(o["grade"], o["boxes"]) for o in report["moved"]], [("A", ["DoD1"])])

    def test_claim_picks_its_own_box_among_several_uncommitted_ticks(self):
        self.env.anchor.write_text(anchor_text(sc=("[x] SC1: first criterion", "[x] SC2: second criterion")),
                                   encoding="utf-8")
        stamp = (UNTIL - datetime.timedelta(minutes=1)).timestamp()
        os.utime(self.env.anchor, (stamp, stamp))
        report = self.ok(self.env.run(claim={"box": "SC2", "kind": "tick", "ref": "worktree"},
                                      coverage=self.env.coverage(SC1="measured(PR #12)", SC2="measured(PR #12)")))
        self.assertEqual(len(report["moved"]), 2)
        self.assertEqual(report["claim"]["result"], "backed")
        self.assertEqual(report["claim"]["grade"], "A")
        self.assertTrue(report["short_path"])

    def test_summary_only_task_commit_is_c(self):
        self.env.commit_anchor(anchor_text(summary="Summary reworded."), at(5))
        self.env.bash("cat notes.md", at(6))
        report = self.ok(self.env.run())
        self.assertEqual(report["moved"], [])
        self.assertEqual([o["kind"] for o in report["observations"]], ["task-edit"])
        self.assertNotEqual(report["verdict"], "PROGRESSING")

    def test_toward_names_the_goal_box_the_task_link_closes(self):
        self.tick(at(5), sc=("[ ] SC1: first criterion", "[x] SC2: second criterion"))
        report = self.ok(self.env.run())
        self.assertEqual(report["toward"], ["Task SC2 → Goal SC2"])


class TestCommits(Case):
    def test_commit_with_a_real_diff_naming_the_box_is_backed_not_short(self):
        sha = self.env.commit_code("app.py", "print('x')\n", at(5), "parse the window — SC2")
        report = self.ok(self.env.run(claim={"box": "SC2", "kind": "commit", "ref": sha}))
        self.assertEqual(report["claim"]["result"], "backed")
        self.assertEqual(report["claim"]["grade"], "B")
        self.assertFalse(report["short_path"])
        self.assertEqual(report["verdict"], "PROGRESSING")

    def test_commit_naming_no_box_is_asserted(self):
        sha = self.env.commit_code("app.py", "print('x')\n", at(5), "parse the window")
        report = self.ok(self.env.run(claim={"box": "SC2", "kind": "commit", "ref": sha}))
        self.assertEqual(report["claim"]["result"], "rejected:asserted")
        self.assertEqual(report["toward"], ["untagged"])

    def test_empty_commit_is_c_and_asserted(self):
        self.env.commit_code("app.py", "print('x')\n", at(-50), "base")
        sha = self.env.commit(self.env.code, "SC2 progress", at(5), allow_empty=True)
        report = self.ok(self.env.run(claim={"box": "SC2", "kind": "commit", "ref": sha}))
        self.assertEqual([o["grade"] for o in report["observations"]], ["C"])
        self.assertEqual(report["claim"]["result"], "rejected:asserted")

    def test_whitespace_only_commit_is_c(self):
        self.env.commit_code("app.py", "print('x')\n", at(-50), "base")
        sha = self.env.commit_code("app.py", "print('x')   \n", at(5), "tidy SC2")
        report = self.ok(self.env.run(claim={"box": "SC2", "kind": "commit", "ref": sha}))
        self.assertEqual([o["grade"] for o in report["observations"]], ["C"])
        self.assertEqual(report["claim"]["result"], "rejected:asserted")

    def test_commit_in_a_repo_outside_scope_is_out_of_scope(self):
        other = self.env.repo("other")
        (other / "f.txt").write_text("x\n", encoding="utf-8")
        sha = self.env.commit(other, "SC2 work", at(5), [other / "f.txt"])
        report = self.ok(self.env.run(claim={"box": "SC2", "kind": "commit", "ref": sha, "repo": str(other)}))
        self.assertEqual(report["claim"]["result"], "rejected:out-of-scope")

    def test_sibling_commit_in_a_shared_root_is_not_counted(self):
        self.env.commit_code("app.py", "print('x')\n", at(-50), "base")
        (self.env.code / "theirs.py").write_text("y\n", encoding="utf-8")
        self.env.commit(self.env.code, "sibling SC2", at(5), [self.env.code / "theirs.py"])
        report = self.ok(self.env.run())
        self.assertEqual(report["observations"], [])


class TestClaims(Case):
    def test_claim_with_no_ref_is_narrative(self):
        report = self.ok(self.env.run(claim={"box": "SC2", "kind": "commit"}))
        self.assertEqual(report["claim"]["result"], "rejected:narrative")

    def test_malformed_claim_json_is_narrative(self):
        report = self.ok(self.env.run(claim="I made progress on SC2"))
        self.assertEqual(report["claim"]["result"], "rejected:narrative")

    def test_pr_test_and_phase_claims_are_asserted(self):
        for kind in ("pr", "test", "phase"):
            report = self.ok(self.env.run(claim={"box": "SC2", "kind": kind, "ref": "412"}))
            self.assertEqual(report["claim"]["result"], "rejected:asserted", kind)

    def test_progress_line_that_only_narrates_is_c_and_asserted(self):
        self.env.commit_anchor(anchor_text(progress=("- 2026-10-18: SC2 done",)), at(5))
        report = self.ok(self.env.run(claim={"box": "SC2", "kind": "progress-entry", "ref": "SC2 done"}))
        self.assertEqual([(o["kind"], o["grade"]) for o in report["observations"]], [("progress-entry", "C")])
        self.assertEqual(report["claim"]["result"], "rejected:asserted")

    def test_progress_line_citing_a_resolving_sha_is_b_and_backed(self):
        line = "- 2026-10-18: SC2 done in %s" % self.env.base[:10]
        self.env.commit_anchor(anchor_text(progress=(line,)), at(5))
        report = self.ok(self.env.run(claim={"box": "SC2", "kind": "progress-entry", "ref": "SC2 done"}))
        self.assertEqual(report["moved"][0]["grade"], "B")
        self.assertEqual(report["claim"]["result"], "backed")
        self.assertFalse(report["short_path"])

    def test_json_echoes_coverage_path_hash_and_claim(self):
        coverage = self.env.coverage(SC1="measured(PR #12)")
        claim = {"box": "SC1", "kind": "tick", "ref": "abcdef1"}
        report = self.ok(self.env.run(claim=claim, coverage=coverage))
        self.assertEqual(report["sources"]["coverage"]["path"], str(coverage))
        self.assertEqual(len(report["sources"]["coverage"]["sha256"]), 64)
        self.assertEqual(report["claim"]["input"], claim)
        self.assertEqual((report["since"], report["until"]), (iso(SINCE), iso(UNTIL)))


class TestStates(Case):
    def test_young_trailing_call_is_tool_running(self):
        self.env.bash("make precommit", UNTIL - datetime.timedelta(seconds=60), pending=True)
        report = self.ok(self.env.run())
        self.assertEqual(report["verdict"], "TOOL-RUNNING")

    def test_a_moved_box_outranks_a_young_trailing_call(self):
        self.tick(at(5))
        self.env.bash("make precommit", UNTIL - datetime.timedelta(seconds=60), pending=True)
        report = self.ok(self.env.run(coverage=self.env.coverage(SC1="measured(PR #12)")))
        self.assertEqual(report["verdict"], "PROGRESSING")
        self.assertEqual(report["pending"]["name"], "Bash")

    def test_trailing_call_past_its_bound_is_not_tool_running(self):
        for minute in (-60, -50):
            self.env.bash("make test", at(minute), took=100)
        self.env.bash("make precommit", UNTIL - datetime.timedelta(seconds=300), pending=True)
        report = self.ok(self.env.run())
        self.assertEqual(report["pending"]["bound_s"], 200)
        self.assertNotEqual(report["verdict"], "TOOL-RUNNING")

    def test_the_drives_own_script_call_is_not_tool_running(self):
        self.env.bash("python3 scripts/progress-window.py --session %s --json" % SID,
                      UNTIL - datetime.timedelta(seconds=5), pending=True)
        report = self.ok(self.env.run())
        self.assertNotEqual(report["verdict"], "TOOL-RUNNING")

    def test_every_box_ticked_in_the_window_is_done_candidate(self):
        self.tick(at(5), sc=("[x] SC1: first criterion", "[x] SC2: second criterion"),
                  dod=("[x] make precommit green",))
        report = self.ok(self.env.run())
        self.assertEqual(report["verdict"], "DONE-CANDIDATE")

    def test_every_box_ticked_before_the_window_is_done_candidate(self):
        self.tick(at(-40), sc=("[x] SC1: first criterion", "[x] SC2: second criterion"),
                  dod=("[x] make precommit green",))
        report = self.ok(self.env.run())
        self.assertEqual(report["verdict"], "DONE-CANDIDATE")

    def test_no_call_in_the_window_with_open_boxes_is_idle(self):
        self.env.bash("ls", at(-20))
        report = self.ok(self.env.run())
        self.assertEqual(report["verdict"], "IDLE")
        self.assertEqual(report["activity"]["tool_calls"], 0)

    def test_registry_busy_idle_flip_is_not_progressing(self):
        self.env.registered(started=SESSION_START, status="idle", updated=at(7))
        self.env.bash("ls", at(3))
        report = self.ok(self.env.run())
        self.assertNotEqual(report["verdict"], "PROGRESSING")
        self.assertEqual(report["moved"], [])

    def test_reads_inside_the_first_twenty_minutes_are_orienting(self):
        self.env.registered(started=at(5))
        self.env.call("Read", {"file_path": "/x.md"}, at(6))
        report = self.ok(self.env.run())
        self.assertEqual(report["verdict"], "ORIENTING")

    def test_no_calls_inside_the_first_twenty_minutes_is_orienting_not_idle(self):
        self.env.registered(started=at(5))
        report = self.ok(self.env.run())
        self.assertEqual(report["verdict"], "ORIENTING")


class TestWindow(Case):
    def test_since_defaults_to_last_drive(self):
        (self.env.state / ("drive-%s.json" % SID)).write_text(
            json.dumps({"last_run": iso(at(5))}), encoding="utf-8")
        report = self.ok(self.env.run(since=None))
        self.assertEqual((report["since"], report["since_source"]), (iso(at(5)), "since last drive"))

    def test_since_defaults_to_fifteen_minutes(self):
        report = self.ok(self.env.run(since=None))
        self.assertEqual((report["since"], report["since_source"]), (iso(SINCE), "default 15 min"))

    def test_lookback_is_capped_at_sixty_minutes(self):
        (self.env.state / ("drive-%s.json" % SID)).write_text(
            json.dumps({"last_run": iso(UNTIL - datetime.timedelta(hours=5))}), encoding="utf-8")
        report = self.ok(self.env.run(since=None))
        self.assertEqual(report["since"], iso(UNTIL - datetime.timedelta(minutes=60)))
        self.assertTrue(report["capped"])

    def test_an_event_is_counted_by_its_own_timestamp(self):
        self.tick(at(-5))
        report = self.ok(self.env.run())
        self.assertEqual(report["moved"], [])


class TestSources(Case):
    def test_unknown_session_is_unknown_exit_2(self):
        code, report, err = self.env.run(session="99999999-0000-0000-0000-000000000000")
        self.assertEqual(code, 2)
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertIn("neither the registry nor any transcript", err)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a 000 file")
    def test_unreadable_transcript_is_unknown_exit_2(self):
        self.env.run()
        os.chmod(self.env.transcript, 0)
        code, report, err = self.env.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertIn("transcript unreadable", err)

    def test_symlinked_project_dir_counts_each_event_once(self):
        (self.env.projects / "alias").symlink_to(self.env.projects / "proj")
        for minute in (2, 4, 6):
            self.env.call("Read", {"file_path": "/x/%d.md" % minute}, at(minute))
        report = self.ok(self.env.run())
        self.assertEqual(len(report["sources"]["transcripts"]), 1)
        self.assertEqual(report["activity"]["tool_calls"], 3)

    def test_api_request_blob_repeats_are_never_counted(self):
        self.env.call("Read", {"file_path": "/x.md"}, at(2))
        report = self.ok(self.env.run())
        self.assertEqual(report["activity"]["tool_calls"], 1)

    def test_text_render_opens_with_the_window_block(self):
        self.tick(at(5))
        self.env.run()
        env = dict(os.environ, SUPERVISOR_PROJECTS_DIR=str(self.env.projects),
                   SUPERVISOR_SESSIONS_DIR=str(self.env.sessions), SUPERVISOR_STATE_DIR=str(self.env.state))
        out = subprocess.run([sys.executable, str(SCRIPT), "--session", SID, "--anchor", str(self.env.anchor),
                              "--since", iso(SINCE), "--until", iso(UNTIL), "--now", iso(UNTIL)],
                             capture_output=True, text=True, env=env).stdout.splitlines()
        self.assertTrue(out[0].startswith("⏱ Window "), out)
        self.assertTrue(out[0].endswith(": PROGRESSING"), out)
        self.assertEqual([l.split(":")[0].strip() for l in out[1:]],
                         ["Moved", "Toward", "Activity", "Claim"])

    def test_json_carries_the_same_block_the_text_render_prints(self):
        self.tick(at(5))
        report = self.ok(self.env.run())
        self.assertTrue(report["block"].startswith("⏱ Window "))
        self.assertIn("Moved:     B  SC1 ticked — rating unavailable", report["block"])


class ResolveAnchorTest(unittest.TestCase):
    """A relative --anchor must still find a vault task file.

    The defect: worker-drive runs from whatever cwd the session holds, which is a
    repo worktree whenever the session works in one. `--anchor "<task>.md"` then
    resolved against that worktree, the open failed, and the run reported UNKNOWN
    -- so the measurement the script exists to take was never taken, on every run.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.tmp], check=False))
        self.root = os.path.join(self.tmp, "Obsidian")
        self.tasks = os.path.join(self.root, "private-personal", "25 Tasks")
        os.makedirs(self.tasks)
        self.target = os.path.join(self.tasks, "A Task.md")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("---\nstatus: in_progress\n---\n")

    def resolve(self, raw):
        """Import the script fresh so OBSIDIAN_ROOT is read from this test's env."""
        import importlib.util

        os.environ["OBSIDIAN_ROOT"] = self.root
        self.addCleanup(os.environ.pop, "OBSIDIAN_ROOT", None)
        spec = importlib.util.spec_from_file_location("progress_window_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.resolve_anchor(raw)

    def test_relative_anchor_resolves_under_obsidian_root(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(os.path.realpath(self.target), self.resolve("A Task.md"))

    def test_absolute_anchor_is_returned_unchanged(self):
        self.assertEqual(os.path.realpath(self.target), self.resolve(self.target))

    def test_unknown_basename_stays_unresolved(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(os.path.realpath("Missing.md"), self.resolve("Missing.md"))


if __name__ == "__main__":
    unittest.main()
