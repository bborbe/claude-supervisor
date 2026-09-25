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
  context-usage.py --threshold 60  set the threshold (default 70)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=70.0)
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

    limit = a.over if a.over is not None else (a.threshold if (a.compactable or a.over) else None)
    shown = [r for r in rows if limit is None or r["pct"] >= limit]
    if a.compactable:
        shown = [r for r in shown if is_candidate(r)]

    if live is None:
        print("⚠️  `wezterm cli list` unreadable — pane ids withheld. A pane that cannot "
              "be checked is UNKNOWN: not live, and not gone either.")
    print(f"Context usage ({len(shown)} of {len(rows)} sessions, highest first)")
    for r in shown:
        name = (r["rec"].get("session_name") or r["sid"][:8])[:34]
        size = r["rec"].get("context_window_size") or 0
        used = int(r["pct"] / 100 * size) if size else 0
        flag = "⌛" if r["blocked"] else ("⚙" if r["busy"] else " ")
        print(f" {flag} {r['pct']:>5.0f}%  {bar(r['pct'])}  "
              f"{used/1000:>5.0f}k/{size/1000:.0f}k  {name:<34}  "
              f"pane {r['pane']:>4}  {age(r['rec'].get('ts'))}")

    over = [r for r in rows if r["pct"] >= a.threshold]
    cand = [r for r in over if is_candidate(r)]
    if over:
        print(f"\n⚠️  Over {a.threshold:.0f}%: {len(over)}")
        for r in over:
            why = ("this session — never a candidate" if r["self"]
                   else "blocked" if r["blocked"]
                   else "in a tool call" if r["busy"]
                   else "pane gone — not compactable" if r["phantom"]
                   else "pane unverified — not compactable" if not r["addressable"]
                   else "idle — compactable")
            print(f"   {r['pct']:>5.0f}%  {(r['rec'].get('session_name') or r['sid'][:8])[:40]:<40}  pane {r['pane']:>4}  {why}")
    if cand:
        print(f"\n♻️  Compaction candidates ({len(cand)}) — idle and over threshold. "
              f"Confirm the composer is empty on the pane before sending.")
        for r in cand:
            print(f"   {r['pct']:>5.0f}%  pane {r['pane']:>4}  {(r['rec'].get('session_name') or r['sid'][:8])[:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
