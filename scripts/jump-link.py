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
import json
import os
import re
import subprocess
import sys
import urllib.parse

DEFAULT_TOKEN_PATH = os.path.expanduser("~/.claude/secrets/jump-token")
DEFAULT_PORT = 1337
DEFAULT_HOST = "127.0.0.1"

# A hostname or IPv4 literal. Deliberately narrow: this value goes into a URL
# unencoded, so anything outside this set (a scheme, a path, userinfo, spaces)
# is a value we would rather discard than emit.
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


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

    # Validate host/port before they reach the URL. These come from the
    # environment rather than the operator, but a malformed value would produce
    # a link that looks fine and resolves nowhere, which is the failure this
    # script exists to avoid — so fall back to the defaults rather than emit it.
    host = os.environ.get("JUMP_HOST", DEFAULT_HOST)
    if not _HOST_RE.match(host or ""):
        host = DEFAULT_HOST
    try:
        port = int(os.environ.get("JUMP_PORT", DEFAULT_PORT))
        if not 1 <= port <= 65535:
            raise ValueError(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORT

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

    # ENCODE, never interpolate. The token is file-sourced, not operator-typed,
    # but it is still a value being placed into a query string: a token holding
    # `&` or `#` would otherwise inject a second parameter or truncate itself,
    # and the link would fail in a way that reads as "the server is down".
    # urlencode also keeps this correct if the token alphabet ever widens.
    query = urllib.parse.urlencode({"pane": pane, "t": token})
    link = "http://%s:%s/jump?%s" % (host, port, query)

    if label:
        title = pane_title(pane)
        print("jump: %s — %s" % (title, link) if title else "jump: %s" % link)
    else:
        print(link)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
