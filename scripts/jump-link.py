#!/usr/bin/env python3
"""Emit a clickable jump target for a pane id.

    jump-link.py <pane-id>

Prints, on one line, the best available way for the operator to reach that pane:

  * a clickable `http://127.0.0.1:1337/jump?pane=<N>&t=<token>` URL when the
    local fleet-jump server is configured, or
  * the literal `/supervisor:jump <N>` fallback when it is not.

ALWAYS ONE LINE, ALWAYS USABLE. The manager commands print this into a panel the
operator acts on, so a value that cannot be followed is worse than the old
command: a dead link reads as a working one and the operator clicks nothing.
Every failure path therefore degrades to the fallback rather than printing a
broken URL.

WHY THE TOKEN IS READ HERE AND NOT IN THE COMMAND. The token must never be
committed to a repo, so it cannot live in `commands/*.md`. This script reads it
from `~/.claude/secrets/jump-token` (0600) at emit time. The token is not a
secret from the operator — it is on their screen by design, inside every link —
only from a web page, which cannot read it cross-origin. That is what defeats
the CSRF case where a visited page fires `<img src=".../jump?pane=X">`.

WHY IT DOES NOT VALIDATE THE PANE. The server validates the pane id against the
live WezTerm list when the link is followed, and refuses a stale one with a
readable page. Validating here as well would put a `wezterm cli list` call on
every manager sweep to guard a case the server already covers, and would still
race the click. The pane id handed over is the one the sweep just resolved.

`--label` prefixes the pane's title (`jump: <title> — <link>`), matching how the
jump list reads today. It costs one extra `wezterm cli list`; use it for the
primary handover line, not for a column repeated per row.
"""
import os
import subprocess
import sys

DEFAULT_TOKEN_PATH = os.path.expanduser("~/.claude/secrets/jump-token")
DEFAULT_PORT = 1337
DEFAULT_HOST = "127.0.0.1"
DEFAULT_JUMP_PY = os.path.expanduser(
    "~/.claude/plugins/marketplaces/claude-supervisor/scripts/jump.py"
)


def clean_title(title):
    """Strip the leading status glyph wezterm shows before the session name."""
    t = (title or "").strip()
    for glyph in ("✳", "◐", "◑", "⏺", "✻", "✽", "·", "●", "○"):
        if t.startswith(glyph):
            t = t[len(glyph):].strip()
            break
    return t or "(untitled)"


def pane_title(pane_id):
    """The pane's title, or None. Never raises — a label is a nicety, not a gate."""
    try:
        raw = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                             capture_output=True, text=True, timeout=5).stdout
        import json
        for p in json.loads(raw):
            if str(p.get("pane_id")) == str(pane_id):
                return clean_title(p.get("title"))
    except Exception:
        return None
    return None


def main(argv):
    label = "--label" in argv
    args = [a for a in argv if a != "--label"]

    if len(args) != 1:
        sys.stderr.write("usage: jump-link.py <pane-id> [--label]\n")
        return 2

    pane = args[0]
    if not pane.isdigit():
        sys.stderr.write("jump-link: pane id must be an integer\n")
        return 2

    fallback = "/supervisor:jump %s" % pane

    token_path = os.environ.get("JUMP_TOKEN_PATH", DEFAULT_TOKEN_PATH)
    host = os.environ.get("JUMP_HOST", DEFAULT_HOST)
    port = os.environ.get("JUMP_PORT", str(DEFAULT_PORT))

    try:
        with open(token_path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        token = ""

    if not token:
        # No server configured (or an unreadable token file) — hand over the
        # command that has always worked rather than a link that cannot.
        print(fallback)
        return 0

    link = "http://%s:%s/jump?pane=%s&t=%s" % (host, port, pane, token)

    if label:
        title = pane_title(pane)
        print("jump: %s — %s" % (title, link) if title else "jump: %s" % link)
    else:
        print(link)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
