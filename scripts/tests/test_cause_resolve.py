"""cause-resolve.py: a resolver that always answers `can't tell` must not pass.

The whole point of the resolver is that a cause is *measured*, never borrowed. So every
case here is built to fail the laziest implementation of that idea, not to demonstrate a
happy path:

  - the positive control fails a stub that answers `unverified` unconditionally;
  - the sibling-borrow case fails a resolver that greps the sessions root instead of the
    one session directory, because the sibling's file names the target in a real reap tick;
  - the drain case fails a resolver that accepts any line naming the session;
  - the refusal case fails a resolver that accepts any `verb=reap`.

Each fixture is a JSONL events file copied into a temporary `sessions/<sid>/` tree, so the
path the resolver walks is the real one — `--sessions-root` and `--ledger` are the same
arguments production uses, only pointed at the fixture.
"""

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "cause-resolve.py"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "cause-resolve"

TARGET = "aaaaaaaa-1111-2222-3333-444444444444"
SIBLING = "bbbbbbbb-1111-2222-3333-444444444444"


def _load_module():
    spec = importlib.util.spec_from_file_location("cause_resolve", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CauseResolveTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="cause-resolve-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.sessions = self.root / "sessions"
        self.ledger = self.root / "ledger.jsonl"
        shutil.copyfile(FIXTURES / "ledger.jsonl", self.ledger)

    def _install(self, sid, fixture):
        target = self.sessions / sid
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / fixture, target / "events.jsonl")

    def _run(self, sid):
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--session",
                sid,
                "--sessions-root",
                str(self.sessions),
                "--ledger",
                str(self.ledger),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_positive_control_resolves_reaper(self):
        """A line naming the sid AND stating the action must resolve, not hedge.

        This is the case a `can't tell` stub fails: without it every other criterion on
        the task is satisfiable by an implementation that never attributes anything.
        """
        self._install(TARGET, "01-own-log-asserts.jsonl")
        result = self._run(TARGET)
        self.assertEqual(result["verdict"], "asserted")
        self.assertEqual(result["cause"], "reaper")
        self.assertIn("verb=reap outcome=closed", result["line"])

    def test_sibling_log_is_never_borrowed(self):
        """The target's own log is silent; the SIBLING's names the target in a reap tick.

        A resolver that globs `sessions/*/events.jsonl` finds that line and reports
        `reaper`. That is the exact round-60 failure, so this test must fail it.
        """
        self._install(TARGET, "02-target-silent.jsonl")
        self._install(SIBLING, "02-sibling-asserts-target.jsonl")
        result = self._run(TARGET)
        self.assertEqual(result["verdict"], "unverified")
        self.assertIsNone(result["cause"])
        self.assertNotIn("sibling", (result["line"] or ""))

    def test_names_but_does_not_assert(self):
        """`drain: … at turn boundary` names the session and ends nothing."""
        self._install(TARGET, "03-names-but-does-not-assert.jsonl")
        result = self._run(TARGET)
        self.assertEqual(result["verdict"], "unverified")
        self.assertIn("names-but-does-not-assert", result["reason"])
        self.assertIn("at turn boundary", result["reason"])

    def test_refusal_outcome_is_not_a_cause(self):
        """The reaper declining to reap is evidence it did NOT reap."""
        self._install(TARGET, "04-refusal-outcome.jsonl")
        result = self._run(TARGET)
        self.assertEqual(result["verdict"], "unverified")

    def test_ledger_is_reported_as_context_only(self):
        """The ledger row is surfaced, and never turned into a cause."""
        self._install(TARGET, "02-target-silent.jsonl")
        result = self._run(TARGET)
        self.assertEqual(result["verdict"], "unverified")
        self.assertIsNotNone(result["ledger"])
        self.assertEqual(result["ledger"]["reason"], "absent-from-sources")
        self.assertEqual(result["ledger"]["state"], "closed")

    def test_absent_session_directory_is_unverified(self):
        result = self._run(TARGET)
        self.assertEqual(result["verdict"], "unverified")
        self.assertIn("absent", result["reason"])

    def test_only_the_target_directory_is_opened(self):
        """Guard the read path directly, not only through its verdict.

        The sibling-borrow test proves the *answer* is right; this proves the *read* is
        scoped, so a future refactor that globs the root cannot pass on a lucky verdict.
        """
        module = _load_module()
        self._install(TARGET, "02-target-silent.jsonl")
        self._install(SIBLING, "02-sibling-asserts-target.jsonl")
        opened = []
        real_open = pathlib.Path.open

        def spy(path_self, *args, **kwargs):
            opened.append(str(path_self))
            return real_open(path_self, *args, **kwargs)

        pathlib.Path.open = spy
        try:
            module.resolve(TARGET, self.sessions, self.ledger)
        finally:
            pathlib.Path.open = real_open

        session_reads = [p for p in opened if "/sessions/" in p]
        self.assertTrue(session_reads, "the target's own log was never opened")
        for path in session_reads:
            self.assertIn(TARGET, path, "read outside the target session directory: %s" % path)


if __name__ == "__main__":
    unittest.main()
