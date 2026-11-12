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
    """Run one tick and return `(rows, ok)`; `rows` is `[(sid, verb, outcome)]`.

    The verb paths are parameters so a test can stand in scripts that record their calls —
    the real verbs signal whatever the registry names.
    """
    reaper = reaper or load_reaper()
    rows = []
    ok = True
    for session_id in reaper.candidate_session_ids(sessions_dir):
        code, first, _ = run_verb(reaper_path, session_id, dry_run)
        if code == 0:
            rows.append((session_id, "reap", "would-close" if dry_run else "closed"))
            continue
        if code != 1:
            ok = False
            rows.append((session_id, "reap", f"error:{first or code}"))
            continue
        if first != "task-terminal":
            rows.append((session_id, "reap", first or "refused"))
            continue
        code, first, _ = run_verb(closer_path, session_id, dry_run)
        if code == 0:
            rows.append((session_id, "close-worker", "would-close" if dry_run else "closed"))
        elif code == 1:
            rows.append((session_id, "close-worker", first or "refused"))
        else:
            ok = False
            rows.append((session_id, "close-worker", f"error:{first or code}"))
    return rows, ok


def summary_line(rows):
    """The tick's accounting, derived from the per-session lines it just wrote.

    `rows` IS those lines, so each count is a filter over them rather than a second tally
    that could drift from what was printed. Every close here is the 15-minute idle close —
    a finished-task row still had to clear `IDLE_FLOOR` before it could reach
    `close-worker.py`, so there is no no-wait close path in this tick and the second count
    is 0 by construction. It is printed anyway: a future no-wait path then shows up as a
    non-zero instead of silently redefining the first count.

    The `dry-run` marker is the caller's to prefix, not this function's: it describes the
    run, while every count here is the same whether or not anything was signalled.
    """
    closed = [row for row in rows if row[2] in ("closed", "would-close")]
    return (
        f"candidates={len(rows)} "
        f"closed {len(closed)} past the 15-min trigger · closed 0 with no wait"
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
    for session_id, verb, outcome in rows:
        print(f"{stamp} idle-reaper-tick session={session_id} verb={verb} outcome={outcome}")
    # Accounting, reconciled against the per-session lines this tick just wrote.
    mode = " dry-run" if args.dry_run else ""
    print(f"{stamp} idle-reaper-tick{mode} {summary_line(rows)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
