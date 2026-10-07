#!/usr/bin/env python3
"""plugin-version-census — which supervisor plugin version each LIVE session is serving.

A plugin command body is read from disk when the session starts, so installing a newer
plugin does not reach a session that is already running. The per-session probe is
documented in `docs/fleet-surface.md` § *A plugin install does not reach a running
session — the probe and the lever*; what did not exist is the **fleet** reading: how
many live sessions are behind, which ones, and how to reach each.

  (default)   human report — `installed: <ver> · live: N · stale: S`, then one line per
              stale session: `<sid8>  loaded <ver>  <name>` plus its jump target
  --json      the same rows as JSON, for a caller that renders its own table
  --reload    THE LEVER — for every stale session whose pane resolves, activate the pane
              and type `/reload-plugins`, then re-read the markers and print the delta

⚠️ **The marker count is not the session count, and reading it as one is the trap this
script exists to avoid.** The harness writes `.../<version>/.in_use/<pid>` at session
start, and a reload writes a NEW entry **and never removes the old one**, so one pid sits
in several version directories at once (measured 2026-10-07: pid 25483 in 5, pid 56704 in
9). Counting markers over the whole cache reported **17 versions / 81 markers** against a
fleet of **44 live sessions across 8 versions** — a five-fold overstatement, in the
direction that looks like more evidence. The reading is therefore **the newest marker
carrying a live pid**, newest by mtime, which is the same `ls -dt … | head -1` rule the
probe uses; `-t` is load-bearing there and it is load-bearing here.

⚠️ **Liveness comes from `session-liveness.py`, never from the cache.** The cache keeps a
marker for every session that has ever run, so it cannot answer "is this one live"; the
registry deletes an entry when its session exits. A second registry reader is the exact
defect `session-liveness.py`'s own header documents, so this script shells out to it and
holds no reading of its own.

⚠️ **A session with no marker is `unknown`, never `stale`.** A headless worker is an
in-process `query()` and a cluster worker runs in a pod on another machine; neither
necessarily holds a local `.in_use` entry. Reporting them stale would name a fix for a
session the lever cannot even reach, and "no marker" is a fact about the probe.

⚠️ **`--reload` types into panes, so it steals focus and only reaches a TAB session.** It
is the one mutating mode; the census itself is read-only. A session with no resolvable
pane is skipped and named, never guessed at.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SESSIONS = os.path.expanduser("~/.claude/sessions")
DEFAULT_CACHE = os.path.expanduser("~/.claude/plugins/cache")
DEFAULT_INSTALLED = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
DEFAULT_MARKETPLACE = "claude-supervisor"
DEFAULT_PLUGIN = "supervisor"
LIVENESS = os.path.join(HERE, "session-liveness.py")

# Mirrors server/tab.mjs: readiness is the composer glyph, not a fixed sleep.
COMPOSER = "❯"  # ❯
RELOAD_READY_TIMEOUT = 15.0
RELOAD_SETTLE_TIMEOUT = 30.0


def installed_version(path=DEFAULT_INSTALLED, plugin=DEFAULT_PLUGIN, marketplace=DEFAULT_MARKETPLACE):
    """The version the next session to start will load, or None when unreadable.

    Read from `installPath`'s basename rather than the sibling `version` key: the path is
    what the cache actually holds, and the two disagreeing would make the census compare
    against a number no directory carries.
    """
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    entries = (data.get("plugins") or {}).get(f"{plugin}@{marketplace}") or []
    if not entries:
        return None
    install = entries[0].get("installPath") or ""
    return os.path.basename(install.rstrip("/")) or None


def newest_marker(cache_dir, marketplace, plugin, pid):
    """(version, mtime) for the newest `.in_use/<pid>` under the plugin's cache, else None."""
    pattern = os.path.join(cache_dir, marketplace, plugin, "*", ".in_use", str(pid))
    hits = []
    for path in glob.glob(pattern):
        try:
            hits.append((os.path.getmtime(path), path.split(os.sep)[-3]))
        except OSError:
            continue
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0][1], hits[0][0]


def live_sessions(liveness=LIVENESS):
    """The live-session records, from `session-liveness.py --list --json`.

    Returns (records, error). An unreadable probe is an error, never an empty list: the
    census would then report `live: 0 · stale: 0`, which reads as a healthy fleet.
    """
    if not os.path.exists(liveness):
        return None, f"session-liveness.py not found at {liveness}"
    try:
        proc = subprocess.run(
            [sys.executable, liveness, "--list", "--json"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run session-liveness.py: {exc}"
    if proc.returncode != 0:
        return None, f"session-liveness.py exited {proc.returncode}: {proc.stderr.strip()}"
    try:
        return json.loads(proc.stdout), None
    except ValueError as exc:
        return None, f"session-liveness.py emitted unparseable JSON: {exc}"


def census(sessions, installed, cache_dir=DEFAULT_CACHE, marketplace=DEFAULT_MARKETPLACE,
           plugin=DEFAULT_PLUGIN):
    """One row per live session: what it serves, and whether that is the installed version.

    `state` is `current` / `stale` / `unknown`. `unknown` is a session with no marker —
    a different fact from stale, and the one a caller must not fold into it.
    """
    rows = []
    for session in sessions:
        pid = session.get("pid")
        marker = newest_marker(cache_dir, marketplace, plugin, pid) if pid else None
        loaded, mtime = marker if marker else (None, None)
        if loaded is None:
            state = "unknown"
        elif installed is not None and loaded != installed:
            state = "stale"
        else:
            state = "current"
        rows.append({
            "session_id": session.get("sessionId") or "",
            "sid8": (session.get("sessionId") or "")[:8],
            "name": session.get("name") or "",
            "pid": pid,
            "loaded": loaded,
            "marker_mtime": mtime,
            "state": state,
        })
    rows.sort(key=lambda r: (r["state"] != "stale", r["loaded"] or "", r["name"]))
    return rows


def wezterm_panes():
    """{pane_id(str): {"tty": tty_name}} or None when the query could not run.

    None, never {}: a failed `wezterm cli list` cannot prove a pane is absent any more
    than it can prove one is live, so no caller may read it as "nothing to reach".
    """
    try:
        proc = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return {str(p["pane_id"]): {"tty": p.get("tty_name") or ""} for p in json.loads(proc.stdout)}
    except (ValueError, KeyError):
        return None


def _pid_ttys(pids):
    """{pid(int): tty(str)} for the pids `ps` knows about, or None when `ps` could not run.

    Three states, as every transport read in this repo must have: None for a query that
    failed, `{}` for a `ps` that answered with nothing, a map for content. Collapsing the
    first into the second would report "no session has a terminal" — a reading that looks
    like a fleet with nothing to reach.
    """
    if not pids:
        return {}
    try:
        proc = subprocess.run(["ps", "-o", "pid=,tty=", "-p", ",".join(str(p) for p in pids)],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    out = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            out[int(parts[0])] = parts[1]
    return out


def panes_for_pids(pids, panes=None):
    """{pid(int): pane_id(str)} — the join is the tty, which both `ps` and wezterm carry."""
    panes = wezterm_panes() if panes is None else panes
    if panes is None:
        return {}
    by_tty = {}
    for pane_id, info in panes.items():
        tty = (info.get("tty") or "").replace("/dev/", "")
        if tty:
            by_tty.setdefault(tty, pane_id)
    ttys = _pid_ttys(pids)
    if ttys is None:
        return {}
    return {pid: by_tty[tty] for pid, tty in ttys.items() if tty in by_tty}


def reload_pane(pane_id, marker_path):
    """Type `/reload-plugins` into an idle pane and confirm the marker moved.

    Activate first: `send-text` reaches a pane only once its tab is active (measured
    2026-09-07 in `server/tab.mjs`, and again here). Readiness is the composer glyph, not
    a sleep. Success is the marker's mtime advancing past the value read before the send —
    the pane's own echo is not evidence the reload ran.
    """
    before = None
    if marker_path and os.path.exists(marker_path):
        before = os.path.getmtime(marker_path)

    try:
        activated = subprocess.run(["wezterm", "cli", "activate-pane", "--pane-id", str(pane_id)],
                                   capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"pane": pane_id, "ok": False, "why": f"activate-pane failed: {exc}"}
    if activated.returncode != 0:
        return {"pane": pane_id, "ok": False,
                "why": f"activate-pane exited {activated.returncode}: {activated.stderr.strip()}"}

    deadline = time.time() + RELOAD_READY_TIMEOUT
    while time.time() < deadline:
        try:
            text = subprocess.run(["wezterm", "cli", "get-text", "--pane-id", str(pane_id)],
                                  capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
        if COMPOSER in text:
            break
        time.sleep(1)
    else:
        return {"pane": pane_id, "ok": False, "why": "no composer glyph — pane not at a prompt"}

    try:
        sent = subprocess.run(["wezterm", "cli", "send-text", "--pane-id", str(pane_id),
                               "--no-paste", "/reload-plugins\r"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"pane": pane_id, "ok": False, "why": f"send-text failed: {exc}"}
    if sent.returncode != 0:
        return {"pane": pane_id, "ok": False,
                "why": f"send-text exited {sent.returncode}: {sent.stderr.strip()}"}

    if before is None:
        # No marker to compare against: the send landed, the reload cannot be confirmed.
        return {"pane": pane_id, "ok": False, "why": "no marker before the send — unverified"}

    deadline = time.time() + RELOAD_SETTLE_TIMEOUT
    while time.time() < deadline:
        try:
            if os.path.getmtime(marker_path) > before:
                return {"pane": pane_id, "ok": True, "why": "marker advanced"}
        except OSError:
            pass
        time.sleep(1)
    return {"pane": pane_id, "ok": False, "why": "marker did not advance — reload unconfirmed"}


def render(rows, installed):
    stale = [r for r in rows if r["state"] == "stale"]
    unknown = [r for r in rows if r["state"] == "unknown"]
    head = f"installed: {installed or 'unknown'} · live: {len(rows)} · stale: {len(stale)}"
    if unknown:
        head += f" · unknown: {len(unknown)}"
    lines = [head]
    for row in stale:
        jump = row.get("jump") or "—"
        lines.append(f"  {row['sid8']}  loaded {row['loaded']}  {row['name']}  {jump}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Which supervisor plugin version is each live session serving?")
    parser.add_argument("--json", action="store_true", help="emit the rows as JSON")
    parser.add_argument("--reload", action="store_true",
                        help="THE LEVER: type /reload-plugins into every stale session's pane, then re-read")
    parser.add_argument("--marketplace", default=DEFAULT_MARKETPLACE)
    parser.add_argument("--plugin", default=DEFAULT_PLUGIN)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE)
    parser.add_argument("--installed-json", default=DEFAULT_INSTALLED)
    parser.add_argument("--sessions-json", default=None,
                        help="read the live-session records from this file instead of session-liveness.py")
    parser.add_argument("--no-panes", action="store_true", help="skip pane resolution (no jump targets)")
    args = parser.parse_args(argv)

    installed = installed_version(args.installed_json, args.plugin, args.marketplace)
    if args.sessions_json:
        try:
            with open(args.sessions_json) as handle:
                sessions = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"could not read {args.sessions_json}: {exc}", file=sys.stderr)
            return 2
    else:
        sessions, error = live_sessions()
        if error:
            print(error, file=sys.stderr)
            return 2

    rows = census(sessions, installed, args.cache_dir, args.marketplace, args.plugin)

    stale = [r for r in rows if r["state"] == "stale"]
    if stale and not args.no_panes:
        panes = panes_for_pids([r["pid"] for r in stale if r["pid"]])
        for row in stale:
            row["pane"] = panes.get(row["pid"])
            row["jump"] = f"/supervisor:jump {row['pane']}" if row["pane"] else None

    if args.reload:
        reached = 0
        for row in stale:
            pane = row.get("pane")
            if not pane:
                row["reload"] = {"ok": False, "why": "no resolvable pane"}
                continue
            marker = os.path.join(args.cache_dir, args.marketplace, args.plugin,
                                  row["loaded"], ".in_use", str(row["pid"]))
            row["reload"] = reload_pane(pane, marker)
            reached += 1 if row["reload"]["ok"] else 0
        if not args.json:
            for row in stale:
                result = row.get("reload") or {}
                verdict = "reloaded" if result.get("ok") else "not reloaded"
                print(f"  {row['sid8']}  pane {row.get('pane') or '—'}  {verdict}  ({result.get('why')})")
        # Re-read after the lever so the delta is measured, not inferred.
        rows = census(sessions, installed, args.cache_dir, args.marketplace, args.plugin)
        if not args.json:
            print(f"lever: reached {reached} pane(s)")

    if args.json:
        print(json.dumps({"installed": installed, "rows": rows}, indent=2))
        return 0
    print(render(rows, installed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
