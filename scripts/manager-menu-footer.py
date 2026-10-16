#!/usr/bin/env python3
"""Measure SC2's card clause: does any MANAGER pane render a modal instead of a card?

The rule is *"an operator-only decision reaches the operator as an attention-board card
carrying a `--dedup-key`, never as a menu in the manager's pane"*. The guard
(`scripts/manager-menu-guard.py`) enforces the *menu* half by denying manager-owned
options; nothing measured whether a manager pane renders a **modal** at all. This is that
instrument: it reads every manager pane and reports how many carry a modal footer.

⚠️ **Strict tokens only, anchored at the START of a line.** The footer is one of three
literal markers, and the line-start anchor is not a stylistic choice — a bare containment
match reports a gate on any pane that merely *discusses* one. Measured 2026-10-03:
`grep -cE 'Enter to select|Esc to cancel'` returned 1 on a pane holding no modal at all,
the hit being a task page's own sentence rendered in the viewport.

⚠️ **NEVER match the bare word `AskUserQuestion`.** It appears in the statusline as the
name of the tool in flight, so it is present on panes holding no modal. Measured
2026-10-16 at 15:01:28: exactly that pattern made an observer report a `FOOTER` in a pane
whose guard record held no deny. `--self-test` pins both directions: a fixture that
renders a footer must be reported, and a fixture carrying the bare tool name with no
footer must not be.

⚠️ **A failed pane read is `None`, never `0`.** This follows `docs/pane-reads.md`: an
empty result is a real answer ("the transport replied, and the answer was nothing") while
a failed read is the absence of an answer, and collapsing the second into the first is
the silent defect the whole class shares. A sweep that cannot read a pane reports it as
unread, never as un-gated.

Usage:
    manager-menu-footer.py                 # sweep every manager pane
    manager-menu-footer.py --fixture FILE  # the positive control: read a FILE as a pane
    manager-menu-footer.py --self-test     # both controls, no live reads
    manager-menu-footer.py --json

Exit codes: 0 the sweep ran · 1 the transport failed (a pane list or a pane read that
could not be taken) · 2 a fixture or argument was unreadable.
"""
import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(module_name, filename):
    """Import a sibling script by path. They are shipped side by side, not installed."""
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The guard owns the manager rule. Imported rather than reimplemented: a second copy of
# "what is a manager" would drift, and the drift would be silent — this instrument would
# sweep a set the guard never denies.
guard = _load("manager_menu_guard", "manager-menu-guard.py")

# ⚠️ **The strict token set, and every member is a literal footer fragment.** `Enter to
# select` and `Esc to cancel` are the two `who-needs-my-help.py` anchors; `Tab to toggle`
# is named by this task. `Tab to amend` is included as well because the Manager Session
# runbook records it as the discriminator INSIDE the permission-modal line
# (`Esc to cancel · Tab to amend`) — omitting it would make this instrument blind to that
# whole modal shape, which is the one class it exists to see.
FOOTER_TOKENS = ("Enter to select", "Esc to cancel", "Tab to toggle", "Tab to amend")

# ⚠️ The token that must NOT be one of them. Kept as a named constant so `--self-test`
# can pin the negative direction against the exact string that produced the false report.
NEVER_A_TOKEN = "AskUserQuestion"

# The statusline is `⏵⏵ auto mode on …` and carries the in-flight tool name. Stripping the
# decoration is what lets a pane title be matched to a session name (the join
# `who-needs-me.py` uses: name the session from its registry entry, then match that name
# against the live panes' glyph-stripped titles).
_GLYPHS = re.compile(r"[^\w\s-]")


def strip_glyphs(text):
    """The text without decoration, collapsed — the join `who-needs-me.py` uses."""
    return " ".join(_GLYPHS.sub(" ", text or "").split()).strip().lower()


def wezterm_panes():
    """`pane id -> wezterm record`, or `None` when the query failed.

    `None` is a distinct answer from `{}`: a failed `wezterm cli list` cannot prove a pane
    is gone, so no caller may read it as "no panes exist". Same convention as the
    sibling's own `wezterm_panes()`, and the reason this file returns `None` rather than
    an empty map on every transport failure.
    """
    try:
        result = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                                capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        return {str(p["pane_id"]): p for p in json.loads(result.stdout)}
    except (ValueError, KeyError, TypeError):
        return None


def pane_lines(pane_id):
    """The pane's VISIBLE screen, as lines; `None` when the read failed.

    `wezterm cli get-text` defaults to `--start-line 0`, the first line of the terminal
    screen, so the read never reaches scrollback — which is what makes the line-start
    anchor meaningful at all. A buffer-wide read would match dead scrollback and report a
    modal on a pane that is working.
    """
    try:
        result = subprocess.run(
            ["wezterm", "cli", "get-text", "--pane-id", str(pane_id)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.splitlines()


def footer_in_lines(lines):
    """Whether any line BEGINS with a strict footer token.

    ⚠️ `startswith`, never `in`. The whole instrument is this one word: a containment
    test is what reported a footer on a pane that merely rendered the phrase in prose.
    """
    for line in lines or ():
        for token in FOOTER_TOKENS:
            if line.startswith(token):
                return True
    return False


def scan_pane(pane_id):
    """`True` footer / `False` no footer / `None` unreadable — three answers, not two."""
    lines = pane_lines(pane_id)
    if lines is None:
        return None
    return footer_in_lines(lines)


def manager_panes():
    """`[(session_id, name, pane_id, pane_record)]` for every live MANAGER session.

    ⚠️ Returns `None` when the pane list is unreadable — the caller must refuse rather
    than sweep an empty set, because an empty sweep and a broken mux socket are the same
    render and only one of them is a clean bill.
    """
    panes = wezterm_panes()
    if panes is None:
        return None
    topics, goals = guard.vault_page_names()
    by_title = {}
    for pane_id, record in panes.items():
        title = strip_glyphs(record.get("title"))
        if title:
            by_title.setdefault(title, pane_id)

    found = []
    for path in sorted(glob.glob(os.path.join(guard.sessions_dir(), "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError):
            continue
        if guard.manager_arm(entry, topics, goals) is None:
            continue
        session_id = entry.get("sessionId")
        name = entry.get("name") or ""
        pane_id = by_title.get(strip_glyphs(name))
        if pane_id is None:
            continue
        found.append((session_id, name, pane_id, panes[pane_id]))
    return found


def sweep():
    """`(rows, unreadable_count)` — one row per manager pane with a readable screen."""
    panes = manager_panes()
    if panes is None:
        return None, None
    rows, unreadable = [], 0
    for session_id, name, pane_id, _ in panes:
        verdict = scan_pane(pane_id)
        if verdict is None:
            unreadable += 1
        rows.append({"session_id": session_id, "name": name, "pane_id": pane_id,
                     "footer": verdict})
    return rows, unreadable


def self_test():
    """Both controls. Returns `(ok, lines)`.

    ⚠️ **A probe that has never been red is not known to be capable of red**, so the
    positive control is asserted first and the negative one is not optional: it pins the
    exact string that produced the 2026-10-16 false report.
    """
    out, ok = [], True

    positive = ["some ordinary pane text", "Enter to select · ↑/↓ to navigate · Esc to cancel"]
    got = footer_in_lines(positive)
    out.append(f"  positive control (a rendered footer)      -> {got}  (must be True)")
    ok = ok and got is True

    negative = ["AskUserQuestion", "⏵⏵ auto mode on · AskUserQuestion · esc to interrupt"]
    got = footer_in_lines(negative)
    out.append(f"  negative control (bare tool name only)    -> {got}  (must be False)")
    ok = ok and got is False

    # ⚠️ The containment trap, pinned directly: the token present MID-line is content,
    # not a render. This is the 2026-10-03 measurement, in one line.
    mid_line = ["the runbook says Enter to select is the question-modal anchor"]
    got = footer_in_lines(mid_line)
    out.append(f"  mid-line token (prose about a modal)      -> {got}  (must be False)")
    ok = ok and got is False

    empty = footer_in_lines([])
    out.append(f"  empty screen                              -> {empty}  (must be False)")
    ok = ok and empty is False
    return ok, out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", help="read FILE as a pane screen instead of the live fleet")
    parser.add_argument("--self-test", action="store_true", help="run both controls and exit")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        ok, lines = self_test()
        print("manager-menu-footer self-test:")
        print("\n".join(lines))
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1

    if args.fixture:
        try:
            with open(args.fixture, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError as exc:
            print(f"unreadable fixture {args.fixture}: {exc}", file=sys.stderr)
            return 2
        verdict = footer_in_lines(lines)
        if args.json:
            print(json.dumps({"fixture": args.fixture, "footer": verdict}))
        else:
            print(f"fixture {args.fixture}: footer={'yes' if verdict else 'no'}")
        return 0

    rows, unreadable = sweep()
    if rows is None:
        print("WezTerm pane list unreadable — the mux socket is not answering, so no "
              "manager pane can be proven footer-free. Refusing to print a sweep that "
              "would read as a clean fleet.", file=sys.stderr)
        return 1

    with_footer = [r for r in rows if r["footer"] is True]
    if args.json:
        print(json.dumps({"manager_panes": len(rows), "with_footer": len(with_footer),
                          "unreadable": unreadable, "rows": rows}, indent=2))
        return 0

    print(f"manager panes read: {len(rows)}")
    print(f"  modal footers:    {len(with_footer)}   (required: 0)")
    if unreadable:
        print(f"  ⚠️ unreadable:    {unreadable}   — neither verdict is complete while a "
              f"pane could not be read")
    for row in with_footer:
        print(f"    FOOTER  {row['name']} (pane {row['pane_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
