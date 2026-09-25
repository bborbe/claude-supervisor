#!/usr/bin/env python3
"""Jump to a Claude Code session's WezTerm tab.

  jump.py                 newest session needing attention
  jump.py <N>             activate N — the tab if tab N exists, else pane N. Refuses
                          only when both exist and disagree (`tab:` / `pane:` force it)
  jump.py tab:<N>         activate that tab explicitly (forces past the ambiguity check)
  jump.py pane:<N>        activate that pane explicitly
  jump.py <name>          case-insensitive substring match on tab titles
  jump.py <session-id>    a sessionId or its prefix (6+ chars) — what the sweep tables print
  jump.py --list          print the attention queue (what bare jump would pick first)
  jump.py --reset         clear the rotation state (every blocked session offered again)

Repeated bare `jump.py` calls ROTATE: the session you just jumped to drops out of
the queue for --cooldown minutes (default 15), so the next call lands on the next
one. A skipped session comes back by itself when the cooldown lapses, or at once
if it raises a NEW gate in the meantime. Nothing is ever dropped permanently.
  jump.py --dry-run       print the resolved target, activate nothing

The executor for the `wezterm cli activate-tab --tab-id N` lines that
/fleet-status, /fleet-loop, /topic-status and /topic-manager print but never run.

Attention data is reused from who-needs-me.py (same hook-written state), not re-derived.
"""
import argparse, base64, glob, importlib.util, json, os, re, subprocess, sys, time

# Plugin-relative when running as an installed plugin; falls back to ~/.claude so the
# script still runs standalone (mirrors coding/scripts/validate-citations.sh).
PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.expanduser(
    "~/.claude/plugins/marketplaces/claude-supervisor"
)
WNM_PATH = os.path.join(PLUGIN_ROOT, "scripts", "who-needs-me.py")
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
VISITED_PATH = os.path.expanduser("~/.claude/state/jump-visited.json")
COOLDOWN_DEFAULT_MIN = 15


def load_wnm():
    """Import who-needs-me.py (hyphenated filename -> importlib) for its attention parsing."""
    spec = importlib.util.spec_from_file_location("who_needs_me", WNM_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wezterm_panes():
    """{pane_id(str): {tab_id, window_id, title}}, or `None` when the query failed.

    ⚠️ `None`, never `{}`: a failed `wezterm cli list` cannot prove a pane is gone any
    more than it can prove one is live, so no caller may read it as "no panes exist".
    `{}` is reserved for a WezTerm that answered with zero panes — a real empty answer
    that must keep working. Same convention as `who-needs-me.py`'s `wezterm_panes()`.
    """
    try:
        r = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        return {str(p["pane_id"]): p for p in json.loads(r.stdout)}
    except Exception:
        return None


def build_tabs(pmap):
    """{tab_id(int): {"window_id": int, "title": str, "pane_ids": [str]}}"""
    tabs = {}
    for pid, p in pmap.items():
        tid = p.get("tab_id")
        if tid is None:
            continue
        t = tabs.setdefault(tid, {"window_id": p.get("window_id"), "title": "", "pane_ids": []})
        t["pane_ids"].append(pid)
        # Tab title is carried on every pane of the tab; prefer a non-empty one.
        if not t["title"] and p.get("title"):
            t["title"] = p["title"]
    return tabs


def current_window():
    """Window id of the calling pane (WEZTERM_PANE), or None outside wezterm."""
    pane = os.environ.get("WEZTERM_PANE")
    if not pane:
        return None
    try:
        raw = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                             capture_output=True, text=True, timeout=5).stdout
        for p in json.loads(raw):
            if str(p.get("pane_id")) == str(pane):
                return p.get("window_id")
    except Exception:
        pass
    return None


def clean_title(title):
    """Strip the leading status glyph wezterm shows before the session name."""
    t = (title or "").strip()
    for glyph in ("✳", "◐", "◑", "⏺", "✻", "✽", "·", "●", "○"):
        if t.startswith(glyph):
            t = t[len(glyph):].strip()
            break
    return t or "(untitled)"


def raise_pane(pane_id, pmap):
    """Focus a pane AND raise its OS window.

    `wezterm cli activate-tab` / `activate-pane` cannot cross OS windows, and macOS
    Accessibility cannot focus WezTerm's windows (AXRaise / AXMain / AXFocused /
    `activate` all measured as no-ops, 2026-09-18). The raise therefore happens
    inside WezTerm: writing an OSC 1337 SetUserVar to the pane's tty fires the
    `user-var-changed` hook in ~/.config/wezterm/wezterm.lua, which runs
    `pane:activate()` + `window:focus()` with the real window object in hand.
    Returns True when the escape was written; the caller falls back otherwise.
    """
    tty = pmap.get(str(pane_id), {}).get("tty_name")
    if not tty:
        return False
    payload = base64.b64encode(b"1").decode()
    try:
        with open(tty, "w") as fh:
            fh.write(f"\033]1337;SetUserVar=raise={payload}\007")
        return True
    except OSError:
        return False


def tab_focus_pane(tab_id, tabs, pmap):
    """The pane to raise for a tab: its active pane, else its first."""
    for pid in tabs[tab_id]["pane_ids"]:
        if pmap.get(pid, {}).get("is_active"):
            return pid
    return tabs[tab_id]["pane_ids"][0]


def activate_tab(tab_id, tabs, dry_run, pmap=None):
    if dry_run:
        return 0
    if pmap:
        if raise_pane(tab_focus_pane(tab_id, tabs, pmap), pmap):
            return 0
    # Fallback — same-window only; the report() warning covers the cross-window case.
    return subprocess.call(["wezterm", "cli", "activate-tab", "--tab-id", str(tab_id)])


def report(tab_id, tabs, dry_run, raised=True):
    t = tabs[tab_id]
    win = t["window_id"]
    cur = current_window()
    name = clean_title(t["title"])
    verb = "would jump to" if dry_run else "↪️ jumped to"
    line = f"{verb} {name} — tab {tab_id}, window {win}"
    if not raised and cur is not None and win is not None and cur != win:
        line += f"\n   ⚠️  target is in window {win}, you are in {cur} — raise hook unavailable and activate-tab cannot cross windows; switch window yourself to see it"
    print(line)


def pane_sessions(pmap):
    """{pane_id: session_id} for panes whose running Claude session can be identified.

    Built from ~/.claude/sessions/<pid>.json (pid -> sessionId), joined to panes
    through the tty: `ps` gives pid -> tty, wezterm gives pane -> tty_name. A pane
    absent from the result is simply unidentifiable, which the caller treats as
    "no evidence", never as "mismatch".
    """
    sid_by_pid = {}
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            sid_by_pid[str(d["pid"])] = d["sessionId"]
        except Exception:
            continue
    if not sid_by_pid:
        return {}
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,tty="],
                            capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    tty_by_pid = {}
    for line in ps.splitlines():
        parts = line.split()
        if len(parts) == 2:
            tty_by_pid[parts[0]] = parts[1]
    sid_by_tty = {tty_by_pid[pid]: sid for pid, sid in sid_by_pid.items() if pid in tty_by_pid}
    out = {}
    for pane, p in pmap.items():
        tty = (p.get("tty_name") or "").replace("/dev/", "")
        if tty and tty in sid_by_tty:
            out[pane] = sid_by_tty[tty]
    return out


def attention_queue(wnm, pmap):
    """Blocked sessions (permission / open question) joined to live panes, newest-first.

    The caller's own pane is excluded: jumping to yourself is a no-op, and the
    entry is usually not even yours — a headless supervisor worker inherits
    WEZTERM_PANE from the session that spawned it, so its hook files its gate
    under the manager's pane id (measured 2026-09-18: agent_7's prompt showed as
    "Fleet Manager, tab 0"). Its prompts are answered via mcp__supervisor__*,
    never by a jump.
    """
    me = os.environ.get("WEZTERM_PANE")
    # Liveness through who-needs-me.py, for the same reason the classification below
    # goes through it: a filter re-derived here drifts from the feed's. This line WAS
    # that drift -- pane existence standing in for session liveness -- so an item whose
    # session had exited stayed jumpable here after the feed had correctly dropped it.
    # The reader owns the rule; this surface borrows it.
    records = wnm.load("needs")
    quiet = wnm.quiet_session_ids(records, wnm.live_session_ids())
    live = lambda r: (wnm.is_live(r, pmap, quiet)
                      and str(r.get("pane")) != str(me))
    needs = [wnm.reclassify_idle(r) for r in records if live(r)]

    # Classify through who-needs-me.py rather than re-deriving the predicate here, so
    # the two surfaces cannot disagree about who needs you. This previously inlined
    # `kind in ("permission", "question")`, which skipped every rule is_open_gate()
    # carries: an answered record stayed listed, and a `later (on <trigger>):` parked
    # wait written straight in by the hook, a peer-gate restatement and a
    # finished-work close gate all counted as gates to jump to. Measured 2026-09-19:
    # the feed dropped panes 223/277 while this surface still offered them.
    #
    # Two passes, as in who-needs-me.py: the pane set carrying a gate is computed
    # without peer-dedup, so a restating pane is dropped only when the pane it names
    # is itself a gate.
    open_panes = {str(r.get("pane")) for r in needs
                  if wnm.is_open_gate(r, task_status=wnm.task_status_from_closer)}
    blocked = [r for r in needs
               if wnm.is_open_gate(r, open_panes=open_panes, task_status=wnm.task_status_from_closer)]

    # Drop records whose pane now runs a DIFFERENT session than the one that filed
    # them. WezTerm renumbers pane ids across a restart (measured 2026-09-18: a
    # worker went 238 -> 33), so a recycled id can put a live session on a dead
    # session's gate. who-needs-me.py's name_of() reads the name off the pane's
    # CURRENT title, so such a row shows the new session's name beside the old
    # session's gate text and the jump lands on the wrong session, with nothing in
    # the output saying so — a silent wrong answer, not a dead link.
    # Fail OPEN: only a positive disagreement drops a row. An unidentifiable pane
    # keeps its row, because losing a real gate is worse than a rare wrong jump.
    owners = pane_sessions(pmap)
    def owned(r):
        cur = owners.get(str(r.get("pane")))
        return cur is None or not r.get("session_id") or cur == r["session_id"]
    kept = [r for r in blocked if owned(r)]
    dropped = len(blocked) - len(kept)

    kept.sort(key=lambda r: r["ts"], reverse=True)
    if dropped:
        print(f"   \u00b7 {dropped} stale attention row(s) hidden — their pane id now "
              f"belongs to a different session", file=sys.stderr)
    return kept


def load_visited():
    """{session_id: last-jumped-to unix ts}. Missing/corrupt file reads as empty."""
    try:
        with open(VISITED_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def record_visit(sid, visited):
    """Stamp a session as just-jumped-to. Best effort: a failed write costs rotation, not the jump."""
    if not sid:
        return
    visited[str(sid)] = time.time()
    try:
        os.makedirs(os.path.dirname(VISITED_PATH), exist_ok=True)
        tmp = VISITED_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(visited, fh)
        os.replace(tmp, VISITED_PATH)
    except OSError:
        pass


def is_fresh(rec, visited, cooldown_s, now):
    """Should this entry be offered by a bare jump right now?

    Three ways to be fresh, and the middle one is the point: a gate raised AFTER
    you last jumped there is new work, not the thing you skipped, so it comes
    back immediately rather than waiting out the cooldown.
    """
    seen = visited.get(str(rec.get("session_id")))
    if seen is None:
        return True
    if rec["ts"] > seen:
        return True
    return (now - seen) > cooldown_s


def pick_next(q, visited, cooldown_s):
    """(record, skipped_count). Never dead-ends: with everything in cooldown it
    returns the least-recently-visited entry, so repeated calls still rotate."""
    now = time.time()
    fresh = [r for r in q if is_fresh(r, visited, cooldown_s, now)]
    if fresh:
        return fresh[0], len(q) - len(fresh)
    return min(q, key=lambda r: visited.get(str(r.get("session_id")), 0)), len(q)


def resolve_number(n, tabs, pmap):
    """Bare number. Returns ("tab", id) | ("pane", id) | (None, error_message).

    A number resolving in only ONE namespace is not ambiguous — jump to it.
    The ambiguity guard is the both-exist-and-disagree case below, and nothing
    else: refusing a sole pane match printed a hint the operator then had to
    retype, for a collision that did not exist. The fleet and topic lists print
    pane ids, so bare numbers from those lists ARE pane ids.
    """
    tab_exists = n in tabs
    pane_exists = str(n) in pmap
    pane_tab = pmap[str(n)]["tab_id"] if pane_exists else None

    if tab_exists and pane_exists and pane_tab != n:
        return None, (
            f"❌ {n} is ambiguous — tab {n} is {clean_title(tabs[n]['title'])!r}, "
            f"but pane {n} is {clean_title(pmap[str(n)]['title'])!r} (tab {pane_tab}).\n"
            f"   Use `jump tab:{n}` for the tab, or `jump pane:{n}` for the pane."
        )
    if tab_exists:
        return "tab", n
    if pane_exists:
        return "pane", n
    return None, f"❌ no tab or pane {n}."


def activate_pane_by_id(pane, tabs, pmap, dry_run):
    """Activate a pane by id. The caller has already checked `pane in pmap`."""
    tid = pmap[pane]["tab_id"]
    if dry_run:
        print(f"would activate pane {pane} (tab {tid}, window {pmap[pane]['window_id']})")
        return 0
    raised = raise_pane(pane, pmap)
    if not raised:
        subprocess.call(["wezterm", "cli", "activate-pane", "--pane-id", pane])
    report(tid, tabs, False, raised)
    return 0


def resolve_name(name, tabs):
    """Case-insensitive substring match on tab titles. Refuses on 0 or >1."""
    needle = name.lower()
    hits = [tid for tid, t in tabs.items() if needle in clean_title(t["title"]).lower()]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"❌ no tab title matches {name!r}."
    listing = "\n".join(f"   tab {tid}  window {tabs[tid]['window_id']}  {clean_title(tabs[tid]['title'])}"
                        for tid in sorted(hits))
    return None, f"❌ {len(hits)} tabs match {name!r} — be more specific:\n{listing}"


def looks_like_session_ref(target):
    """True for a sessionId prefix: 6+ hex chars, dashes allowed (a full uuid)."""
    bare = target.replace("-", "")
    return len(bare) >= 6 and bool(re.fullmatch(r"[0-9a-fA-F]+", bare))


def resolve_session_ref(ref, tabs):
    """Resolve a sessionId (or its prefix) to a tab, via the session record's name.

    ⚠️ This is NOT the `[ref]` that ListAgents prints. That ref is an ephemeral
    per-connection handle: it is persisted nowhere under ~/.claude/, it is not
    derivable from the sessionId / socket / pid / name (all tested 2026-09-18),
    and it is not stable — one session read `[d1bad8]` and later `[a84cfe]` with
    the same pane, tab and sessionId throughout. So the tables must print the
    *sessionId* prefix for this to resolve; the roster ref cannot be jumped to.
    """
    hits = []
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        sid = str(d.get("sessionId") or "")
        if sid.lower().startswith(ref.lower()):
            hits.append((sid, d.get("name") or "", d.get("pid")))
    if not hits:
        return None, f"❌ no session id starts with {ref!r}."
    if len(hits) > 1:
        listing = "\n".join(f"   {sid[:8]}  pid {pid}  {name}" for sid, name, pid in hits)
        return None, f"❌ {len(hits)} sessions match {ref!r} — be more specific:\n{listing}"
    sid, name, pid = hits[0]
    if not name:
        return None, f"❌ session {sid[:8]} (pid {pid}) has no name — cannot map it to a tab."
    tid, err = resolve_name(name, tabs)
    if err:
        return None, f"❌ session {sid[:8]} is {name!r}, but {err.lstrip('❌ ')}"
    return tid, None


def main():
    ap = argparse.ArgumentParser(description="Jump to a Claude Code session's WezTerm tab.")
    ap.add_argument("target", nargs="?", help="a number (tab if tab N exists, else pane N), tab:<N>, pane:<N>, a tab-title substring, or a sessionId prefix")
    ap.add_argument("--list", action="store_true", help="print the attention queue, activate nothing")
    ap.add_argument("--dry-run", action="store_true", help="print the resolved target, activate nothing")
    ap.add_argument("--cooldown", type=float, default=COOLDOWN_DEFAULT_MIN, metavar="MIN",
                    help=f"minutes a skipped session stays out of the bare-jump rotation (default {COOLDOWN_DEFAULT_MIN})")
    ap.add_argument("--reset", action="store_true",
                    help="clear the rotation state, so every blocked session is offered again")
    a = ap.parse_args()

    if a.reset:
        try:
            os.remove(VISITED_PATH)
            print("Rotation state cleared — every blocked session is back in the queue.")
        except FileNotFoundError:
            print("Rotation state was already empty.")
        return 0

    pmap = wezterm_panes()
    if pmap is None:
        # `is None`, never `not pmap`: a reachable WezTerm holding no panes is a real
        # empty answer and must keep working. Only a failed read is a refusal — and it
        # is the read that failed, not the fleet that is empty, so say so.
        print("❌ `wezterm cli list` unreadable — the pane list failed, so no pane can "
              "be resolved. Is this session inside WezTerm, and is "
              "WEZTERM_UNIX_SOCKET set correctly?", file=sys.stderr)
        return 1
    tabs = build_tabs(pmap)

    if a.list:
        wnm = load_wnm()
        q = attention_queue(wnm, pmap)
        if not q:
            print("Nothing needs you.")
            return 0
        print(f"Needs you ({len(q)}, newest first)")
        visited = load_visited()
        cooldown_s = a.cooldown * 60
        nxt, skipped = pick_next(q, visited, cooldown_s)
        now = time.time()
        for r in q:
            pane = str(r.get("pane"))
            tid = pmap.get(pane, {}).get("tab_id")
            mark = "\u2192" if r is nxt else (" " if is_fresh(r, visited, cooldown_s, now) else "\u00b7")
            label = clean_title(wnm.name_of(r, pmap))
            print(f" {mark} tab {str(tid):>4}  {wnm.age(r['ts']):>7}  {label[:46]:<46}  "
                  f"{r['kind']}: {r['detail'][:50]}")
        if skipped:
            print(f"   \u00b7 = visited in the last {a.cooldown:g}m, out of rotation "
                  f"({skipped} of {len(q)}). --reset clears it.")
        return 0

    if a.target is None:
        wnm = load_wnm()
        q = attention_queue(wnm, pmap)
        if not q:
            print("Nothing needs you — nothing to jump to.")
            return 0
        # Rotate: a session you just jumped to drops out of the bare-jump queue for
        # --cooldown minutes, so calling jump again lands on the NEXT one. It returns
        # on its own once the cooldown lapses, or immediately if it raises a new gate.
        visited = load_visited()
        rec, skipped = pick_next(q, visited, a.cooldown * 60)
        pane = str(rec.get("pane"))
        tid = pmap.get(pane, {}).get("tab_id")
        if tid is None:
            print(f"❌ attention row has pane {pane}, not in the live pane list.", file=sys.stderr)
            return 1
        # The attention row names the exact pane — raise that, not the tab's guess.
        raised = a.dry_run or raise_pane(pane, pmap)
        if not raised:
            subprocess.call(["wezterm", "cli", "activate-pane", "--pane-id", pane])
        if not a.dry_run:
            record_visit(rec.get("session_id"), visited)
        report(tid, tabs, a.dry_run, raised)
        if skipped:
            n = len(q)
            if skipped >= n:
                print(f"   \u00b7 all {n} recently visited — this is the least-recently-seen of them")
            else:
                print(f"   \u00b7 skipped {skipped} of {n} visited in the last {a.cooldown:g}m "
                      f"(--reset to clear, --list to see them)")
        return 0

    if a.target.startswith("tab:"):
        raw = a.target.split(":", 1)[1]
        if not raw.isdigit() or int(raw) not in tabs:
            print(f"❌ no live tab {raw}.", file=sys.stderr)
            return 1
        tid = int(raw)
        rc = activate_tab(tid, tabs, a.dry_run, pmap)
        report(tid, tabs, a.dry_run, rc == 0)
        return 0

    if a.target.startswith("pane:"):
        pane = a.target.split(":", 1)[1]
        if pane not in pmap:
            print(f"❌ no live pane {pane}.", file=sys.stderr)
            return 1
        return activate_pane_by_id(pane, tabs, pmap, a.dry_run)

    if a.target.isdigit():
        kind, val = resolve_number(int(a.target), tabs, pmap)
        if kind is None:
            print(val, file=sys.stderr)
            return 1
        if kind == "pane":
            return activate_pane_by_id(str(val), tabs, pmap, a.dry_run)
        rc = activate_tab(val, tabs, a.dry_run, pmap)
        report(val, tabs, a.dry_run, rc == 0)
        return 0

    if looks_like_session_ref(a.target):
        tid, err = resolve_session_ref(a.target, tabs)
        if tid is None:
            print(err, file=sys.stderr)
            return 1
        rc = activate_tab(tid, tabs, a.dry_run, pmap)
        report(tid, tabs, a.dry_run, rc == 0)
        return 0

    tid, err = resolve_name(a.target, tabs)
    if err:
        print(err, file=sys.stderr)
        return 1
    rc = activate_tab(tid, tabs, a.dry_run, pmap)
    report(tid, tabs, a.dry_run, rc == 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
