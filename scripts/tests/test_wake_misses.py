"""wake-misses.py — the last-wake reading fleet-triage § 3.6 counts as NO-LOOP."""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(os.path.dirname(HERE), "wake-misses.py")
NOW = 1_800_000_000.0  # fixed clock
RECENT = "2027-01-15T07:00:00Z"  # 1 h before NOW
OLD = "2027-01-10T08:00:00Z"  # ~5 days before NOW


def load():
    spec = importlib.util.spec_from_file_location("wake_misses_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def row(ts, wake, notified="sent: x"):
    return f"{ts}\t10\tVERDICT change\t{notified}\t{wake}\tpid=1(attention-controller)\n"


class WakeMisses(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.root = tempfile.mkdtemp(prefix="wm-")
        self.vault = os.path.join(self.root, "private-personal")
        os.makedirs(self.vault)

    def _ledger(self, slug, *lines, subject=None):
        with open(os.path.join(self.vault, slug + ".ledger.txt"), "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        if subject:
            with open(os.path.join(self.vault, slug + ".json"), "w", encoding="utf-8") as fh:
                json.dump({"topic": subject}, fh)

    def _run(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.mod.main(["--state-dir", self.root, "--now", str(NOW), *argv])
        return rc, buf.getvalue().strip().splitlines()

    def test_last_actionable_no_manager_is_a_miss_named_by_subject(self):
        self._ledger("dpt",
                     row(RECENT, "wake=WOKEN session=6f14c290 live=yes"),
                     row(RECENT, "wake=NO-MANAGER session=339d0e33 live=no"),
                     row(RECENT, "wake=none", notified="-"),
                     subject="Development Pipeline Throughput")
        rc, out = self._run()
        self.assertEqual(0, rc)
        self.assertEqual(
            ["MISS", "private-personal", "Development Pipeline Throughput", RECENT,
             "NO-MANAGER", "339d0e33", "no"],
            out[0].split("\t"),
        )
        self.assertEqual("wake-misses=1 stale=0 measured=1 unmeasured=0", out[-1])

    def test_woken_and_guard_skip_are_not_misses(self):
        self._ledger("a", row(RECENT, "wake=NO-PANE session=x live=yes"),
                     row(RECENT, "wake=WOKEN session=x live=yes"))
        self._ledger("b", row(RECENT, "wake=SKIP:spinner session=y live=yes"))
        rc, out = self._run()
        self.assertEqual(["wake-misses=0 stale=0 measured=2 unmeasured=0"], out)

    def test_headless_and_no_pane_on_a_live_manager_are_misses(self):
        self._ledger("h", row(RECENT, "wake=HEADLESS session=6f14c290 live=yes"))
        self._ledger("p", row(RECENT, "wake=NO-PANE session=0dbb4336 live=yes"))
        _, out = self._run()
        self.assertEqual("wake-misses=2 stale=0 measured=2 unmeasured=0", out[-1])

    def test_a_failed_wake_run_is_a_miss(self):
        self._ledger("e", row(RECENT, "wake=ERROR session=- live=unknown"))
        _, out = self._run()
        self.assertTrue(out[0].startswith("MISS\t"))

    def test_a_miss_outside_the_window_is_stale_not_counted(self):
        self._ledger("old", row(OLD, "wake=NO-MANAGER session=dd92955f live=no"))
        _, out = self._run()
        self.assertTrue(out[0].startswith("STALE\t"))
        self.assertEqual("wake-misses=0 stale=1 measured=1 unmeasured=0", out[-1])

    def test_a_ledger_without_the_column_is_unmeasured_never_clean(self):
        self._ledger("pre", f"{RECENT}\t10\tVERDICT change\tsent: x\tpid=1\n")
        _, out = self._run()
        self.assertEqual(["wake-misses=0 stale=0 measured=0 unmeasured=1"], out)

    def test_only_wake_none_lines_is_measured_without_a_miss(self):
        self._ledger("quiet", row(RECENT, "wake=none", notified="-"))
        _, out = self._run()
        self.assertEqual(["wake-misses=0 stale=0 measured=1 unmeasured=0"], out)

    def test_subject_falls_back_to_the_slug(self):
        self._ledger("no-json", row(RECENT, "wake=NO-PANE session=x live=yes"))
        _, out = self._run()
        self.assertEqual("no-json", out[0].split("\t")[2])

    def test_unreadable_state_dir_exits_2(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = self.mod.main(["--state-dir", os.path.join(self.root, "missing")])
        self.assertEqual(2, rc)
        self.assertEqual("", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
