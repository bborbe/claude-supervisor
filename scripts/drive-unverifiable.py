#!/usr/bin/env python3
"""Silence is investigated: a session unverifiable for 2 consecutive rounds is acted on.

`agents/fleet-drive.md` classifies a row `unverifiable` when a probe could not run, no
task resolved, or no `CLOSERS` row exists -- and its verdict table then says
"observation only, never nudged". That is correct for one round and wrong for a run of
them: the row leaves the digest's candidate set, `drive-retained.py` keeps re-probing it
from the task file, and nothing ever reads the session itself. Measured 2026-10-21: one
drive leg marked 99 of 101 candidates unverifiable and did nothing.

So a session unverifiable for **two consecutive rounds** has its pane read, and gets one
of two actions:

  * **nudge** -- the pane renders a closer line, so the line is quoted back to the session
    verbatim. Read-only context: it authorises nothing, and it says so.
  * **escalation** -- the pane resolves no closer (gone, unreadable, or rendering none),
    so the manager cannot see what the session waits on and an attention card says so.

WHY THE PANE READ IS HERE AND NOT IN THE AGENT. `agents/fleet-drive.md` forbids the agent
looking for a pane itself, and the one place it may not look is exactly where the answer
lives. A script is the sanctioned route: it holds `python3`, the agent does not render a
link, and -- the load-bearing part -- a markdown file has no callable seam, so the
acceptance replay could not discriminate a build that reads the pane from one that
reprints a stored string. `decide()` is that seam; the test stubs it and counts.

WHY AT LEAST TWO, AND WHY IT STILL FIRES ONLY ONCE. The trigger is `streak >= 2`; the
thing that stops a repeat is the ledger's `unverifiable_action` marker, not the count. An
equality test on `== 2` reads as the tighter rule and is the wrong one: a row held back by
the per-round cap keeps advancing its streak, so the round that finally admits it sees 3,
never 2, and it would be acted on never. The marker also carries the ordinary case — a
session silent for six rounds gets one action, not five, which is the nag the ledger's
suppression rule exists to prevent. A verdict that is not `unverifiable` clears both the
streak and the marker, so a session that recovers and later goes silent is acted on again.
The streak is consecutive only when its round tag equals the ledger's own `round`, so a
gap resets the count without anything having to notice it.

WHY AT MOST THREE ROWS A ROUND. The threshold bounds how LONG a session waits, not how
MANY act at once, and the two are different hazards. Measured on the first `v0.201.0`
round (2026-10-22T13:01Z): 112 sessions reached streak 1 in the same round, because the
field was new and every retained row started from nothing -- so the next round would have
opened up to 112 pane reads and emitted up to 112 nudges or attention cards in one tick.
Most were rows with no task file or long-dead sessions, whose only possible action is a
card saying "no pane resolves": nothing the operator can DO, which is the test every
card has to pass. The cap orders by LONGEST streak first, then session id, so a backlog
drains oldest-first and nothing starves; a held-back row keeps its streak and its empty
action marker, so it stays eligible, and `render()` names the rows held back so a backlog
is visible rather than silently dropped. A cap that reported nothing would be
indistinguishable from a threshold that stopped firing.

WHAT THIS DOES NOT DO. It never decides the row's verdict -- that is the agent's, from
the digest. It reads the ledger and this round's entries, and writes only the entries it
is handed.

Subcommands: handle
"""
import argparse
import json
import os
import subprocess
import sys

#: Exit code for a ledger or payload this script will not act on. Distinct from 1 (a
#: crash) so a caller can tell "refused" from "broke" -- a refused round acted on nothing.
REFUSED = 2

#: The consecutive-unverifiable count at which a row is acted on. Exactly this value, not
#: `>=`: see the module docstring.
THRESHOLD = 2

#: How many rows may be acted on in ONE round. The threshold alone is unbounded, and that
#: is not a theoretical worry: the first round on `v0.201.0` put 112 sessions at streak 1
#: at once, so the next round would have emitted up to 112 nudges or attention cards in a
#: single tick. Most were rows with no task file or long-dead sessions, whose only possible
#: action is a card saying "no pane resolves" -- pure noise on a board the operator reads
#: for things they must DO. Same shape, same remedy as `next-focus.py`'s "at most three per
#: round", adopted after 38 open rows once put 38 cards up at once.
MAX_ACTIONS = 3

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_SCRIPT = os.path.join(HERE, "drive-ledger.py")
PANE_SCRIPT = os.path.join(HERE, "who-needs-me.py")
LINK_SCRIPT = os.path.join(HERE, "jump-link.py")

#: A closer line begins the line it renders on. Matched with `startswith` after stripping
#: leading indentation -- never by containment, because a pane discussing a closer (a task
#: page, a session quoting another) would otherwise match. Same rule, same reason, as
#: `agents/gate-relay-read.md` step 3.
CLOSER_MARKERS = ("👤 You:", "pick —", "approve:", "review:", "you run:", "later (on")

#: The command a rendered line names as the pane read. A constant, not the invoking argv,
#: so the payload records the same command every round and a reader can re-run it verbatim.
PANE_COMMAND = "wezterm cli get-text --pane-id"


def read_ledger(script=None):
    """(ledger, None) or (None, reason). Never treats a refusal as an empty ledger.

    `drive-ledger.py read` normalises every entry through `canonical()`, so a value read
    back from here is already a clean non-negative int and this file needs no second
    coercion rule -- the ledger owns the storage and the normalisation together.
    """
    out = subprocess.run(
        [sys.executable, str(script or LEDGER_SCRIPT), "read"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None, (out.stderr or "").strip() or "drive-ledger.py read refused"
    try:
        return json.loads(out.stdout), None
    except ValueError as exc:
        return None, "drive-ledger.py read emitted unparseable JSON: %s" % exc


def resolve_pane(sid, script=None):
    """The WezTerm pane id for a session, or "" when none resolves.

    Delegates to `who-needs-me.py --pane-for`, the plugin's one session→pane resolver: it
    resolves a prefix, refuses an ambiguous one rather than guessing, and reads the
    registry at call time. A second resolver here would drift from the sweep's.
    """
    out = subprocess.run(
        [sys.executable, str(script or PANE_SCRIPT), "--pane-for", sid],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return ""
    lines = [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def pane_text(pane):
    """The pane's visible screen, or None when it cannot be read.

    `None` is not an empty screen. An unreadable pane and a pane rendering no closer are
    different states -- the first says the read failed, the second says the session
    showed nothing -- and folding them together would let a broken read render as a
    session with nothing to say. Same three-state rule as `scripts/live-workers.py`.
    """
    out = subprocess.run(
        ["wezterm", "cli", "get-text", "--pane-id", str(pane)],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    return out.stdout


def render_link(pane, script=None):
    """The clickable jump target for a pane, or "" when none can be rendered.

    This is the one place the plugin renders a link with a script rather than by hand --
    `agents/fleet-drive.md` forbids the AGENT rendering one because it holds no python3,
    and `jump-link.py` is the instrument that does. Every failure path there already
    degrades to the `/supervisor:jump <N>` fallback, so "" here means the pane id itself
    was empty, not that the link server is down.
    """
    if not pane:
        return ""
    out = subprocess.run(
        [sys.executable, str(script or LINK_SCRIPT), str(pane)],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return ""
    lines = [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def last_closer(text):
    """The last closer line the pane renders, or "" when it renders none.

    Last, not first: a pane holds prior conversation, and the question a session is
    actually waiting on is the one at the bottom. Line-start matching, never containment
    -- see CLOSER_MARKERS.
    """
    found = ""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(CLOSER_MARKERS):
            found = stripped
    return found


def advance(entry, prior, ledger_round, round_tag):
    """(streak, round) for one entry after this round.

    A verdict that is not `unverifiable` RESETS both fields, and that is load-bearing
    rather than tidy: it is what makes "unverifiable in round 1, verified in round 2"
    start a fresh run at 1 the next time the session goes silent, instead of continuing
    an old count straight to an action.
    """
    if (entry.get("verdict") or "") != "unverifiable":
        return 0, ""
    if (prior.get("unverifiable_round") or "") == ledger_round and ledger_round:
        return (prior.get("unverifiable_streak") or 0) + 1, round_tag
    return 1, round_tag


def decide(sid, entry, pane_for_fn, pane_text_fn, link_fn):
    """One row's action at the threshold. This is the seam the test stubs.

    Returns {"action", "pane", "link", "closer", "pane_line", "why", "text"}. `action` is
    `nudge` when the pane renders a closer line and `escalation` otherwise -- including
    when the pane is gone or unreadable, because those are the cases where the manager
    most needs to know the session could not be read at all.
    """
    name = entry.get("name") or "?"
    task_file = (entry.get("task_file") or "").strip()
    probe = entry.get("probe") or ""
    pane = pane_for_fn(sid)
    if not pane:
        return {
            "action": "escalation",
            "pane": "",
            "link": "",
            "closer": "",
            "pane_line": "%s → no pane resolved" % PANE_COMMAND,
            "why": "no pane resolves for the session",
            "text": escalation_text(name, sid, task_file, probe, "no pane resolves"),
        }
    text = pane_text_fn(pane)
    if text is None:
        return {
            "action": "escalation",
            "pane": pane,
            "link": link_fn(pane),
            "closer": "",
            "pane_line": "%s %s → unreadable" % (PANE_COMMAND, pane),
            "why": "the pane exists but could not be read",
            "text": escalation_text(
                name, sid, task_file, probe, "pane %s unreadable" % pane
            ),
        }
    closer = last_closer(text)
    if not closer:
        return {
            "action": "escalation",
            "pane": pane,
            "link": link_fn(pane),
            "closer": "",
            "pane_line": "%s %s → read, no closer line" % (PANE_COMMAND, pane),
            "why": "the pane renders no closer line",
            "text": escalation_text(
                name, sid, task_file, probe, "pane %s renders no closer line" % pane
            ),
        }
    return {
        "action": "nudge",
        "pane": pane,
        "link": link_fn(pane),
        "closer": closer,
        "pane_line": "%s %s → read" % (PANE_COMMAND, pane),
        "why": "",
        "text": nudge_text(closer),
    }


def nudge_text(closer):
    """The nudge, quoting the pane's own closer line verbatim.

    Read-only context, and it says so in its own words: it authorises nothing, it is not
    a course correction, and it does not clear the closer it quotes. A closer that is
    still awaiting the operator is still awaiting the operator -- the drive leg has no
    answer to give and must not look like one.
    """
    return (
        "Read-only context from the fleet drive leg — not a course correction, not a green "
        "light past any gate, and nothing was authorised on your behalf. Your session has "
        "been unverifiable for %d consecutive rounds, so your pane was read this round. Its "
        "last closer line reads: %s — If that line is still awaiting the operator, it still "
        "is. If it is stale, continue on your own next unchecked subtask under your own "
        "execution rules." % (THRESHOLD, closer)
    )


def escalation_text(name, sid, task_file, probe, why):
    """The escalation card's payload: the session, why, and what the read returned."""
    return (
        "Unverifiable for %d consecutive rounds: %s [%s] — %s. Task: %s. Probe: %s. No "
        "nudge was sent because the session's own state could not be read from its pane, "
        "so a manager needs to look."
        % (
            THRESHOLD,
            name,
            (sid or "")[:8],
            why,
            task_file or "no task file",
            probe or "no probe recorded",
        )
    )


def plan(entries, ledger, round_tag, pane_for_fn, pane_text_fn, link_fn, max_actions=MAX_ACTIONS):
    """Advance every entry's streak and act on at most `max_actions` rows at the threshold.

    Pure over its inputs and its three injected seams, so the acceptance replay drives it
    through two rounds against a tmp ledger without a WezTerm or a session registry. It
    mutates the entries it is given (they ARE the merge payload) and returns
    `(acted, held)`: the rows acted on, and the rows that reached the threshold but were
    held back by the cap this round.

    ⚠️ **The cap is applied AFTER every streak is advanced, never before.** A held-back
    row must still have its `unverifiable_streak` written this round, or its count resets
    and it can never reach the threshold again -- the backlog would silently empty itself.
    Ordering is longest streak first so a backlog drains oldest-first; the session id
    tie-break keeps two runs of the same ledger identical.
    """
    prior = {}
    for entry in ledger.get("entries") or []:
        sid = entry.get("session_id")
        if sid:
            prior[sid] = entry
    ledger_round = ledger.get("round") or ""
    candidates = []
    for entry in entries:
        sid = entry.get("session_id") or ""
        before = prior.get(sid) or {}
        streak, streak_round = advance(entry, before, ledger_round, round_tag)
        entry["unverifiable_streak"] = streak
        entry["unverifiable_round"] = streak_round
        # A verdict that is not `unverifiable` clears the action marker with the streak, so
        # a session that recovers and later goes silent again can be acted on a second time.
        entry["unverifiable_action"] = (
            (before.get("unverifiable_action") or "")
            if (entry.get("verdict") or "") == "unverifiable"
            else ""
        )
        # `>=`, not `==`: a row held back by the cap keeps advancing, so an equality test
        # would never match it again. The action marker is what stops a second action.
        if streak >= THRESHOLD and not entry["unverifiable_action"]:
            candidates.append((entry, sid))
    candidates.sort(key=lambda pair: (-(pair[0].get("unverifiable_streak") or 0), pair[1]))
    chosen = candidates[: max(0, max_actions)]
    held = candidates[max(0, max_actions) :]
    acted = []
    for entry, sid in chosen:
        decision = decide(sid, entry, pane_for_fn, pane_text_fn, link_fn)
        entry["unverifiable_action"] = decision["action"]
        acted.append((entry, decision))
    return acted, held


def render(acted, held=()):
    """The `UNVERIFIABLE` block plus the two paste-ready blocks.

    One line per acted row, however long the list. A summary line is exactly the shape a
    build that never read a pane would also produce, which is why the rows are rendered
    here rather than left to prose in `agents/fleet-drive.md` -- the same argument
    `drive-retained.py` records for `RETAINED`.

    `held` rows are those the per-round cap pushed to a later round. They are NAMED, not
    counted away: a bare "N held back" would leave the operator unable to tell which
    sessions are queued, and a silent cap is indistinguishable from a threshold that
    stopped firing. They appear under their own line so the two blocks above stay exactly
    what will be sent this round.
    """
    lines = [
        "UNVERIFIABLE — a session unverifiable %d consecutive rounds, its pane read, and the "
        "action taken" % THRESHOLD
    ]
    for entry, decision in acted:
        lines.append(
            "  %s [%s] · streak %d · pane %s · %s"
            % (
                entry.get("name") or "?",
                (entry.get("session_id") or "")[:8],
                THRESHOLD,
                decision["pane"] or "—",
                decision["action"],
            )
        )
        lines.append("      pane: %s" % decision["pane_line"])
        if decision["closer"]:
            lines.append("      closer: %s" % decision["closer"])
        else:
            lines.append("      why: %s" % decision["why"])
    if not acted:
        lines.append("  (none)")

    lines.append("UNVERIFIABLE NUDGES — paste each line into NUDGES")
    nudges = [(e, d) for e, d in acted if d["action"] == "nudge"]
    for entry, decision in nudges:
        lines.append("  TO: %s | %s" % (entry.get("name") or "?", decision["text"]))
    if not nudges:
        lines.append("  (none)")

    lines.append(
        "UNVERIFIABLE ESCALATIONS — paste into ESCALATION and post one attention card each"
    )
    escalations = [(e, d) for e, d in acted if d["action"] == "escalation"]
    if escalations:
        lines.append("  unverifiable for %d consecutive rounds" % THRESHOLD)
        for entry, decision in escalations:
            lines.append(
                "    %s [%s] · pane %s · %s — %s → %s"
                % (
                    entry.get("name") or "?",
                    (entry.get("session_id") or "")[:8],
                    decision["pane"] or "—",
                    decision["link"] or "no pane — no jump target rendered",
                    decision["pane_line"],
                    decision["why"],
                )
            )
            lines.append("      card: %s" % decision["text"])
    else:
        lines.append("  (none)")

    if held:
        lines.append(
            "UNVERIFIABLE HELD BACK — at the threshold, deferred to a later round by the "
            "per-round cap of %d; their streaks keep advancing" % MAX_ACTIONS
        )
        for entry, sid in held:
            lines.append(
                "  %s [%s] · streak %d"
                % (
                    entry.get("name") or "?",
                    (sid or "")[:8],
                    entry.get("unverifiable_streak") or THRESHOLD,
                )
            )
    return "\n".join(lines)


def cmd_handle(args):
    ledger, reason = read_ledger(args.ledger_script)
    if reason:
        print("drive-unverifiable: %s" % reason, file=sys.stderr)
        return REFUSED
    try:
        with open(args.entries, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        print("drive-unverifiable: entries unreadable: %s" % exc, file=sys.stderr)
        return REFUSED
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        print(
            "drive-unverifiable: entries payload carries no `entries` list -- nothing acted "
            "on",
            file=sys.stderr,
        )
        return REFUSED
    round_tag = payload.get("round") or ""
    acted, held = plan(
        payload["entries"],
        ledger,
        round_tag,
        resolve_pane,
        pane_text,
        render_link,
        max_actions=args.max_actions,
    )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    print(render(acted, held))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="drive-unverifiable.py")
    sub = parser.add_subparsers(dest="command")
    handle = sub.add_parser(
        "handle",
        help="advance every entry's consecutive-unverifiable streak and act at the "
        "threshold",
    )
    handle.add_argument(
        "--entries",
        required=True,
        help="this round's merge payload, the same JSON handed to drive-ledger.py merge",
    )
    handle.add_argument(
        "--json-out",
        default=None,
        help="also write the entries back here, with the streak fields this round set",
    )
    handle.add_argument(
        "--ledger-script", default=None, help="path to drive-ledger.py (default: the sibling)"
    )
    handle.add_argument(
        "--max-actions",
        type=int,
        default=MAX_ACTIONS,
        help="rows acted on in one round (default %d); the rest are held back and keep "
        "their streak" % MAX_ACTIONS,
    )
    args = parser.parse_args(argv)
    if args.command == "handle":
        return cmd_handle(args)
    parser.print_help(sys.stderr)
    return REFUSED


if __name__ == "__main__":
    sys.exit(main())
