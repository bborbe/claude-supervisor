#!/usr/bin/env python3
"""Tests for scripts/resolve-folders.py — the three folder names a subject command reads.

The script's whole value is in its failure branches, so most of these are negative. Two are
not, and they matter most:

- **the emitted paths are ABSOLUTE**, joined against the owning vault's root. The config stores
  bare names (`23 Topics`), which resolve only from the vault root; emitting them bare from a
  subdirectory makes `find "24 Goals"` match nothing, which every membership check then reports
  as "this topic has no members" — a wrong answer that reads exactly like a real one.
- **a subdirectory of a vault resolves to that vault**, by nearest ancestor. An exact-equality
  test would report a session started in `…/25 Tasks` as unconfigured.

⚠️ **`fake_run` records its argv and kwargs and the tests assert them.** An earlier version
discarded both, so deleting `timeout=…` or `--output json` from the script left the suite
green — the hang protection and the JSON contract were pinned by nothing.
"""

import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

_HERE = pathlib.Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location(
    "resolve_folders", str(_HERE.parent / "resolve-folders.py")
)
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)

EXPECTED_ARGV = ["vault-cli", "--output", "json", "config", "list"]


def run_main(payload=None, returncode=0, stderr="", cwd="/somewhere", raises=None, getcwd=None):
    """Run `main()` with `vault-cli config list` and the cwd faked. Returns (code, stdout, call)."""
    seen = {}

    def fake_run(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=EXPECTED_ARGV, returncode=returncode,
            stdout=payload if isinstance(payload, str) else json.dumps(payload),
            stderr=stderr,
        )

    out = io.StringIO()
    with mock.patch.object(rf.subprocess, "run", fake_run), \
         mock.patch.object(rf.os, "getcwd", getcwd or (lambda: cwd)), \
         redirect_stdout(out):
        code = rf.main()
    return code, out.getvalue().strip(), seen


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.sub = os.path.join(self.root, "25 Tasks")
        for d in ("23 Topics", "24 Goals", "25 Tasks"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        os.makedirs(self.sub, exist_ok=True)
        self.vaults = [{
            "name": "private-personal", "path": self.root,
            "topics_dir": "23 Topics", "goals_dir": "24 Goals", "tasks_dir": "25 Tasks",
        }]

    def make_vault(self, name, path, **extra):
        for d in ("23 Topics", "24 Goals", "25 Tasks"):
            os.makedirs(os.path.join(path, d), exist_ok=True)
        return dict(
            {"name": name, "path": path,
             "topics_dir": "23 Topics", "goals_dir": "24 Goals", "tasks_dir": "25 Tasks"},
            **extra,
        )


class ResolvesTheOwningVault(Base):
    def test_from_the_vault_root(self):
        code, out, seen = run_main(self.vaults, cwd=self.root)
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            "|".join(
                [os.path.join(self.root, d) for d in ("23 Topics", "24 Goals", "25 Tasks")]
                + ["private-personal"]
            ),
        )

    def test_the_cli_contract_is_pinned(self):
        """Deleting `--output json` or `timeout=` must fail here, not pass silently."""
        run_main(self.vaults, cwd=self.root)
        code, _out, seen = run_main(self.vaults, cwd=self.root)
        self.assertEqual(seen["args"][0], EXPECTED_ARGV)
        self.assertEqual(seen["kwargs"]["timeout"], rf.TIMEOUT_SECONDS)
        self.assertTrue(seen["kwargs"]["capture_output"])
        self.assertTrue(seen["kwargs"]["text"])

    def test_from_a_subdirectory_emits_the_same_absolute_paths(self):
        """The regression this file exists for: bare names would not resolve from here."""
        code_root, out_root, _ = run_main(self.vaults, cwd=self.root)
        code_sub, out_sub, _ = run_main(self.vaults, cwd=self.sub)
        self.assertEqual((code_root, code_sub), (0, 0))
        self.assertEqual(out_root, out_sub)
        fields = out_sub.split("|")
        for path in fields[:3]:
            self.assertTrue(os.path.isabs(path), f"{path!r} is not absolute")
            self.assertTrue(path.startswith(self.root + os.sep), path)
        self.assertEqual(fields[3], "private-personal")

    def test_the_nearest_ancestor_wins(self):
        outer = os.path.join(self.root, "outer")
        inner = os.path.join(outer, "inner")
        os.makedirs(os.path.join(inner, "deep"), exist_ok=True)
        vaults = [self.make_vault("outer", outer), self.make_vault("inner", inner)]
        code, out, _ = run_main(vaults, cwd=os.path.join(inner, "deep"))
        self.assertEqual(code, 0)
        self.assertEqual(out.split("|")[0], os.path.join(inner, "23 Topics"))
        self.assertEqual(out.split("|")[3], "inner")

    def test_a_sibling_sharing_a_string_prefix_does_not_match(self):
        """`/tmp/vault` must not claim `/tmp/vault2` — the `+ os.sep` guard is load-bearing."""
        sibling = self.root + "2"
        os.makedirs(sibling, exist_ok=True)
        self.addCleanup(lambda: os.rmdir(sibling) if os.path.isdir(sibling) else None)
        code, out, _ = run_main(self.vaults, cwd=sibling)
        self.assertEqual(code, 1)
        self.assertIn("inside no configured vault", out)


class RefusesRatherThanDefaults(Base):
    def test_outside_every_vault(self):
        code, out, _ = run_main([self.vaults[0]], cwd="/definitely/not/a/vault")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("UNKNOWN "), out)
        self.assertIn("inside no configured vault", out)

    def test_a_blank_path_cannot_claim_the_cwd(self):
        """`realpath("")` is the cwd, so a blank path would outrank every real vault."""
        blank = dict(self.vaults[0], path="   ")
        code, out, _ = run_main([blank], cwd=self.root)
        self.assertEqual(code, 1)
        self.assertIn("no vault with a path", out)

    def test_two_vaults_on_one_path_are_ambiguous(self):
        alias = dict(self.vaults[0], name="Personal")
        code, out, _ = run_main([self.vaults[0], alias], cwd=self.root)
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", out)

    def test_a_missing_key_is_reported_not_defaulted(self):
        vault = dict(self.vaults[0])
        del vault["goals_dir"]
        code, out, _ = run_main([vault], cwd=self.root)
        self.assertEqual(code, 1)
        self.assertIn("goals_dir", out)

    def test_a_blank_key_is_reported_like_a_missing_one(self):
        code, out, _ = run_main([dict(self.vaults[0], topics_dir="  ")], cwd=self.root)
        self.assertEqual(code, 1)
        self.assertIn("topics_dir", out)

    def test_a_non_string_key_is_refused_not_stringified(self):
        """`topics_dir: ["a"]` would join to a path that looks real."""
        code, out, _ = run_main([dict(self.vaults[0], topics_dir=["a"])], cwd=self.root)
        self.assertEqual(code, 1)
        self.assertIn("topics_dir", out)

    def test_a_stale_key_is_refused(self):
        """A renamed folder must be loud, not a plausible path that matches nothing."""
        os.rename(os.path.join(self.root, "24 Goals"), os.path.join(self.root, "24 Goal"))
        self.addCleanup(
            lambda: os.rename(os.path.join(self.root, "24 Goal"), os.path.join(self.root, "24 Goals"))
        )
        code, out, _ = run_main([self.vaults[0]], cwd=self.root)
        self.assertEqual(code, 1)
        self.assertIn("stale", out)

    def test_a_vault_without_a_name_is_refused(self):
        code, out, _ = run_main([dict(self.vaults[0], name="")], cwd=self.root)
        self.assertEqual(code, 1)
        self.assertIn("no registered name", out)

    def test_a_name_containing_the_separator_is_refused(self):
        code, out, _ = run_main([dict(self.vaults[0], name="a|b")], cwd=self.root)
        self.assertEqual(code, 1)
        self.assertIn("field separator", out)

    def test_an_unreadable_cwd_is_reported_not_raised(self):
        def boom():
            raise FileNotFoundError(2, "No such file or directory")
        code, out, _ = run_main(self.vaults, getcwd=boom)
        self.assertEqual(code, 1)
        self.assertIn("cwd could not be read", out)


class CliFailures(Base):
    def test_vault_cli_non_zero_reports_stderr(self):
        code, out, _ = run_main(returncode=2, stderr="boom\n")
        self.assertEqual(code, 1)
        self.assertIn("exited 2", out)
        self.assertIn("boom", out)

    def test_vault_cli_non_zero_falls_back_to_stdout(self):
        code, out, _ = run_main(returncode=2, stderr="", cwd="/tmp")
        self.assertEqual(code, 1)
        self.assertIn("exited 2", out)

    def test_vault_cli_not_on_path(self):
        code, out, _ = run_main(raises=FileNotFoundError())
        self.assertEqual(code, 1)
        self.assertIn("not on PATH", out)

    def test_vault_cli_times_out(self):
        code, out, _ = run_main(raises=subprocess.TimeoutExpired(cmd="vault-cli", timeout=10))
        self.assertEqual(code, 1)
        self.assertIn("timed out", out)

    def test_vault_cli_oserror(self):
        code, out, _ = run_main(raises=OSError(13, "Permission denied"))
        self.assertEqual(code, 1)
        self.assertIn("could not be run", out)
        self.assertIn("Permission denied", out)

    def test_non_json(self):
        code, out, _ = run_main("{not json")
        self.assertEqual(code, 1)
        self.assertIn("did not print JSON", out)

    def test_non_list_payload(self):
        code, out, _ = run_main({"vaults": []})
        self.assertEqual(code, 1)
        self.assertIn("not a list", out)

    def test_entries_without_a_path_do_not_count(self):
        code, out, _ = run_main([{"name": "nameless", "topics_dir": "a"}], cwd="/tmp")
        self.assertEqual(code, 1)
        self.assertIn("no vault with a path", out)


if __name__ == "__main__":
    unittest.main()
