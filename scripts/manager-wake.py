#!/usr/bin/env python3
"""Wake a subject's manager session by typing into its WezTerm pane — on change only.

The sweep gate already answers "changed vs unchanged" model-free
(`manager-predispatch.py` in this plugin; the vault's `sweep-gate.py` for the loop
tick). This script is the pane-wake half of wiring that verdict into the wake path
(operator pick, attention card 1803ce3c, 2026-11-10): an unchanged tick types
nothing and costs zero model rounds; a changed tick types one wake prompt into the
manager's pane, costing one model turn.

    manager-wake.py --vault <path> --subject <name> [--tier auto|goal|topic|fleet]
                    [--interval 15] [--dry-run]

`--tier auto` (the default) matches any branch and derives the drive prompt from
the resolved manager's own `branch`, so a caller that enumerates subjects without
knowing their tier (the detached loop) need not know it.

Resolution, in order:

  1. the subject's manager session id — the newest
     `~/.claude/state/worker-manager/<sid>.json` whose `subject` matches and whose
     `branch` matches the tier (`goal` / `topic` / `fleet`). The file name is the
     session id.
  2. that session's pane — `jump.py`'s `wezterm_panes` / `pane_sessions`, reused
     rather than duplicated: those two reads are already enumerated and driven by
     `transport-read-check.py`, and a second copy here would add a transport-read
     site nothing drives.
  3. `pane-wake-guard.py --pane-id <id>` decides whether a wake is safe:
       `wake` / `wake:suggestion` -> type the prompt
       `skip:*`                   -> print `SKIP <reason>`, type nothing

Submission is CONFIRMED, not assumed. The repo has twice measured the
swallowed-submit defect (`scripts/send-delivery-drill.py`): `send-text` reaching
the pty is not proof the Enter submitted. After typing, the pane is re-read and
the result is `WOKEN` only when the composer cleared or the session started
working; a composer that still holds the prompt is `UNVERIFIED`, never a success.

Exit status: 0 for every wake decision — woken, unverified, skipped, or no manager
found. A non-zero here would make the detached loop read a quiet tick as FAILED,
which is exactly the confusion this task removes. 2 is reserved for a usage error
(a bad `--interval`).

⚠️ This types into a live manager pane. It never activates the tab (`send-text`
reaches an inactive pane), and it never wakes a pane `pane-wake-guard.py` refuses —
a false `wake` types into a working session, which the guard exists to prevent.
"""
import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.expanduser(
    "~/.claude/plugins/marketplaces/claude-supervisor"
)
GUARD = os.path.join(PLUGIN_ROOT, "scripts", "pane-wake-guard.py")
MANAGER_STATE = os.path.expanduser("~/.claude/state/worker-manager")
VALID_BRANCHES = ("goal", "topic", "fleet")
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PROMPT_GLYPH = "❯"
QUEUED_HINTS = ("queued message", "queued messages", "press up to edit queued")

_GUARD_MOD = None


def _guard_mod():
    """`pane-wake-guard.py` imported once, for its `read_pane` and ANSI helpers."""
    global _GUARD_MOD
    if _GUARD_MOD is None:
        spec = importlib.util.spec_from_file_location("supervisor_pane_wake_guard", GUARD)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GUARD_MOD = mod
    return _GUARD_MOD


def _pane_text(pane_id):
    """The pane's visible text (ANSI included), or None on a failed read."""
    return _guard_mod().read_pane(pane_id)


def composer_line(text):
    """The last line carrying the prompt glyph, ANSI/box stripped, or None."""
    if text is None:
        return None
    gw = _guard_mod()
    for line in reversed(text.splitlines()):
        s = gw._lead_stripped(gw.strip_ansi(line))
        if s.startswith(PROMPT_GLYPH):
            return s
    return None


def _jump():
    """`jump.py`'s pane reads, imported by path (hyphenated filename)."""
    path = os.path.join(PLUGIN_ROOT, "scripts", "jump.py")
    spec = importlib.util.spec_from_file_location("supervisor_jump", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_manager_session(subject, tier):
    """(session_id, branch) for the subject's manager, or (None, None).

    Newest `resolved_at` wins. `tier` may be `auto` to match any branch; otherwise
    it must equal the registration's `branch`. The `vault` field is deliberately NOT
    a filter: the state files carry the old vault name (`Personal`) while the live
    vault is `private-personal`, so a vault match would refuse every manager.
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
        branch = (d.get("branch") or "").strip().lower()
        if not branch:
            continue
        if tier != "auto" and branch != tier:
            continue
        sid = os.path.splitext(os.path.basename(f))[0]
        if not SESSION_ID_RE.match(sid):
            continue  # `last-<vault>.json` and other non-session records in this dir
        ts = d.get("resolved_at") or ""
        if best is None or ts > best[0]:
            best = (ts, sid, branch)
    if not best:
        return None, None
    return best[1], best[2]


def resolve_pane(session_id):
    """The pane id whose tty belongs to `session_id`, or None.

    `None` means EITHER the pane read failed OR no pane matched — the caller types
    nothing in both cases, so a transient read failure costs one skipped wake (the
    next tick retries) and never a wrong one. The distinction is not carried because
    the only consequence is "no wake this tick".
    """
    jump = _jump()
    pmap = jump.wezterm_panes()
    if pmap is None:
        return None
    sessions = jump.pane_sessions(pmap)
    if not sessions:
        return None
    for pane, sid in sessions.items():
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
    """Type the wake prompt and CONFIRM it left the composer; never report WOKEN unverified.

    Mirrors `server/tab.mjs` `confirmSubmission`: read the composer line, treat our
    text still sitting there as a swallowed Enter and RE-PRESS it, and report WOKEN
    only when the composer cleared AND the prompt is visible in the transcript — so a
    send that never landed is UNVERIFIED, not a false success. This is an ACTION, not
    a read: it reports its own outcome rather than collapsing a failure into an empty
    result, and it is enumerated in `transport-read-check.py` for mechanism/list
    agreement, deliberately not driven into the three states.
    """
    if dry_run:
        return f"WOULD-WAKE pane={pane_id} prompt={prompt!r}"
    head = prompt.split("\n")[0].strip()[:40]
    saw_ours = False
    for attempt in range(3):
        payload = prompt + "\r" if attempt == 0 else "\r"
        try:
            r = subprocess.run(["wezterm", "cli", "send-text", "--pane-id", str(pane_id),
                                "--no-paste", payload],
                               capture_output=True, text=True, timeout=10)
        except Exception as exc:  # noqa: BLE001
            return f"WAKE-FAILED pane={pane_id} {exc}"
        if r.returncode != 0:
            return f"WAKE-FAILED pane={pane_id} {(r.stderr or '').strip()}"
        time.sleep(0.4)  # let the text reach the composer before the first read
        for _ in range(5):
            time.sleep(0.5)
            text = _pane_text(pane_id)
            line = composer_line(text)
            if line is None:
                continue
            composer = line[len(PROMPT_GLYPH):].strip()
            if composer == "":
                if head in text:
                    return f"WOKEN pane={pane_id} prompt={prompt!r}"
                continue  # not landed yet — keep polling this attempt, never claim success
            if head in composer:
                saw_ours = True
                break  # still in the composer: swallowed Enter, retried on the next attempt
            if any(h in composer.lower() for h in QUEUED_HINTS):
                return f"WOKEN pane={pane_id} prompt={prompt!r} (queued)"
            return (f"UNVERIFIED pane={pane_id} prompt={prompt!r} "
                    "— composer holds text this send did not write")
    if saw_ours:
        return f"FAILED pane={pane_id} prompt={prompt!r} — the Enter was swallowed"
    return f"UNVERIFIED pane={pane_id} prompt={prompt!r} — the submit could not be confirmed"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", default=None,
                    help="vault path or name (context only; not a resolution filter)")
    ap.add_argument("--subject", required=True, help="goal / topic / 'Fleet Manager'")
    ap.add_argument("--tier", choices=["auto", "goal", "topic", "fleet"], default="auto")
    ap.add_argument("--interval", default="15",
                    help="minutes for the drive round's --interval (default 15)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and guard, but type nothing")
    args = ap.parse_args(argv)

    # The interval is interpolated into the typed prompt, so validate it rather than
    # passing an arbitrary string through to a live pane.
    if not (args.interval.isdigit() and 1 <= int(args.interval) <= 1440):
        print(f"USAGE: --interval must be an integer 1..1440 minutes, got {args.interval!r}",
              file=sys.stderr)
        return 2

    sid, branch = resolve_manager_session(args.subject, args.tier)
    if not sid:
        print(f"NO-MANAGER subject={args.subject!r} tier={args.tier}")
        return 0
    if branch not in VALID_BRANCHES:
        print(f"NO-MANAGER subject={args.subject!r} (unexpected branch {branch!r})")
        return 0

    pane = resolve_pane(sid)
    if not pane:
        print(f"NO-PANE session={sid[:8]} subject={args.subject!r}")
        return 0

    verdict = guard(pane)
    if not verdict.startswith("wake"):
        print(f"SKIP {verdict} pane={pane} session={sid[:8]}")
        return 0

    prompt = f"/supervisor:{branch}-drive --interval {args.interval}m"
    print(wake(pane, prompt, args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
