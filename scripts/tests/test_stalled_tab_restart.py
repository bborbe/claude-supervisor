#!/usr/bin/env python3
"""Tests for scripts/stalled-tab-restart.py.

The load-bearing property is that `--allow-busy-lapse` is reached from exactly one place —
the operator's `ANSWERED:` on this session's card — and from nowhere else. Every unattended
path runs `restart-worker.py --dry-run`, so a row that flipped to `idle` between the sweep
and the call cannot be restarted by a round that was only trying to report a refusal.

The fakes below are real executables written to a temp dir and injected by patching the
module's own path constants, so the script's subprocess shape is exercised rather than
mocked away. `FAKE_LOG` records every invocation's argv, which is what lets a test assert
the presence or absence of `--allow-busy-lapse` and `--dry-run` rather than inferring it.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "stalled_tab_restart", os.path.join(_SCRIPTS, "stalled-tab-restart.py")
)
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)


FAKE_RESTART = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"script": "restart", "argv": sys.argv[1:]}) + "\n")
ctl = json.load(open(os.environ["FAKE_CONTROL"], encoding="utf-8"))
spec = ctl["restart"]["dry" if "--dry-run" in sys.argv else "real"]
print("\n".join(spec["out"]))
sys.exit(spec["rc"])
'''

FAKE_ASK = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"script": "ask", "argv": sys.argv[1:]}) + "\n")
ctl = json.load(open(os.environ["FAKE_CONTROL"], encoding="utf-8"))
verb = sys.argv[1]
if verb == "post":
    rc = ctl.get("post_rc", 0)
    if rc != 0:
        print("REFUSED: the store would not take it")
        sys.exit(rc)
    item = ctl.get("post_id", "item-1")
    # The real shapes: `post` prints ITEM_ID then a POLL hint line, and `poll` prints
    # `ANSWERED: <content>`. A fake that printed a bare id would hide the parser defect
    # this file exists to catch.
    print(f"ITEM_ID: {item}")
    print(f"POLL: python3 attention-ask.py poll {item}")
    sys.exit(0)
if verb == "poll":
    print(ctl.get("poll", "OPEN"))
    sys.exit(0)
sys.exit(2)
'''

FAKE_HOLDS = r'''#!/usr/bin/env python3
import json, os, sys
ctl = json.load(open(os.environ["FAKE_CONTROL"], encoding="utf-8"))
print(ctl.get("hold_reason", "operator hold"))
sys.exit(ctl.get("hold_rc", 1))
'''


class Base(unittest.TestCase):
    SID = "11111111-2222-3333-4444-555555555555"
    TASK = "A Stalled Task"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.vault = os.path.join(root, "vault")
        os.makedirs(os.path.join(self.vault, "25 Tasks"))
        self.task_path = os.path.join(self.vault, "25 Tasks", self.TASK + ".md")
        with open(self.task_path, "w", encoding="utf-8") as fh:
            fh.write("---\nstatus: in_progress\n---\n\n# Summary\n")

        self.log = os.path.join(root, "calls.log")
        self.control = os.path.join(root, "control.json")
        for name, body in (("restart-worker.py", FAKE_RESTART),
                           ("attention-ask.py", FAKE_ASK),
                           ("session-holds.py", FAKE_HOLDS)):
            path = os.path.join(root, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(path, 0o755)

        # The state dir is per-test: a shared one would let one case's cap leak into another.
        self.state = os.path.join(root, "state")
        os.environ["FAKE_LOG"] = self.log
        os.environ["FAKE_CONTROL"] = self.control
        self.addCleanup(os.environ.pop, "FAKE_LOG", None)
        self.addCleanup(os.environ.pop, "FAKE_CONTROL", None)

        self._saved = (st.RESTART, st.ASK, st.HOLDS, st.STATE_DIR)
        st.RESTART = os.path.join(root, "restart-worker.py")
        st.ASK = os.path.join(root, "attention-ask.py")
        st.HOLDS = os.path.join(root, "session-holds.py")
        st.STATE_DIR = self.state

        def restore():
            st.RESTART, st.ASK, st.HOLDS, st.STATE_DIR = self._saved

        self.addCleanup(restore)

        self.control_data({
            "restart": {"dry": {"rc": 1, "out": ["busy-target", "status is busy"]},
                        "real": {"rc": 0, "out": ["   ✅ killed pid 42; started a FRESH "
                                                  "session (not x) in a new tab (pane 9)"]}},
            "post_id": "item-1",
            "poll": "OPEN",
            "hold_rc": 1,
        })

    def control_data(self, data):
        with open(self.control, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def patch_control(self, **over):
        with open(self.control, encoding="utf-8") as fh:
            data = json.load(fh)
        data.update(over)
        self.control_data(data)

    def calls(self, script):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r["argv"] for r in rows if r["script"] == script]

    def run_leg(self, stall_count=2):
        out = []
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = st.main(["--vault", self.vault, "--task", self.TASK,
                          "--dir", "25 Tasks", "--session-id", self.SID,
                          "--stall-count", str(stall_count)])
        out = buf.getvalue().strip().splitlines()
        return rc, out

    def lifted_calls(self):
        return [a for a in self.calls("restart") if "--allow-busy-lapse" in a]

    def assert_no_unattended_lift(self):
        """The invariant: NOTHING passes the lift unless the operator said `restart it`.

        The earlier form only rejected an argv carrying both the lift and `--dry-run`, which
        an unattended call with the lift and no `--dry-run` sails straight through — the
        exact shape of the bug. This asserts the absence directly.
        """
        self.assertEqual([], self.lifted_calls(),
                         "an unattended path reached --allow-busy-lapse")


class Verdicts(Base):
    def test_below_the_threshold_is_skipped_and_never_probes(self):
        rc, lines = self.run_leg(stall_count=1)
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], "skipped")
        self.assertEqual([], self.calls("restart"))

    def test_a_held_session_is_left_alone(self):
        self.patch_control(hold_rc=0, hold_reason="operator hold")
        rc, lines = self.run_leg()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], "held")
        self.assertEqual([], self.calls("restart"))

    def test_an_undecidable_hold_probe_reads_as_held(self):
        """Exit 2 is `unknown`, and unknown is not clear — the leg must not act on it."""
        self.patch_control(hold_rc=2, hold_reason="")
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "held")
        self.assertEqual([], self.calls("restart"))

    def test_another_refusal_token_is_reported_verbatim(self):
        """SC3: the token is the script's own, never composed here."""
        self.patch_control(restart={"dry": {"rc": 1, "out": ["stale-load-path", "nothing newer"]},
                                    "real": {"rc": 0, "out": ["unused"]}})
        rc, lines = self.run_leg()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], "skipped")
        self.assertEqual(lines[1], "stale-load-path")

    def test_a_cleared_stall_is_not_restarted(self):
        """The dry run passing means the row is no longer busy. Acting here would restart a
        session the sweep no longer classifies as stalled."""
        self.patch_control(restart={"dry": {"rc": 0, "out": ["   ✅ would kill the pid"]},
                                    "real": {"rc": 0, "out": ["unused"]}})
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "skipped")
        self.assertEqual([], self.calls("ask"))


class Handover(Base):
    def test_an_unanswered_card_is_carded_and_nothing_is_killed(self):
        rc, lines = self.run_leg()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], "carded")
        posts = [a for a in self.calls("ask") if a[0] == "post"]
        self.assertEqual(1, len(posts), "one card per session, never a second")
        self.assertNotIn("--allow-busy-lapse", self.calls("restart")[0])

    def test_the_card_key_is_scoped_per_session_so_a_rerun_does_not_stack(self):
        self.run_leg()
        post = [a for a in self.calls("ask") if a[0] == "post"][0]
        key = post[post.index("--dedup-key") + 1]
        self.assertTrue(key.startswith("stuck-tab:"), key)

    def test_a_repeat_stall_after_a_restart_is_asked_again(self):
        """A bare `stuck-tab:<slug>` would resolve to the answered item forever, so the
        operator would never be re-asked on the one row this leg exists for."""
        self.patch_control(poll="ANSWERED: option: restart it")
        self.run_leg()
        first = [a for a in self.calls("ask") if a[0] == "post"][0]
        first_key = first[first.index("--dedup-key") + 1]
        self.patch_control(poll="OPEN", post_id="item-2")
        self.run_leg()
        posts = [a for a in self.calls("ask") if a[0] == "post"]
        second_key = posts[-1][posts[-1].index("--dedup-key") + 1]
        self.assertNotEqual(first_key, second_key,
                            "the same dedup key after a restart means the operator is never re-asked")

    def test_declined_is_not_an_answer(self):
        self.patch_control(poll="DECLINED")
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "carded")
        self.assertEqual(1, len(self.calls("restart")), "only the dry run may have run")
        self.assert_no_unattended_lift()

    def test_answered_leave_it_does_not_restart(self):
        """`ANSWERED` alone is not permission to kill. The card offers `restart it` and
        `leave it`, and acting on the terminal alone would make the card's safest answer the
        most destructive one."""
        self.patch_control(poll="ANSWERED: option: leave it")
        rc, lines = self.run_leg()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], "carded")
        self.assertEqual([], self.lifted_calls())

    def test_answered_leave_it_is_recorded_so_it_is_not_reread(self):
        self.patch_control(poll="ANSWERED: option: leave it")
        self.run_leg()
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "carded")
        self.assertIn("already consumed", lines[1])
        self.assertEqual([], self.lifted_calls())

    def test_a_failed_post_is_cannot_tell(self):
        self.patch_control(post_rc=1)
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "cannot-tell")
        self.assertEqual([], self.lifted_calls())

    def test_the_item_id_is_read_from_the_item_id_line(self):
        """`post` prints `ITEM_ID: <id>` then a `POLL:` hint line. Reading the LAST line
        yields that hint, so `poll` answers `cannot-tell` forever and the leg reports
        `carded` every round while never restarting anything."""
        self.patch_control(poll="ANSWERED: option: restart it")
        self.run_leg()
        polls = [a for a in self.calls("ask") if a[0] == "poll"]
        self.assertEqual(["item-1"], polls[0][1:2])

    def test_answered_runs_the_real_restart_with_the_lift(self):
        self.patch_control(poll="ANSWERED: option: restart it")
        rc, lines = self.run_leg()
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[0], "restart")
        real = [a for a in self.calls("restart") if "--dry-run" not in a]
        self.assertEqual(1, len(real))
        self.assertIn("--allow-busy-lapse", real[0])
        self.assertIn("--reason", real[0])
        self.assertEqual("lapse", real[0][real[0].index("--reason") + 1])
        self.assertIn("--fresh-prompt-task", real[0])

    def test_one_answer_never_becomes_a_loop(self):
        """The card is marked consumed BEFORE the call, so a second round that still reads
        `ANSWERED` does not restart again."""
        self.patch_control(poll="ANSWERED: option: restart it")
        self.run_leg()
        before = len([a for a in self.calls("restart") if "--dry-run" not in a])
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "carded")
        after = len([a for a in self.calls("restart") if "--dry-run" not in a])
        self.assertEqual(before, after, "a consumed card restarted the session a second time")


class AnswerShape(unittest.TestCase):
    """The store prefixes an answer with its KIND — `option: restart it` — because
    `attention-ask.py` renders through `describe_one`, whose shape is `{kind}: {value}`.
    Comparing the raw content against the bare label never matches, and the mismatch falls
    into the leave-it branch, so every approval is recorded as a decline and no restart ever
    runs. The whole feature is inert that way, which is why this is pinned directly."""

    def test_the_option_kind_is_stripped(self):
        self.assertEqual("restart it", st.answer_value("option: restart it"))

    def test_the_leave_label_is_stripped_too(self):
        self.assertEqual("leave it", st.answer_value("option: leave it"))

    def test_a_bare_label_still_works(self):
        self.assertEqual("restart it", st.answer_value("restart it"))

    def test_an_empty_answer_is_empty(self):
        self.assertEqual("", st.answer_value(""))
        self.assertEqual("", st.answer_value(None))


class FailClosed(Base):
    def test_a_corrupt_state_file_fails_closed(self):
        """A present-but-unreadable store could hide a spent cap or a consumed card, and
        both exist to stop a restart. Reading it as fresh resets the cap — fail-open."""
        path = st.state_path(self.vault, self.TASK)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "cannot-tell")
        self.assertEqual([], self.lifted_calls())

    def test_a_state_object_with_a_wrong_typed_counter_fails_closed(self):
        path = st.state_path(self.vault, self.TASK)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"restarts": "two", "consumed": []}, fh)
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "cannot-tell")
        self.assertEqual([], self.lifted_calls())

    def test_a_missing_hold_probe_reads_as_held(self):
        """`session-holds.py` absent means heldness is unknown, and unknown is not clear."""
        os.remove(st.HOLDS)
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "held")
        self.assertEqual([], self.calls("restart"))


class Cap(Base):
    def test_the_cap_stops_the_third_restart_and_escalates_once(self):
        self.patch_control(poll="ANSWERED: option: restart it")
        self.run_leg()                      # restart 1
        self.patch_control(poll="ANSWERED: option: restart it", post_id="item-2")
        self.run_leg()                      # restart 2
        self.patch_control(poll="ANSWERED: option: restart it", post_id="item-3")
        rc, lines = self.run_leg()          # capped
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], "capped")
        real = [a for a in self.calls("restart") if "--dry-run" not in a]
        self.assertEqual(2, len(real), "a third restart ran despite the cap")
        keys = [a[a.index("--dedup-key") + 1] for a in self.calls("ask") if a[0] == "post"]
        self.assertIn("stuck-tab-capped:" + st.slug_for(self.TASK), keys)

    def test_progress_resets_the_cap(self):
        self.patch_control(poll="ANSWERED: option: restart it")
        self.run_leg()
        self.patch_control(poll="ANSWERED: option: restart it", post_id="item-2")
        self.run_leg()
        # A newer task page is the reset signal, exactly worker-keepalive.py's.
        os.utime(self.task_path, (0, 10 ** 10))
        self.patch_control(poll="ANSWERED: option: restart it", post_id="item-3")
        rc, lines = self.run_leg()
        self.assertEqual(lines[0], "restart", "progress did not reset the cap")
        real = [a for a in self.calls("restart") if "--dry-run" not in a]
        self.assertEqual(3, len(real))


if __name__ == "__main__":
    unittest.main()
