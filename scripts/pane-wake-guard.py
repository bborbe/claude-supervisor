#!/usr/bin/env python3
"""Decide whether an idle tab session's pane may be woken by typing into it.

`/supervisor:fleet-triage` wakes a live tab session that is idle at the prompt through
`send_agent_message` (`server/tab.mjs` `sendToPane`). That call waits for an EMPTY `❯`
composer, which refuses a selection modal's `❯ 1.` row — but a pane that is mid-turn also
shows an empty composer (`scripts/send-delivery-drill.py`), so the call alone would type
into a working session. This guard runs BEFORE the call and refuses every pane the wake
must not touch. It is code, not model judgement, so it can be pinned by fixtures.

Verdicts (one line on stdout):

  wake                     idle at an empty composer, nothing on screen forbids typing
  wake:suggestion          idle; the composer holds only Claude Code's dim next-prompt suggestion
  skip:spinner             the session is mid-turn
  skip:operator-ask        the last `👤 You:` closer on screen asks the operator for something
  skip:unclassified        the pane read back empty — never guess a target
  skip:modal-select        a selection modal (`Enter to select` begins a line)
  skip:modal-cancel        a permission / question modal (`Esc to cancel` begins a line)
  skip:option-list         a numbered option list (`❯ 1.` begins a line)
  skip:composer-not-empty  the input box holds typed (non-dim) text — possibly the operator's own
  skip:no-composer         no `❯` prompt on screen, so idleness is unproven
  skip:machine-wait        the last turn declared a machine wait (`🟡 WAITING` / `⏰ Ends:`)
  skip:sleeping            the subject is date-gated asleep (`manager-liveness.py --state`)
  skip:state-unreadable    a subject resolved but its state could not be read — fail closed
  error:<reason>           the pane could not be read

Exit status: 0 for `wake` / `wake:suggestion`, 1 for any `skip:*`, 2 for `error:*`.

Modal markers are matched ANCHORED TO THE LINE START (after leading whitespace and box
drawing), never anywhere in the line — the rule `agents/gate-relay-send.md` measured: a
bare containment test matches the marker quoted inside prose (a task page, a diff) and
refuses a pane that holds no modal. The spinner marker is matched by containment, because
a false `skip` costs one missed wake and a false `wake` types into a working session.

Usage:
    pane-wake-guard.py --pane-id 940          # reads the visible screen via wezterm
    pane-wake-guard.py --text-file pane.txt   # a captured screen (fixtures, replays)
"""
import argparse
import importlib.util
import os
import re
import subprocess
import sys

PROMPT_GLYPH = "❯"  # ❯
LEAD = " \t │┃║|"  # whitespace, NBSP and box-drawing verticals

SPINNER_CONTAINS = ("esc to interrupt",)
# A Claude Code spinner line: a glyph, a gerund ending in an ellipsis, then `(<n>s`.
SPINNER_LINE = re.compile(r"^\S\s+\w[\w ]*…\s*\(\d+[smh]")
MODAL_SELECT = "Enter to select"
MODAL_CANCEL = "Esc to cancel"
OPTION_ROW = re.compile("^" + PROMPT_GLYPH + r"\s*1\.")
MACHINE_WAIT = ("\U0001f7e1 WAITING", "⏰ Ends:")
SLEEPING = "SLEEPING"
# A subject RESOLVED but its state could not be read. Distinct from `None`, which means
# no subject resolved at all — see `verdict`'s docstring for why only this one fails closed.
STATE_UNREADABLE = "UNREADABLE"
OPERATOR_ASK = "\U0001f464 You:"  # 👤 You:

# The escape stripper and the faint parser are REUSED from the reload lever, never
# re-derived: `_visible_is_dim` parses SGR positionally, so the `2` inside a truecolor
# `38;2;r;g;b` is not read as faint — a membership test would call the operator's
# colourised typed text a "suggestion" and type into it, the one direction this guard
# exists to refuse. Claude Code draws its next-prompt SUGGESTION dim (SGR 2); plain
# `get-text` cannot tell it from typed input — measured 2026-11-08: 27 of 39 live panes
# read as a non-empty composer, and every sampled one was `\x1b[0;2m<suggestion>`.
_census_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin-version-census.py")
_spec = importlib.util.spec_from_file_location("plugin_version_census", _census_path)
_census = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_census)


def strip_ansi(text):
    return _census._plain(text)


def is_dim_only(segment):
    """True when every visible character of `segment` is drawn faint, and some is."""
    return _census._visible_is_dim(segment)


def _lines(text):
    return [ln.rstrip() for ln in strip_ansi(text).splitlines()]


def _lead_stripped(line):
    return line.lstrip(LEAD)


def verdict(text, allow_machine_wait=False, subject_state=None):
    """Return the verdict string for one captured screen. Pure — no I/O.

    `allow_machine_wait` carves out the one caller a declared machine wait is
    waiting for: a sweep-gate wake. A goal manager's resting state IS a machine wait
    (the loop cadence is the machine), so refusing it would make the gate unable to
    wake the sessions it exists to wake. An operator ask still blocks — that check
    runs after this one, so a pane that is both stays refused.

    `subject_state` is `manager-liveness.py --state`'s first field, read by the
    caller and passed in (this function stays pure). A `SLEEPING` subject is idle by
    definition — the sleep exists to produce exactly that state — so it is refused
    before the screen is even considered. A sleeping pane shows an empty composer and
    no `👤 You:` ask, which is why the screen checks cannot tell it from a lapse.

    `STATE_UNREADABLE` means a subject RESOLVED but its state could not be read — a
    non-zero exit, an unparsable line, a timeout. That is refused too, and it is the
    one place this guard fails closed: a missed wake costs a manager nothing (its gate
    re-arms it) while a spurious wake types into a pane whose session then closes it
    for good. Measured 2026-11-13: three `DEFAULT_MESSAGE` wakes landed inside a
    `SLEEPING` window because the failed read fell through to the screen verdict.

    `None` means NO subject resolved — a hand-opened tab, or a session dispatched
    before the record existed. That is not a state either way, so the screen verdict
    decides, exactly as before this parameter existed.
    """
    if subject_state == SLEEPING:
        return "skip:sleeping"
    if subject_state == STATE_UNREADABLE:
        return "skip:state-unreadable"
    if text is None:
        return "error:unreadable"
    if not text.strip():
        return "skip:unclassified"
    lines = _lines(text)

    for ln in lines:
        low = ln.lower()
        if any(m in low for m in SPINNER_CONTAINS) or SPINNER_LINE.match(_lead_stripped(ln)):
            return "skip:spinner"

    for ln in lines:
        s = _lead_stripped(ln)
        if s.startswith(MODAL_SELECT):
            return "skip:modal-select"
        if s.startswith(MODAL_CANCEL):
            return "skip:modal-cancel"
        if OPTION_ROW.match(s):
            return "skip:option-list"

    raw_lines = [ln.rstrip() for ln in text.splitlines()]
    prompts = [ln for ln in raw_lines if _lead_stripped(strip_ansi(ln)).startswith(PROMPT_GLYPH)]
    if not prompts:
        return "skip:no-composer"
    raw = prompts[-1]
    composer = _lead_stripped(strip_ansi(raw))[len(PROMPT_GLYPH):]
    if composer.strip(" \t "):
        after_glyph = raw[raw.index(PROMPT_GLYPH) + len(PROMPT_GLYPH):]
        if not is_dim_only(after_glyph):
            return "skip:composer-not-empty"
        suggestion = True
    else:
        suggestion = False

    if any(m in ln for ln in lines for m in MACHINE_WAIT) and not allow_machine_wait:
        return "skip:machine-wait"

    # The last closer on screen asks the operator for something: the pane is waiting on a
    # human, and a wake would either answer that question or bury it.
    asks = [ln for ln in lines if OPERATOR_ASK in ln]
    if asks:
        verb = asks[-1].split(OPERATOR_ASK, 1)[1].strip().lower()
        if not verb.startswith("nothing"):
            return "skip:operator-ask"

    # A dim suggestion is not text anyone typed. The reload lever measured that a send into
    # such a composer on a long-idle pane lands (`plugin-version-census.py` `pane_is_ready`,
    # 2026-10-07), so it is wakeable — reported apart so a run shows which box it typed over.
    return "wake:suggestion" if suggestion else "wake"


def read_pane(pane_id):
    """The visible screen — `get-text`'s default scope, never a fixed tail."""
    try:
        res = subprocess.run(
            ["wezterm", "cli", "get-text", "--escapes", "--pane-id", str(pane_id)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pane-id")
    src.add_argument("--text-file")
    ap.add_argument("--allow-machine-wait", action="store_true",
                    help="a sweep-gate wake may pass a declared machine wait "
                         "(an operator ask still blocks)")
    ap.add_argument("--subject-state", default=None,
                    help="`manager-liveness.py --state`'s first field for the subject; "
                         "`SLEEPING` refuses the wake")
    args = ap.parse_args(argv)

    if args.pane_id is not None:
        text = read_pane(args.pane_id)
    else:
        try:
            with open(args.text_file, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = None

    v = verdict(text, allow_machine_wait=args.allow_machine_wait,
                subject_state=args.subject_state)
    print(v)
    if v.startswith("wake"):
        return 0
    return 1 if v.startswith("skip:") else 2


if __name__ == "__main__":
    sys.exit(main())
