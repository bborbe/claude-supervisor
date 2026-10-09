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
        # e4fd1919 was headless: no registry row, a live heartbeat.
        fx = Fixture(STUCK_FIXTURE, status=None, heartbeat_age_s=5)
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
        # A headless worker is not in the registry at all, and STUCK must not
        # depend on the registry half, which cannot see it.
        #
        # ⚠️ The heartbeat IS required, and this fixture used to omit it. A live
        # headless worker re-stamps every `HEARTBEAT_INTERVAL_MS` (30 s) against a
        # `HEARTBEAT_TTL_MS` (60 s) read, so one ALWAYS holds a fresh stamp:
        # `status=None` with no stamp at all is a session that is gone, not a
        # headless one. Pinning that shape as `stuck:` is what gated a dead
        # session; the sibling test below is the corrected reading.
        fx = Fixture(STUCK_FIXTURE, status=None, heartbeat_age_s=5)
        self.addCleanup(fx.cleanup)
        state = fx.probe()
        self.assertEqual(state[SID8][2], "stuck:stream-closed")
        self.assertIs(state[SID8][3], True)

    def test_a_dead_session_with_a_stuck_tail_is_unregistered_not_gated(self):
        # THE FIX. A session with no registry entry AND no heartbeat is gone, so a
        # `stuck` marker left in its frozen tail must not gate it — that gate is
        # offered to the operator with a heal ladder pointed at a session that no
        # longer exists, and nothing can answer it. Measured 2026-10-09 on a real
        # tracked set: this shape rendered `GATED e4fd1919 [stuck:stream-closed]`
        # against `gated: 1  held: 13`.
        #
        # Its positive controls are the tests either side of it: the headless shape
        # above (status=None + fresh stamp → `stuck:`) and the registered shape
        # below (status="idle" → `stuck-tab:`). A fix that deletes the `stuck` limb
        # outright passes this test and fails both of those.
        fx = Fixture(STUCK_FIXTURE, status=None)
        self.addCleanup(fx.cleanup)
        state = fx.probe()
        self.assertEqual(state[SID8][2], "unregistered")
        self.assertIsNone(state[SID8][3], "held, never cleared")
        self.assertEqual(watch.gated_keys(state), [])

    def test_a_registered_worker_with_a_stuck_tail_still_reads_stuck_tab(self):
        # The second positive control: `stuck-tab:` is what keeps a registered (tab)
        # worker off the heal ladder's headless-resume rung, and it must survive the
        # reordering above. The registry decides here, not the heartbeat.
        fx = Fixture(STUCK_FIXTURE, status="idle")
        self.addCleanup(fx.cleanup)
        state = fx.probe()
        self.assertEqual(state[SID8][2], "stuck-tab:stream-closed")
        self.assertIs(state[SID8][3], True)
        self.assertEqual(watch.gated_keys(state), [SID8])


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

    def test_a_future_dated_stamp_is_not_live(self):
        # Negative age must not read live forever — that would clear a held gate.
        self.fx.stamp(SID, -300)
        self.assertFalse(watch.heartbeat_live(SID8, self.fx.live))

    def test_a_missing_store_is_false_not_an_exception(self):
        self.assertFalse(watch.heartbeat_live(SID8, os.path.join(self.fx.dir, "nope")))


class HeartbeatConstantsTest(unittest.TestCase):
    """The watcher mirrors `server/heartbeat.mjs`; drift would silently misread liveness."""

    def test_constants_match_the_server(self):
        import re
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "server", "heartbeat.mjs"), encoding="utf-8").read()
        ttl_ms = int(re.search(r"HEARTBEAT_TTL_MS = ([\d_]+)", src).group(1).replace("_", ""))
        self.assertEqual(watch.HEARTBEAT_TTL_S * 1000, ttl_ms)
        reach = re.search(r"REACHABILITY_FILE = '([^']+)\.json'", src).group(1)
        self.assertIn(reach, watch.LIVE_SKIP)


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

    def test_stuck_outranks_registry_waiting(self):
        gated, reason = watch.is_gated("waiting", "nothing", stuck="stream-closed")
        self.assertIs(gated, True)
        self.assertEqual(reason, "stuck-tab:stream-closed")

    def test_a_stuck_tab_worker_never_reads_as_a_headless_heal_candidate(self):
        # A registered worker is a tab; the ladder's first rung is a headless resume.
        for status in ("waiting", "idle", "busy"):
            _, reason = watch.is_gated(status, "nothing", stuck="stream-closed")
            self.assertFalse(reason.startswith("stuck:"), status)
        # The headless contrast: status None WITH a fresh heartbeat is the one
        # shape that legitimately reads `stuck:` — the heal ladder's own case.
        # ⚠️ Without the heartbeat this is a DEAD session and reads `unregistered`
        # instead; see ReplayTest.test_a_dead_session_with_a_stuck_tail_is_unregistered_not_gated
        # for the reading, and why the heartbeat is the discriminator.
        _, reason = watch.is_gated(None, "nothing", headless_live=True,
                                   stuck="stream-closed")
        self.assertEqual(reason, "stuck:stream-closed")


if __name__ == "__main__":
    unittest.main()
