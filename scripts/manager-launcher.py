#!/usr/bin/env python3
"""`manager-launcher.py` — print the launcher a MANAGER session opens on, and why.

Usage: manager-launcher.py <launcher> [<topic or goal page>] [--default-source manager-default|vault]
Prints `<launcher path>\t<source>`, source one of `subject` / `manager-default` /
`claude-sibling` / `vault`; exits 1 with a reason on stderr when the result is unsafe or
missing.

Rule (single home: `docs/fleet-surface.md` § Which launcher a manager opens on): the subject
page's `manager_launcher:`, else the launcher handed in. Mirrors `resolveManagerLauncher` in
`server/spawn-cwd.mjs`.

⚠️ **Provenance is passed in, never inferred from the filename.** The caller knows whether the
value it read came from the vault's configured `manager_launcher` or from `claude_script`, and
says so with `--default-source`; this script only reports it. An earlier version guessed from a
`-claude` suffix, which diverged from the JS resolver in two real cases — a configured manager
launcher that does not end in `-claude` (`cc-private-glm`) was misreported, and an older
vault-cli whose `claude_script` already ends in `-claude` was reported as a configured default
it never named.

⚠️ The `-claude` sibling survives only as the COMPATIBILITY rung for a vault-cli older than the
`manager_launcher` key: the caller passes `--default-source vault` with `claude_script`, and the
sibling is derived here, reported as `claude-sibling`. The handed-in value is shape-checked in
every branch — it is executed by a shell, and `resolveManagerLauncher` checks the configured
default for the same reason.
"""
import argparse
import os
import re
import sys

SAFE = re.compile(r"^[A-Za-z0-9._/-]+$")


def field(path, key):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    fm = text[3:end] if end != -1 else ""
    m = re.search(rf"^{re.escape(key)}:[ \t]*['\"]?([^'\"\n]*?)['\"]?[ \t]*$", fm, re.M)
    return m.group(1).strip() if m else ""


def sibling(vault_launcher):
    head, base = os.path.split(vault_launcher)
    base = re.sub(r"-deepseek$", "", base)
    return vault_launcher if base.endswith("-claude") else os.path.join(head, base + "-claude")


def safe(value):
    return bool(SAFE.match(value)) and ".." not in value.split("/")


def resolve(launcher, page=None, default_source="vault"):
    own = field(page, "manager_launcher") if page else ""
    if own:
        if not safe(own):
            return None, f"manager_launcher {own!r} is not a plain script name or path"
        name, source = (own if "/" in own else os.path.join(os.path.dirname(launcher), own)), "subject"
    else:
        if not safe(launcher):
            return None, f"manager launcher {launcher!r} is not a plain script name or path"
        if default_source == "manager-default":
            name, source = launcher, "manager-default"
        else:
            sib = os.path.expanduser(sibling(launcher))
            name, source = (sib, "claude-sibling") if os.path.isfile(sib) else (launcher, "vault")
    name = os.path.expanduser(name)
    if not os.path.isfile(name):
        return None, f"launcher {name} ({source}) does not exist"
    return name, source


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("launcher", nargs="?")
    ap.add_argument("page", nargs="?")
    ap.add_argument("--default-source", choices=("manager-default", "vault"), default="vault")
    try:
        args = ap.parse_args(argv[1:])
    except SystemExit:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    if not args.launcher:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    path, source = resolve(args.launcher, args.page, args.default_source)
    if path is None:
        print(source, file=sys.stderr)
        return 1
    print(f"{path}\t{source}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
