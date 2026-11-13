#!/usr/bin/env python3
"""`idle-reaper-tick.py` — one tick of the 15-minute idle close, with no session as its parent.

The operator's rule (2026-11-07, restated 2026-11-10): a headless worker idle for 15 minutes
exits. `reap-idle-headless.py` is the close verb and `close-worker.py` the finished-task verb;
neither decides *when* to run. This is the *when*: `launchd/com.bborbe.idle-reaper.plist`
runs it every 900 s, a fresh process per tick, so there is no loop to die silently.

One tick:

  1. Enumerate candidates with `reap-idle-headless.py`'s own `candidate_session_ids()` —
     every registry record that is `entrypoint: sdk-cli` and `status: idle`. Imported, not
     re-derived, so this file and `--all` can never disagree on the population.
  2. Run `reap-idle-headless.py <sid>` once per candidate. Its eleven guards decide; this
     file never second-guesses a refusal.
  3. Route exactly one refusal onward: `task-terminal` means the task is finished, which is
     `close-worker.py`'s population, so `close-worker.py <sid>` runs next. Every other
     refusal is final for this tick and is only reported.

⚠️ **One verb per candidate, never both on success.** The reaper refuses `task-terminal`
before it signals anything, so a candidate reaches `close-worker.py` only when the reaper
did nothing to it.

Every candidate prints one line naming its session id and outcome, followed by a single
summary line. The full session id, not a prefix, is what the task's SC1/SC2 evidence joins
on. The tick exits 0 when it ran to completion, refusals included: a refusal is the guards
working, and a non-zero exit every 15 minutes would bury a real fault in launchd's log.
It exits 1 only when it could not run at all (the verb scripts are missing, or a verb exits
with an unexpected code).
"""
import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REAPER = os.path.join(HERE, "reap-idle-headless.py")
CLOSER = os.path.join(HERE, "close-worker.py")
VERB_TIMEOUT = 300  # seconds; a single verb run reads the wrapper, the store and the vault
#: Why this tick closed a row. Every close it can currently perform is the 15-minute idle
#: close — a finished-task row still had to clear `IDLE_FLOOR` before it could reach
#: `close-worker.py`. A future close path that skips that floor tags its rows differently,
#: which is what makes `summary_line`'s two counts real rather than one count and a literal.
TRIGGER_IDLE = "idle-15min"


def load_reaper():
    spec = importlib.util.spec_from_file_location("reap_idle_headless", REAPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_verb(script, session_id, dry_run):
    """`(exit_code, first_line, output)` for one verb run on one session id."""
    argv = [sys.executable, script, session_id]
    if dry_run:
        argv.append("--dry-run")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=VERB_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "timeout", ""
    out = done.stdout or ""
    first = out.splitlines()[0].strip() if out.strip() else ""
    return done.returncode, first, out + (done.stderr or "")


def tick(sessions_dir=None, dry_run=False, reaper=None, reaper_path=REAPER, closer_path=CLOSER):
    """Run one tick and return `(rows, ok)`; `rows` is `[(sid, verb, outcome, trigger)]`.

    The verb paths are parameters so a test can stand in scripts that record their calls —
    the real verbs signal whatever the registry names.
    """
    reaper = reaper or load_reaper()
    rows = []
    ok = True
    for session_id in reaper.candidate_session_ids(sessions_dir):
        code, first, _ = run_verb(reaper_path, session_id, dry_run)
        if code == 0:
            # A close carries its own token on the first line (`unanchored-closed`); a plain
            # reap prints the `🧹` header first. Only the token is mapped — everything else
            # stays `closed`, so the existing close path is unchanged.
            if first == "unanchored-closed":
                outcome = "would-unanchored-close" if dry_run else "unanchored-closed"
            else:
                outcome = "would-close" if dry_run else "closed"
            rows.append((session_id, "reap", outcome, TRIGGER_IDLE))
            continue
        if code != 1:
            ok = False
            rows.append((session_id, "reap", f"error:{first or code}", TRIGGER_IDLE))
            continue
        if first != "task-terminal":
            rows.append((session_id, "reap", first or "refused", TRIGGER_IDLE))
            continue
        code, first, _ = run_verb(closer_path, session_id, dry_run)
        if code == 0:
            rows.append((session_id, "close-worker", "would-close" if dry_run else "closed", TRIGGER_IDLE))
        elif code == 1:
            rows.append((session_id, "close-worker", first or "refused", TRIGGER_IDLE))
        else:
            ok = False
            rows.append((session_id, "close-worker", f"error:{first or code}", TRIGGER_IDLE))
    return rows, ok


def summary_line(rows):
    """The tick's accounting, derived from the per-session lines it just wrote.

    `rows` IS those lines, so every count is a filter over them rather than a second tally
    that could drift from what was printed. Both close counts are derived, never literals:
    a close is *past the 15-min trigger* when its row carries `TRIGGER_IDLE`, and *with no
    wait* when it does not. Today every close this tick can perform is the former, so the
    second count is 0 — but it is computed, so a future close path that skips `IDLE_FLOOR`
    moves it instead of silently redefining the first count.

    The `dry-run` marker is the caller's to prefix, not this function's: it describes the
    run, while every count here is the same whether or not anything was signalled.
    """
    closed = [
        row
        for row in rows
        if row[2]
        in ("closed", "would-close", "unanchored-closed", "would-unanchored-close")
    ]
    past_trigger = [row for row in closed if row[3] == TRIGGER_IDLE]
    no_wait = [row for row in closed if row[3] != TRIGGER_IDLE]
    return (
        f"candidates={len(rows)} "
        f"closed {len(past_trigger)} past the 15-min trigger · "
        f"closed {len(no_wait)} with no wait · "
        f"refused {len(rows) - len(closed)}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="One tick of the 15-minute idle close.")
    parser.add_argument("--dry-run", action="store_true", help="run every guard, close nothing")
    parser.add_argument("--sessions-dir", default=None, help="override the session registry")
    args = parser.parse_args(argv)

    for path in (REAPER, CLOSER):
        if not os.path.exists(path):
            print(f"❌ idle-reaper-tick: missing verb {path}")
            return 1
    # Both verbs resolve a session's task through `vault-cli config list`, and when the binary
    # is missing they swallow that into an empty config. Every candidate then reads
    # `task-unresolved`, a tick of normal-looking refusals that closes nothing, forever. So a
    # missing `vault-cli` is checked here and made a fault. `SUPERVISOR_VAULT_CONFIG` is the
    # verbs' own override and makes the binary unnecessary.
    if not os.environ.get("SUPERVISOR_VAULT_CONFIG") and not shutil.which("vault-cli"):
        print(
            "❌ idle-reaper-tick: `vault-cli` is not on PATH, so every task would read "
            f"task-unresolved and nothing would close. PATH={os.environ.get('PATH', '')}"
        )
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, ok = tick(args.sessions_dir, args.dry_run)
    for session_id, verb, outcome, trigger in rows:
        print(
            f"{stamp} idle-reaper-tick session={session_id} verb={verb} "
            f"outcome={outcome} trigger={trigger}"
        )
    # Accounting, reconciled against the per-session lines this tick just wrote.
    mode = " dry-run" if args.dry_run else ""
    print(f"{stamp} idle-reaper-tick{mode} {summary_line(rows)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
