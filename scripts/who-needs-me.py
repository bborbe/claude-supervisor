#!/usr/bin/env python3
"""List Claude Code sessions that need the operator, joined to WezTerm panes.

Reads ~/.claude/state/attention/*.needs.json / *.tool.json written by
~/.claude/hooks/attention-log.py.  Sections:
  Needs you       — permission prompt or open question (oldest first)
  Probably stuck  — inside one tool call longer than --stuck-min (default 20)
  Reapable        — close gate whose anchored task is already finished
  Idle            — turn ended, waiting for a prompt (only with --idle)
--jump PANE activates that WezTerm pane.

The feed answers "was a gate raised", never "is a gate open" — it is hook-written
and goes stale until the session's next tool call. Read the pane before reporting
any gate as open.
"""
import argparse, glob, json, os, re, subprocess, sys, time, urllib.parse

STATE = os.path.expanduser("~/.claude/state/attention")
OBSIDIAN = os.path.expanduser("~/Documents/Obsidian")

# Closer verbs describing a parked wait rather than an open gate. `later (on
# <trigger>):` names the event that resumes the work; until it fires there is
# nothing for the operator to answer. Measured 2026-09-19: two such closers sat at
# the top of `Needs you` for over five hours, reported as neglect.
PARKED_VERBS = ("later (on ",)

# A close gate whose anchored task is finished is the operator's call but not an
# open gate — it is reapable, and is listed separately.
CLOSE_GATE = "approve: /vault-cli:session-close"

# `&nbsp;` is six literal characters, not whitespace, so strip() does not remove it.
_ENTITY = re.compile(r"&(?:nbsp|#160|#xa0);", re.I)
_PANE_REF = re.compile(r"\bpane\s*#?\s*(\d+)\b", re.I)
_TASK_LINK = re.compile(r"📌 Task: \[[^\]]*\]\(obsidian://open\?vault=[^&]+&file=([^)]+)\)")


def normalize_closer(text):
    """Reduce a closer verb to a comparable form.

    Panes have emitted `👤 You: &nbsp;&nbsp;&nbsp;&nbsp;nothing`. The entity is
    text, so `startswith("nothing")` missed it and a parked session was
    reclassified as an open question. Strip entities and collapse whitespace so
    the comparison sees the verb rather than its padding.
    """
    text = _ENTITY.sub(" ", text or "")
    return " ".join(text.split()).strip()


def is_parked_verb(detail):
    """True when a closer names the event that resumes the work.

    `later (on <trigger>):` is a parked wait: until that event fires there is
    nothing for the operator to answer, so it is not an open gate regardless of
    age. Measured 2026-09-19: two such closers sat at the top of `Needs you` for
    over five hours, reported as neglect.

    Checked on both paths on purpose. A parked closer reaches the feed two ways —
    re-derived from an `idle` record's transcript, or written straight in by the
    hook as `kind: "question"`. Both live instances measured 2026-09-19 arrived the
    second way, so a reclassify-only check would have missed every real one.
    """
    return normalize_closer(detail).startswith(PARKED_VERBS)


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
    """Stop hook may predate the closer-panel rule; re-derive from the transcript.

    Handles the two text-only false-positive classes. A closer that is absent,
    `nothing`, or a `later (on <trigger>):` parked wait is left as `idle` — it is
    genuinely idle, and should still appear under --idle rather than in the gate
    list.
    """
    if rec.get("kind") != "idle":
        return rec
    lines = [l.strip() for l in last_assistant_text(rec).splitlines() if l.strip()]
    you = [l for l in lines if l.startswith("👤 You:")]
    ask = normalize_closer(you[-1].split(":", 1)[1]) if you else ""
    if not ask or ask.startswith("nothing") or is_parked_verb(ask):
        return rec
    return dict(rec, kind="question", detail=ask)


def answered(rec):
    """True once the hook marked the record answered — the operator has acted."""
    return rec.get("state") == "answered"


def read_status(path):
    """The `status:` value from a vault task's frontmatter, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(4000)
    except Exception:
        return None
    if not head.startswith("---"):
        return None
    parts = head.split("---", 2)
    if len(parts) < 3:
        return None
    m = re.search(r"^status:\s*(\S+)", parts[1], re.M)
    return m.group(1).strip().strip("\"'") if m else None


def task_status_from_closer(rec):
    """`status:` of the vault task this session anchored, read from its closer.

    The closer names its own task (`📌 Task: [..](obsidian://open?vault=..&file=..)`),
    so no vault scan is needed. Returns None when there is no anchor or the file is
    not on disk — which keeps the record an open gate rather than suppressing it.
    """
    m = _TASK_LINK.search(last_assistant_text(rec))
    if not m:
        return None
    rel = urllib.parse.unquote(m.group(1))
    for vault in sorted(glob.glob(os.path.join(OBSIDIAN, "*"))):
        path = os.path.join(vault, rel + ".md")
        if os.path.exists(path):
            return read_status(path)
    return None


def is_reapable(rec, task_status):
    """A close gate whose anchored task already reads `status: completed`.

    The close is genuinely the operator's call, so it stays visible — but the work
    is done and nothing is blocked, so it is not an open gate. Listed separately
    rather than counted under `Needs you`.

    Fails safe: no resolver, or an unresolvable task, leaves it an open gate.
    """
    if not (rec.get("detail") or "").startswith(CLOSE_GATE):
        return False
    if task_status is None:
        return False
    return task_status(rec) == "completed"


def names_peer_gate(rec, open_panes):
    """True when this closer restates another pane's gate rather than raising its own.

    A session relaying "the approval in pane 285 is yours alone" is not a second
    decision — pane 285 already carries it, and the operator's action belongs
    there. Suppressed only when the named pane is *itself* an open gate in this
    run, so a closer that merely mentions a pane id (a ticket reference, a log
    line) is left alone and a real gate is never silently eaten.
    """
    if not open_panes:
        return False
    mine = str(rec.get("pane"))
    for named in _PANE_REF.findall(rec.get("detail") or ""):
        if named != mine and named in open_panes:
            return True
    return False


def is_open_gate(rec, open_panes=frozenset(), task_status=None):
    """A gate the operator has not answered yet.

    The record carries `state` ("open" until its answering event fires, then "answered")
    and `cleared_by`, naming that event. The old rule was file-existence — the hook
    deleted the record on ANY later event, so a background task completing erased an open
    gate and the pane vanished from the feed with nothing left to classify. Reproduced
    2026-09-18 on a throwaway pane: Stop wrote the gate, PostToolUse removed it, and the
    feed could not tell that apart from the operator having answered.

    `open_panes` and `task_status` carry the context the context-dependent classes
    need; both default to absent, which degrades to the text-only rule.
    """
    if answered(rec) or rec.get("kind") not in ("permission", "question"):
        return False
    if is_parked_verb(rec.get("detail")):
        return False
    if is_reapable(rec, task_status):
        return False
    if names_peer_gate(rec, open_panes):
        return False
    return True


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

    # Two passes: the pane set that carries a gate is computed without peer-dedup,
    # so a restating pane is dropped only when the pane it names is itself a gate.
    open_panes = {str(r.get("pane")) for r in needs
                  if is_open_gate(r, task_status=task_status_from_closer)}
    blocked = sorted([r for r in needs
                      if is_open_gate(r, open_panes=open_panes, task_status=task_status_from_closer)],
                     key=lambda r: r["ts"])
    reapable = sorted([r for r in needs if not answered(r)
                       and is_reapable(r, task_status_from_closer)], key=lambda r: r["ts"])
    stuck = sorted([r for r in tools if time.time() - r["ts"] > a.stuck_min * 60], key=lambda r: r["ts"])
    idle = sorted([r for r in needs if r["kind"] == "idle" and not answered(r)], key=lambda r: r["ts"])

    print(f"Needs you ({len(blocked)})")
    for r in blocked:
        print(row(r, pmap, f"{r['kind']}: {r['detail']}"))
    print(f"\nProbably stuck > {a.stuck_min}m ({len(stuck)})")
    for r in stuck:
        print(row(r, pmap, r["detail"]))
    print(f"\nReapable ({len(reapable)})  — finished work on a close gate, yours to close")
    for r in reapable:
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
