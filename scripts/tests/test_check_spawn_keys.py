#!/usr/bin/env python3
"""Tests for scripts/check-spawn-keys.py.

The check exists because a hand-maintained copy of the wrapper's key set had already
drifted once — five keys were missing, so a config using any of them was told the key was
ignored while the wrapper honoured it. A check that cannot itself be seen to fail is no
better than the copy it replaced, so every failure path below is exercised against a
fixture rather than trusted.

The parse is regex over Go source, which makes "the source changed shape" a real risk:
a silent zero-match would leave the derived set empty and compare nothing. Each test
therefore asserts a *specific* message, not merely that something failed.
"""

import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("check_spawn_keys", str(_HERE.parent / "check-spawn-keys.py"))
csk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(csk)

AXES = [
    ("maxSessionsSoft", "maxSessionsHard"),
    ("maxWorkerSoft", "maxWorkerHard"),
    ("maxHeadlessSoft", "maxHeadlessHard"),
    ("maxInteractiveSoft", "maxInteractiveHard"),
    ("maxManagersSoft", "maxManagersHard"),
    ("maxTopicManagersSoft", "maxTopicManagersHard"),
    ("maxGoalManagersSoft", "maxGoalManagersHard"),
]

RENAMED = [
    ("maxWorkerSoft", "maxConcurrent"),
    ("maxWorkerHard", "maxConcurrentHard"),
    ("maxManagersSoft", "maxManagers"),
    ("maxSessionsSoft", "maxSessions"),
    ("maxHeadlessSoft", "maxHeadless"),
    ("maxInteractiveSoft", "maxInteractive"),
    ("maxTopicManagersSoft", "maxTopicManagers"),
    ("maxGoalManagersSoft", "maxGoalManagers"),
]


def axes_go(pairs=AXES):
    body = "".join(f'\tAxis{i}: {{"{s}", "{h}"}},\n' for i, (s, h) in enumerate(pairs))
    return "package pkg\n\nvar axisKeys = map[string][2]string{\n" + body + "}\n"


def limits_go(renamed=RENAMED, tags=None, fold_calls=1):
    """A stand-in for pkg/spawn_limits.go carrying the three things the check reads."""
    if tags is None:
        tags = [n for pair in AXES for n in pair] + [old for _, old in renamed]
    fields = "".join(f'\t\tF{i} json.RawMessage `json:"{t}"`\n' for i, t in enumerate(tags))
    renamed_body = "".join(f'\t{{"{c}", "{l}"}},\n' for c, l in renamed)
    calls = "".join("\tf.applyLegacySpawnKeys(path)\n" for _ in range(fold_calls))
    return (
        "package pkg\n\n"
        "var renamedSpawnKeys = []struct{ current, legacy string }{\n" + renamed_body + "}\n\n"
        "type SpawnConfigFile struct {\n\tSpawn struct {\n" + fields + "\t} `json:\"spawn\"`\n}\n\n"
        "func load() {\n" + calls + "}\n\n"
        "func (f *SpawnConfigFile) applyLegacySpawnKeys(path string) {\n}\n"
    )


class DerivedKeysTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        (self.root / "pkg").mkdir()

    def tearDown(self):
        self.dir.cleanup()

    def write(self, axes=None, limits=None):
        (self.root / "pkg" / "spawn_axes.go").write_text(axes or axes_go(), encoding="utf-8")
        (self.root / "pkg" / "spawn_limits.go").write_text(limits or limits_go(), encoding="utf-8")

    def test_derives_the_full_set(self):
        self.write()
        axis_keys, legacy_keys = csk.derived_keys(self.root)
        self.assertEqual(len(axis_keys), 14)
        self.assertEqual(len(legacy_keys), 8)

    def test_missing_axis_keys_block_fails_loudly(self):
        self.write(axes="package pkg\n\nvar somethingElse = 1\n")
        with self.assertRaises(SystemExit) as cm:
            csk.derived_keys(self.root)
        self.assertIn("no `var axisKeys` block", str(cm.exception))

    def test_a_short_axis_table_fails_rather_than_comparing_a_partial_set(self):
        self.write(axes=axes_go(AXES[:6]))
        with self.assertRaises(SystemExit) as cm:
            csk.derived_keys(self.root)
        self.assertIn("expected 14", str(cm.exception))

    def test_missing_renamed_block_fails_loudly(self):
        self.write(limits="package pkg\n\nfunc (f *SpawnConfigFile) applyLegacySpawnKeys(p string) {}\n")
        with self.assertRaises(SystemExit) as cm:
            csk.derived_keys(self.root)
        self.assertIn("no `var renamedSpawnKeys` block", str(cm.exception))

    def test_a_renamed_key_with_no_json_tag_fails(self):
        """`axisKeys` is the display; the struct tag is what the parser matches on."""
        self.write(limits=limits_go(tags=[n for pair in AXES for n in pair]))
        with self.assertRaises(SystemExit) as cm:
            csk.derived_keys(self.root)
        self.assertIn("declares no json tag", str(cm.exception))

    def test_an_unwired_fold_fails(self):
        """A defined-but-never-called fold leaves every legacy key at its default."""
        self.write(limits=limits_go(fold_calls=0))
        with self.assertRaises(SystemExit) as cm:
            csk.derived_keys(self.root)
        self.assertIn("never calls it", str(cm.exception))


class JsListTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name) / "spawn-mode.mjs"
        self.orig = csk.SPAWN_MODE
        csk.SPAWN_MODE = self.path

    def tearDown(self):
        csk.SPAWN_MODE = self.orig
        self.dir.cleanup()

    def write(self, keys):
        body = ",\n  ".join(f"'{k}'" for k in keys)
        self.path.write_text(f"const KNOWN_SPAWN_KEYS = [\n  {body},\n]\n", encoding="utf-8")

    def test_reads_the_list(self):
        self.write(["mode", "maxWorkerSoft"])
        self.assertEqual(csk.js_keys(), ["mode", "maxWorkerSoft"])

    def test_a_missing_list_fails_loudly(self):
        self.path.write_text("const SOMETHING_ELSE = []\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            csk.js_keys()
        self.assertIn("no `const KNOWN_SPAWN_KEYS", str(cm.exception))


class MainTest(unittest.TestCase):
    """End-to-end through main(), because the duplicate check lives there."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "spawn_axes.go").write_text(axes_go(), encoding="utf-8")
        (self.root / "pkg" / "spawn_limits.go").write_text(limits_go(), encoding="utf-8")
        self.mjs = self.root / "spawn-mode.mjs"
        self.origs = (csk.wrapper_root, csk.SPAWN_MODE, csk.describe)
        csk.wrapper_root = lambda: self.root
        csk.SPAWN_MODE = self.mjs
        csk.describe = lambda root: "fixture"

    def tearDown(self):
        csk.wrapper_root, csk.SPAWN_MODE, csk.describe = self.origs
        self.dir.cleanup()

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                csk.main()
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue(), err.getvalue()

    def full_set(self):
        return (
            [n for pair in AXES for n in pair]
            + [old for _, old in RENAMED]
            + list(csk.NON_AXIS_KEYS)
        )

    def write_js(self, keys):
        body = ",\n  ".join(f"'{k}'" for k in keys)
        self.mjs.write_text(f"const KNOWN_SPAWN_KEYS = [\n  {body},\n]\n", encoding="utf-8")

    def test_a_matching_list_passes(self):
        self.write_js(self.full_set())
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("spawn-keys ok: 24 keys", out)

    def test_a_missing_key_fails_naming_it(self):
        keys = [k for k in self.full_set() if k != "maxSessionsSoft"]
        self.write_js(keys)
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("maxSessionsSoft", err)

    def test_a_duplicate_fails(self):
        self.write_js(self.full_set() + ["maxWorkerSoft"])
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("more than once", err)

    def test_an_unknown_key_fails(self):
        self.write_js(self.full_set() + ["maxTypo"])
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("maxTypo", err)

    def test_no_checkout_skips_rather_than_passing_silently(self):
        csk.wrapper_root = lambda: None
        code, _, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("SKIP", err)


if __name__ == "__main__":
    unittest.main()
