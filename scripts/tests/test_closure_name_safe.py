#!/usr/bin/env python3
"""Tests for scripts/closure-name-safe.py.

The contract is narrow on purpose: inside a double-quoted shell word only `$`, a backtick,
`"` and `\\` can still do harm, so those four are refused and everything else — spaces,
apostrophes, parentheses, `%`, unicode — passes. Over-refusing would block ordinary page
names; under-refusing would leave the one unattended interpolation in the tree open.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load("closure_name_safe", "closure-name-safe.py")


class OffendersTest(unittest.TestCase):
    def test_an_ordinary_name_is_clean(self):
        self.assertEqual([], guard.offenders("A Finished Goal Closes Itself"))

    def test_a_quote_is_refused(self):
        self.assertEqual(['"'], guard.offenders('A "quoted" name'))

    def test_a_dollar_is_refused(self):
        self.assertEqual(["$"], guard.offenders("Costs $(rm -rf /)"))

    def test_a_backtick_is_refused(self):
        self.assertEqual(["`"], guard.offenders("Run `whoami`"))

    def test_a_backslash_is_refused(self):
        self.assertEqual(["\\"], guard.offenders("Back\\slash"))

    def test_each_offender_is_reported_once(self):
        self.assertEqual(["$", "`"], guard.offenders("$a$b`c`"))

    def test_safe_punctuation_passes(self):
        # Spaces, apostrophes, parentheses, percent, ampersand and unicode are all inert
        # inside a double-quoted word; refusing them would block ordinary page names.
        self.assertEqual([], guard.offenders("Ben's 100% (draft) & more — ünïcode"))


class CliTest(unittest.TestCase):
    def test_safe_name_exits_zero(self):
        self.assertEqual(0, guard.main(["closure-name-safe.py", "An Ordinary Page"]))

    def test_unsafe_name_exits_one(self):
        self.assertEqual(1, guard.main(["closure-name-safe.py", 'A "quoted" page']))

    def test_wrong_arity_exits_two(self):
        self.assertEqual(2, guard.main(["closure-name-safe.py"]))


if __name__ == "__main__":
    unittest.main()
