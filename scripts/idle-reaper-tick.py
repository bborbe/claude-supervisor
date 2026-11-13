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
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REAPER = os.path.join(HERE, "reap-idle-headless.py")
CLOSER = os.path.join(HERE, "close-worker.py")
ASKER = os.path.join(HERE, "attention-ask.py")
VERB_TIMEOUT = 300  # seconds; a single verb run reads the wrapper, the store and the vault
#: Why this tick closed a row. Every close it can currently perform is the 15-minute idle
#: close — a finished-task row still had to clear `IDLE_FLOOR` before it could reach
#: `close-worker.py`. A future close path that skips that floor tags its rows differently,
#: which is what makes `summary_line`'s two counts real rather than one count and a literal.
TRIGGER_IDLE = "idle-15min"

#: The producer id every park card is posted under. ⚠️ **Not the session id, and that is
#: deliberate:** the tick is a launchd process with no `CLAUDE_CODE_SESSION_ID`, so
#: `attention-ask.py post` refuses without an explicit `--producer-id` (`:826-833`). A
#: stable value also keeps the cards pollable by the tick across restarts.
PRODUCER_ID = "idle-reaper-tick"

#: How long a park card suppresses a re-post for the same `<sid>:<requested_at>` park.
#: Matches `attention-ask.py`'s own 24 h `--expires-at` default, so dedup holds exactly as
#: long as the card lives and a park that outlives it resurfaces on the next tick instead
#: of going invisible again.
CARD_TTL_SECONDS = 24 * 60 * 60

#: The dedup marker. ⚠️ **A file, not the store.** `open_cards()` filters
#: `producer_id == session_id`, so it cannot see the tick's own cards — and the store's
#: dedup on an answered or cleared key is not something this tick can rely on. The marker
#: makes "one card per park" a property of this code rather than of a remote service's
#: retention policy, and gives `--dry-run` an artifact to prove it did not write.
CARDED_MARKER = os.environ.get("SUPERVISOR_IDLE_REAPER_CARDED") or os.path.expanduser(
    "~/.claude/state/idle-reaper-carded.json"
)

#: Marker entries older than this are pruned on write. Long enough that a park nobody
#: cleared cannot come back to life by forgetting; short enough that the file stays small.
CARDED_RETENTION_SECONDS = 7 * 24 * 60 * 60


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


def _load_carded(path):
    """The `<sid>:<requested_at>` -> epoch map, or `{}` when absent or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: float(value)
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, (int, float))
    }


def _save_carded(path, carded, now):
    """Write the marker, pruning entries past the retention window.

    A write failure is reported and swallowed: the cards are the deliverable and the marker
    only suppresses duplicates, so losing it costs at most one repeated card, while failing
    the tick would report a fault for a tick that worked.
    """
    pruned = {key: value for key, value in carded.items() if now - value < CARDED_RETENTION_SECONDS}
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(pruned, handle, indent=2, sort_keys=True)
    except OSError as error:
        print(f"   ⚠️ card marker {path} could not be written: {error}")


def park_payload(session_id, park_age):
    """The card body.

    ⚠️ Says **not reaped** and names `busy-target`, never implying a close: the operator's
    ruling is that the registry's non-idle status wins and the guard stays absolute, so the
    card reports a refusal, not an action.
    """
    return (
        f"session {session_id} has had a question parked for {int(park_age)}s and was not "
        "reaped: the registry reports a non-idle status, so busy-target refuses it. "
        "Nothing was closed; the pid is still alive."
    )


def post_park_card(session_id, requested_at, park_age, ask_path=ASKER, timeout=VERB_TIMEOUT):
    """Post one card and return `(ok, item_id_or_error)`.

    ⚠️ `--payload` is mandatory and `--producer-id` is mandatory **in practice** — a
    launchd tick has no `CLAUDE_CODE_SESSION_ID` to fall back on, so `attention-ask.py`
    refuses without it (`:826-833`). The `ITEM_ID:` line is the only place a pollable id
    comes from, so that line, not the exit code, is what a success reads from.
    """
    argv = [
        sys.executable,
        ask_path,
        "post",
        "--dedup-key",
        f"{session_id}:{requested_at}",
        "--producer-id",
        PRODUCER_ID,
        "--producer-kind",
        "cron",
        "--mechanism",
        "ack",
        "--payload",
        park_payload(session_id, park_age),
    ]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    for line in (done.stdout or "").splitlines():
        if line.startswith("ITEM_ID:"):
            return True, line.split(":", 1)[1].strip()
    # ⚠️ The failure text is the diagnostic, and it is not always on stdout — a transport
    # failure is reported on stderr. Returning `exit 1` alone would make "the store is
    # down" indistinguishable from "the argv was rejected", which is the distinction this
    # caller most needs.
    for stream in (done.stdout or "", done.stderr or ""):
        for line in stream.splitlines():
            if line.strip():
                return False, line.strip()
    return False, f"exit {done.returncode}"


def card_parked_sessions(
    reaper,
    sessions_dir=None,
    dry_run=False,
    now=None,
    ask_path=ASKER,
    marker_path=CARDED_MARKER,
    post=None,
):
    """One card per parked-`busy` session, and never a close.

    ⚠️ **Cards stay outside `rows` on purpose.** `summary_line()` derives `candidates=` and
    `refused=` from `rows`, so a card row would corrupt both census counts; the caller
    prints these on their own lines instead. `--dry-run` posts nothing and writes no
    marker, so an operator's close-nothing pass cannot mint live cards.

    Returns `(cards, ok)`; `cards` is `[(session_id, outcome)]`, and `ok` is False only
    when a card that should have posted did not. A deduped session is `deduped` and a dry
    run is `would-card` — neither is a fault.
    """
    now = time.time() if now is None else now
    poster = post or post_park_card
    carded = _load_carded(marker_path)
    cards = []
    ok = True
    for session_id, park_age, requested_at in reaper.busy_parked_session_ids(
        sessions_dir, now=now
    ):
        key = f"{session_id}:{requested_at}"
        posted_at = carded.get(key)
        if posted_at is not None and now - posted_at < CARD_TTL_SECONDS:
            cards.append((session_id, "deduped"))
            continue
        if dry_run:
            cards.append((session_id, "would-card"))
            continue
        posted, detail = poster(session_id, requested_at, park_age)
        if posted:
            carded[key] = now
            cards.append((session_id, f"carded:{detail}"))
        else:
            ok = False
            cards.append((session_id, f"error:{detail}"))
    if carded and not dry_run:
        _save_carded(marker_path, carded, now)
    return cards, ok


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

    for path in (REAPER, CLOSER, ASKER):
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
    reaper = load_reaper()
    rows, ok = tick(args.sessions_dir, args.dry_run, reaper=reaper)
    for session_id, verb, outcome, trigger in rows:
        print(
            f"{stamp} idle-reaper-tick session={session_id} verb={verb} "
            f"outcome={outcome} trigger={trigger}"
        )
    # The parked-`busy` cards. ⚠️ Deliberately NOT folded into `rows`: `summary_line()`
    # derives `candidates=` and `refused=` from those, and a card is neither a candidate
    # nor a refusal — it is a session the candidate filter can never return.
    cards, cards_ok = card_parked_sessions(reaper, args.sessions_dir, args.dry_run)
    for session_id, outcome in cards:
        print(
            f"{stamp} idle-reaper-tick session={session_id} verb=card "
            f"outcome={outcome} trigger={TRIGGER_IDLE}"
        )
    # Accounting, reconciled against the per-session lines this tick just wrote.
    mode = " dry-run" if args.dry_run else ""
    print(f"{stamp} idle-reaper-tick{mode} {summary_line(rows)}")
    return 0 if (ok and cards_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
