#!/usr/bin/env python3
"""`resolve-launcher.py` — print the launcher, source, role and chip a RESUME must use.

Usage: resolve-launcher.py <session-id> --cwd <cwd>
Prints `<launcher path>\t<source>\t<role>\t<chip>`; exits 1 with the reason on stderr
when the launcher cannot be resolved.

Why this exists: `commands/open.md` § 3.1's resume branch execs a launcher it used to
take from PROSE — "apply the rule yourself: `vault-cli task get … launcher`". Prose is
not code. Whichever session ran the resume picked the value, nothing checked the result,
and the failure is silent by construction: a `role: agent` row resumed through the
CALLER's launcher looks like a normal start. Measured 2026-11-07: two `role: agent` rows
with no `launcher:` came back as `cc-private-claude --resume … /color cyan` because the
manager session that resumed them ran on Opus.

The rule itself is NOT restated here. The launcher comes from `restart-worker.py`'s
`resolve_launcher` — task `launcher:` → vault `worker_launcher` → vault `claude_script`,
with the `role: human` Claude rule and the manager rule already applied there — so the
resume path and the restart path can never disagree. This script adds only the two
things a resume needs on top: the role (read from the same claimant page) and the chip
(resolved from the role map, the same store `commands/open.md` § 3.0 reads).

⚠️ A missing or unreadable role map is NOT "default to cyan". The chip falls back to the
role's own published default — `pink` for an agent, `orange` for a manager, `cyan` for a
human — because the role is the fact and the map is only where its colour is stored. The
report says which was used so a stale map is visible rather than silent.
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The published role map — the same store `commands/open.md` § 3.0 and `server/config.mjs`
# read. `SUPERVISOR_ROLE_MAP` first so a drill can point at a fixture tree.
ROLE_MAP = os.environ.get("SUPERVISOR_ROLE_MAP") or os.path.expanduser(
    "~/.cache/wezterm-role-map.json"
)

# role → (window, default chip). Matrix Rule 1: an absent `role:` means agent, so this
# table is keyed on the RESOLVED role, never on the raw field.
ROLE_WINDOW = {"agent": "Agents", "manager": "Managers", "human": "Direct"}
ROLE_CHIP = {"agent": "pink", "manager": "orange", "human": "cyan"}


def load_sibling(filename):
    """Import a sibling script by path — the repo's own convention for sharing code."""
    name = os.path.basename(filename).replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def role_map(path=None):
    """The published role map as a dict, or `None` when it cannot be read.

    `None` is a distinct answer from `{}`: an unreadable map cannot prove a chip is
    absent, and reporting a fabricated default as if it were published is the silence
    this script exists to remove.
    """
    try:
        with open(path or ROLE_MAP, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def chip_for(role, mapping=None):
    """`(chip, source)` for a resolved role — the map's value, else the role's own default.

    `source` is `role-map` or `role-default`, so a caller can report that the map was
    missing instead of passing a fallback off as a published value.
    """
    published = (mapping or {}).get(ROLE_WINDOW.get(role, "Agents")) or {}
    chip = str(published.get("chip") or "").strip().lower()
    if chip:
        return chip, "role-map"
    return ROLE_CHIP.get(role, ROLE_CHIP["agent"]), "role-default"


def resolve(session_id, cwd):
    """`(launcher, source, role, chip)` for resuming `session_id`, or `(None, reason, …)`."""
    rw = load_sibling("restart-worker.py")
    fb = load_sibling("fleet-board.py")
    launcher, launcher_source = rw.resolve_launcher(session_id, cwd, fb)
    if launcher is None:
        return None, launcher_source, "", ""
    # An absent `role:` means agent — matrix Rule 1 — and that default lives here, at the
    # consumer, so `claimant_role` can keep reporting "" for "the page said nothing".
    role = rw.claimant_role(session_id, cwd, fb) or "agent"
    chip, _chip_source = chip_for(role, role_map())
    return launcher, launcher_source, role, chip


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("session_id", nargs="?")
    ap.add_argument("--cwd", default="")
    try:
        args = ap.parse_args(argv[1:])
    except SystemExit:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    if not args.session_id or not args.cwd:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    launcher, source, role, chip = resolve(args.session_id, args.cwd)
    if launcher is None:
        print(source, file=sys.stderr)
        return 1
    print(f"{launcher}\t{source}\t{role}\t{chip}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
