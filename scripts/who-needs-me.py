#!/usr/bin/env python3
"""List Claude Code sessions that need the operator, joined to WezTerm panes.

Reads the attention store written by ~/.claude/hooks/attention-log.py. Two formats
coexist while the fleet rolls over, and both are read:
  <sid>.events.jsonl  append-only event log — one `open` line per item, one `close`
                      line when it clears; folded by `load_events()` into current state
  <sid>.needs.json    the older one-file-per-session snapshot
  <sid>.tool.json     session inside a tool call (stale => probably stuck)
Sections:
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

STATE = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser("~/.claude/state/attention")
OBSIDIAN = os.path.expanduser("~/Documents/Obsidian")

# Closer verbs describing a parked wait rather than an open gate. `later (on
# <trigger>):` names the event that resumes the work; until it fires there is
# nothing for the operator to answer. Measured 2026-09-19: two such closers sat at
# the top of `Needs you` for over five hours, reported as neglect.
PARKED_VERBS = ("later (on ",)

# A close gate whose anchored task is finished is the operator's call but not an
# open gate — it is reapable, and is listed separately.
#
# Two closer forms are sanctioned and both must count. `vault-cli:sync-progress`
# Phase 6 emits `approve: /vault-cli:session-close` verbatim; the operator's global
# ⚪ DONE rule prescribes the `pick` form instead. Matching only the first silently
# ignored every session that followed the second (measured 2026-09-19, pane 17).
CLOSE_GATE = "approve: /vault-cli:session-close"
_CLOSE_TARGET = "/vault-cli:session-close"
# The first (recommended) disposition of a `pick` menu, up to the `· 2.` separator.
_PICK_FIRST = re.compile(r"^pick\s*[—–-]\s*1\.\s*(.*?)(?:\s*·\s*2\.|$)")

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

    Reads BOTH store formats, deliberately. The hook was rewritten to an append-only
    event log (`<sid>.events.jsonl`, one `open` line per item and one `close` line
    when it clears) and the two formats coexist while the fleet rolls over: a
    session that has not yet restarted still has a `.needs.json`, and dropping that
    branch before the last one ages out would silently empty the feed. The
    `.needs.json` branch is removed only once no session writes it.
    """
    me = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    out = []
    if suffix == "needs":
        for rec in load_events():
            if me and rec.get("session_id") == me:
                continue
            out.append(rec)
    for p in glob.glob(os.path.join(STATE, f"*.{suffix}.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if me and d.get("session_id") == me:
            continue
        out.append(d)
    return out


def load_events():
    """Fold each session's event log into its currently-open items.

    The log is append-only and never rewritten: `open` lines carry the item,
    `close` lines carry only the `item_id` they clear. An item is open exactly
    when its `item_id` has an `open` line and no matching `close`.

    `state` is reconstructed here rather than read from the record, so the whole
    downstream classification (`answered()`, `is_open_gate()`) keeps working
    unchanged against the new format — the record shape stays what every other
    function already expects.

    A malformed line is skipped, not fatal: the hook appends while this reads, so
    a torn final line is expected rather than exceptional.
    """
    out = []
    for p in glob.glob(os.path.join(STATE, "*.events.jsonl")):
        opened, closed = {}, set()
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    iid = r.get("item_id")
                    if not iid:
                        continue
                    if r.get("type") == "open":
                        opened[iid] = r
                    else:
                        closed.add(iid)
        except Exception:
            continue
        for iid, rec in opened.items():
            # `state` mirrors the old on-disk marker so `answered()` is unchanged.
            out.append(dict(rec, state="answered" if iid in closed else "open"))
    return out


def panes():
    try:
        raw = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                             capture_output=True, text=True, timeout=5).stdout
        return {str(p["pane_id"]): p for p in json.loads(raw)}
    except Exception:
        return {}


_TEXT_CACHE = {}


def last_assistant_text(rec):
    """The transcript's last assistant text block, memoized per session.

    Memoized because classification now asks for it more than once per record —
    `is_reapable()` resolves the live closer from it while `task_status_from_closer()`
    resolves the anchored task from it — and the read is a 200KB tail seek. The
    script is one-shot, so a per-run cache cannot go stale within a run.
    """
    key = rec.get("session_id")
    if key is not None and key in _TEXT_CACHE:
        return _TEXT_CACHE[key]
    text = _read_last_assistant_text(rec)
    if key is not None:
        _TEXT_CACHE[key] = text
    return text


def _read_last_assistant_text(rec):
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


def closer_from_transcript(rec):
    """The last `👤 You:` closer line in the transcript, normalized; "" when absent."""
    lines = [l.strip() for l in last_assistant_text(rec).splitlines() if l.strip()]
    you = [l for l in lines if l.startswith("👤 You:")]
    return normalize_closer(you[-1].split(":", 1)[1]) if you else ""


def current_closer(rec):
    """The session's live closer text — re-derived from the transcript, not cached.

    `detail` is written once by the hook and never refreshed, so a hook-written
    `question` record goes stale the moment its session clears one gate and raises
    another. `reclassify_idle()` already re-derives from the transcript, but it
    returns early on `kind != "idle"`, so every hook-written gate kept the old text.
    Measured 2026-09-19: panes 338 and 254 carried a cleared `you run:` push gate in
    `detail` while their live closer was `approve: /vault-cli:session-close`.

    Falls back to `detail` when no transcript is readable, so a record whose session
    file is gone is classified on what the hook recorded rather than dropped.
    """
    return closer_from_transcript(rec) or (rec.get("detail") or "")


def reclassify_idle(rec):
    """Stop hook may predate the closer-panel rule; re-derive from the transcript.

    Handles the two text-only false-positive classes. A closer that is absent,
    `nothing`, or a `later (on <trigger>):` parked wait is left as `idle` — it is
    genuinely idle, and should still appear under --idle rather than in the gate
    list.
    """
    if rec.get("kind") != "idle":
        return rec
    ask = closer_from_transcript(rec)
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


def is_close_gate_closer(text):
    """A closer whose decision is closing this session.

    Both sanctioned forms count — the `approve:` form and the `pick` form.

    Rejected on purpose, one for each direction of error:
      * a `later (on <trigger>):` line that merely mentions session-close — that is
        a deferral, not a close gate. `is_parked_verb()` is checked first, so
        widening a prefix test into a containment test cannot turn a parked wait
        into a close gate. This is the trap: a false-negative fix becoming a false
        positive.
      * a `pick` menu whose first/recommended disposition is not session-close —
        the operator is being offered other work, so it is not a close gate. The
        fleet emits these routinely (`pick — 1. keep SC3 as designed; …`).
    """
    text = normalize_closer(text)
    if not text or is_parked_verb(text):
        return False
    if text.startswith(CLOSE_GATE):
        return True
    m = _PICK_FIRST.match(text)
    return bool(m) and _CLOSE_TARGET in m.group(1)


def is_reapable(rec, task_status):
    """A close gate whose anchored task already reads `status: completed`.

    The close is genuinely the operator's call, so it stays visible — but the work
    is done and nothing is blocked, so it is not an open gate. Listed separately
    rather than counted under `Needs you`.

    Reads the *live* closer rather than the record's cached `detail`, and accepts
    both sanctioned close forms — the two defects measured 2026-09-19, which left
    `Reapable (0)` while three finished sessions sat in `Needs you` in the same run.

    Fails safe: no resolver, or an unresolvable task, leaves it an open gate.
    """
    if not is_close_gate_closer(current_closer(rec)):
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


def is_rendered_panel(rec):
    """A closer panel the `Stop` handler rendered — not a parked gate.

    `kind` cannot tell them apart: the `Stop` panel path and the `Notification`
    elicitation path both write `kind="question"` with an identical `cleared_by`
    (`{"event": "UserPromptSubmit"}`), and the two `idle` paths collide the same
    way. The writer is recorded in `event`: `Stop` means the session ended its
    turn and rendered a line describing what waiting looks like; anything else
    is a real parked tool call.

    A record with no `event` predates the marker and keeps the old behaviour —
    counted as a gate — so nothing is silently reclassified by this change.
    """
    return rec.get("event") == "Stop"


def is_open_gate(rec, open_panes=frozenset(), task_status=None, include_panels=False):
    """A gate the operator has not answered yet.

    The record carries `state` ("open" until its answering event fires, then "answered")
    and `cleared_by`, naming that event. The old rule was file-existence — the hook
    deleted the record on ANY later event, so a background task completing erased an open
    gate and the pane vanished from the feed with nothing left to classify. Reproduced
    2026-09-18 on a throwaway pane: Stop wrote the gate, PostToolUse removed it, and the
    feed could not tell that apart from the operator having answered.

    `open_panes` and `task_status` carry the context the context-dependent classes
    need; both default to absent, which degrades to the text-only rule.

    `include_panels` is the one knob that separates a *block* from a *soft signal*.
    A rendered closer panel is not waiting on the operator, so it must not be
    counted as a block — but it must still be listed, or `/who-needs-me` silently
    starts hiding panes. Callers pass True to enumerate the panel group through
    this same predicate, so the two surfaces cannot disagree about which rows are
    panels.
    """
    if answered(rec) or rec.get("kind") not in ("permission", "question"):
        return False
    if is_parked_verb(rec.get("detail")):
        return False
    if is_reapable(rec, task_status):
        return False
    if names_peer_gate(rec, open_panes):
        return False
    if is_rendered_panel(rec) and not include_panels:
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
    # Rendered closer panels: the same predicate with `include_panels=True`, so the
    # block count and the panel list can never disagree about which rows are panels.
    # They stay VISIBLE — the soft signal is what this command is for — and are
    # simply not counted as blocks.
    panels = sorted([r for r in needs
                     if is_rendered_panel(r) and not answered(r)
                     and is_open_gate(r, open_panes=open_panes,
                                      task_status=task_status_from_closer, include_panels=True)],
                    key=lambda r: r["ts"])
    reapable = sorted([r for r in needs if not answered(r)
                       and is_reapable(r, task_status_from_closer)], key=lambda r: r["ts"])
    stuck = sorted([r for r in tools if time.time() - r["ts"] > a.stuck_min * 60], key=lambda r: r["ts"])
    idle = sorted([r for r in needs if r["kind"] == "idle" and not answered(r)], key=lambda r: r["ts"])

    print(f"Needs you ({len(blocked)})")
    for r in blocked:
        print(row(r, pmap, f"{r['kind']}: {r['detail']}"))
    print(f"\nRendered panels ({len(panels)})  — a closer line, not a parked gate")
    for r in panels:
        print(row(r, pmap, r["detail"]))
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
