"""heartbeat-state.py: the event half of the heartbeat, pinned.

The timer answers *is this session alive* from a stamp's age; it cannot answer *what is it
doing*, because it fires on a clock rather than on an event. This script is the event half —
a Claude Code hook calls it and the next tick carries the state onto the stamp.

Two properties are load-bearing, and both are about what the script must NOT do:

`test_unknown_state_writes_nothing` — the state arrives as an argument from the manifest, and
an argument outside the vocabulary is a caller bug. Writing it through would put a value on
the wire that no reader's vocabulary contains, and the attention store passes enums through
verbatim rather than validating them.

`test_missing_session_id_writes_nothing` — with no id there is nothing to key the record on.
Writing under a guess would leave a row in the shared store that no session can ever clear.

`test_failure_paths_still_exit_zero` pins the fail-open contract: this runs inside the
operator's turn on every prompt and every stop, so a hook that exits non-zero degrades the
session it exists to observe.
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "heartbeat-state.py"

SESSION = "fa942d7a-c190-46dd-93f3-8dfe3280046b"


class HeartbeatStateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hb-state-test-")
        self.env = {**os.environ, "SUPERVISOR_HEARTBEAT_STATE_DIR": self.dir}

    def run_hook(self, state, payload):
        return subprocess.run(
            ["python3", str(SCRIPT), state],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
        )

    def records(self):
        return [n for n in os.listdir(self.dir) if n.endswith(".json")]

    def entries(self):
        """Every entry, not just `.json` ones.

        ⚠️ The no-write and traversal assertions claim NOTHING was written, and a `.json`
        filter cannot establish that — a leaked `<id>.json.tmp` passes it silently. Those
        assertions use this; the ones about a record specifically keep using `records()`.
        """
        return os.listdir(self.dir)

    def test_known_state_is_recorded(self):
        result = self.run_hook("busy", {"session_id": SESSION})
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.dir, f"{SESSION}.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["session_id"], SESSION)
        self.assertEqual(record["state"], "busy")
        self.assertIn("at", record)

    def test_unknown_state_writes_nothing(self):
        self.run_hook("napping", {"session_id": SESSION})
        self.assertEqual(self.records(), [])

    def test_missing_session_id_writes_nothing(self):
        self.run_hook("idle", {})
        self.assertEqual(self.records(), [])

    def test_the_kill_switch_disables_every_path(self):
        # ⚠️ `SUPERVISOR_HEARTBEAT_STATE=off` must stop the hook before anything else, on the
        # write AND the clear path. It is the operator's only lever short of editing a plugin
        # manifest, so it has to hold for every invocation rather than just `busy` — and an
        # untested lever is not a lever.
        env = {**self.env, "SUPERVISOR_HEARTBEAT_STATE": "off"}
        for state in ("busy", "idle", "waiting-on-operator", "clear"):
            result = subprocess.run(
                ["python3", str(SCRIPT), state],
                input=json.dumps({"session_id": SESSION}),
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, f"{state} must exit 0 when disabled")
        self.assertEqual(self.entries(), [], "the kill switch must write nothing")

    def test_session_id_cannot_escape_the_state_directory(self):
        # ⚠️ The id arrives from the hook payload — untrusted input on a path-join — so a
        # separator or a `..` component must write nothing rather than write outside the
        # directory. Same guard as `validateSessionID` in attention-controller and the
        # precedent in `resolve-task-file.py`.
        #
        # ⚠️ `entries()`, not `records()`: the claim is that NOTHING is written, which a
        # `.json` filter cannot establish. And the absolute case is in the tuple because a
        # leading `/` is a different class from an embedded separator — the code comment names
        # it explicitly, and `a/b` alone would not cover it.
        for bad in ("../escaped", "..", ".", "a/b", "a\\b", "/abs", "/etc/passwd", ""):
            self.run_hook("busy", {"session_id": bad})
        self.assertEqual(self.entries(), [], "a traversing id must write nothing")
        self.assertFalse(
            os.path.exists(os.path.join(self.dir, "..", "escaped.json")),
            "nothing may be written outside the state directory",
        )

    def test_clear_removes_the_record(self):
        # ⚠️ The SessionEnd hook. Without it the directory grows monotonically — this hook
        # fires for every session on the machine, and a record never unlinked outlives the
        # session it describes. The sibling heartbeat store has a sweep for the same reason.
        self.run_hook("busy", {"session_id": SESSION})
        self.assertEqual(len(self.records()), 1)
        self.run_hook("clear", {"session_id": SESSION})
        self.assertEqual(self.records(), [], "SessionEnd must leave nothing behind")

    def test_clear_on_a_missing_record_is_a_no_op(self):
        # Fail-open: clearing a record that was never written is normal, not an error.
        result = self.run_hook("clear", {"session_id": SESSION})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.records(), [])

    def test_no_leftover_temp_file(self):
        # The timer reads this directory on a clock, so a half-written record must never be
        # visible — the write is a temp file plus a rename, and the temp must not survive it.
        self.run_hook("waiting-on-operator", {"session_id": SESSION})
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_failure_paths_still_exit_zero(self):
        # ⚠️ Fail-open: an unwritable state directory must not fail the operator's turn.
        #
        # ⚠️ A chmod-0500 directory this test creates, NOT `/proc/definitely-not-writable`.
        # `/proc` is Linux-specific: on a runner without it the path is CREATABLE, the hook
        # writes a record into a freshly-made directory, and the assertion passes while
        # exercising nothing — the silent vacuity this suite exists to avoid. This is portable
        # and self-cleaning.
        readonly = tempfile.mkdtemp(prefix="hb-state-readonly-")
        self.addCleanup(shutil.rmtree, readonly, ignore_errors=True)
        os.chmod(readonly, 0o500)
        self.addCleanup(os.chmod, readonly, 0o700)
        env = {
            **os.environ,
            "SUPERVISOR_HEARTBEAT_STATE_DIR": os.path.join(readonly, "nested"),
        }
        result = subprocess.run(
            ["python3", str(SCRIPT), "busy"],
            input=json.dumps({"session_id": SESSION}),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, "a hook must never fail the turn it observes")

    def test_malformed_stdin_still_exits_zero(self):
        result = subprocess.run(
            ["python3", str(SCRIPT), "busy"],
            input="{not json",
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
