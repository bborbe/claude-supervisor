"""worker-target.py: the fleet-wide worker target is read, not remembered.

The defect this instrument exists for was measured on 2026-10-02. A manager tick compares a
live count against the target, and only the count had a read command — the target had a
*pointer* to `docs/fleet-surface.md` § Spawn a worker item 5, which says where the value
lives and what it defaults to but is not a read. So a manager read `spawn.maxConcurrent` by
hand **once**, on its pre-spawn cap check, and carried the value for two hours: a target
changed to 12 left it posting under-target cards against a remembered 18, and its own drive
leg then derived the config from that memory (*"So the fleet config sets maxConcurrent 18"*)
rather than reading the file.

The load-bearing cases are the two UNKNOWN tests. A resolver that falls back to the default
when it cannot read the file answers `20` on a fleet an operator has capped at 12 — a number
nobody chose, reported as though it were configured, in the direction that reads as healthy.
`test_invalid_value_is_unknown_not_default` and
`test_unparseable_file_is_unknown_not_default` are the halves that refuse it.

The third is `test_missing_file_is_the_default`, which looks like the same case and is the
opposite one: no file means no key, which is the documented default and exactly how
`server/config.mjs` treats it. Collapsing "absent" into "unreadable" would make the normal
case — most machines have no config file — an error.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "worker-target.py"


class WorkerTargetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run_script(self, config, *args, env=None):
        """`(exit code, stdout, stderr)` with `SUPERVISOR_CONFIG` pointed at `config`."""
        environment = dict(os.environ, SUPERVISOR_CONFIG=config)
        # BOTH cap variables, not just the soft one: either left in the ambient environment
        # would override the fixture's config and make these assertions test the operator's
        # machine instead of the file under test.
        environment.pop("SUPERVISOR_MAX_CONCURRENT", None)
        environment.pop("SUPERVISOR_MAX_CONCURRENT_HARD", None)
        environment.update(env or {})
        out = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, env=environment,
        )
        return out.returncode, out.stdout, out.stderr

    # --- the value ---------------------------------------------------------------

    def test_missing_file_is_the_default(self):
        """No file means no key, which is the default — not an unreadable config."""
        code, out, _ = self.run_script(str(self.root / "does-not-exist.json"))
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "20")

    def test_absent_key_is_the_default(self):
        path = self.write("absent.json", '{"spawn": {"mode": "interactive"}}')
        code, out, _ = self.run_script(path)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "20")

    def test_explicit_key_is_read(self):
        """The case the whole instrument exists for: a configured 12 must not read as 20."""
        path = self.write("twelve.json", '{"spawn": {"maxConcurrent": 12}}')
        code, out, _ = self.run_script(path)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "12")

    def test_zero_is_unlimited(self):
        """`0` is the off switch, and it prints the token the fleet-loop marker prints."""
        path = self.write("zero.json", '{"spawn": {"maxConcurrent": 0}}')
        code, out, _ = self.run_script(path)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "unlimited")

    def test_env_wins_over_the_file(self):
        path = self.write("twelve.json", '{"spawn": {"maxConcurrent": 12}}')
        code, out, _ = self.run_script(
            path, env={"SUPERVISOR_MAX_CONCURRENT": "3"}
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "3")

    # --- the load-bearing halves: never guess -------------------------------------

    def test_invalid_value_is_unknown_not_default(self):
        path = self.write("bad.json", '{"spawn": {"maxConcurrent": "lots"}}')
        code, out, err = self.run_script(path)
        self.assertEqual(code, 2, "an invalid limit must not exit 0")
        self.assertEqual(out.strip(), "", "nothing may reach stdout on UNKNOWN")
        self.assertIn("UNKNOWN", err)

    def test_unparseable_file_is_unknown_not_default(self):
        path = self.write("notjson.json", "not json at all")
        code, out, err = self.run_script(path)
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")
        self.assertIn("UNKNOWN", err)

    # --- the output contract ------------------------------------------------------

    def test_bare_output_is_the_number_alone(self):
        """`$(…)` goes straight into a comparison, so stdout carries the token and nothing else."""
        path = self.write("twelve.json", '{"spawn": {"maxConcurrent": 12}}')
        _, out, _ = self.run_script(path)
        self.assertEqual(out, "12\n")
        self.assertNotIn("maxConcurrent", out)

    def test_source_names_the_decider(self):
        """A bare `18` cannot be told from a bare `12`; `--source` is how it is told."""
        absent = self.write("absent.json", "{}")
        twelve = self.write("twelve.json", '{"spawn": {"maxConcurrent": 12}}')

        _, out, _ = self.run_script(absent, "--source")
        self.assertEqual(out.strip(), "20 default")

        _, out, _ = self.run_script(twelve, "--source")
        self.assertEqual(out.strip(), "12 config")

        _, out, _ = self.run_script(
            twelve, "--source", env={"SUPERVISOR_MAX_CONCURRENT": "3"}
        )
        self.assertEqual(out.strip(), "3 env")

    def test_zero_reports_its_source_too(self):
        path = self.write("zero.json", '{"spawn": {"maxConcurrent": 0}}')
        _, out, _ = self.run_script(path, "--source")
        self.assertEqual(out.strip(), "unlimited config")

    # --- the number is not restated here ------------------------------------------

    def test_resolution_comes_from_the_server_module(self):
        """The instrument calls the real resolver; it does not carry its own copy of the rule.

        A restated default is a second counter, and a `grep` cannot tell it from the real
        one — which is the failure `check-worker-target.py` exists to refuse. So the script
        must name `spawn-mode.mjs`, and must not carry a literal default of its own.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("resolveMaxConcurrent", source)
        self.assertIn("spawn-mode.mjs", source)
        self.assertNotIn("DEFAULT_MAX_CONCURRENT = 20", source)


    # --- the hard cap (`--hard`, added 2026-10-04) -------------------------------

    def test_hard_flag_reads_the_hard_cap_from_the_same_resolver(self):
        """`--hard` prints the hard ceiling, and it comes from the SAME resolver call as the
        soft target, so the band a manager reports and the band the server enforces cannot
        drift. A regression where `--hard` echoed the soft value would leave this whole suite
        green while every reader was handed the wrong ceiling."""
        config = self.write("config.json", json.dumps({"spawn": {"maxConcurrent": 30, "maxConcurrentHard": 50}}))
        self.assertEqual(self.run_script(config)[1].strip(), "30")
        self.assertEqual(self.run_script(config, "--hard")[1].strip(), "50")
        self.assertEqual(self.run_script(config, "--hard", "--source")[1].strip(), "50 config")

    def test_hard_flag_defaults_to_50_when_the_key_is_absent(self):
        """The hard default is a real default, reachable with no config edit: a machine that has
        never set either key still ships the 20/50 pair."""
        config = self.write("config.json", json.dumps({"spawn": {}}))
        self.assertEqual(self.run_script(config)[1].strip(), "20")
        self.assertEqual(self.run_script(config, "--hard")[1].strip(), "50")

    def test_hard_flag_reports_unlimited_when_the_soft_off_switch_is_thrown(self):
        """`0` on the soft key disables the PAIR, so `--hard` reads `unlimited` too — not a bare
        `50`, which would render as a live ceiling a reader could act on."""
        config = self.write("config.json", json.dumps({"spawn": {"maxConcurrent": 0}}))
        self.assertEqual(self.run_script(config)[1].strip(), "unlimited")
        self.assertEqual(self.run_script(config, "--hard")[1].strip(), "unlimited")

    def test_zero_on_the_hard_key_is_unknown_not_a_silent_ceiling_removal(self):
        """The resolver refuses `0` on the hard key, so this instrument reports UNKNOWN with the
        reason — never `unlimited` (which a reader would act on) and never `50` (which would
        hide that the operator's line was rejected)."""
        config = self.write("config.json", json.dumps({"spawn": {"maxConcurrentHard": 0}}))
        code, out, err = self.run_script(config, "--hard")
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")
        self.assertIn("UNKNOWN", err)
        self.assertIn("maxConcurrentHard", err)


if __name__ == "__main__":
    unittest.main()
