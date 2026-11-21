#!/usr/bin/env python3
"""Every symbol an importer reads off `session-ownership.py` must exist there.

⚠️ **This file exists because a repoint shipped that `make precommit` could not
catch.** `gate-answer-check.py` was repointed from `gate-owner-filter.py` to
`session-ownership.py`; the suite passed because it mocks `load_inputs`
wholesale, so nothing executed the `filt.live_ids()` call at `:747` and every
real invocation raised `AttributeError`. The review found it, not the tests.

The guard is static on purpose: it reads each importer's `filt.<name>` uses and
asserts the module defines them, so a symbol that exists only on the filter
cannot be reached through a loader pointed at the new module.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import re
import tempfile
import unittest

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(filename):
    with open(os.path.join(_SCRIPTS, filename), encoding="utf-8") as handle:
        return handle.read()


class TestSessionOwnershipSymbols(unittest.TestCase):
    def test_gate_answer_check_reads_only_symbols_session_ownership_defines(self):
        ownership = _load("session_ownership", "session-ownership.py")
        used = sorted(set(re.findall(r"\bfilt\.([A-Za-z_][A-Za-z0-9_]*)", _read("gate-answer-check.py"))))
        self.assertTrue(used, "the probe found no filt.<name> use — it is blind, not clean")
        missing = [name for name in used if not hasattr(ownership, name)]
        self.assertEqual([], missing, f"session-ownership.py is missing {missing}; used: {used}")

    def test_live_ids_fails_open_on_an_unreadable_registry(self):
        ownership = _load("session_ownership", "session-ownership.py")
        self.assertIsNone(ownership.live_ids(os.path.join(_SCRIPTS, "no-such-registry")))

    def test_live_ids_reads_both_field_spellings(self):
        ownership = _load("session_ownership", "session-ownership.py")
        with tempfile.TemporaryDirectory() as directory:
            for name, record in (
                ("a.json", {"sessionId": "sid-camel"}),
                ("b.json", {"session_id": "sid-snake"}),
                ("c.json", {"unrelated": True}),
            ):
                with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                    json.dump(record, handle)
            self.assertEqual({"sid-camel", "sid-snake"}, ownership.live_ids(directory))


if __name__ == "__main__":
    unittest.main()
