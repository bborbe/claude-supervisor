#!/usr/bin/env python3
"""Select the topic pages the fleet keep-alive leg's § 1 must look at.

The leg selected on `claude_session_id` — the exact field the stood-down handover clears —
so a page it had just handed over dropped out of the population and no replacement manager
was ever opened for it. This module selects on the shared resolver's verdict instead
(`scripts/owner.py`, the one ownership definition) and never re-derives ownership here.

**Population: every topic page whose verdict is not `loop-stopped`.** A deliberate
stand-down is contractually never re-opened (`docs/session-tiers.md` § Keep-alive), and
every other state — including `no-session`, which is what a handed-over page reads — is a
candidate the action ladder decides. `claude_session_id` is never consulted: a page that
carries none is exactly the page this leg exists to recover.

Output matches the shape `agents/fleet-keepalive-actor.md` § 1 renders: one line per
candidate, `name<TAB>` followed by the ladder's first two lines, each tab-terminated.
"""

import argparse
import importlib.util
import os
import pathlib
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: The one reason that removes a page from the population. Everything else is the ladder's
#: to decide — this module answers "should the leg look at it", never "what should it do".
REFUSED_REASON = "loop-stopped"


def load_owner():
    """Import `scripts/owner.py` — the ONE ownership definition. Do not restate it."""
    spec = importlib.util.spec_from_file_location("owner", os.path.join(HERE, "owner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_ladder(script, vault, subject, directory):
    """The ladder's first two lines for one subject, newline-joined (never re-derived)."""
    out = subprocess.run(
        [sys.executable or "python3", script, "--vault", vault, "--subject", subject,
         "--branch", "topic", "--dir", directory],
        stdout=subprocess.PIPE, text=True).stdout
    return "\n".join(out.split("\n")[:2])


def candidates(vault, directory, script, of_page=None, ladder=None):
    """`[(name, ladder_head)]` — every topic page the leg must look at, in name order.

    `of_page` and `ladder` are injected by the tests; production passes neither.
    """
    owner = load_owner()
    of_page = of_page or owner.of_page
    ladder = ladder or run_ladder
    rows = []
    for path in sorted((pathlib.Path(vault) / directory).glob("*.md")):
        if path.name.startswith("."):
            continue                       # bash `*.md` never matches a dotfile
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue                       # an unreadable file fails `grep -q` too
        verdict = of_page(owner.frontmatter(text), path.stem, "topic", vault)
        if verdict.reason == REFUSED_REASON:
            continue
        rows.append((path.stem, ladder(script, vault, path.stem, directory)))
    return rows


def resolve_directory(vault, explicit):
    """The topics directory — the vault's own config, never an assumed `23 Topics`."""
    if explicit:
        return explicit, ""
    out = subprocess.run(
        [sys.executable or "python3", os.path.join(HERE, "resolve-folders.py"),
         "--vault", vault, "--require", "topics_dir"],
        stdout=subprocess.PIPE, text=True).stdout.strip()
    if not out or out.startswith("UNKNOWN"):
        return "", out or "resolve-folders.py printed nothing"
    return out.split("|")[0], ""


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--vault", required=True)
    parser.add_argument("--dir", default="", help="topics directory (default: the vault's config)")
    args = parser.parse_args(argv)

    directory, problem = resolve_directory(args.vault, args.dir)
    if problem:
        print("folders: " + problem, file=sys.stderr)
        return 2

    for name, head in candidates(args.vault, directory, os.path.join(HERE, "topic-keepalive.py")):
        print(name + "\t" + head.replace("\n", "\t"), end="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
