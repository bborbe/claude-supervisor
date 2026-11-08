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
things a resume needs on top: the role and the chip.

⚠️ **The role is read BEFORE the launcher, and that order is load-bearing.** A manager
session usually carries no task page, so a claimant-only lookup reads it as a worker and
resumes it on the worker default. `restart-worker.py`'s `resume_role` runs the same
`role_of` predicate the restart path uses and hands `resolve_launcher` the manager's
`subject`; without that a resumed goal or topic manager comes back on the worker model
while every line of this file reports success.

⚠️ A missing or unreadable role map is NOT "default to cyan". The chip falls back to the
role's own published default — `pink` for an agent, `orange` for a manager, `cyan` for a
human — because the role is the fact and the map is only where its colour is stored.
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

# The colours `/color` accepts, from `commands/open.md` § Arguments. A published chip
# outside this set is refused rather than forwarded — see `chip_for`.
VALID_CHIPS = frozenset(
    ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan", "default")
)


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


def safe_role(role):
    """`role` narrowed to the three the chip table knows — anything else is `agent`.

    ⚠️ **The role is author-controlled text read from a task page, and this script's output
    is TAB-DELIMITED.** The call site splits it with `cut -f1` / `cut -f4`, so a `role:`
    value carrying a tab shifts every field after it: `cut -f4` would hand that text to
    `$CHIP`, which `commands/open.md` re-emits inside `bash -lc` — past the chip allow-list
    and into the inner shell. Only these three values are ever emitted. Caught in review.
    """
    return role if role in ROLE_CHIP else "agent"


def chip_for(role, mapping=None):
    """`(chip, source)` for a resolved role — the map's value, else the role's own default.

    `source` is `role-map` or `role-default`.

    ⚠️ **A published chip is only accepted when it is one of the eight valid colours.** The
    value is re-emitted by `commands/open.md` as the spawn prompt `"/color <chip>"` inside a
    `bash -lc` string, so a corrupt or hand-edited role map carrying shell text would be
    parsed by that inner shell. Anything outside the allow list falls back to the role's own
    default rather than reaching the spawn — the same fail-closed direction the repo takes
    for every other value it executes.
    """
    published = (mapping or {}).get(ROLE_WINDOW.get(role, "Agents")) or {}
    chip = str(published.get("chip") or "").strip().lower()
    if chip in VALID_CHIPS:
        return chip, "role-map"
    return ROLE_CHIP.get(role, ROLE_CHIP["agent"]), "role-default"


def resolve(session_id, cwd):
    """`(launcher, source, role, chip)` for resuming `session_id`, or `(None, reason, …)`.

    ⚠️ The launcher is shape-checked before it is returned. `commands/open.md` re-emits it
    inside a `bash -lc` string, where a value carrying a quote or a `$(…)` is parsed a
    second time by that inner shell — `SUPERVISOR_LAUNCHER` is the one arm that skips
    `resolve_launcher`'s own check, so the guard belongs here, at the boundary.
    """
    rw = load_sibling("restart-worker.py")
    fb = load_sibling("fleet-board.py")
    # The role is read FIRST, because it decides whether `resolve_launcher` takes the
    # manager arm: a manager session carries no task page, so passing no `subject` would
    # resume it on the worker default.
    role, subject, role_error = rw.resume_role(session_id, cwd, fb)
    if role_error:
        return None, role_error, "", ""
    launcher, launcher_source = rw.resolve_launcher(session_id, cwd, fb, subject)
    if launcher is None:
        return None, launcher_source, "", ""
    if not rw.SAFE_LAUNCHER.match(launcher) or ".." in launcher.split("/"):
        return None, f"launcher {launcher!r} is not a plain script name or path", "", ""
    role = safe_role(role)
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
    # Belt and braces over the two field-level validations above: this line is split by
    # `cut` at the call site, so a delimiter inside any field would shift every field after
    # it. Refuse rather than emit a line whose shape lies about its contents.
    if any("\t" in field or "\n" in field for field in (launcher, source, role, chip)):
        print("a resolved field carries a tab or newline; refusing to emit", file=sys.stderr)
        return 1
    print(f"{launcher}\t{source}\t{role}\t{chip}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
