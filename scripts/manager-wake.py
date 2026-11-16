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

  1. the subject's manager session id — the newest LIVE session among the union of
     `~/.claude/state/worker-manager/<sid>.json` (whose `subject` and `branch` match)
     and `~/.claude/state/sweep-gate/<branch>--<slug>.session` (the id `--arm` wrote).
     Live = listed in `~/.claude/sessions/*.json` with a running pid. Registrations
     that all name dead sessions are `NO-MANAGER`, never `NO-PANE`.
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

Every result line ends `session=<sid8> live=<yes|no|unknown>` (`session=-` when no
registration names the subject), so a caller's ledger can tell a missed live manager
from a dead registration.

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
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "pane-wake-guard.py")
LIVENESS = os.path.join(HERE, "manager-liveness.py")
# The three state paths below are Claude Code's per-user state, expanded per user at run
# time — the same location on every install, not a path on one author's machine.
MANAGER_STATE = os.path.expanduser("~/.claude/state/worker-manager")
# The `.session` markers `manager-liveness.py --arm` writes — same env override as that
# script's STATE_DIR, so a test or drill isolating one isolates both.
SESSION_MARKER_DIR = os.path.expanduser(
    os.environ.get("MANAGER_LIVENESS_STATE_DIR", "~/.claude/state/sweep-gate")
)
# Claude Code's session registry — `<pid>.json` per running session.
SESSIONS_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSIONS_DIR") or "~/.claude/sessions"
)
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
    path = os.path.join(HERE, "jump.py")
    spec = importlib.util.spec_from_file_location("supervisor_jump", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ts(value):
    """Parse an RFC3339 `resolved_at`; `None` when absent or unparseable."""
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    # A naive stamp is read as UTC, so it compares with the aware marker mtimes rather
    # than raising TypeError mid-resolution.
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _newer(a, b):
    """True when datetime `a` is strictly newer than `b` (None sorts oldest)."""
    if a is None:
        return False
    return b is None or a > b


def _base_slug(subject):
    """`manager-liveness.py`'s `base_slug`, so the marker path is the one `--arm` wrote."""
    return re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")


def registered_candidates(subject, tier):
    """[(ts, sid, branch)] — every registration naming the subject, from BOTH registries.

    1. `~/.claude/state/worker-manager/<sid>.json` — written when a session resolves its
       subject (`docs/subject-resolution.md` § Recording); `ts` is its `resolved_at`.
    2. `~/.claude/state/sweep-gate/<branch>--<slug>.session` — written by
       `manager-liveness.py --arm` on every arm, from inside the manager's own session;
       `ts` is the file's mtime, i.e. the last arm.

    ⚠️ **Neither registry is complete on its own, so the union is the reading.** Measured
    2026-11-15: the `.session` marker knew 11 of 12 live managers, while `worker-manager/`
    had no registration for the live Development Pipeline Throughput manager at all — its
    newest registration there named a session dead for 135 h, so every wake on that
    subject read `NO-PANE` against a dead session while the real manager went unwoken.

    The `vault` field is deliberately NOT a filter: the state files carry the old vault
    name (`Personal`) while the live vault is `private-personal`, so a vault match would
    refuse every manager.
    """
    want = (subject or "").strip().lower()
    out = []
    for f in glob.glob(os.path.join(MANAGER_STATE, "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        if not isinstance(d, dict):
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
        out.append((_ts(d.get("resolved_at")), sid, branch))
    base = _base_slug(want)
    if base:
        for branch in (VALID_BRANCHES if tier == "auto" else (tier,)):
            marker = os.path.join(SESSION_MARKER_DIR, f"{branch}--{base}.session")
            try:
                with open(marker, encoding="utf-8") as fh:
                    sid = fh.read().strip()
                mtime = os.path.getmtime(marker)
            except OSError:
                continue
            if not SESSION_ID_RE.match(sid):
                continue
            out.append((datetime.fromtimestamp(mtime, tz=timezone.utc), sid, branch))
    return out


def _pid_alive(pid):
    """False only when the pid provably does not exist; an unreadable pid is not death."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (TypeError, ValueError, OSError):
        return True
    return True


def live_session_ids(registry_dir=None):
    """Session ids Claude Code's registry lists with a running pid, or None when unreadable.

    `~/.claude/sessions/<pid>.json` exists while a session runs; its `pid` is checked too,
    because a session killed hard leaves its file behind. `None` is "cannot tell", never
    "nothing is live" — the caller degrades to newest-registration and says so.
    """
    directory = registry_dir or SESSIONS_DIR
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    live = set()
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(rec, dict):
            continue
        sid = rec.get("sessionId") or rec.get("session_id")
        if sid and _pid_alive(rec.get("pid")):
            live.add(sid)
    return live


def _pid_tty(pid):
    """The pid's controlling tty (`ttys012`), `""` when it has none, None when unreadable."""
    try:
        r = subprocess.run(["ps", "-o", "tty=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=5)
    except (TypeError, ValueError, OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    tty = r.stdout.strip()
    return "" if tty in ("", "??", "?") else tty


def is_headless(session_id, registry_dir=None):
    """True only when BOTH reads agree the session runs without a terminal.

    `entrypoint: sdk-cli` alone is not proof: a tab opened from a headless leg inherits
    `CLAUDE_CODE_ENTRYPOINT` and registers `sdk-cli` while owning a pane. The pid having
    no controlling tty is the second, independent read; an unreadable tty is not headless.
    """
    live = [r for r in _registry_records(session_id, registry_dir) if _pid_alive(r.get("pid"))]
    if not live:
        return False
    return all(r.get("entrypoint") == "sdk-cli" and _pid_tty(r.get("pid")) == "" for r in live)


def _newest(rows):
    best = None
    for row in rows:
        if best is None or _newer(row[0], best[0]):
            best = row
    return best


def resolve_manager_session(subject, tier):
    """(session_id, branch, live) for the subject's manager.

    Among every registration in both registries (`registered_candidates`), the NEWEST
    LIVE session wins and `live` is True. A dead registration never outranks a live one,
    however recent: a manager that died after re-registering is exactly the case that
    typed wakes into nothing.

    - registrations exist, none live -> (newest dead sid, branch, False): the caller
      reports `NO-MANAGER` naming that session, never `NO-PANE`.
    - no registration at all          -> (None, None, False).
    - the registry is unreadable      -> (newest sid, branch, None): liveness unknown, so
      the wake proceeds on recency as before and the pane lookup is the gate.
    """
    cands = registered_candidates(subject, tier)
    if not cands:
        return None, None, False
    live = live_session_ids()
    if live is None:
        best = _newest(cands)
        return best[1], best[2], None
    alive = _newest([c for c in cands if c[1] in live])
    if alive:
        return alive[1], alive[2], True
    dead = _newest(cands)
    return dead[1], dead[2], False


def live_word(live):
    """The `live=` field value: `yes` / `no` / `unknown`."""
    return {True: "yes", False: "no"}.get(live, "unknown")


# Reasons a pane lookup can miss that a second read may not repeat; anything else is a
# standing fact about the session and is reported on the first read.
TRANSIENT_MISSES = ("wezterm-unreadable", "tty-unreadable")
PANE_RETRY_DELAY_S = 2.0
#: Attempts for a TRANSIENT_MISSES reason, not the single retry this used to allow.
#: Measured 2026-11-16: over 24 h the gate logged 20 `NO-PANE` results, every one
#: `reason=wezterm-unreadable`, while wakes minutes either side of each failure succeeded —
#: so `wezterm cli list` fails per call, flakily, and one 2 s retry was not enough. Four
#: attempts span ~6 s inside a tick that already takes seconds, which is the right budget
#: for a wake that would otherwise be silently lost.
PANE_ATTEMPTS = 4


def _registry_records(session_id, registry_dir=None):
    """Every registry record carrying `session_id` — a resumed session can leave a stale twin."""
    directory = registry_dir or SESSIONS_DIR
    out = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(rec, dict) and (rec.get("sessionId") or rec.get("session_id")) == session_id:
            out.append(rec)
    return out


def find_pane(session_id):
    """(pane_id, None) for the pane whose tty the session's own pid holds, else (None, reason).

    The join runs FROM the target session — registry record -> pid -> tty -> the pane with
    that `tty_name` — rather than through `jump.py`'s `pane_sessions` map, which keys
    `{tty: session}` with last-write-wins: any second registered session on the manager's
    tty made the manager's pane resolve to that other session and the wake read `NO-PANE`.
    The pane list itself is still `jump.py`'s `wezterm_panes`, the read
    `transport-read-check.py` already enumerates and drives.

    Reasons: `wezterm-unreadable` (the pane list failed), `no-registry-entry` (no
    `~/.claude/sessions` record carries the id), `tty-unreadable` (`ps` failed for every
    pid), `no-tty` (the pid holds no terminal), `tty-not-a-pane:<tty>` (the tty matches no
    WezTerm pane).
    """
    pmap = _jump().wezterm_panes()
    if pmap is None:
        return None, "wezterm-unreadable"
    records = _registry_records(session_id)
    if not records:
        return None, "no-registry-entry"
    ttys, unreadable = [], 0
    for rec in records:
        tty = _pid_tty(rec.get("pid"))
        if tty is None:
            unreadable += 1
        elif tty:
            ttys.append(tty)
    if not ttys:
        return None, ("tty-unreadable" if unreadable == len(records) else "no-tty")
    for pane, p in pmap.items():
        if (p.get("tty_name") or "").replace("/dev/", "") in ttys:
            return pane, None
    return None, f"tty-not-a-pane:{ttys[0]}"


def resolve_pane(session_id):
    """(pane_id, None) or (None, reason) — `find_pane`, retried once on a transient miss.

    Measured 2026-11-14 17:26–17:32Z: four live managers, each heartbeating in its own
    pane throughout, all read `NO-PANE` in one six-minute window and resolved again
    afterwards. The failing read was never recorded, so the reason is now carried on
    the result line, and a read that can fail transiently is retried `PANE_ATTEMPTS`
    times before the tick gives the wake up.
    """
    pane, reason = find_pane(session_id)
    for _ in range(PANE_ATTEMPTS - 1):
        if pane is not None or reason not in TRANSIENT_MISSES:
            break
        time.sleep(PANE_RETRY_DELAY_S)
        pane, reason = find_pane(session_id)
    return pane, reason


def read_state(subject, branch):
    """`manager-liveness.py --state`'s first field, or the fail-closed sentinel.

    This caller always knows its subject, so a failed read is `STATE_UNREADABLE`, never
    `None` — the guard refuses the wake on it. A missed wake re-arms the manager through
    its gate; a spurious wake types into a pane whose session then closes it for good.
    Never a fabricated state: a broken liveness read must not read as "awake", and a
    fabricated `SLEEPING` must not suppress every wake.
    """
    try:
        r = subprocess.run([sys.executable, LIVENESS, "--state",
                            "--subject", subject, "--branch", branch],
                           capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001 — any failure is unreadable, never a state
        return _guard_mod().STATE_UNREADABLE
    lines = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not lines:
        return _guard_mod().STATE_UNREADABLE
    return lines[0].split("\t", 1)[0] or _guard_mod().STATE_UNREADABLE


def guard(pane_id, subject_state=None):
    """`pane-wake-guard.py`'s verdict line for the pane."""
    argv = [sys.executable, GUARD, "--pane-id", str(pane_id), "--allow-machine-wait"]
    if subject_state:
        argv += ["--subject-state", subject_state]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
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

    # Every result line ends `session=<sid8> live=<yes|no|unknown>`, so the caller's ledger
    # can record WHICH session a wake resolved and whether it was alive — a `NO-PANE` on a
    # live manager and one on a dead registration are different defects.
    sid, branch, live = resolve_manager_session(args.subject, args.tier)
    if not sid:
        print(f"NO-MANAGER subject={args.subject!r} tier={args.tier} session=- live=no")
        return 0
    tail = f"session={sid[:8]} live={live_word(live)}"
    if live is False:
        # Registrations exist, but every one names a dead session: there is no manager to
        # wake, and saying `NO-PANE` would blame the pane lookup for a missing manager.
        print(f"NO-MANAGER subject={args.subject!r} tier={args.tier} {tail}")
        return 0
    if branch not in VALID_BRANCHES:
        print(f"NO-MANAGER subject={args.subject!r} (unexpected branch {branch!r}) {tail}")
        return 0

    pane, miss = resolve_pane(sid)
    if not pane:
        if live and is_headless(sid):
            # A live manager running headless (`claude --print`, no tty) has no pane BY
            # CONSTRUCTION, so no pane-wake can ever reach it. Named apart from `NO-PANE`,
            # which is a pane lookup that missed a session that has one — measured
            # 2026-11-15: the live Development Pipeline Throughput manager was a headless
            # `--resume`, and every wake on it was lost under `NO-PANE`.
            print(f"HEADLESS subject={args.subject!r} {tail}")
            return 0
        print(f"NO-PANE reason={miss} subject={args.subject!r} {tail}")
        return 0

    verdict = guard(pane, read_state(args.subject, branch))
    if not verdict.startswith("wake"):
        print(f"SKIP {verdict} pane={pane} {tail}")
        return 0

    prompt = f"/supervisor:{branch}-drive --interval {args.interval}m"
    print(f"{wake(pane, prompt, args.dry_run)} {tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
