"""The 24 h escalation: one card per stalled wait, and a clock that resets on change.

The load-bearing tests are `test_unchanged_for_24h_cards_once` and
`test_changed_observable_resets_the_clock`. The defect this change removes is a wait
that suppresses every other reader forever; the defect it could easily introduce is the
opposite one — a card every round for the same stall, which trains the operator to
ignore the card the same way a false park check-in trains a manager to discount its
cell. Counting posts is what distinguishes "carded once" from "carded repeatedly": an
assertion on the rendered text alone passes for both.
"""
import datetime
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "drive-escalate.py"
DECLARED_WAIT = pathlib.Path(__file__).resolve().parent.parent / "declared-wait.py"

_spec = importlib.util.spec_from_file_location("drive_escalate", SCRIPT)
drive_escalate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drive_escalate)

HOUR = 3600
NOW = 1794336000.0  # a fixed clock, so the boundary arithmetic is exact


def stop_record(ts, ends="PR #12 merged"):
    return json.dumps({"event": "Stop", "detail": "⏰ Ends:   %s" % ends, "ts": ts})


class EscalateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.attention = os.path.join(self.tmp, "attention")
        os.makedirs(self.attention)
        self.store = os.path.join(self.tmp, "waits.json")
        self._prior_state = os.environ.get("ATTENTION_STATE_DIR")
        os.environ["ATTENTION_STATE_DIR"] = self.attention
        self.posts = []
        self._real_post = drive_escalate.post_card

        def fake_post(row, threshold=drive_escalate.DEFAULT_THRESHOLD_SECONDS,
                      ask_script=None):
            self.posts.append(row["session_id"])
            return "ITEM%d" % len(self.posts)

        drive_escalate.post_card = fake_post
        # Liveness is stubbed live by default: every fixture sid shares the `aaaaaaaa`
        # prefix, so one set member covers them all. The real probe would ask the
        # operator's session registry, which has nothing to do with the fixtures.
        self._real_live = drive_escalate.live_sessions
        drive_escalate.live_sessions = lambda script=None: (
            {"aaaaaaaa-0000-0000-0000-000000000001"},
            None,
        )

    def tearDown(self):
        drive_escalate.post_card = self._real_post
        drive_escalate.live_sessions = self._real_live
        if self._prior_state is None:
            os.environ.pop("ATTENTION_STATE_DIR", None)
        else:
            os.environ["ATTENTION_STATE_DIR"] = self._prior_state
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_events(self, sid, *records):
        path = os.path.join(self.attention, "%s.events.jsonl" % sid)
        with open(path, "w", encoding="utf-8") as handle:
            for rec in records:
                handle.write(rec + "\n")

    def render(self, now=NOW):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drive_escalate.main(
                ["render", "--now", str(now), "--store", self.store,
                 "--declared-wait-script", str(DECLARED_WAIT)]
            )
        return rc, buf.getvalue()

    # --- SC1 -----------------------------------------------------------------

    def test_unchanged_for_24h_cards_once(self):
        """One card at 24 h; a second round at the same observable posts none."""
        sid = "aaaaaaaa-0000-0000-0000-000000000001"
        self.write_events(sid, stop_record(NOW - 25 * HOUR))
        rc, out = self.render()
        self.assertEqual(rc, 0)
        self.assertIn("card: ITEM1", out)
        self.assertEqual(self.posts, [sid])

        rc, out = self.render(now=NOW + 60)
        self.assertEqual(rc, 0)
        self.assertIn("already carded", out)
        self.assertEqual(len(self.posts), 1, "a second round must not re-card")

    def test_fresh_wait_is_not_carded(self):
        """23 h is under the threshold — no card, and no store entry to confuse later rounds."""
        sid = "aaaaaaaa-0000-0000-0000-000000000002"
        self.write_events(sid, stop_record(NOW - 23 * HOUR))
        rc, out = self.render()
        self.assertEqual(rc, 0)
        self.assertNotIn("ITEM", out)
        self.assertEqual(self.posts, [])
        self.assertIn("(none)", out)

    # --- SC2 -----------------------------------------------------------------

    def test_changed_observable_resets_the_clock(self):
        """A re-declaration at hour 23 is fresh at hour 24 and cardable again later."""
        sid = "aaaaaaaa-0000-0000-0000-000000000003"
        self.write_events(sid, stop_record(NOW - 25 * HOUR, ends="PR #12 merged"))
        self.render()
        self.assertEqual(len(self.posts), 1, "the first stall cards")

        # The session re-parks at "hour 23" on a different observable.
        changed_at = NOW + 1
        self.write_events(sid, stop_record(changed_at, ends="PR #13 merged"))
        rc, out = self.render(now=changed_at + 1 * HOUR)
        self.assertIn("(none)", out, "1 h after the change is fresh, not stale")
        self.assertEqual(len(self.posts), 1, "a changed observable must not re-card")

        # 25 h after the change the new wait is stale, and the old card does not cover it.
        rc, out = self.render(now=changed_at + 25 * HOUR)
        self.assertIn("card: ITEM2", out)
        self.assertEqual(len(self.posts), 2, "a later stall on a new observable cards")

    def test_wait_since_rebaselines_on_change(self):
        """`wait_since` names the NEW declaration, not the first one ever seen."""
        sid = "aaaaaaaa-0000-0000-0000-000000000004"
        self.write_events(sid, stop_record(NOW - 25 * HOUR, ends="PR #12 merged"))
        self.render()
        changed_at = NOW + 10
        self.write_events(sid, stop_record(changed_at, ends="PR #13 merged"))
        rc, out = self.render(now=changed_at + 25 * HOUR)
        expected = datetime.datetime.fromtimestamp(changed_at).astimezone().isoformat(
            timespec="seconds"
        )
        self.assertIn(expected, out)

    # --- degenerate inputs ---------------------------------------------------

    def test_unreadable_ts_is_reported_not_guessed(self):
        """A record with no numeric `ts` renders unreadable and never cards."""
        sid = "aaaaaaaa-0000-0000-0000-000000000005"
        self.write_events(
            sid, json.dumps({"event": "Stop", "detail": "⏰ Ends:   x", "ts": None})
        )
        rc, out = self.render()
        self.assertEqual(rc, 0)
        self.assertEqual(self.posts, [])
        self.assertIn("(none)", out)

    def test_failed_post_is_not_recorded_as_carded(self):
        """A post that returns no id leaves the row cardable, so the next round retries."""
        sid = "aaaaaaaa-0000-0000-0000-000000000006"
        self.write_events(sid, stop_record(NOW - 25 * HOUR))
        drive_escalate.post_card = lambda row, threshold=None, ask_script=None: ""
        rc, out = self.render()
        self.assertIn("post failed", out)
        with open(self.store, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(
            stored[sid]["wait_carded"], "",
            "a failed post must not record a carded marker",
        )
        self.assertEqual(stored[sid]["wait_observable"], "PR #12 merged")

        drive_escalate.post_card = lambda row, threshold=None, ask_script=None: "ITEM9"
        rc, out = self.render(now=NOW + 60)
        self.assertIn("card: ITEM9", out, "the retry must post")

    def test_every_declaring_wait_is_recorded(self):
        """A fresh wait is recorded too — the store is the round's record, not only its cards."""
        sid = "aaaaaaaa-0000-0000-0000-000000000009"
        self.write_events(sid, stop_record(NOW - 2 * HOUR, ends="CI run 77"))
        self.render()
        with open(self.store, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertTrue(stored[sid]["wait_since"], "wait_since is stamped")
        self.assertEqual(stored[sid]["wait_observable"], "CI run 77")
        self.assertEqual(stored[sid]["wait_carded"], "", "fresh, so nothing carded yet")
        self.assertEqual(self.posts, [])

    def test_no_post_leaves_the_dedup_state_alone(self):
        """`--no-post` must not mark a stall carded, or the next real round suppresses it.

        This is the bug the local review caught: keying the carded marker off a non-empty
        `row["card"]` meant the dry run's own "not posted (--no-post)" string counted as a
        post, so every stall was recorded as carded and never actually sent.
        """
        sid = "aaaaaaaa-0000-0000-0000-00000000000c"
        self.write_events(sid, stop_record(NOW - 25 * HOUR))
        buf = io.StringIO()
        with redirect_stdout(buf):
            drive_escalate.main(
                ["render", "--now", str(NOW), "--store", self.store, "--no-post",
                 "--declared-wait-script", str(DECLARED_WAIT)]
            )
        self.assertIn("not posted (--no-post)", buf.getvalue())
        with open(self.store, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(
            stored[sid]["wait_carded"], "",
            "a dry run must leave the dedup state exactly as it found it",
        )
        self.assertEqual(self.posts, [], "a dry run posts nothing")

        # The real round that follows must still send the card.
        rc, out = self.render(now=NOW + 60)
        self.assertIn("card: ITEM1", out)
        self.assertEqual(len(self.posts), 1)

    def test_corrupt_store_refuses_rather_than_recarding(self):
        """An unparseable store is a refusal, never an empty one — else every card repeats."""
        sid = "aaaaaaaa-0000-0000-0000-000000000007"
        self.write_events(sid, stop_record(NOW - 25 * HOUR))
        with open(self.store, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        rc, out = self.render()
        self.assertEqual(rc, drive_escalate.REFUSED)
        self.assertEqual(self.posts, [], "a refused round posts nothing")

    def test_no_declaration_means_no_row(self):
        """A session whose newest Stop carries no `⏰ Ends:` is not a wait at all."""
        sid = "aaaaaaaa-0000-0000-0000-000000000008"
        self.write_events(sid, json.dumps({"event": "Stop", "detail": "nothing", "ts": NOW}))
        rc, out = self.render()
        self.assertEqual(rc, 0)
        self.assertIn("(none)", out)

    def test_a_dead_session_is_never_carded(self):
        """317 live logs declare stale waits; carding the dead ones would flood the board."""
        sid = "aaaaaaaa-0000-0000-0000-00000000000a"
        self.write_events(sid, stop_record(NOW - 72 * HOUR))
        drive_escalate.live_sessions = lambda script=None: (set(), None)
        rc, out = self.render()
        self.assertEqual(rc, 0)
        self.assertEqual(self.posts, [], "an exited session waits on nothing")
        self.assertIn("(none)", out)

    def test_an_unknowable_session_is_not_carded(self):
        """A failed liveness probe licenses neither carding nor a claim the wait ended."""
        sid = "aaaaaaaa-0000-0000-0000-00000000000b"
        self.write_events(sid, stop_record(NOW - 72 * HOUR))
        drive_escalate.live_sessions = lambda script=None: (None, "endpoint down")
        rc, out = self.render()
        self.assertEqual(rc, 0)
        self.assertEqual(self.posts, [], "cannot-tell must not card")


class ObservableKeyTest(unittest.TestCase):
    def test_key_pairs_timestamp_and_text(self):
        a = drive_escalate.observable_key({"ts": 100.0, "ends": "x"})
        b = drive_escalate.observable_key({"ts": 100.0, "ends": "y"})
        c = drive_escalate.observable_key({"ts": 101.0, "ends": "x"})
        self.assertNotEqual(a, b, "a different observable is a different wait")
        self.assertNotEqual(a, c, "a re-declaration is a different wait")
        self.assertEqual(a, drive_escalate.observable_key({"ts": 100.0, "ends": "x"}))


if __name__ == "__main__":
    unittest.main()
