#!/usr/bin/env python3
"""fleet-colours — the session-colour census: what each live session is coloured.

Colour is the operator's attention-cost ranking, and it is persisted in exactly
one place: the session transcript. Every turn re-emits
`{"type":"agent-color","agentColor":"<colour>"}` carrying the *current* colour,
so the LAST such record in a transcript is the live value. Neither
`wezterm cli list` (18 fields) nor `~/.claude/sessions/*.json` carries a colour
key, which is why the census reads transcripts.

Joins the session registry (`~/.claude/sessions/*.json`) to transcripts
(`~/.claude/projects/*/<session-id>.jsonl`) on `sessionId`.

The backlog measure is green+blue+cyan: sessions that cost a keystroke per step
and are not yet converted to something autonomous. `default` is reported
separately rather than folded in, because a session whose transcript carries no
`agent-color` record has *unknown* colour, not a known-neutral one. `purple` is
a lifecycle overlay (finished), not a role, and is likewise reported apart.

Claude panes are identified by joining `wezterm cli list` tty_name against the
tty of a live `claude` process in `ps`, keyed on the registry's `pid`. Panes are
NOT matched on their title glyph (an animation frame that changes with session
state) nor on cwd (every vault session shares one, so cwd collides).

The census is ALWAYS every live session — there is no project scope to get
wrong, so a count can never silently undercount the fleet.

Usage: fleet-colours.py [--session ID] [--json]
  --session ID  resolve one session only (full id or unique prefix); prints its
                colour and exits 0, for scripting.
  --json        machine-readable output instead of the table.

The retired scoping flags (`--all`, `--vault NAME`) are accepted and ignored
(removed 2026-09-19). They used to narrow the census to one project, which
silently undercounted the fleet; tolerating them rather than rejecting keeps
callers that still pass them working.

Exit codes: 0 success · 1 a --session id that does not resolve · 2 no registry.
"""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

HOME = Path.home()
SESSIONS = HOME / ".claude" / "sessions"
PROJECTS = HOME / ".claude" / "projects"

BACKLOG = ("green", "blue", "cyan")

# A transcript with no agent-color record at all is `default` (colour never set);
# a session with no transcript on disk is `unknown` (nothing to read). Different
# states — collapsing them would report a guess as a measurement.
DEFAULT = "default"
UNKNOWN = "unknown"


def parse_args(argv):
    session, as_json = None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        # `--all` and `--vault NAME` are retired scoping flags: accepted and
        # ignored (2026-09-19). The census is always every live session, so
        # there is nothing left for them to select. Tolerated rather than
        # rejected so callers that still pass them keep working.
        if a == "--all":
            pass
        elif a == "--vault" and i + 1 < len(argv):
            i += 1
        elif a == "--session" and i + 1 < len(argv):
            i += 1
            session = argv[i]
        elif a == "--json":
            as_json = True
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return None
        i += 1
    return session, as_json


def registry() -> list[dict]:
    """Live sessions, from the registry Claude Code maintains."""
    if not SESSIONS.is_dir():
        return []
    out = []
    for f in sorted(SESSIONS.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sid = d.get("sessionId")
        if sid:
            out.append({
                "session_id": sid,
                "name": d.get("name") or "",
                "cwd": d.get("cwd") or "",
                "pid": d.get("pid"),
            })
    return out


def find_transcript(sid: str) -> Path | None:
    """The session's transcript, or None when it has none on disk."""
    hits = list(PROJECTS.glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


def last_colour(path: Path) -> str | None:
    """The last agent-color record in the transcript — the live colour.

    None means the transcript exists but carries no record: colour never set.
    """
    colour = None
    try:
        with path.open("r", errors="ignore") as fh:
            for line in fh:
                # Match the quoted type VALUE, not `"type":"agent-color"` — the
                # latter assumes compact separators, so a serialization change
                # upstream would silently report every session as `default`.
                if '"agent-color"' not in line:
                    continue
                try:
                    colour = json.loads(line).get("agentColor") or colour
                except ValueError:
                    continue
    except OSError:
        return None
    return colour


def pid_ttys() -> dict[int, str]:
    """pid → tty, for every live `claude` process. `??` (no tty) is dropped."""
    try:
        ps = subprocess.run(["ps", "-eo", "pid=,tty=,comm="],
                            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or parts[2].strip() != "claude":
            continue
        pid, tty = parts[0].strip(), parts[1].strip()
        if not pid.isdigit() or not tty or tty == "??":
            continue
        out[int(pid)] = tty if tty.startswith("/dev/") else f"/dev/{tty}"
    return out


def pane_titles() -> dict[str, dict]:
    """tty_name → pane, for every pane wezterm reports."""
    try:
        out = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                             capture_output=True, text=True, timeout=10).stdout
        panes = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return {p["tty_name"]: p for p in panes if p.get("tty_name")}


def project_label(cwd: str) -> str:
    if not cwd:
        return "—"
    parts = [p for p in Path(cwd).parts if p not in ("/", "Users")]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "—")


def census(session: str | None) -> list[dict]:
    ttys = pid_ttys()
    panes = pane_titles()
    rows = []
    for rec in registry():
        sid = rec["session_id"]
        if session and not sid.startswith(session):
            continue
        label = project_label(rec["cwd"])
        transcript = find_transcript(sid)
        colour = UNKNOWN if transcript is None else (last_colour(transcript) or DEFAULT)
        # pid → tty → pane. The registry carries the pid; wezterm exposes the tty.
        tty = ttys.get(rec["pid"]) if isinstance(rec["pid"], int) else None
        pane = panes.get(tty) if tty else None
        rows.append({
            "session_id": sid,
            "colour": colour,
            "name": rec["name"],
            "project": label,
            "transcript": str(transcript) if transcript else "",
            "pane": pane["pane_id"] if pane else None,
        })
    return rows


def counts_of(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["colour"]] = counts.get(r["colour"], 0) + 1
    return counts


def render(rows: list[dict]) -> None:
    # The transcript path is a column, not decoration: it is the provenance of
    # every colour printed, so a reader can re-read the same file and check it.
    print(f"{'COLOUR':<9} {'SESSION':<10} {'PROJECT':<20} NAME")
    for r in sorted(rows, key=lambda r: (r["colour"], r["project"], r["session_id"])):
        print(f"{r['colour']:<9} {r['session_id'][:8]:<10} {r['project']:<20} {r['name'] or '—'}")
        print(f"{'':<9} {'':<10} {'':<20} └ {r['transcript'] or '(no transcript — colour unreadable)'}")

    counts = counts_of(rows)
    backlog = sum(counts.get(c, 0) for c in BACKLOG)
    detail = " · ".join(f"{c} {counts.get(c, 0)}" for c in BACKLOG)
    paned = sum(1 for r in rows if r["pane"] is not None)
    print()
    print(f"backlog (green/blue/cyan):  {backlog}   ({detail})")
    print(f"default (colour never set): {counts.get(DEFAULT, 0)}")
    print(f"purple (finished):          {counts.get('purple', 0)}")
    other = {k: v for k, v in counts.items() if k not in BACKLOG + ("default", "purple", UNKNOWN)}
    if other:
        print(f"other:                      {sum(other.values())}   (" +
              " · ".join(f"{k} {v}" for k, v in sorted(other.items())) + ")")
    print(f"unknown (no transcript):    {counts.get(UNKNOWN, 0)}")
    print(f"\n{len(rows)} live sessions · {paned} in a wezterm pane · {len(rows) - paned} headless")


def main() -> int:
    parsed = parse_args(sys.argv[1:])
    if parsed is None:
        return 2
    session, as_json = parsed
    if not SESSIONS.is_dir():
        print("no ~/.claude/sessions", file=sys.stderr)
        return 2

    if session:
        rows = census(session)
        if not rows:
            print(f"no live session matching {session!r}", file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(r["colour"])
        return 0

    rows = census(None)
    if as_json:
        counts = counts_of(rows)
        print(json.dumps({
            "sessions": rows,
            "counts": counts,
            "backlog": sum(counts.get(c, 0) for c in BACKLOG),
            "paned": sum(1 for r in rows if r["pane"] is not None),
            "total": len(rows),
        }, indent=2))
        return 0
    render(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
