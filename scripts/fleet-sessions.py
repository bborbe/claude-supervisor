#!/usr/bin/env python3
"""fleet-sessions — active Claude Code sessions + what each is working on.

Liveness signal = last-message timestamp of each session transcript
(~/.claude/projects/<escaped-cwd>/<session-id>.jsonl). This covers *fresh*
sessions too (they write a transcript even without --resume in `ps`), which the
ps-only approach missed.

Work mapping = vault `claude_session_id:` frontmatter stamps → task/goal title.
Live-process flag = the session id appears in a running `claude --resume <id>`.

Spawn mode + attribution = the supervisor's spawn ledger
(`SUPERVISOR_LEDGER_DIR`, default `$XDG_STATE_HOME|~/.local/state/claude-supervisor/sessions`),
joined to the roster on the session id. The ledger is the only store that records the
spawn edge — mode, the manager that spawned it, the label it was given, the launcher —
so without it a headless worker and a human tab render identically and no row can say
whose work it is. A session the ledger does not know renders `unknown` for both, never
a blank and never a guess: an absent record means *not recorded*, which is a different
claim from *not spawned*.

The two spawn counts are derived at render time from `spawned_at` over **two** buckets,
because `spawned_at` is UTC-only and the day boundary therefore has two defensible
readings. Both are printed and each is labelled, rather than one being silently chosen.
No count in this view rests on the ledger's `status` / `ended_at` — those do not close
reliably for any mode, so they are not a liveness source (see ledger.mjs).

The fleet is ALWAYS every project — every transcript, newest first, no time
filter. There is no scope to get wrong, so a sweep can never silently narrow
and read an out-of-scope live session as gone.

Usage: fleet-sessions.py [ignored flags]
  Arguments are accepted and ignored. Before 2026-09-19 the script scoped to
  the current working directory's project unless `--all` widened it; the
  operator rejected that default outright — a fleet sweep must always cover
  every live session on the machine, regardless of which vault created it.
  The retired flags (`--all`, `--minutes N`, `--vault NAME`) are still
  tolerated rather than rejected so callers that pass them keep working.
"""
from __future__ import annotations
import calendar, json, os, re, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Both ambient roots resolve PER CALL rather than at import, the way
# `session-liveness.py`'s `_start_cache_path()` does. The documented run command
# (`python3 -m unittest discover -s scripts/tests`) loads every test module into ONE
# process, so an import-time read is already fixed by the time any suite could isolate
# it — and a suite that reads the machine's real roots answers for the host rather than
# for the code. On a clean CI runner neither root exists, so `main()` printed
# `no ~/.claude/projects` and every render assertion then failed against that string
# instead of against the render.

def projects_dir() -> Path:
    """Transcript root. `SUPERVISOR_PROJECTS_DIR` overrides; default `~/.claude/projects`.

    The override is the repo's established name for this root — `server/config.mjs`
    reads the same key — so the reader and the server resolve one root, not two.
    """
    override = os.environ.get("SUPERVISOR_PROJECTS_DIR")
    return Path(os.path.expanduser(override)) if override else HOME / ".claude" / "projects"

def obsidian_dir() -> Path:
    """Vault root. `OBSIDIAN_DIR` overrides; default `~/Documents/Obsidian`."""
    override = os.environ.get("OBSIDIAN_DIR")
    return Path(os.path.expanduser(override)) if override else HOME / "Documents" / "Obsidian"

# Retired scoping flags — `--all`, `--minutes N` and `--vault NAME` are accepted
# and ignored (removed 2026-09-19). They used to narrow the roster to a single
# project and a time window; the fleet is now always every project, newest
# first, with no time filter, so there is nothing left for them to select.
# Tolerated rather than rejected so callers that still pass them keep working —
# the Fleet Manager Session runbook passes `--all`.

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

def _walk_task_stamps():
    """Every `(session_id, path, text)` a vault task/goal file stamps.

    A generator, so the readers below share one pass over the vaults. They used
    to walk independently, which was pure duplication: measured 2026-10-01 over
    1834 stamped sessions, `build_work_map()` took 2.90s and `build_task_meta()`
    a further 3.57s over the same trees.
    """
    pat = re.compile(r'claude_session_id:\s*["\']?(' + UUID_RE.pattern + ')')
    known = vault_dirs_from_cli()
    root = obsidian_dir()
    if not root.is_dir(): return
    for vault in root.iterdir():
        if not vault.is_dir(): continue
        for sub in known.get(os.path.realpath(vault)) or PROBE_DIRS:
            d = vault / sub
            if not d.is_dir(): continue
            for f in d.glob("*.md"):
                try:
                    text = f.read_text("utf-8", "replace")
                except Exception:
                    continue
                m = pat.search(text[:600])
                if m:
                    yield m.group(1), f, text


def build_work_map() -> dict[str, list[str]]:
    """session-id -> [task/goal titles] from vault frontmatter stamps."""
    out: dict[str, list[str]] = {}
    for sid, f, _ in _walk_task_stamps():
        out.setdefault(sid, []).append(f.stem)
    return out


def build_task_index() -> tuple[dict[str, list[str]], dict[str, dict]]:
    """One walk, both outputs: `(titles_by_sid, meta_by_sid)`.

    The board needs both, and calling the two readers separately walked every
    vault twice for the same data — measured 6.47s against 3.6s for one pass.
    Reading the whole file here costs the title-only caller nothing measurable:
    the read is the same syscall on files this size, and it is the duplicated
    *walk* that was expensive.
    """
    titles: dict[str, list[str]] = {}
    meta: dict[str, dict] = {}
    for sid, f, text in _walk_task_stamps():
        titles.setdefault(sid, []).append(f.stem)
        status = _TASK_STATUS.search(text)
        phase = _TASK_PHASE.search(text)
        meta.setdefault(sid, {
            "title": f.stem,
            "path": str(f),
            "status": status.group(1) if status else "",
            "phase": phase.group(1) if phase else "",
            "open_boxes": len(_OPEN_BOX.findall(text)),
        })
    return titles, meta

_TASK_STATUS = re.compile(r"^status:\s*[\"']?([A-Za-z_]+)", re.M)
_TASK_PHASE = re.compile(r"^phase:\s*[\"']?([A-Za-z_]+)", re.M)
# The vault's own open-box convention: `- [ ]` unchecked, `- [/]` in progress.
_OPEN_BOX = re.compile(r"^\s*-\s*\[[ /]\]", re.M)


def build_task_meta() -> dict[str, dict]:
    """session-id -> the stamped task's own state, for the board's `Unblocks` column.

    `build_work_map()` answers only *which* task a session stamped. The
    classification also needs that task's state — `status` for the reap/close
    test, `phase` plus its open-box count for the nudge test — so this walks the
    same vaults, matches the same stamp, and keeps a richer record.

    A sibling rather than a widening of `build_work_map()`: that one feeds the
    roster's title column, and its callers would have to learn a new shape for
    no gain.

    First stamp wins, and the directory order is what makes that meaningful —
    `vault_dirs_from_cli()` lists `tasks_dir` before `goals_dir`, and
    `PROBE_DIRS` puts the task folders first, so a session that stamped both a
    task and a goal anchors on the task.
    """
    return build_task_index()[1]


def ledger_dir() -> Path:
    """The spawn ledger directory, resolved from the writer's own env override.

    Mirrors `server/config.mjs`: `SUPERVISOR_LEDGER_DIR` wins, else
    `$XDG_STATE_HOME|~/.local/state` + `/claude-supervisor/sessions`. Resolved
    rather than hardcoded so a relocated ledger (or an isolated test run) is read
    without editing this file, and so the reader cannot drift from the writer.

    Deliberately NOT `SUPERVISOR_SESSIONS_DIR`: that name already means the live
    registry (`~/.claude/sessions`), a different store with a different lifetime.
    """
    override = os.environ.get("SUPERVISOR_LEDGER_DIR")
    if override:
        return Path(os.path.expanduser(override))
    state = os.environ.get("XDG_STATE_HOME") or str(HOME / ".local" / "state")
    return Path(os.path.expanduser(state)) / "claude-supervisor" / "sessions"

def read_ledger() -> dict[str, dict]:
    """session-id -> spawn record. A missing or unreadable store is `{}`.

    Absence degrades the two new columns to `unknown`; it never aborts the
    roster, which is the view's primary job and does not depend on the ledger.
    """
    out: dict[str, dict] = {}
    try:
        for f in ledger_dir().glob("*.json"):
            try:
                rec = json.loads(f.read_text("utf-8", "replace"))
            except Exception:
                continue
            sid = rec.get("session_id") or f.stem
            if sid:
                out[sid] = rec
    except Exception:
        return {}
    return out

def _parse_spawned_at(value) -> datetime | None:
    """Parse the ledger's ISO 8601 `spawned_at`; None when absent or unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

def spawn_counts(records: dict[str, dict], now_ts: float) -> tuple[int, int]:
    """(spawned today UTC, spawned today local) over `spawned_at`, derived now.

    Two buckets because `spawned_at` is UTC-only and the day boundary has two
    defensible readings; printing both labelled beats silently picking one.
    Computed from the records on every render — never cached, never a constant.
    """
    utc_day = datetime.fromtimestamp(now_ts, timezone.utc).date()
    local_day = datetime.fromtimestamp(now_ts).date()
    utc_n = local_n = 0
    for rec in records.values():
        dt = _parse_spawned_at(rec.get("spawned_at"))
        if dt is None:
            continue
        if dt.astimezone(timezone.utc).date() == utc_day:
            utc_n += 1
        if dt.astimezone().date() == local_day:
            local_n += 1
    return utc_n, local_n

# An absent ledger record is a *different* claim from "not spawned": the spawn edge
# was never recorded for this session. Rendered explicitly so the operator can tell
# it apart from a value the ledger actually carries.
UNKNOWN = "unknown"

def spawn_mode(rec: dict | None) -> str:
    """The ledger's `mode` for this session, or `unknown` when it has no record."""
    mode = (rec or {}).get("mode")
    return mode if mode else UNKNOWN

def attribution(rec: dict | None) -> str:
    """Which manager spawned this session, and under what label.

    Composed from the spawn edge the ledger records: `parent_session` (the manager
    that called spawn_agent), `agent_id` (its handle in that manager) and `label`.
    Falls back to `unknown` when there is no record, and to whichever of the three
    is present when the others are not — never a guessed default.
    """
    if not rec:
        return UNKNOWN
    parent = (rec.get("parent_session") or "")[:8]
    agent = rec.get("agent_id") or ""
    label = rec.get("label") or ""
    if parent and label:
        return f"{parent} {label}"
    if parent and agent:
        return f"{parent} {agent}"
    if label:
        return label
    if parent:
        return parent
    if agent:
        return agent
    return UNKNOWN

def live_processes() -> tuple[int, set[str]] | None:
    """Returns (count of running `claude --model` sessions, resumed-session ids).

    Definitive per-PID → session mapping isn't available: a process keeps neither
    its transcript open nor the session id in env/argv (except --resume), and cwd
    is shared across sessions so it can't disambiguate. So `●` = exact --resume
    match only; for everyone else, transcript recency is the liveness signal.

    `None` when the `ps` failed — never `(0, set())`. A zero here is a measurement
    the caller prints as fact, so a failed read must not be able to produce one.
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
        return None
    return count, resumed

def human_age(sec: float) -> str:
    if sec < 90: return f"{int(sec)}s ago"
    if sec < 5400: return f"{int(sec/60)}m ago"
    if sec < 172800: return f"{int(sec/3600)}h ago"
    return f"{int(sec/86400)}d ago"

def render_row(age: float, proj: str, sid: str, working: str, live: bool, rec: dict | None) -> str:
    """One roster row: the five original columns, then mode and attribution.

    The new pair is appended rather than interleaved so the first five columns
    keep their positions and widths — `fleet-status` and `fleet-loop` parse
    `SESSION` and `WORKING ON` by name against them.
    """
    flag = "●" if live else " "
    return (f"{human_age(age):<12} {proj:<12} {flag:<4} {sid[:8]:<10} "
            f"{working[:43]:<44} {spawn_mode(rec):<11} {attribution(rec)}")

def main():
    now = time.time()
    projects = projects_dir()
    if not projects.is_dir():
        print("no ~/.claude/projects"); return
    work = build_work_map()
    ledger = read_ledger()
    live = live_processes()
    proc_count, resumed = (None, set()) if live is None else live

    rows = []
    for jsonl in projects.glob("*/*.jsonl"):
        sid = jsonl.stem
        if not UUID_RE.fullmatch(sid): continue
        proj = project_label(jsonl.parent.name)
        rows.append((now - last_message_ts(jsonl), proj, sid))

    rows.sort(key=lambda r: r[0])
    print(f"{'LAST-ACTIVE':<12} {'PROJECT':<12} {'LIVE':<4} {'SESSION':<10} "
          f"{'WORKING ON':<44} {'SPAWN MODE':<11} ATTRIBUTION")
    for age, proj, sid in rows:
        titles = work.get(sid, [])
        titles = sorted(titles, key=lambda t: (t.startswith(("Start Day", "Check", "Cleanup", "Feed", "Turn")), t))
        working = titles[0] if titles else "—"
        print(render_row(age, proj, sid, working, sid in resumed, ledger.get(sid)))
    utc_n, local_n = spawn_counts(ledger, now)
    count_field = ("⚠️ `ps` unreadable — live `claude` session count withheld, not zero"
                   if proc_count is None
                   else f"{proc_count} live `claude` sessions (ps, machine-wide)")
    print(f"\n{count_field} · scope: all projects · "
          f"● = confirmed via --resume · "
          f"age = last transcript message (liveness proxy; <~5m ≈ active, hours ≈ stale)")
    print(f"ledger: {len(ledger)} records at {ledger_dir()} · "
          f"{len(rows) - sum(1 for _, _, sid in rows if sid in ledger)} roster rows with no ledger record "
          f"(rendered `{UNKNOWN}`) · spawn mode + attribution are ledger-sourced, never inferred")
    print(f"spawned today (UTC): {utc_n}")
    print(f"spawned today (local): {local_n}")

if __name__ == "__main__":
    main()
