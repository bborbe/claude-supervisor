#!/usr/bin/env python3
"""Tests for scripts/resolve-folders.py — the three folder names a subject command reads.

The script's whole value is in its failure branches, so most of these are negative. Two are
not, and they are the ones that matter most:

- **the emitted paths are ABSOLUTE**, joined against the owning vault's root. The config stores
  bare names (`23 Topics`), which resolve only from the vault root; emitting them bare from a
  subdirectory makes `find "24 Goals"` match nothing, which every membership check then reports
  as "this topic has no members" — a wrong answer that reads exactly like a real one.
- **a subdirectory of a vault resolves to that vault**, by nearest ancestor. An exact-equality
  test would report a session started in `…/25 Tasks` as unconfigured.

A gate that refused everything would satisfy every negative case here on its own, which is why
the two positive cases are asserted with their exact output rather than a bare exit code.
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


def run_main(payload=None, returncode=0, stderr="", cwd="/somewhere", raises=None):
    """Run `main()` with `vault-cli config list` and the cwd faked. Returns (code, stdout)."""

    def fake_run(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=["vault-cli"], returncode=returncode,
            stdout=payload if isinstance(payload, str) else json.dumps(payload),
            stderr=stderr,
        )

    out = io.StringIO()
    with mock.patch.object(rf.subprocess, "run", fake_run), \
         mock.patch.object(rf.os, "getcwd", lambda: cwd), \
         redirect_stdout(out):
        code = rf.main()
    return code, out.getvalue().strip()


class ResolvesTheOwningVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.realpath(self.tmp)
        self.sub = os.path.join(self.root, "25 Tasks")
        os.makedirs(self.sub, exist_ok=True)
        self.vaults = [{
            "name": "private-personal", "path": self.root,
            "topics_dir": "23 Topics", "goals_dir": "24 Goals", "tasks_dir": "25 Tasks",
        }]

    def test_from_the_vault_root(self):
        code, out = run_main(self.vaults, cwd=self.root)
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            "|".join(os.path.join(self.root, d) for d in ("23 Topics", "24 Goals", "25 Tasks")),
        )

    def test_from_a_subdirectory_emits_the_same_absolute_paths(self):
        """The regression this file exists for: bare names would not resolve from here."""
        code_root, out_root = run_main(self.vaults, cwd=self.root)
        code_sub, out_sub = run_main(self.vaults, cwd=self.sub)
        self.assertEqual((code_root, code_sub), (0, 0))
        self.assertEqual(out_root, out_sub)
        for path in out_sub.split("|"):
            self.assertTrue(os.path.isabs(path), f"{path!r} is not absolute")
            self.assertTrue(path.startswith(self.root + os.sep), path)

    def test_the_nearest_ancestor_wins(self):
        outer = os.path.join(self.root, "outer")
        inner = os.path.join(outer, "inner")
        os.makedirs(inner, exist_ok=True)
        vaults = [
            dict(self.vaults[0], name="outer", path=outer),
            dict(self.vaults[0], name="inner", path=inner),
        ]
        os.makedirs(os.path.join(inner, "deep"), exist_ok=True)
        code, out = run_main(vaults, cwd=os.path.join(inner, "deep"))
        self.assertEqual(code, 0)
        self.assertTrue(out.split("|")[0].startswith(inner + os.sep), out)


class RefusesRatherThanDefaults(unittest.TestCase):
    def test_outside_every_vault(self):
        code, out = run_main(
            [{"name": "v", "path": "/tmp", "topics_dir": "a", "goals_dir": "b", "tasks_dir": "c"}],
            cwd="/definitely/not/a/vault",
        )
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("UNKNOWN "), out)
        self.assertIn("inside no configured vault", out)

    def test_a_missing_key_is_reported_not_defaulted(self):
        code, out = run_main(
            [{"name": "v", "path": "/tmp", "topics_dir": "a", "tasks_dir": "c"}],
            cwd="/tmp",
        )
        self.assertEqual(code, 1)
        self.assertIn("goals_dir", out)

    def test_a_blank_key_is_reported_like_a_missing_one(self):
        code, out = run_main(
            [{"name": "v", "path": "/tmp", "topics_dir": "  ", "goals_dir": "b", "tasks_dir": "c"}],
            cwd="/tmp",
        )
        self.assertEqual(code, 1)
        self.assertIn("topics_dir", out)

    def test_vault_cli_non_zero(self):
        code, out = run_main(returncode=2, stderr="boom\n")
        self.assertEqual(code, 1)
        self.assertIn("exited 2", out)
        self.assertIn("boom", out)

    def test_vault_cli_not_on_path(self):
        code, out = run_main(raises=FileNotFoundError())
        self.assertEqual(code, 1)
        self.assertIn("not on PATH", out)

    def test_vault_cli_times_out(self):
        code, out = run_main(raises=subprocess.TimeoutExpired(cmd="vault-cli", timeout=10))
        self.assertEqual(code, 1)
        self.assertIn("timed out", out)

    def test_non_json(self):
        code, out = run_main("{not json")
        self.assertEqual(code, 1)
        self.assertIn("did not print JSON", out)

    def test_non_list_payload(self):
        code, out = run_main({"vaults": []})
        self.assertEqual(code, 1)
        self.assertIn("not a list", out)

    def test_entries_without_a_path_do_not_count(self):
        code, out = run_main([{"name": "nameless", "topics_dir": "a"}], cwd="/tmp")
        self.assertEqual(code, 1)
        self.assertIn("no vault with a path", out)


if __name__ == "__main__":
    unittest.main()
