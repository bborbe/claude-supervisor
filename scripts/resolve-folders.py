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

⚠️ **A resolved folder that does not exist is also refused.** A key left stale by a renumber
emits a plausible absolute path, the caller's `find` matches nothing, and the report reads as
"no match" on a page that exists. `isdir` turns that into the loud answer the docstring above
argues for.

Prints one line, `TOPICS_DIR|GOALS_DIR|TASKS_DIR|VAULT_NAME`, and exits 0. On any failure prints
`UNKNOWN <one-line reason>` and exits 1 — the caller reports that reason against every row
that reads a folder and stops, rather than running checks over empty paths.

⚠️ **The fourth field is the registered vault NAME, and it is not derivable from the three
paths.** `docs/subject-resolution.md` keys two of its four subject sources on that name — a
session record counts only when its `vault` matches, and the last-subject fallback reads
`last-<vault>.json` — while the directory basename routinely differs from the registered name
(`Personal` on disk, `private-personal` in config). A caller with no `vault-cli` grant cannot
reconstruct it, so the resolver hands it over rather than leaving the caller to invent one.
"""

import json
import os
import subprocess

TIMEOUT_SECONDS = 10
KEYS = ("topics_dir", "goals_dir", "tasks_dir")


def fail(reason: str) -> int:
    print(f"UNKNOWN {reason}")
    return 1


def configured_vaults() -> tuple[list[dict], int, str]:
    """Every configured vault, the entry count, plus a one-line reason (`""` on success)."""
    try:
        res = subprocess.run(
            ["vault-cli", "--output", "json", "config", "list"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return [], 0, "vault-cli is not on PATH"
    except subprocess.TimeoutExpired:
        return [], 0, f"vault-cli config list timed out after {TIMEOUT_SECONDS}s"
    except OSError as exc:
        return [], 0, f"vault-cli could not be run: {exc.strerror or exc}"

    if res.returncode != 0:
        detail = ((res.stderr or "").strip() or (res.stdout or "").strip()).splitlines()
        suffix = f": {detail[0]}" if detail else ""
        return [], 0, f"vault-cli config list exited {res.returncode}{suffix}"

    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError:
        return [], 0, "vault-cli config list did not print JSON"

    if not isinstance(payload, list):
        return [], 0, f"vault-cli config list returned {type(payload).__name__}, not a list"

    # ⚠️ A blank path is rejected, not accepted. `realpath(expanduser(""))` is the cwd, so an
    # entry carrying `path: ""` would match the cwd exactly and — being the longest match —
    # outrank every real vault, emitting `cwd/23 Topics` with exit 0.
    vaults = [
        v for v in payload
        if isinstance(v, dict) and isinstance(v.get("path"), str) and v["path"].strip()
    ]
    if not vaults:
        return [], len(payload), "vault-cli config list names no vault with a path"
    return vaults, len(payload), ""


def owning_vault(vaults: list[dict]) -> tuple[dict, str] | str:
    """The vault whose root is the cwd or its nearest ancestor, or a reason string."""
    try:
        cwd = os.path.realpath(os.getcwd())
    except OSError as exc:
        return f"the session's cwd could not be read: {exc.strerror or exc}"

    matches: list[tuple[str, dict]] = []
    for vault in vaults:
        root = os.path.realpath(os.path.expanduser(vault["path"]))
        if cwd == root or cwd.startswith(root + os.sep):
            matches.append((root, vault))

    if not matches:
        return (
            "the session's cwd is inside no configured vault "
            f"({len(vaults)} configured) — run this from a vault, not from a bare directory"
        )

    matches.sort(key=lambda m: len(m[0]), reverse=True)
    best_root, best_vault = matches[0]
    # ⚠️ A tie is reported, not broken silently. Two configs can resolve to one path through a
    # symlink alias, and the caller keys two subject sources on the name — which one won would
    # then depend on config order.
    if len(matches) > 1 and len(matches[1][0]) == len(best_root):
        names = sorted({str(m[1].get("name") or m[0]) for m in matches[:2]})
        return f"two vaults resolve to {best_root}: {', '.join(names)} — ambiguous"
    return best_vault, best_root


def main() -> int:
    vaults, total, reason = configured_vaults()
    if reason:
        return fail(reason)

    owned = owning_vault(vaults)
    if isinstance(owned, str):
        return fail(owned)
    vault, root = owned

    # ⚠️ A non-string value is not a folder name. `topics_dir: ["a"]` coerces to "['a']" and
    # joins to a path that looks real, so the type is checked rather than stringified.
    bad = [k for k in KEYS if not (isinstance(vault.get(k), str) and vault[k].strip())]
    if bad:
        name = vault.get("name") or vault["path"]
        return fail(f"vault {name!r} does not configure {', '.join(bad)}")

    name = str(vault.get("name") or "").strip()
    if not name:
        return fail(f"vault at {root!r} carries no registered name")
    if "|" in name:
        return fail(f"vault name {name!r} contains '|', which is the field separator")

    folders = [os.path.join(root, vault[k].strip()) for k in KEYS]
    absent = [folder for folder in folders if not os.path.isdir(folder)]
    if absent:
        listed = ", ".join(repr(folder) for folder in absent)
        verb = "it does" if len(absent) == 1 else "they do"
        return fail(
            f"vault {name!r} configures {listed} but {verb} not exist — the key is stale"
        )

    # ⚠️ ABSOLUTE, joined against the owning vault's root — never the bare relative name.
    # The config stores `23 Topics`, which resolves only from the vault root; a caller that
    # ran `find "24 Goals"` from `…/private-personal/25 Tasks` would match nothing and read
    # as "this topic has no members". That is the misdiagnosis this file exists to remove,
    # and emitting a relative name would have moved it one step later rather than fixed it.
    print("|".join(folders + [name]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
