#!/usr/bin/env python3
"""plugin-version-census — which supervisor plugin version each LIVE session is serving.

A plugin command body is read from disk when the session starts, so installing a newer
plugin does not reach a session that is already running. The per-session probe is
documented in `docs/fleet-surface.md` § *A plugin install does not reach a running
session — the probe and the lever*; what did not exist is the **fleet** reading: how
many live sessions are behind, which ones, and how to reach each.

  (default)   human report — `installed: <ver> · live: N · stale: S` (plus `· unknown: N`
              when any live session has no marker), then one line per stale session:
              `<sid8>  loaded <ver>  <name>` plus its jump target
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

⚠️ **`unknown` is never `stale`, and it covers two different facts.** A session with no
marker: a headless worker is an in-process `query()` and a cluster worker runs in a pod on
another machine, so neither necessarily holds a local `.in_use` entry, and reporting them
stale would name a fix for a session the lever cannot even reach. And an **unreadable
installed version**: with no baseline to compare against, every session would read
`current` and the header would print `stale: 0` — a clean fleet derived from a probe that
could not read its own reference.

⚠️ **`--reload` types into panes, so it steals focus and only reaches a TAB session.** It
is the one mutating mode; the census itself is read-only. A session with no resolvable pane
is skipped and named, never guessed at. ⚠️ **Success is the newest marker for that pid
moving** — never the pre-reload version directory's mtime, which a reload never touches.
"""
import argparse
import glob
import json
import os
import re
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

# SGR / charset escapes, so the glyph can be matched on the visible text of a line read with
# `get-text --escapes` — the placeholder hint is only distinguishable from typed text by its
# styling, and that styling is not in the plain read.
_SGR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\([A-Za-z]")
_SGR_ATTRS = re.compile(r"\x1b\[([0-9;]*)m")


def _plain(line):
    """The line's visible text, with escape sequences removed."""
    return _SGR.sub("", line)


def _is_dim(segment):
    """True when any SGR sequence in `segment` carries attribute 2 (faint).

    Read as a parameter list rather than a substring: `2` must be one whole attribute, so
    `[0;2m` is dim and `[0;24m` (underline off) is not.
    """
    return any("2" in attrs.split(";") for attrs in _SGR_ATTRS.findall(segment))


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
    # `entries[0]` is the only record this key carries in practice (one marketplace, one
    # install). A second would mean two install paths for one plugin@marketplace, which the
    # harness does not produce — and if it ever did, the newest `lastUpdated` would be the
    # one a session loads. Named rather than silently first-wins.
    install = entries[0].get("installPath") or ""
    return os.path.basename(install.rstrip("/")) or None


def newest_marker(cache_dir, marketplace, plugin, pid):
    """(version, mtime) for the newest `.in_use/<pid>` under the plugin's cache, else None.

    Newest **by mtime alone**. Sorting `(mtime, version)` tuples would break an mtime tie
    lexicographically on the version string, so under coarse filesystem mtime granularity
    two markers written in the same tick would resolve to `0.9.0` over `0.114.1` — a wrong
    answer that reads as a measurement. A tie on mtime is genuinely ambiguous, so the tie
    is broken by the largest mtime and nothing else.
    """
    pattern = os.path.join(cache_dir, marketplace, plugin, "*", ".in_use", str(pid))
    hits = []
    for path in glob.glob(pattern):
        try:
            hits.append((os.path.getmtime(path), path.split(os.sep)[-3]))
        except OSError:
            continue
    if not hits:
        return None
    mtime, version = max(hits, key=lambda hit: hit[0])
    return version, mtime


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

    `state` is `current` / `stale` / `unknown`. `unknown` covers two different facts a
    caller must not fold into the others: a session with **no marker** (a headless or
    cluster worker), and an **unreadable installed version**. The second is the collapse
    `live_sessions()` refuses one level up — with no baseline to compare against, every
    session would read `current` and the header would print `stale: 0`, a clean fleet
    derived from a probe that could not read its own reference.
    """
    rows = []
    for session in sessions:
        pid = session.get("pid")
        marker = newest_marker(cache_dir, marketplace, plugin, pid) if pid else None
        loaded, mtime = marker if marker else (None, None)
        if loaded is None or installed is None:
            state = "unknown"
        elif loaded != installed:
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
    """{pid(int): pane_id(str)} — the join is the tty, which both `ps` and wezterm carry.

    Three states, and the join is where both halves' contracts are easiest to lose: `None`
    when either read failed, `{}` when both answered and nothing joined, a map otherwise.
    Collapsing a failed read into `{}` here would put "this fleet has no panes" on screen
    for a `wezterm` that is simply unreachable — and the caller prints exactly that string
    per stale row, so the collapse would be reported as a measurement.
    """
    panes = wezterm_panes() if panes is None else panes
    if panes is None:
        return None
    by_tty = {}
    for pane_id, info in panes.items():
        tty = (info.get("tty") or "").replace("/dev/", "")
        if tty:
            by_tty.setdefault(tty, pane_id)
    ttys = _pid_ttys(pids)
    if ttys is None:
        return None
    return {pid: by_tty[tty] for pid, tty in ttys.items() if tty in by_tty}


def composer_line(text):
    """The LAST line whose visible text starts with the prompt glyph, or None.

    Mirrors `server/tab.mjs:composerLine()` for the position rule. Every echoed submitted
    prompt above it carries the same glyph, so **position** — not the glyph alone — is what
    identifies the composer; a `COMPOSER in text` substring test matches any echoed prompt in
    scrollback and passes on the first poll whatever the pane is doing. Reads escape-laden
    text (see `pane_is_ready`), so the glyph is matched after stripping SGR sequences.
    """
    for line in reversed(str(text).split("\n")):
        if _plain(line).startswith(COMPOSER):
            return line
    return None


def pane_is_ready(text):
    """True when the composer is EMPTY — including when it holds only the TUI's placeholder.

    ⚠️ **A placeholder is not text.** Measured 2026-10-07 on an idle pane: the composer reads
    `❯\\xa0\\x1b[0;2mTry "how does … work?"` — the hint is drawn **dim** (SGR 2) and is not
    something anyone typed. `server/tab.mjs:isReady()` deliberately refuses it, because a
    submit sent while the TUI is still *painting* can be swallowed (measured 2026-09-20). That
    is the right rule for a **spawn**, where the paint is in flight; it is the wrong one for
    this lever, whose whole job is a **long-idle** session — where the placeholder is always
    present, and where the same send lands (measured 2026-10-07: `/reload-plugins` into a
    placeholder composer executed and moved the marker). Mirroring it unchanged refused every
    idle session the lever exists to reach.

    What the gate is actually for is kept: **text somebody typed** is not dim, so a composer
    holding the operator's unsent answer — the 2026-10-05 case, `❯ draft ok` parked 3h22m — is
    still refused rather than concatenated with a reload command.
    """
    line = composer_line(text)
    if line is None:
        return False
    plain = _plain(line)
    if plain[len(COMPOSER):].strip() == "":
        return True
    return _is_dim(line[line.index(COMPOSER) + len(COMPOSER):])


def reload_pane(pane_id, pid, cache_dir, marketplace, plugin):
    """Type `/reload-plugins` into an idle pane and confirm the reload landed.

    Activate first: `send-text` reaches a pane only once its tab is active (measured
    2026-09-20 in `server/tab.mjs`, and again here). Readiness is an empty composer, not a
    sleep.

    ⚠️ **Success is the newest marker for this pid changing — never the pre-reload version
    directory's mtime.** A reload writes a NEW `.in_use/<pid>` entry under the *installed*
    version and **never touches the old one**, so watching `row['loaded']`'s marker can
    never fire: a stale session that reloads correctly would report `unconfirmed` after the
    full settle timeout, once per pane, serially. The comparison is therefore the
    `newest_marker()` pair — version or mtime — which is the same rule the census reads.
    """
    before = newest_marker(cache_dir, marketplace, plugin, pid)
    if before is None:
        # No marker to compare against: the send could land, but nothing could confirm it.
        return {"pane": pane_id, "ok": False, "why": "no marker before the send — unverified"}

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
            text = subprocess.run(["wezterm", "cli", "get-text", "--pane-id", str(pane_id), "--escapes"],
                                  capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
        if pane_is_ready(text):
            break
        time.sleep(1)
    else:
        return {"pane": pane_id, "ok": False, "why": "composer not empty — pane not idle at a prompt"}

    try:
        sent = subprocess.run(["wezterm", "cli", "send-text", "--pane-id", str(pane_id),
                               "--no-paste", "/reload-plugins\r"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"pane": pane_id, "ok": False, "why": f"send-text failed: {exc}"}
    if sent.returncode != 0:
        return {"pane": pane_id, "ok": False,
                "why": f"send-text exited {sent.returncode}: {sent.stderr.strip()}"}

    deadline = time.time() + RELOAD_SETTLE_TIMEOUT
    while time.time() < deadline:
        after = newest_marker(cache_dir, marketplace, plugin, pid)
        if after and (after[0] != before[0] or after[1] > before[1]):
            return {"pane": pane_id, "ok": True, "why": f"marker moved {before[0]} → {after[0]}"}
        time.sleep(1)
    return {"pane": pane_id, "ok": False, "why": "marker did not move — reload unconfirmed"}


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

    # A lever that cannot reach a pane is not a degraded lever, it is a no-op that would
    # report `no resolvable pane` for the whole fleet and read as "nothing was stale enough".
    if args.reload and args.no_panes:
        print("--reload needs pane resolution; --no-panes disables it", file=sys.stderr)
        return 2

    installed = installed_version(args.installed_json, args.plugin, args.marketplace)
    if args.sessions_json:
        try:
            with open(args.sessions_json) as handle:
                sessions = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"could not read {args.sessions_json}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(sessions, list) or not all(isinstance(s, dict) for s in sessions):
            print(f"{args.sessions_json} must hold a list of session records", file=sys.stderr)
            return 2
    else:
        sessions, error = live_sessions()
        if error:
            print(error, file=sys.stderr)
            return 2

    rows = census(sessions, installed, args.cache_dir, args.marketplace, args.plugin)

    stale = [r for r in rows if r["state"] == "stale"]
    panes = None
    if stale and not args.no_panes:
        panes = panes_for_pids([r["pid"] for r in stale if r["pid"]])
        if panes is None:
            # The pane read failed. Say so once, and do not let the per-row fallback below
            # print `no resolvable pane` — that string would be a measurement this run did
            # not take.
            print("pane read failed (wezterm or ps unreachable) — no jump targets resolved",
                  file=sys.stderr)
            panes = {}
        for row in stale:
            row["pane"] = panes.get(row["pid"])
            row["jump"] = f"/supervisor:jump {row['pane']}" if row["pane"] else None

    lever = None
    if args.reload:
        results = {}
        reached = 0
        for row in stale:
            pane = row.get("pane")
            if not pane:
                results[row["session_id"]] = {"ok": False, "why": "no resolvable pane"}
                continue
            results[row["session_id"]] = reload_pane(pane, row["pid"], args.cache_dir,
                                                     args.marketplace, args.plugin)
            reached += 1 if results[row["session_id"]]["ok"] else 0
        if not args.json:
            for row in stale:
                result = results[row["session_id"]]
                verdict = "reloaded" if result.get("ok") else "not reloaded"
                print(f"  {row['sid8']}  pane {row.get('pane') or '—'}  {verdict}  ({result.get('why')})")
        # Re-read after the lever so the delta is measured, not inferred. `census` rebuilds
        # every row, so both the results and the jump targets have to be carried back on —
        # attaching them to the pre-lever rows would drop them from `--json` entirely, and
        # the fresh rows would render an em-dash where the jump target belongs.
        rows = census(sessions, installed, args.cache_dir, args.marketplace, args.plugin)
        for row in rows:
            if row["state"] == "stale" and panes is not None:
                row["pane"] = panes.get(row["pid"])
                row["jump"] = f"/supervisor:jump {row['pane']}" if row["pane"] else None
        lever = {"reached": reached, "results": results}

    if args.json:
        payload = {"installed": installed, "rows": rows}
        if lever is not None:
            payload["lever"] = lever
        print(json.dumps(payload, indent=2))
        return 0
    print(render(rows, installed))
    if lever is not None:
        print(f"lever: reached {lever['reached']} pane(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
