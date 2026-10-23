#!/usr/bin/env python3
"""Resolve the three vault folders a subject command reads, from `vault-cli config list`.

`/supervisor:topic-verify` needs `topics_dir`, `goals_dir` and `tasks_dir` before it reads a
single page. Each is **declared per vault, never fixed** — the operator's vault renumbered
`23 Goals` → `24 Goals` and `24 Tasks` → `25 Tasks` on 2026-09-13 — so a literal is wrong in
every vault that numbers differently, and silently wrong is worse than loudly absent.

⚠️ **This exists as a script rather than an inline `python3 -c` for two reasons, and the
second is the one that bites.** The first is the repo rule that command files carry no
script bodies. The second is that an inline snippet has nowhere to put a `returncode`
check, a `timeout`, or an `except` — and every one of those is a failure mode observed here,
not a hypothetical: a `vault-cli` that exits non-zero, hangs on a locked config, or prints
something that is not JSON all reach the caller as an empty string, which reads as "this
vault has no folders" rather than "the probe failed".

⚠️ **The match is on an ANCESTOR, not on the exact cwd.** A session started in
`…/private-personal/25 Tasks` is inside a vault and must resolve it; an exact-equality test
would miss it and report the vault as unconfigured, which is the misdiagnosis this file was
written to remove. The longest matching prefix wins, so a vault nested inside another still
resolves to itself.

⚠️ **A missing key is reported, never defaulted.** `topics_dir` present and `goals_dir`
absent is not a vault with a default goals folder — it is a config that cannot answer, and
substituting a literal there is the hardcoded-path defect this whole file exists to remove,
applied quietly instead of loudly.

Prints one line, `TOPICS_DIR|GOALS_DIR|TASKS_DIR`, and exits 0. On any failure prints
`UNKNOWN <one-line reason>` and exits 1 — the caller reports that reason against every row
that reads a folder and stops, rather than running checks over empty paths.
"""

import json
import os
import subprocess
import sys

TIMEOUT_SECONDS = 10
KEYS = ("topics_dir", "goals_dir", "tasks_dir")


def fail(reason: str) -> int:
    print(f"UNKNOWN {reason}")
    return 1


def configured_vaults() -> tuple[list[dict], str]:
    """Every configured vault, plus a one-line degradation reason (`""` on success)."""
    try:
        res = subprocess.run(
            ["vault-cli", "--output", "json", "config", "list"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return [], "vault-cli is not on PATH"
    except subprocess.TimeoutExpired:
        return [], f"vault-cli config list timed out after {TIMEOUT_SECONDS}s"
    except OSError as exc:
        return [], f"vault-cli could not be run: {exc.strerror or exc}"

    if res.returncode != 0:
        detail = (res.stderr or "").strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        return [], f"vault-cli config list exited {res.returncode}{suffix}"

    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError:
        return [], "vault-cli config list did not print JSON"

    if not isinstance(payload, list):
        return [], f"vault-cli config list returned {type(payload).__name__}, not a list"

    vaults = [v for v in payload if isinstance(v, dict) and isinstance(v.get("path"), str)]
    if not vaults:
        return [], "vault-cli config list names no vault with a path"
    return vaults, ""


def owning_vault(vaults: list[dict]) -> tuple[dict, str] | None:
    """The vault whose root is the cwd or its nearest ancestor, with that resolved root."""
    cwd = os.path.realpath(os.getcwd())
    best: tuple[dict, str] | None = None
    best_len = -1
    for vault in vaults:
        root = os.path.realpath(os.path.expanduser(vault["path"]))
        if cwd == root or cwd.startswith(root + os.sep):
            if len(root) > best_len:
                best, best_len = (vault, root), len(root)
    return best


def main() -> int:
    vaults, reason = configured_vaults()
    if reason:
        return fail(reason)

    owned = owning_vault(vaults)
    if owned is None:
        return fail(
            "the session's cwd is inside no configured vault "
            f"({len(vaults)} configured) — run this from a vault, not from a bare directory"
        )
    vault, root = owned

    missing = [k for k in KEYS if not str(vault.get(k) or "").strip()]
    if missing:
        name = vault.get("name") or vault["path"]
        return fail(f"vault {name!r} does not configure {', '.join(missing)}")

    # ⚠️ ABSOLUTE, joined against the owning vault's root — never the bare relative name.
    # The config stores `23 Topics`, which resolves only from the vault root; a caller that
    # ran `find "24 Goals"` from `…/private-personal/25 Tasks` would match nothing and read
    # as "this topic has no members". That is the misdiagnosis this file exists to remove,
    # and emitting a relative name would have moved it one step later rather than fixed it.
    print("|".join(os.path.join(root, str(vault[k]).strip()) for k in KEYS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
