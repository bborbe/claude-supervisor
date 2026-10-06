"""Tests for the STUCK half of scripts/manager-attention-watch.py.

⚠️ **Both fixtures are SYNTHETIC, and deliberately so.** The task this serves named
"a stored copy of the e4fd1919 transcript", but `bborbe/claude-supervisor` is a
PUBLIC repo — committing a real session would publish the operator's work email,
vault paths and task narrative into permanent git history. The fixtures reproduce
the three signal SHAPES and nothing else, which is also the better test: minimal,
deterministic, and the negative control differs in exactly one way.

The properties worth pinning, because each is a way the detector could look like it
works while being wrong:

  - **The 2026-10-05 defect itself.** A headless worker whose permission channel
    dies reads UNREGISTERED forever, because it has no pid and the session registry
    can never list it. Without the heartbeat union it is HELD and never announced —
    the exact silent direction this file exists to close.
  - **"Consecutive" counts PERMISSION OUTCOMES, not records.** The e4fd1919 shape
    interleaves assistant text and attachments between failures; a record-reading
    resets on every one of them and the ≥3 trigger never fires. This is the
    operator's call, taken 2026-10-05.
  - **`🔴 BLOCKED` matches as a line PREFIX.** The phrase also occurs mid-line in a
    worker's own prose about it; a substring match fires on a worker that is merely
    discussing a block.
  - **A missing heartbeat is not a dead one.** Every session on a host without the
    store produces no stamp, and the registry half already covers those.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(SCRIPTS, "tests", "fixtures")
SID = "e4fd1919-b082-4bb4-aebb-d7185cd61502"
SID8 = SID[:8]
TASK = "Vault UI Serves Task and Assignee Lists from a Watcher-Maintained Index"
STUCK_FIXTURE = "e4fd1919-transcript.jsonl"
CLEAN_FIXTURE = "not-stuck-transcript.jsonl"
STREAM_CLOSED = "Tool permission request failed: AbortError: Stream closed"


def load(name, filename):
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watch = load("manager_attention_watch", "manager-attention-watch.py")
heal = load("stuck_heal", "stuck-heal.py")


def result(content, is_error):
    """One `user` record carrying a single `tool_result` block."""
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "is_error": is_error,
         "content": content}]}}


def assistant_text(text):
    return {"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}


class Fixture:
    """A temp vault + transcript tree + registry + heartbeat store, one task."""

    def __init__(self, fixture_name, status="idle", heartbeat_age_s=None):
        self.dir = tempfile.mkdtemp(prefix="stuck-test-")
        # `projects` is the ROOT — the script globs one level down, matching the
        # real `~/.claude/projects/<munged-cwd>/<sid>.jsonl` layout.
        self.projects = os.path.join(self.dir, "projects")
        self.projdir = os.path.join(self.projects, "-Users-someone-my-vault")
        self.tasks = os.path.join(self.dir, "tasks")
        self.sessions = os.path.join(self.dir, "sessions")
        self.live = os.path.join(self.dir, "live")
        for d in (self.projdir, self.tasks, self.sessions, self.live):
            os.makedirs(d)
        self.tracked = os.path.join(self.dir, "tracked.txt")
        with open(self.tracked, "w") as fh:
            fh.write(TASK + "\n")
        with open(os.path.join(self.tasks, TASK + ".md"), "w", encoding="utf-8") as fh:
            fh.write("---\nclaude_session_id: %s\n---\nx\n" % SID)
        if fixture_name is not None:
            shutil.copyfile(os.path.join(FIXTURES, fixture_name),
                            os.path.join(self.projdir, SID + ".jsonl"))
        if status is not None:
            with open(os.path.join(self.sessions, "96345.json"), "w") as fh:
                json.dump({"sessionId": SID, "status": status, "pid": 96345}, fh)
        if heartbeat_age_s is not None:
            self.stamp(SID, heartbeat_age_s)

    def stamp(self, name, age_s, body=None):
        p = os.path.join(self.live, name + ".json")
        with open(p, "w") as fh:
            json.dump(body or {"sessionId": name, "pid": None, "mode": "headless",
                               "at": "2026-01-01T00:00:00.000Z"}, fh)
        mtime = time.time() - age_s
        os.utime(p, (mtime, mtime))
        return p

    def probe(self):
        return watch.probe(self.tracked, self.tasks, self.projects,
                           self.sessions, live_dir=self.live)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class ReplayTest(unittest.TestCase):
    """SC1 — the e4fd1919 shape, replayed, and its negative control."""

    def test_e4fd1919_replay_is_one_stuck_entry_keyed_on_the_session(self):
        fx = Fixture(STUCK_FIXTURE)
        self.addCleanup(fx.cleanup)
        state = fx.probe()
        stuck = {s: v for s, v in state.items() if v[2].startswith("stuck:")}
        self.assertEqual(list(stuck), [SID8],
                         "exactly one STUCK entry, keyed on the session")
        self.assertEqual(stuck[SID8][2], "stuck:stream-closed")
        self.assertIs(stuck[SID8][3], True)
        self.assertEqual(watch.gated_keys(state), [SID8])

    def test_hard_coded_detector_fails_on_the_non_stuck_fixture(self):
        # The negative control: same record SHAPES, successful tool_results and a
        # clean closer. A detector hard-coded to "Stream closed", or to "carries
        # tool_results", passes the replay above and fails here.
        fx = Fixture(CLEAN_FIXTURE)
        self.addCleanup(fx.cleanup)
        state = fx.probe()
        self.assertEqual({s: v for s, v in state.items()
                          if v[2].startswith("stuck:")}, {})
        self.assertNotIn(SID8, watch.gated_keys(state))

    def test_stuck_fixture_without_the_registry_entry_is_still_stuck(self):
        # The defect's own shape: a headless worker is not in the registry at all.
        # STUCK must not depend on the registry half, which cannot see it.
        fx = Fixture(STUCK_FIXTURE, status=None)
        self.addCleanup(fx.cleanup)
        state = fx.probe()
        self.assertEqual(state[SID8][2], "stuck:stream-closed")


class PermissionFailureRunTest(unittest.TestCase):
    def test_counts_among_outcomes_not_records(self):
        # The operator's call, 2026-10-05. Assistant text and attachments between
        # failures must NOT reset the run — a record-reading scores this 1.
        tail = "\n".join([
            json.dumps(assistant_text("retrying")),
            json.dumps(result(STREAM_CLOSED, True)),
            json.dumps({"type": "attachment", "message": {"content": "noise"}}),
            json.dumps(assistant_text("retrying again")),
            json.dumps(result(STREAM_CLOSED, True)),
            json.dumps(result(STREAM_CLOSED, True)),
        ])
        self.assertEqual(watch.permission_failure_run(tail), 3)

    def test_a_successful_result_resets_the_run(self):
        # TRAILING, not longest: a tool that ran means the channel works again.
        tail = "\n".join([
            json.dumps(result(STREAM_CLOSED, True)),
            json.dumps(result(STREAM_CLOSED, True)),
            json.dumps(result("ok", False)),
            json.dumps(result(STREAM_CLOSED, True)),
        ])
        self.assertEqual(watch.permission_failure_run(tail), 1)

    def test_a_non_permission_error_resets_the_run(self):
        tail = "\n".join([
            json.dumps(result(STREAM_CLOSED, True)),
            json.dumps(result("bash: no such file", True)),
            json.dumps(result(STREAM_CLOSED, True)),
        ])
        self.assertEqual(watch.permission_failure_run(tail), 1)

    def test_empty_tail_is_zero(self):
        self.assertEqual(watch.permission_failure_run(""), 0)
        self.assertEqual(watch.permission_failure_run(None), 0)


class StuckReasonTest(unittest.TestCase):
    def test_stream_closed_wins_over_the_count(self):
        # Ordering is not cosmetic: the card should name the CAUSE. A worker that
        # both lost its channel and counted three failures reads `stream-closed`.
        tail = "\n".join(json.dumps(result(STREAM_CLOSED, True)) for _ in range(4))
        self.assertEqual(watch.stuck_reason(None, tail), "stream-closed")

    def test_blocked_closer_matches_as_a_line_prefix(self):
        text = "some prose\n🔴 BLOCKED · nothing running · needs intervention\n👤 You: nothing"
        self.assertEqual(watch.stuck_reason(text, ""), "blocked-closer")

    def test_a_quoted_blocked_phrase_mid_line_does_not_fire(self):
        # A worker merely DISCUSSING a block must not be reported as one.
        text = "I saw the line `🔴 BLOCKED · nothing running` in the other pane."
        self.assertIsNone(watch.stuck_reason(text, ""))

    def test_permission_failures_fire_at_three(self):
        # ⚠️ Deliberately NOT the Stream-closed wording: that string short-circuits
        # to `stream-closed` before the count is consulted, so a tail built from it
        # tests the ordering, not the threshold. This is a permission failure that
        # is not a closed channel.
        other = "Tool permission request failed: Request timed out"
        two = "\n".join(json.dumps(result(other, True)) for _ in range(2))
        three = two + "\n" + json.dumps(result(other, True))
        self.assertIsNone(watch.stuck_reason(None, two))
        self.assertEqual(watch.stuck_reason(None, three), "permission-failures")

    def test_prose_quoting_stream_closed_is_not_stuck(self):
        # The defect review caught: a worker DISCUSSING the error — quoting it in
        # its own text, as this detector's own author did — is healthy. Only tool
        # results count.
        tail = "\n".join([
            json.dumps(assistant_text("The worker died with `" + STREAM_CLOSED + "`.")),
            json.dumps(result("ok", False)),
        ])
        self.assertIsNone(watch.stuck_reason(None, tail))

    def test_a_recovered_channel_is_not_stuck(self):
        # The channel died, then a tool ran: recovered. A longest-run reading kept
        # this STUCK until the evidence scrolled out, and the heal ladder would have
        # resumed a working session.
        tail = "\n".join([json.dumps(result(STREAM_CLOSED, True)) for _ in range(4)]
                         + [json.dumps(result("ok", False))])
        self.assertIsNone(watch.stuck_reason(None, tail))

    def test_a_clean_transcript_is_not_stuck(self):
        self.assertIsNone(watch.stuck_reason("👤 You: nothing", ""))


class HeartbeatLiveTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(None, status=None)
        self.addCleanup(self.fx.cleanup)

    def test_a_fresh_stamp_is_live(self):
        self.fx.stamp(SID, 5)
        self.assertTrue(watch.heartbeat_live(SID8, self.fx.live))

    def test_a_stale_stamp_is_not_live(self):
        self.fx.stamp(SID, watch.HEARTBEAT_TTL_S + 30)
        self.assertFalse(watch.heartbeat_live(SID8, self.fx.live))

    def test_the_at_field_does_not_decide_age(self):
        # The stamp body claims 2026-01-01; only the mtime is fresh. Reading `at`
        # would call this dead — and a crashed writer's stale `at` would call a
        # dead session live. `server/heartbeat.mjs:62` is the authority.
        self.fx.stamp(SID, 5)
        self.assertTrue(watch.heartbeat_live(SID8, self.fx.live))

    def test_the_reachability_file_is_not_a_session(self):
        self.fx.stamp("_cluster-reachability", 5)
        self.assertFalse(watch.heartbeat_live(SID8, self.fx.live))

    def test_a_missing_store_is_false_not_an_exception(self):
        self.assertFalse(watch.heartbeat_live(SID8, os.path.join(self.fx.dir, "nope")))


class HeadlessGateTest(unittest.TestCase):
    """UNREGISTERED and HEADLESS are different facts and must not collapse."""

    def test_no_heartbeat_and_no_registry_is_held_not_cleared(self):
        fx = Fixture(CLEAN_FIXTURE, status=None)
        self.addCleanup(fx.cleanup)
        state = fx.probe()
        self.assertIsNone(state[SID8][3], "held, never cleared")
        self.assertEqual(state[SID8][2], "unregistered")

    def test_a_live_heartbeat_with_no_registry_reads_headless(self):
        fx = Fixture(CLEAN_FIXTURE, status=None, heartbeat_age_s=5)
        self.addCleanup(fx.cleanup)
        state = fx.probe()
        self.assertIs(state[SID8][3], False)
        self.assertEqual(state[SID8][2], "headless:no-ask")

    def test_a_headless_worker_holding_an_ask_is_gated(self):
        gated, reason = watch.is_gated(None, "pick — 1. alpha", headless_live=True)
        self.assertIs(gated, True)
        self.assertEqual(reason, "headless+closer")

    def test_a_headless_worker_with_nothing_is_not_gated(self):
        gated, reason = watch.is_gated(None, "nothing — clean", headless_live=True)
        self.assertIs(gated, False)
        self.assertEqual(reason, "headless:no-ask")

    def test_the_registry_half_still_wins_when_it_has_an_answer(self):
        gated, reason = watch.is_gated("waiting", "nothing", headless_live=True)
        self.assertIs(gated, True)
        self.assertEqual(reason, "registry:waiting")


class HealDecideTest(unittest.TestCase):
    """The ladder, as a table. `deaths` counts RESUMES, not deaths."""

    def test_first_death_resumes_headless(self):
        self.assertEqual(heal.decide("stream-closed", 0), "resume-headless")

    def test_second_death_reopens_interactive(self):
        self.assertEqual(heal.decide("stream-closed", 1), "reopen-interactive")

    def test_a_third_death_reports_only(self):
        self.assertEqual(heal.decide("stream-closed", 2), "report-only")
        self.assertEqual(heal.decide("stream-closed", 9), "report-only")

    def test_a_parked_gate_is_never_healed_whatever_the_count(self):
        # SC4's structural half: no count makes a judgement gate resumable.
        for deaths in (0, 1, 2):
            self.assertEqual(heal.decide("parked-gate", deaths), "report-only")

    def test_an_unknown_kind_is_reported_not_guessed(self):
        self.assertEqual(heal.decide("something-new", 0), "report-only")


class HealTest(unittest.TestCase):
    """SC2-SC4 — the ladder driven through recording effects."""

    def setUp(self):
        self.calls = []

        def rec(name):
            def f(*a):
                self.calls.append((name, a))
                if name == "spawn_interactive":
                    return "222"
                if name == "jump_link":
                    return "http://127.0.0.1:1337/jump?pane=" + str(a[0])
                return None
            return f

        self.effects = dict(
            spawn_headless=rec("spawn_headless"),
            spawn_interactive=rec("spawn_interactive"),
            set_task_mode=rec("set_task_mode"),
            post_card=rec("post_card"),
            jump_link=rec("jump_link"),
        )

    def named(self, name):
        return [a for n, a in self.calls if n == name]

    def test_first_death_resumes_exactly_once_in_the_same_session(self):
        action = heal.heal(SID, TASK, "stream-closed", 0, **self.effects)
        self.assertEqual(action, "resume-headless")
        self.assertEqual(self.named("spawn_headless"), [(SID,)],
                         "exactly one call, carrying the SAME session id")
        self.assertEqual(self.named("spawn_interactive"), [])
        self.assertEqual(self.named("set_task_mode"), [])
        self.assertEqual(self.named("post_card"), [])

    def test_second_death_goes_interactive_and_never_resumes_again(self):
        action = heal.heal(SID, TASK, "stream-closed", 1, **self.effects)
        self.assertEqual(action, "reopen-interactive")
        self.assertEqual(self.named("spawn_headless"), [],
                         "no third headless resume occurs")
        self.assertEqual(self.named("set_task_mode"), [(TASK, "interactive")])
        self.assertEqual(len(self.named("spawn_interactive")), 1)
        cards = self.named("post_card")
        self.assertEqual(len(cards), 1)
        self.assertEqual(self.named("jump_link"), [("222",)],
                         "the jump link is built from the NEW pane id")
        self.assertIn("pane=222", cards[0][1], "the card carries the jump link")

    def test_a_parked_judgement_gate_is_reported_and_never_answered(self):
        action = heal.heal(SID, TASK, "parked-gate", 0, **self.effects)
        self.assertEqual(action, "report-only")
        self.assertEqual(self.named("spawn_headless"), [])
        self.assertEqual(self.named("spawn_interactive"), [])
        self.assertEqual(self.named("set_task_mode"), [])
        self.assertEqual(len(self.named("post_card")), 1)
        self.assertNotIn("answer_permission", {n for n, _ in self.calls},
                         "zero answer_permission calls")


class DeathsFromLedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="heal-ledger-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, **rec):
        with open(os.path.join(self.dir, name + ".json"), "w") as fh:
            json.dump(rec, fh)

    def test_a_missing_ledger_is_zero_not_an_exception(self):
        self.assertEqual(
            heal.deaths_from_ledger(SID, os.path.join(self.dir, "nope")), 0)

    def test_resumes_of_this_session_are_counted_by_resumed_from(self):
        # A resume keeps the SAME session id, so counting distinct ids would
        # always read 1 and the ladder would never leave the first rung.
        self.write("a", session_id=SID, resumed_from=None)
        self.write("b", session_id=SID, resumed_from=SID)
        self.write("c", session_id="other", resumed_from="other")
        self.assertEqual(heal.deaths_from_ledger(SID, self.dir), 1)

    def test_an_eight_char_prefix_resolves(self):
        # The form a manager actually holds: the watcher prints 8 chars, and an
        # exact-match lookup would read 0 and re-resume an already-resumed session.
        self.write("a", session_id=SID, resumed_from=SID)
        self.assertEqual(heal.deaths_from_ledger(SID[:8], self.dir), 1)

    def test_an_unreadable_record_is_skipped_not_fatal(self):
        self.write("a", session_id=SID, resumed_from=SID)
        with open(os.path.join(self.dir, "broken.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(heal.deaths_from_ledger(SID, self.dir), 1)


REPO = os.path.dirname(SCRIPTS)
SHIPPING_MODULE = os.path.join(REPO, "server", "shipping-settings.mjs")


def shipping_settings(arg):
    """`shippingSettings(arg)` from the JS module, read through `node`.

    ⚠️ Bridged rather than restated. The settings live in `server/` because the
    server applies them; a Python copy of the allowlist here would pass this test
    while the module drifted, which is the failure SC5 exists to catch.
    """
    src = ("import(process.argv[1]).then(m => process.stdout.write("
           "JSON.stringify(m.shippingSettings(JSON.parse(process.argv[2])))))")
    out = subprocess.run(["node", "--input-type=module", "-e", src,
                          SHIPPING_MODULE, json.dumps(arg)],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is required to read the JS module")
class PreventionSettingsTest(unittest.TestCase):
    """SC5 — a shipping worker never needs the permission channel to edit or commit."""

    def test_prevention_settings(self):
        s = shipping_settings(True)
        self.assertEqual(s["permissions"]["defaultMode"], "acceptEdits")
        self.assertEqual(s["permissions"]["allow"],
                         ["Bash(git add:*)", "Bash(git commit:*)", "Bash(git push:*)"])
        # The negative control: a non-shipping worker is spawned without them.
        self.assertIsNone(shipping_settings(False))
        self.assertIsNone(shipping_settings(None))


if __name__ == "__main__":
    unittest.main()
