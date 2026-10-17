#!/usr/bin/env python3
"""Per-session context usage, so a manager can see a worker filling up before it stalls.

Source: ~/.claude/state/context/<sid>.json, written by ~/.claude/statusline-command.sh.
The statusline is the only place Claude Code hands a session its own context_window
(`used_percentage`, `remaining_percentage`, `context_window_size`), and it writes nothing
to disk — so that script publishes it, throttled to one write per rounded percentage move.

Reads ~/.claude/state/attention/*.{needs,tool}.json for pane + blocked/in-tool state, so a
session that is mid-tool-call or waiting on the human is never offered as a compaction
candidate.

Every recorded pane id is resolved against `wezterm cli list` before it is printed. The
state files are written at event time and never rewritten, so a pane id outlives its pane,
and printing one unchecked gave a dead pane the confidence of a live one (measured
2026-09-20: `--compactable` offered pane 109 for a 72h-old session whose pane did not
exist). A pane that cannot be confirmed is withheld, never printed and never offered for
compaction — compaction is a no-ask action, so a phantom candidate is a run that reports
success and does nothing.

  context-usage.py                 all sessions, highest usage first
  context-usage.py --over 70       only sessions at/over 70%
  context-usage.py --compactable   over the threshold AND neither blocked nor in a tool call
  context-usage.py --threshold 60  set the percentage threshold (default 70)
  context-usage.py --threshold-tokens 500000
                                   set the absolute bound: over at/above 500k tokens used

A token bound is a PRECONDITION, not an alternative to the percentage: a session
below it is under the bound however full its window is. That is load-bearing on a
small window, where the percentage alone is far below the floor — 70% of 128k is
90k — and an ORed bound let the percentage fire there on its own. The percentage
is therefore consulted only when no token bound is given, and the run prints the
single bound in force so a gate's output names what it actually applied.

The absolute bound is what this fleet runs on: the sessions here use 1M-token
windows, so 70% is 700k tokens and the percentage gate is unreachable in practice
(measured 2026-10-13: 85 sessions live in 24h, all 1M windows, 380k–620k used,
none near 70%).

A record whose `context_window_size` is absent cannot be judged against a token
bound at all. It is reported as UNKNOWN and withheld from the candidate list —
never folded to 0 tokens, which would read as "under every bound" and silently drop
exactly the sessions the bound cannot evaluate.
"""
import argparse, glob, importlib.util, json, os, time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# One definition of "which panes exist" for the whole fleet: `who-needs-me.py`
# owns the query, and this script and `fleet-board.py` both read its answer. Two
# implementations would be two answers waiting to disagree.
wnm = _load("who_needs_me", "who-needs-me.py")

CONTEXT = os.path.expanduser("~/.claude/state/context")
ATTENTION = os.path.expanduser("~/.claude/state/attention")
CLAUDE_CODE_SESSION_ID = os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def load(dirname, suffix):
    # attention files are <sid>.<kind>.json; context files are <sid>.json (empty suffix)
    pat = f"*.{suffix}.json" if suffix else "*.json"
    out = {}
    for p in glob.glob(os.path.join(dirname, pat)):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        sid = d.get("session_id") or os.path.basename(p).split(".")[0]
        out[sid] = d
    return out


def bar(pct, width=10):
    filled = min(width, int(pct * width / 100))
    return "█" * filled + "░" * (width - filled)


def age(ts):
    m = int((time.time() - ts) // 60) if ts else 0
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def live_panes():
    """Confirmed pane ids, or `None` when the query itself failed.

    Delegates to `who-needs-me.py`'s `live_pane_ids()` so the fleet has one answer
    to "which panes exist". `None` is a distinct answer from `set()` — a failed
    `wezterm cli list` cannot prove a pane is gone any more than it can prove one
    is live, so neither caller may read it as "no panes exist".
    """
    return wnm.live_pane_ids()


def pane_state(pane, live):
    """Resolve one recorded pane id against the live set: (display, addressable, phantom).

    A pane that cannot be confirmed is never printed as an id — a printed id reads
    as a live one, which is the whole defect. `phantom` distinguishes "recorded but
    gone" from "never recorded", so the report can say which.
    """
    if live is None:
        return "?", False, False
    if not pane or str(pane) == "?":
        return "—", False, False
    if str(pane) in live:
        return str(pane), True, False
    return "—", False, True


def is_candidate(r):
    """Compaction candidates must be idle, not this session, and have a live pane.

    A candidate is addressed by sending to its pane, so one without a confirmed
    pane is a no-op that reports success.
    """
    return (not r["blocked"] and not r["busy"] and not r["self"]
            and r["addressable"])


def used_tokens(r):
    """Absolute tokens used, or `None` when the record carries no window size.

    `context_window_size` is absent on some records. Folding that to 0 — which the
    display path used to do with `or 0` — reads as "tiny, under every bound", so an
    absolute-token filter built on it would silently drop exactly the sessions it
    cannot evaluate. UNKNOWN is its own answer, the same rule `pane_state` applies
    to an unreadable `wezterm cli list`.
    """
    size = r["rec"].get("context_window_size")
    if not size:
        return None
    return int(r["pct"] / 100 * size)


def bound_verdict(r, threshold, tokens):
    """`True` over, `False` under, `None` when the record cannot be evaluated.

    A token bound is a PRECONDITION, not an alternative. The two bounds used to
    be ORed, which let the percentage fire on its own at 70% of a 128k window —
    90k, far below the 500k floor that must hold on every path. A session below
    the token bound is therefore under the bound however full its window is, and
    the percentage is consulted only when no token bound is given.

    `None` is preserved as its own answer: a record with no window size cannot
    be judged against a token bound, and must not read as "under" it.
    """
    if tokens is None:
        return r["pct"] >= threshold
    used = used_tokens(r)
    return None if used is None else used >= tokens


def bound_label(threshold, tokens):
    """The bound as printed — the in-force bound a gate's own output carries.

    Only the bound that can actually fire is named. Printing "70% or 200k
    tokens" while the percentage cannot fire below the token bound would
    describe a gate this code does not have — the same defect the label was
    added to prevent, since a reader would check the percentage half and find
    it inert.
    """
    if tokens is None:
        return f"{threshold:.0f}%"
    return f"{tokens / 1000:.0f}k tokens"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=70.0)
    ap.add_argument("--threshold-tokens", type=int, default=None, metavar="N",
                    help="absolute bound — over at/above N tokens used")
    ap.add_argument("--over", type=float, metavar="PCT", help="only at/over PCT")
    ap.add_argument("--compactable", action="store_true",
                    help="over threshold AND not blocked AND not in a tool call")
    a = ap.parse_args()

    ctx = load(CONTEXT, "")
    if not ctx:
        print("No context data yet — the statusline publishes it on its next redraw.")
        return 0

    needs = load(ATTENTION, "needs")
    tools = load(ATTENTION, "tool")
    live = live_panes()

    rows = []
    me = CLAUDE_CODE_SESSION_ID
    for sid, d in ctx.items():
        pct = d.get("used_percentage")
        if pct is None:
            continue
        n = needs.get(sid) or {}
        t = tools.get(sid) or {}
        # A stale tool entry (no PostToolUse) is not proof of an in-flight call, but it is
        # the only signal on disk; treat it as busy and let the manager confirm on the pane.
        blocked = n.get("kind") in ("permission", "question")
        busy = bool(t)
        pane, addressable, phantom = pane_state(
            d.get("pane") or n.get("pane") or t.get("pane"), live)
        # This session stays visible (its own fill level is worth seeing) but is never a
        # compaction candidate — compacting yourself loses the loop state mid-round.
        rows.append({"sid": sid, "pct": float(pct), "rec": d, "pane": pane,
                     "addressable": addressable, "phantom": phantom,
                     "blocked": blocked, "busy": busy, "self": bool(me) and sid == me,
                     "detail": n.get("detail", "")})
    rows.sort(key=lambda r: -r["pct"])

    verdicts = {r["sid"]: bound_verdict(r, a.threshold, a.threshold_tokens) for r in rows}
    if a.over is not None:
        shown = [r for r in rows if r["pct"] >= a.over]
    elif a.compactable or a.threshold_tokens is not None:
        # An UNKNOWN verdict is shown rather than filtered out — it is the answer the
        # bound could not produce, and hiding it would read as "under".
        shown = [r for r in rows if verdicts[r["sid"]] is not False]
    else:
        shown = list(rows)
    if a.compactable:
        shown = [r for r in shown if is_candidate(r)]

    if live is None:
        print("⚠️  `wezterm cli list` unreadable — pane ids withheld. A pane that cannot "
              "be checked is UNKNOWN: not live, and not gone either.")
    print(f"Context usage ({len(shown)} of {len(rows)} sessions, highest first)")
    print(f"Bound in force: {bound_label(a.threshold, a.threshold_tokens)}")
    for r in shown:
        name = (r["rec"].get("session_name") or r["sid"][:8])[:34]
        used = used_tokens(r)
        toks = (f"{used / 1000:>5.0f}k/{r['rec'].get('context_window_size', 0) / 1000:.0f}k"
                if used is not None else "    ?k/   ?k")
        flag = "⌛" if r["blocked"] else ("⚙" if r["busy"] else " ")
        print(f" {flag} {r['pct']:>5.0f}%  {bar(r['pct'])}  "
              f"{toks}  {name:<34}  "
              f"pane {r['pane']:>4}  {age(r['rec'].get('ts'))}")

    over = [r for r in rows if verdicts[r["sid"]] is True]
    unknown = [r for r in rows if verdicts[r["sid"]] is None]
    cand = [r for r in over if is_candidate(r)]
    if over:
        print(f"\n⚠️  Over {bound_label(a.threshold, a.threshold_tokens)}: {len(over)}")
        for r in over:
            why = ("this session — never a candidate" if r["self"]
                   else "blocked" if r["blocked"]
                   else "in a tool call" if r["busy"]
                   else "pane gone — not compactable" if r["phantom"]
                   else "pane unverified — not compactable" if not r["addressable"]
                   else "idle — compactable")
            print(f"   {r['pct']:>5.0f}%  {(r['rec'].get('session_name') or r['sid'][:8])[:40]:<40}  pane {r['pane']:>4}  {why}")
    if unknown:
        print(f"\n⚠️  Window UNKNOWN — {len(unknown)} session(s) cannot be judged against the "
              f"{bound_label(a.threshold, a.threshold_tokens)} bound, so none is offered. "
              f"UNKNOWN is not 'under': the record carries no context_window_size.")
        for r in unknown:
            print(f"   {r['pct']:>5.0f}%  {(r['rec'].get('session_name') or r['sid'][:8])[:40]:<40}  "
                  f"pane {r['pane']:>4}  window unknown")
    if cand:
        print(f"\n♻️  Compaction candidates ({len(cand)}) — idle and over threshold. "
              f"Confirm the composer is empty on the pane before sending.")
        for r in cand:
            print(f"   {r['pct']:>5.0f}%  pane {r['pane']:>4}  {(r['rec'].get('session_name') or r['sid'][:8])[:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
