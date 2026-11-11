#!/usr/bin/env python3
"""Wake a subject's manager session by typing into its WezTerm pane — on change only.

The sweep gate already answers "changed vs unchanged" model-free
(`manager-predispatch.py` in this plugin; the vault's `sweep-gate.py` for the loop
tick). This script is the pane-wake half of wiring that verdict into the wake path
(operator pick, attention card 1803ce3c, 2026-11-10): an unchanged tick types
nothing and costs zero model rounds; a changed tick types exactly one wake prompt
into the manager's pane, costing one model turn.

    manager-wake.py --vault <path> --subject <name> [--tier goal|topic|fleet]
                    [--interval 15] [--dry-run]

Resolution, in order:

  1. the subject's manager session id — the newest
     `~/.claude/state/worker-manager/<sid>.json` whose `subject` matches and whose
     `branch` matches the tier (`goal` / `topic` / `fleet`). The file name is the
     session id.
  2. that session's pane — the `ps`/tty join over `wezterm cli list`, the same
     `pane_sessions` join `jump.py` uses.
  3. `pane-wake-guard.py --pane-id <id>` decides whether a wake is safe:
       `wake` / `wake:suggestion` -> type the prompt
       `skip:*`                   -> print `SKIP <reason>`, type nothing

Exit status: 0 for every wake decision — woken, skipped, or no manager found. A
non-zero here would make the detached loop read a quiet tick as FAILED, which is
exactly the confusion this task removes. 2 is reserved for a usage error.

⚠️ This types into a live manager pane. It never activates the tab (`send-text`
reaches an inactive pane), and it never wakes a pane `pane-wake-guard.py` refuses —
a false `wake` types into a working session, which the guard exists to prevent.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.expanduser(
    "~/.claude/plugins/marketplaces/claude-supervisor"
)
GUARD = os.path.join(PLUGIN_ROOT, "scripts", "pane-wake-guard.py")
MANAGER_STATE = os.path.expanduser("~/.claude/state/worker-manager")
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")


def wezterm_panes():
    """{pane_id(str): pane dict}, or None when `wezterm cli list` failed.

    `None`, never `{}`: a failed query cannot prove a pane is gone, so no caller may
    read it as "no panes exist". `{}` is a real empty answer.
    """
    try:
        r = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        return {str(p["pane_id"]): p for p in json.loads(r.stdout)}
    except Exception:
        return None


def pane_sessions(pmap):
    """{pane_id: session_id} for panes whose running Claude session can be identified.

    Built from ~/.claude/sessions/<pid>.json (pid -> sessionId), joined to panes
    through the tty: `ps` gives pid -> tty, wezterm gives pane -> tty_name. A pane
    absent from the result is simply unidentifiable — "no evidence", never "mismatch".
    `None` when `ps` failed, distinct from `{}` (the join ran and found nothing).
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
        return None
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


def resolve_manager_session(subject, tier):
    """The subject's manager session id, or None. Newest `resolved_at` wins.

    Matched on `subject` (case-insensitive) and `branch` == tier. The `vault` field
    is deliberately NOT a filter: the state files carry the old vault name
    (`Personal`) while the live vault is `private-personal`, so a vault match would
    refuse every manager. Subject + tier is the identity that is stable.
    """
    want = (subject or "").strip().lower()
    best = None
    for f in glob.glob(os.path.join(MANAGER_STATE, "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        if (d.get("subject") or "").strip().lower() != want:
            continue
        if (d.get("branch") or "").strip().lower() != tier:
            continue
        sid = os.path.splitext(os.path.basename(f))[0]
        ts = d.get("resolved_at") or ""
        if best is None or ts > best[0]:
            best = (ts, sid)
    return best[1] if best else None


def resolve_pane(session_id):
    """The pane id whose tty belongs to `session_id`, or None."""
    pmap = wezterm_panes()
    if not pmap:
        return None
    for pane, sid in (pane_sessions(pmap) or {}).items():
        if sid == session_id:
            return pane
    return None


def guard(pane_id):
    """`pane-wake-guard.py`'s verdict line for the pane."""
    try:
        r = subprocess.run([sys.executable, GUARD, "--pane-id", str(pane_id)],
                           capture_output=True, text=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 — any failure is a skip, never a wake
        return f"error:{exc}"
    lines = (r.stdout or "").strip().splitlines()
    return lines[0] if lines else "error:no-verdict"


def wake(pane_id, prompt, dry_run):
    """Type the wake prompt into the pane (or say what it would do)."""
    if dry_run:
        return f"WOULD-WAKE pane={pane_id} prompt={prompt!r}"
    try:
        r = subprocess.run(["wezterm", "cli", "send-text", "--pane-id", str(pane_id),
                            "--no-paste", prompt + "\r"],
                           capture_output=True, text=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return f"WAKE-FAILED pane={pane_id} {exc}"
    if r.returncode != 0:
        return f"WAKE-FAILED pane={pane_id} {(r.stderr or '').strip()}"
    return f"WOKEN pane={pane_id} prompt={prompt!r}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", default=None,
                    help="vault path or name (context only; not a resolution filter)")
    ap.add_argument("--subject", required=True, help="goal / topic / 'Fleet Manager'")
    ap.add_argument("--tier", choices=["goal", "topic", "fleet"], default="goal")
    ap.add_argument("--interval", default="15",
                    help="minutes for the drive round's --interval (default 15)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and guard, but type nothing")
    args = ap.parse_args(argv)

    sid = resolve_manager_session(args.subject, args.tier)
    if not sid:
        print(f"NO-MANAGER subject={args.subject!r} tier={args.tier}")
        return 0

    pane = resolve_pane(sid)
    if not pane:
        print(f"NO-PANE session={sid[:8]} subject={args.subject!r}")
        return 0

    verdict = guard(pane)
    if not verdict.startswith("wake"):
        print(f"SKIP {verdict} pane={pane} session={sid[:8]}")
        return 0

    prompt = f"/supervisor:{args.tier}-drive --interval {args.interval}m"
    print(wake(pane, prompt, args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
