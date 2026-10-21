"""The retained-row re-probe: one probe per row per round, and the dedup key.

The load-bearing test here is `test_probe_runs_once_per_retained_row`. The defect this
whole change exists to remove is a block that reprints the ledger's stored `probe`
string and calls it a re-probe -- it would pass any assertion about *content* while
probing nothing. Counting invocations of the stubbed probe is what distinguishes a
re-probe from a copy, and a copy calls it zero times.
"""
import importlib.util
import os
import pathlib
import shutil
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "drive-retained.py"

_spec = importlib.util.spec_from_file_location("drive_retained", SCRIPT)
drive_retained = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drive_retained)


def entry(sid, verdict, **over):
    row = {
        "session_id": sid,
        "name": "Task %s" % sid,
        "verdict": verdict,
        "open_boxes": 3,
        "probe": "",
        "nudge": "none",
        "suppression": "none",
        "task_file": "/vault/25 Tasks/%s.md" % sid,
        "reprobe_verdict": "",
        "reprobe_probe": "",
    }
    row.update(over)
    return row


class TestProbeInvocation(unittest.TestCase):
    def test_probe_runs_once_per_retained_row(self):
        ledger = {
            "round": "r1",
            "entries": [
                entry("aaaa", "blocked"),
                entry("bbbb", "unverifiable"),
                entry("cccc", "revive"),
                entry("dddd", "finished"),
            ],
        }
        calls = []

        def stub(row):
            calls.append(row["session_id"])
            return {"verdict": "needs-step-4", "probe": "stub"}

        rows = [
            drive_retained.disposition(e, probe_fn=stub)
            for e in drive_retained.retained_entries(ledger)
        ]

        self.assertEqual(calls, ["aaaa", "bbbb"])
        self.assertEqual(len(rows), 2)

    def test_a_copy_that_never_probes_fails_the_count(self):
        """The laziest implementation: echo the stored probe, probe nothing."""

        def copy(row):
            return {"verdict": row["verdict"], "probe": row.get("probe") or ""}

        calls = []

        def counting(row):
            calls.append(row["session_id"])
            return copy(row)

        ledger = {"round": "r1", "entries": [entry("aaaa", "blocked", probe="old")]}
        for e in drive_retained.retained_entries(ledger):
            drive_retained.disposition(e, probe_fn=counting)

        self.assertEqual(calls, ["aaaa"], "the probe must run, not be skipped")

    def test_revive_and_finished_rows_are_not_retained(self):
        ledger = {
            "round": "r1",
            "entries": [entry("aaaa", "revive"), entry("bbbb", "finished")],
        }
        self.assertEqual(drive_retained.retained_entries(ledger), [])


class TestDispositions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drive-retained-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.task = os.path.join(self.tmp, "task.md")

    def write_task(self, status, phase, open_boxes):
        lines = ["---", "status: %s" % status, "phase: %s" % phase, "---", ""]
        lines += ["- [ ] step %d" % i for i in range(open_boxes)]
        lines += ["- [x] done %d" % i for i in range(2)]
        with open(self.task, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def run_probe(self, alive=True, **over):
        row = entry("aaaa", "blocked", task_file=self.task, **over)
        return drive_retained.probe(
            row,
            read_task_fn=drive_retained.read_task,
            alive_fn=lambda _sid: alive,
        )

    def test_terminal_task_is_finished(self):
        self.write_task("completed", "done", 0)
        self.assertEqual(self.run_probe()["verdict"], "finished")

    def test_open_boxes_with_dead_session_is_an_orphan(self):
        self.write_task("in_progress", "execution", 3)
        result = self.run_probe(alive=False)
        self.assertEqual(result["verdict"], "blocked")
        self.assertIn("orphan", result["probe"])

    def test_open_boxes_with_live_session_needs_step_4(self):
        self.write_task("in_progress", "execution", 3)
        self.assertEqual(self.run_probe()["verdict"], drive_retained.NEEDS_STEP_4)

    def test_missing_task_file_is_unverifiable(self):
        self.assertEqual(self.run_probe()["verdict"], "unverifiable")

    def test_empty_task_file_is_unverifiable_not_guessed(self):
        row = entry("aaaa", "blocked", task_file="")
        self.assertEqual(drive_retained.probe(row)["verdict"], "unverifiable")

    def test_a_parked_row_becomes_revive_eligible_in_a_later_round(self):
        """Round N: dead session -> blocked. Round N+k: live -> needs-step-4.

        `needs-step-4` is the state step 6 drafts a nudge from, so this is the
        transition the change exists to make reachable. Before step 0 the row was
        never re-probed at all, so it could not move.

        The disposition must also *signal*, because that is what makes it a nudge
        rather than a silent re-render.
        """
        self.write_task("in_progress", "execution", 3)

        first = drive_retained.probe(
            entry("aaaa", "blocked", task_file=self.task),
            read_task_fn=drive_retained.read_task,
            alive_fn=lambda _sid: False,
        )
        self.assertEqual(first["verdict"], "blocked")

        later = drive_retained.probe(
            entry("aaaa", "blocked", task_file=self.task),
            read_task_fn=drive_retained.read_task,
            alive_fn=lambda _sid: True,
        )
        self.assertEqual(later["verdict"], drive_retained.NEEDS_STEP_4)

        out = drive_retained.disposition(
            entry("aaaa", "blocked", task_file=self.task, probe=first["probe"]),
            probe_fn=lambda _row: later,
        )
        self.assertEqual(out["verdict"], drive_retained.NEEDS_STEP_4)
        self.assertEqual(out["suppression"], "signalled")

    def test_unknown_liveness_is_not_death(self):
        """An unreadable registry must not invent an orphan."""
        self.write_task("in_progress", "execution", 3)
        result = self.run_probe(alive=None)
        self.assertNotEqual(result["verdict"], "blocked")
        self.assertEqual(result["verdict"], drive_retained.NEEDS_STEP_4)


class TestDedup(unittest.TestCase):
    def stub(self, verdict, probe):
        return lambda _row: {"verdict": verdict, "probe": probe}

    def test_first_observation_signals(self):
        row = entry("aaaa", "blocked", probe="")
        out = drive_retained.disposition(
            row, probe_fn=self.stub("blocked", "same reason")
        )
        self.assertEqual(out["suppression"], "signalled")

    def test_unchanged_row_is_suppressed(self):
        row = entry("aaaa", "blocked", reprobe_verdict="blocked", reprobe_probe="same reason")
        out = drive_retained.disposition(
            row, probe_fn=self.stub("blocked", "same reason")
        )
        self.assertEqual(out["suppression"], "already signalled")

    def test_changed_verdict_signals(self):
        row = entry("aaaa", "blocked", reprobe_verdict="blocked", reprobe_probe="same reason")
        out = drive_retained.disposition(
            row, probe_fn=self.stub("unverifiable", "same reason")
        )
        self.assertEqual(out["suppression"], "signalled")

    def test_changed_reason_at_the_same_verdict_signals(self):
        row = entry("aaaa", "blocked", reprobe_verdict="blocked", reprobe_probe="old reason")
        out = drive_retained.disposition(
            row, probe_fn=self.stub("blocked", "new reason")
        )
        self.assertEqual(out["suppression"], "signalled")

    def test_a_live_row_stops_resignalling_once_the_agent_resolved_it(self):
        """The 2026-10-21 regression: two consecutive rounds, identical PARKED blocks.

        The agent stores its step-4 conclusion as `verdict` (`unverifiable`), while
        step 0's observation is `needs-step-4` -- a value VERDICTS deliberately does
        not hold. Comparing the observation against the conclusion could never match,
        so the row re-signalled forever. The dedup key must be the observation.
        """
        live = "status=in_progress phase=execution open_boxes=8, session live"
        row = entry(
            "aaaa",
            "unverifiable",
            probe="no CLOSERS row -> fail closed",
            reprobe_verdict="needs-step-4",
            reprobe_probe=live,
        )
        out = drive_retained.disposition(
            row, probe_fn=self.stub("needs-step-4", live)
        )
        self.assertEqual(out["suppression"], "already signalled")

    def test_a_zero_box_row_stops_resignalling_once_the_agent_disagreed(self):
        """Same shape, other vocabulary split: probe says finished, step 1b refuses.

        `probe()` calls open_boxes == 0 terminal; the agent's step 1b re-reads status
        and phase and lands on `unverifiable`. Both verdicts are stable round to
        round, so the row must be suppressed even though the two disagree.
        """
        zero = "status=in_progress phase=execution open_boxes=0"
        row = entry(
            "aaaa",
            "unverifiable",
            probe="not terminal (status not completed/aborted, phase not done)",
            reprobe_verdict="finished",
            reprobe_probe=zero,
        )
        out = drive_retained.disposition(row, probe_fn=self.stub("finished", zero))
        self.assertEqual(out["suppression"], "already signalled")

    def test_the_disposition_reports_the_observation_for_next_round(self):
        row = entry("aaaa", "blocked", reprobe_verdict="", reprobe_probe="")
        out = drive_retained.disposition(
            row, probe_fn=self.stub("needs-step-4", "status=in_progress open_boxes=3")
        )
        self.assertEqual(out["reprobe_verdict"], "needs-step-4")
        self.assertEqual(out["reprobe_probe"], "status=in_progress open_boxes=3")


class TestLivenessMapping(unittest.TestCase):
    """Only LIVE and ABSENT may become booleans; everything else is unknown."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drive-retained-live-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def stub(self, code):
        path = os.path.join(self.tmp, "fake-liveness-%d.py" % code)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("import sys\nsys.exit(%d)\n" % code)
        return path

    def test_live_maps_to_true(self):
        self.assertIs(drive_retained.session_alive("aaaa", self.stub(0)), True)

    def test_absent_maps_to_false(self):
        self.assertIs(drive_retained.session_alive("aaaa", self.stub(1)), False)

    def test_unknown_ambiguous_and_malformed_are_none(self):
        for code in (2, 3, 4):
            self.assertIsNone(
                drive_retained.session_alive("aaaa", self.stub(code)),
                "exit %d must not read as death" % code,
            )


if __name__ == "__main__":
    unittest.main()
