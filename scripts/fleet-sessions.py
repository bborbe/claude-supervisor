#!/usr/bin/env python3
"""fleet-sessions — active Claude Code sessions + what each is working on.

Liveness signal = last-message timestamp of each session transcript
(~/.claude/projects/<escaped-cwd>/<session-id>.jsonl). This covers *fresh*
sessions too (they write a transcript even without --resume in `ps`), which the
ps-only approach missed.

Work mapping = vault `claude_session_id:` frontmatter stamps → task/goal title.
Live-process flag = the session id appears in a running `claude --resume <id>`.

Scope defaults to the CURRENT working directory's project, so a work-vault
session never surfaces personal sessions unless asked. `--all` widens to every
project (and drops the time filter); `--vault NAME` scopes somewhere else.

Usage: fleet-sessions.py [--minutes N] [--all] [--vault NAME]
  --minutes N   only sessions active in the last N min (default 180)
  --all         no filters at all: every project, every transcript, newest
                first. Backward compatible — before scoping existed, `--all`
                already meant "no time filter, all projects".
  --vault NAME  scope to projects matching NAME instead of the current cwd
                (case-insensitive substring of the PROJECT label, e.g.
                `brogrammers`, `personal`, `ws:coding`). Repeatable.
                Overrides the cwd default; ignored when `--all` is given.
"""
from __future__ import annotations
import calendar, json, os, re, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
OBSIDIAN = Path(os.environ.get("OBSIDIAN_DIR", HOME / "Documents" / "Obsidian"))
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

def parse_args(argv):
    minutes, show_all, vaults = 180, False, []
    i = 0
    while i < len(argv):
        if argv[i] == "--all": show_all = True
        elif argv[i] == "--minutes" and i + 1 < len(argv):
            minutes = int(argv[i + 1]); i += 1
        elif argv[i] == "--vault" and i + 1 < len(argv):
            vaults.append(argv[i + 1].lower()); i += 1
        i += 1
    return minutes, show_all, vaults

def cwd_scope() -> str | None:
    """PROJECT label of the current working directory, or None if unmappable.

    Claude Code stores transcripts under ~/.claude/projects/<escaped-cwd>/, so
    the cwd maps onto the same label space the rows use.
    """
    try:
        return project_label(str(Path.cwd()).replace("/", "-")).lower()
    except Exception:
        return None

def last_message_ts(jsonl: Path) -> float:
    """Timestamp of the last transcript line; fall back to file mtime."""
    try:
        with jsonl.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line: continue
            try:
                ts = json.loads(line).get("timestamp")
            except Exception:
                continue
            if ts:
                # ISO 8601 UTC, e.g. 2026-07-17T12:41:00.000Z — parse as UTC (timegm)
                return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        pass
    return jsonl.stat().st_mtime

def project_label(dirname: str) -> str:
    m = re.search(r"Obsidian-([A-Za-z0-9]+)", dirname)
    if m: return m.group(1)
    m = re.search(r"workspaces-([A-Za-z0-9._-]+)", dirname)
    if m: return "ws:" + m.group(1)[:16]
    return dirname.strip("-").split("-")[-1][:16]

# Probed only for vaults absent from the vault-cli config (e.g. the assistant
# vaults). Both generations listed so an unconfigured vault survives a rename.
PROBE_DIRS = ("25 Tasks", "24 Tasks", "24 Goals", "23 Goals", "tasks")

def vault_dirs_from_cli() -> dict[str, list[str]]:
    """vault path -> [tasks_dir, goals_dir], read from vault-cli's config.

    vault-cli's config is the single source of truth for where each vault keeps
    its tasks and goals, so a vault folder rename (2026-09-13: `23 Goals` ->
    `24 Goals`, `24 Tasks` -> `25 Tasks`) needs no edit here. Vaults missing
    from the config fall back to PROBE_DIRS. Any failure returns {} so the
    caller degrades to probing rather than losing the work map entirely.
    """
    try:
        res = subprocess.run(
            ["vault-cli", "--output", "json", "config", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode != 0:
            return {}
        dirs: dict[str, list[str]] = {}
        for v in json.loads(res.stdout):
            path = os.path.expanduser(v.get("path") or "")
            subs = [v[k] for k in ("tasks_dir", "goals_dir") if v.get(k)]
            if path and subs:
                dirs[os.path.realpath(path)] = subs
        return dirs
    except Exception:
        return {}

def build_work_map() -> dict[str, list[str]]:
    """session-id -> [task/goal titles] from vault frontmatter stamps."""
    out: dict[str, list[str]] = {}
    pat = re.compile(r'claude_session_id:\s*["\']?(' + UUID_RE.pattern + ')')
    known = vault_dirs_from_cli()
    for vault in OBSIDIAN.iterdir():
        if not vault.is_dir(): continue
        for sub in known.get(os.path.realpath(vault)) or PROBE_DIRS:
            d = vault / sub
            if not d.is_dir(): continue
            for f in d.glob("*.md"):
                try:
                    head = f.read_text("utf-8", "replace")[:600]
                except Exception:
                    continue
                m = pat.search(head)
                if m:
                    out.setdefault(m.group(1), []).append(f.stem)
    return out

def live_processes() -> tuple[int, set[str]]:
    """Returns (count of running `claude --model` sessions, resumed-session ids).

    Definitive per-PID → session mapping isn't available: a process keeps neither
    its transcript open nor the session id in env/argv (except --resume), and cwd
    is shared across sessions so it can't disambiguate. So `●` = exact --resume
    match only; for everyone else, transcript recency is the liveness signal.
    """
    count, resumed = 0, set()
    try:
        ps = subprocess.run(["ps", "-axww", "-o", "args="], capture_output=True, text=True).stdout
        for line in ps.splitlines():
            # argv is `claude --settings {…} --model …`, so `--model` need not
            # directly follow `claude`; require both, not the literal pair.
            if "claude" not in line or "--model" not in line: continue
            count += 1
            m = re.search(r"--resume\s+(" + UUID_RE.pattern + ")", line)
            if m: resumed.add(m.group(1))
    except Exception:
        pass
    return count, resumed

def human_age(sec: float) -> str:
    if sec < 90: return f"{int(sec)}s ago"
    if sec < 5400: return f"{int(sec/60)}m ago"
    if sec < 172800: return f"{int(sec/3600)}h ago"
    return f"{int(sec/86400)}d ago"

def main():
    minutes, show_all, vaults = parse_args(sys.argv[1:])
    # Default scope = current working directory. --all clears it; --vault replaces it.
    implicit = False
    if show_all:
        vaults = []
    elif not vaults:
        c = cwd_scope()
        if c:
            vaults, implicit = [c], True
    now = time.time()
    if not PROJECTS.is_dir():
        print("no ~/.claude/projects"); return
    work = build_work_map()
    proc_count, resumed = live_processes()

    rows = []
    for jsonl in PROJECTS.glob("*/*.jsonl"):
        sid = jsonl.stem
        if not UUID_RE.fullmatch(sid): continue
        proj = project_label(jsonl.parent.name)
        if vaults and not any(v in proj.lower() for v in vaults): continue
        ts = last_message_ts(jsonl)
        age = now - ts
        if not show_all and sid not in resumed and age > minutes * 60: continue
        rows.append((age, proj, sid))

    rows.sort(key=lambda r: r[0])
    print(f"{'LAST-ACTIVE':<12} {'PROJECT':<12} {'LIVE':<4} {'SESSION':<10} WORKING ON")
    for age, proj, sid in rows:
        titles = work.get(sid, [])
        titles = sorted(titles, key=lambda t: (t.startswith(("Start Day", "Check", "Cleanup", "Feed", "Turn")), t))
        working = titles[0] if titles else "—"
        flag = "●" if sid in resumed else " "
        print(f"{human_age(age):<12} {proj:<12} {flag:<4} {sid[:8]:<10} {working}")
    if vaults:
        scope = f" · scope: {', '.join(vaults)}"
        scope += " (cwd default — pass --all for every project)" if implicit else ""
    else:
        scope = " · scope: all projects"
    print(f"\n{proc_count} live `claude` sessions (ps, machine-wide){scope} · "
          f"● = confirmed via --resume · "
          f"age = last transcript message (liveness proxy; <~5m ≈ active, hours ≈ stale)")

if __name__ == "__main__":
    main()
