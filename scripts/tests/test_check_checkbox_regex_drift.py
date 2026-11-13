#!/usr/bin/env python3
"""Tests for `scripts/check-checkbox-regex-drift.py`.

The guard's whole argument is that an untested copy drifts silently — so the guard
itself must not be the one piece of it with no test. Six outcomes are pinned here,
and the fetch is injected rather than performed: `fetch(url)` is module-level, so the
suite runs offline and never depends on GitHub being reachable.

1. **A matching pair exits 0.** The ordinary case; without it a permanently-red guard
   would look like a working one.
2. **A differing pair exits 1 and prints BOTH literals.** The reading is the whole
   product — a bare "drifted" leaves the reader to diff two files by hand.
3. **An unreadable upstream exits 1 and names the error.** This is the fail-closed
   half: skipping when the source cannot be read would make the guard pass exactly
   when it can prove nothing.
4. **An upstream page carrying no literal exits 1.** The extraction moving or being
   renamed must not read as agreement.
5. **A reader file carrying no mirror exits 1.** The same, for the local side.
6. **A differently-named assignment does not satisfy the extraction.** Unanchored,
   `MY_VERB_BOX = …` matches on the `_VERB_BOX` substring and the guard reports `ok`
   for a line that is not the mirror at all.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drift = _load("check_checkbox_regex_drift", "check-checkbox-regex-drift.py")

LOCAL_LINE = '_VERB_BOX = re.compile(r"^x$", re.M)\n'
REMOTE_LINE = "\tCheckboxRegex = regexp.MustCompile(`^x$`)\n"


class DriftCheckTest(unittest.TestCase):
    def _run(self, local_text, remote_text="", fetch_error=None):
        """Run `main()` against a temp reader file and an injected upstream response."""
        handle = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
        handle.write(local_text)
        handle.close()

        original_reader, original_fetch = drift.READER, drift.fetch
        drift.READER = Path(handle.name)

        def fake_fetch(url):
            if fetch_error is not None:
                raise OSError(fetch_error)
            return remote_text

        drift.fetch = fake_fetch
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = drift.main()
        finally:
            drift.READER, drift.fetch = original_reader, original_fetch
            os.unlink(handle.name)
        return code, buf.getvalue()

    def test_matching_pair_exits_zero(self):
        code, out = self._run(LOCAL_LINE, REMOTE_LINE)
        self.assertEqual(code, 0)
        self.assertIn("checkbox-regex-drift ok", out)

    def test_drift_exits_one_and_prints_both_literals(self):
        code, out = self._run(LOCAL_LINE, "\tCheckboxRegex = regexp.MustCompile(`^y$`)\n")
        self.assertEqual(code, 1)
        self.assertIn("has drifted from its source", out)
        self.assertIn("^x$", out)
        self.assertIn("^y$", out)

    def test_unreadable_upstream_fails_closed_and_names_the_error(self):
        code, out = self._run(LOCAL_LINE, fetch_error="curl: (7) Failed to connect")
        self.assertEqual(code, 1)
        self.assertIn("cannot read the upstream regex", out)
        self.assertIn("curl: (7) Failed to connect", out)

    def test_upstream_without_the_literal_exits_one(self):
        code, out = self._run(LOCAL_LINE, "package storage\n")
        self.assertEqual(code, 1)
        self.assertIn("the literal moved or was renamed", out)

    def test_reader_without_the_mirror_exits_one(self):
        code, out = self._run("# no mirror here\n", REMOTE_LINE)
        self.assertEqual(code, 1)
        self.assertIn("the mirror moved or was renamed", out)

    def test_extraction_ignores_a_differently_named_assignment(self):
        # Unanchored, `MY_VERB_BOX = …` matches on the `_VERB_BOX` substring and the guard
        # reports `ok` for a line that is not the mirror at all.
        code, out = self._run('MY_VERB_BOX = re.compile(r"^x$", re.M)\n', REMOTE_LINE)
        self.assertEqual(code, 1)
        self.assertIn("the mirror moved or was renamed", out)


if __name__ == "__main__":
    unittest.main()
