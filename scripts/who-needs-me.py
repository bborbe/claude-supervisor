#!/usr/bin/env python3
"""List Claude Code sessions that need the operator, joined to WezTerm panes.

Reads ~/.claude/state/attention/*.needs.json / *.tool.json written by
~/.claude/hooks/attention-log.py.  Sections:
  Needs you       — permission prompt or open question (oldest first)
  Probably stuck  — inside one tool call longer than --stuck-min (default 20)
  Idle            — turn ended, waiting for a prompt (only with --idle)
--jump PANE activates that WezTerm pane.
"""
import argparse, glob, json, os, subprocess, sys, time

STATE = os.path.expanduser("~/.claude/state/attention")


def load(suffix):
    """Attention records, minus this session's own.

    A session is never its own attention item: its gate is already in front of the operator
    in its own pane. Listing it makes a manager surface its own question as a peer's, and
    under the ask-here-relay-back rule it would then relay the answer into its own pane.
    Measured 2026-09-17: the fleet-manager session appeared in its own blocked list (tab 0).
    """
    me = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    out = []
    for p in glob.glob(os.path.join(STATE, f"*.{suffix}.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if me and d.get("session_id") == me:
            continue
        out.append(d)
    return out


def panes():
    try:
        raw = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                             capture_output=True, text=True, timeout=5).stdout
        return {str(p["pane_id"]): p for p in json.loads(raw)}
    except Exception:
        return {}


def last_assistant_text(rec):
    path = rec.get("transcript") or next(iter(glob.glob(os.path.expanduser(
        f"~/.claude/projects/*/{rec['session_id']}.jsonl"))), None)
    if not path or not os.path.exists(path):
        return ""
    last = ""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 200_000))
            for line in f.read().decode("utf-8", "replace").splitlines():
                if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                for c in j.get("message", {}).get("content", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        last = c["text"]
    except Exception:
        pass
    return last


def reclassify_idle(rec):
    """Stop hook may predate the closer-panel rule; re-derive from the transcript."""
    if rec.get("kind") != "idle":
        return rec
    lines = [l.strip() for l in last_assistant_text(rec).splitlines() if l.strip()]
    you = [l for l in lines if l.startswith("👤 You:")]
    ask = you[-1].split(":", 1)[1].strip() if you else ""
    if ask and not ask.startswith("nothing"):
        rec = dict(rec, kind="question", detail=ask)
    return rec


def answered(rec):
    """True once the hook marked the record answered — the operator has acted."""
    return rec.get("state") == "answered"


def is_open_gate(rec):
    """A gate the operator has not answered yet.

    The record carries `state` ("open" until its answering event fires, then "answered")
    and `cleared_by`, naming that event. The old rule was file-existence — the hook
    deleted the record on ANY later event, so a background task completing erased an open
    gate and the pane vanished from the feed with nothing left to classify. Reproduced
    2026-09-18 on a throwaway pane: Stop wrote the gate, PostToolUse removed it, and the
    feed could not tell that apart from the operator having answered.
    """
    return not answered(rec) and rec.get("kind") in ("permission", "question")


def age(ts):
    m = int((time.time() - ts) // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def name_of(rec, pmap):
    p = pmap.get(str(rec.get("pane")))
    if p:
        title = p.get("title", "").strip()
        while title and not (title[0].isalnum() or title[0] in "/~._-"):
            title = title[1:].lstrip()  # Claude Code prefixes a status glyph (✳ ◐ ◑ ◒ ◓ ⠿ …)
        return title or os.path.basename(rec.get("cwd", ""))
    return os.path.basename(rec.get("cwd", "")) + " (pane gone)"


def row(rec, pmap, what):
    pane = rec.get("pane") or "?"
    alive = str(pane) in pmap
    jump = f"wezterm cli activate-pane --pane-id {pane}" if alive else "(no live pane)"
    return f"  [{pane:>4}] {age(rec['ts']):>6}  {name_of(rec, pmap)[:50]:<50}  {what[:60]}\n         {jump}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stuck-min", type=int, default=20)
    ap.add_argument("--idle", action="store_true", help="also list idle (turn ended) sessions")
    ap.add_argument("--jump", metavar="PANE", help="activate this WezTerm pane and exit")
    a = ap.parse_args()
    if a.jump:
        sys.exit(subprocess.call(["wezterm", "cli", "activate-pane", "--pane-id", a.jump]))

    pmap = panes()
    live = lambda r: str(r.get("pane")) in pmap
    needs = [reclassify_idle(r) for r in load("needs") if live(r)]
    tools = [r for r in load("tool") if live(r)]

    blocked = sorted([r for r in needs if is_open_gate(r)], key=lambda r: r["ts"])
    stuck = sorted([r for r in tools if time.time() - r["ts"] > a.stuck_min * 60], key=lambda r: r["ts"])
    idle = sorted([r for r in needs if r["kind"] == "idle" and not answered(r)], key=lambda r: r["ts"])

    print(f"Needs you ({len(blocked)})")
    for r in blocked:
        print(row(r, pmap, f"{r['kind']}: {r['detail']}"))
    print(f"\nProbably stuck > {a.stuck_min}m ({len(stuck)})")
    for r in stuck:
        print(row(r, pmap, r["detail"]))
    if a.idle:
        print(f"\nIdle, turn ended ({len(idle)})")
        for r in idle:
            print(row(r, pmap, r["detail"]))
    else:
        print(f"\nIdle, turn ended: {len(idle)}  (--idle to list)")
    if not blocked and not stuck:
        print("\nNothing needs you.")


if __name__ == "__main__":
    main()
