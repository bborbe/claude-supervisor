#!/usr/bin/env python3
"""`manager-launcher.py` — print the launcher a MANAGER session opens on, and why.

Usage: manager-launcher.py <vault manager launcher> [<topic or goal page>]
Prints `<launcher path>\t<source>`, source one of `subject` / `manager-default` /
`claude-sibling` / `vault`; exits 1 with a reason on stderr when the result is unsafe or
missing.

Rule (single home: `docs/fleet-surface.md` § Which launcher a manager opens on): the subject
page's `manager_launcher:`, else the launcher handed in — which the caller read from the vault's
configured `manager_launcher`, reported as `manager-default`. Mirrors `resolveManagerLauncher`
in `server/spawn-cwd.mjs`.

⚠️ The `-claude` sibling survives only as the COMPATIBILITY rung: a vault-cli older than the
`manager_launcher` key emits none, so the caller hands in `claude_script` instead and this
script derives the sibling — reported as `claude-sibling`, never as `manager-default`. The
distinction is deliberate: "the config decided this" and "a naming convention decided this"
must not look alike in a spawn response. A value that already ends in `-claude` is treated as
the configured manager default and passed through unchanged.
"""
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


def resolve(vault_launcher, page=None):
    own = field(page, "manager_launcher") if page else ""
    if own:
        if not SAFE.match(own) or ".." in own.split("/"):
            return None, f"manager_launcher {own!r} is not a plain script name or path"
        name, source = (own if "/" in own else os.path.join(os.path.dirname(vault_launcher), own)), "subject"
    elif sibling(vault_launcher) == vault_launcher:
        # Already a manager launcher (its base ends in `-claude`): the caller read it from the
        # vault's configured `manager_launcher`. Pass it through and say where it came from.
        name, source = vault_launcher, "manager-default"
    else:
        sib = os.path.expanduser(sibling(vault_launcher))
        name, source = (sib, "claude-sibling") if os.path.isfile(sib) else (vault_launcher, "vault")
    name = os.path.expanduser(name)
    if not os.path.isfile(name):
        return None, f"launcher {name} ({source}) does not exist"
    return name, source


def main(argv):
    if len(argv) not in (2, 3) or not argv[1]:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    path, source = resolve(argv[1], argv[2] if len(argv) == 3 else None)
    if path is None:
        print(source, file=sys.stderr)
        return 1
    print(f"{path}\t{source}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
