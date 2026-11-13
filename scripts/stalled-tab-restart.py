#!/usr/bin/env python3
"""`stalled-tab-restart.py` — report a stalled TAB worker's refusal, and hand the restart to
the operator through ONE card.

The fleet sweep already classifies a tab worker `stalled` (`agents/fleet-sweep-reader.md:115`:
`busy`/`shell`, unchanged task mtime across ≥2 sweeps, `--ages` reading `quiet`). Nothing
acted on it. `scripts/restart-worker.py` is the act, but it refuses `busy-target` — and a
stalled session is `busy`/`shell` **by definition**, so the default invocation can never
restart one. The only lift is `--allow-busy-lapse`, and the operator ruled on 2026-11-12 that
an unattended round may not pass it: separating a lapse from a slow round is their judgement,
and a wrong call kills in-flight work.

So this script does two things and never a third:

  1. **Reports the refusal** — it invokes `restart-worker.py --dry-run` and prints the token
     that invocation actually returned. `--dry-run` is load-bearing rather than cautious:
     without `--allow-busy-lapse` the script still acts on a row that flipped to `idle`
     between the sweep and the call, so a live invocation would reintroduce the unattended
     restart the ruling forbids, through a timing window.
  2. **Hands the restart over** — when the refusal is `busy-target`, it posts ONE card
     (dedup `stuck-tab:<slug>`) and, on the operator's `ANSWERED:`, runs the real restart
     exactly once. Nothing else reaches `--allow-busy-lapse`; there is no timer and no
     autonomous branch that does.

⚠️ **There is no `stuck-tab:` card to "extend"** — `CLAUDE.md:135` says a stall is a duration,
not an event, and issues no attention card; `manager-attention-watch.py` renders `stuck-tab:`
as a gate REASON and an incident-ledger row. This script creates the card, and its dedup key
is scoped per session so the gate reason and the card cannot both accumulate.

Verdicts (first line of stdout), one token:

  restart      the operator answered this session's card; the restart ran and was reported
  carded       the card was posted or is already open and unanswered — the row is the
               operator's now, and the next round must not post a second one
  capped       this task has been restarted twice with no progress; one escalation card was
               posted and no third restart will run
  held         the session carries an operator hold; no act, and no card
  skipped      the refusal token was something other than `busy-target`; the row is left for
               the next round. The token is on the second line
  cannot-tell  a probe could not decide; report it and never act
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESTART = os.path.join(HERE, "restart-worker.py")
ASK = os.path.join(HERE, "attention-ask.py")
HOLDS = os.path.join(HERE, "session-holds.py")

STATE_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_STALLED_TAB_STATE", "~/.claude/state/stalled-tab")
)

#: Two restarts with no progress between them is the cap. Deliberately different from
#: `worker-keepalive.py`'s `RESUME_CAP` of 3: the store shape and the reset rule are reused,
#: but a stalled tab is a LIVE session where a resume target is a dead one, so the second
#: restart already carries more evidence than a third resume would.
RESTART_CAP = 2

#: The card's two options, as literals because the answer is compared against them. The
#: operator's `ANSWERED:` carries the chosen content, and only `restart it` may kill.
OPTION_RESTART = "restart it"
OPTION_LEAVE = "leave it"


def slug_for(task):
    """A filesystem-safe key for a task name, stable across rounds."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in task.lower())
    return cleaned.strip("-")[:60] or hashlib.sha1(task.encode()).hexdigest()[:12]


def state_path(vault, task):
    key = hashlib.sha1(os.path.abspath(vault).encode()).hexdigest()[:10]
    return os.path.join(STATE_DIR, key, f"{slug_for(task)}.json")


def fresh_state():
    return {"restarts": 0, "progress_at": None, "consumed": []}


def read_state(vault, task):
    """`(state, None)` when the store is absent or valid, `(None, reason)` when it is not.

    ⚠️ **A present-but-unreadable store is NOT a fresh one.** Treating it as `restarts: 0`
    would reset the cap on any corrupt write, which is the fail-open direction: the cap
    exists to stop a restart loop, and a loop is exactly what a silently-reset counter
    permits. Absence is genuinely fresh — a task this leg has never seen.
    """
    path = state_path(vault, task)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return fresh_state(), None
    except (OSError, ValueError) as error:
        return None, f"state unreadable: {error}"
    if not isinstance(data, dict):
        return None, "state is not an object"
    restarts = data.get("restarts", 0)
    consumed = data.get("consumed", [])
    if not isinstance(restarts, int) or isinstance(restarts, bool):
        return None, f"state restarts is {type(restarts).__name__}, not int"
    if not isinstance(consumed, list) or not all(isinstance(c, str) for c in consumed):
        return None, "state consumed is not a list of strings"
    return {"restarts": restarts, "progress_at": data.get("progress_at"),
            "consumed": consumed}, None


def write_state(vault, task, state):
    """True when the store landed. **A failed write must stop the act, not be swallowed.**

    The consumed marker is written BEFORE the restart precisely so one answer cannot become
    a loop. If the write fails and the restart runs anyway, the marker is gone, the card
    still reads `ANSWERED` next round, and the worker is killed again — the loop the marker
    exists to prevent. The cap has the same shape: a lost count is a bypassed cap.
    """
    path = state_path(vault, task)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def task_progress_stamp(vault, dirname, task):
    """The task page's mtime — the reset signal, exactly `worker-keepalive.py`'s."""
    directory = os.path.join(vault, dirname)
    try:
        for entry in os.listdir(directory):
            if entry.lower() == task.lower() + ".md":
                return os.path.getmtime(os.path.join(directory, entry))
    except OSError:
        pass
    return None


def is_held(session_id):
    """`(held, reason)`. A probe that cannot decide reads as held — never as clear.

    ⚠️ **A missing `session-holds.py` is unknown, not clear.** The backstop is real —
    `restart-worker.py` refuses a held target itself — but it only guards the KILL, not the
    card: posting a restart offer for a session the operator has held is the act the hold
    exists to prevent, and the card would sit there inviting it.
    """
    if not os.path.exists(HOLDS):
        return True, "hold probe absent"
    try:
        out = subprocess.run(
            [sys.executable, HOLDS, "is-held", session_id],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return True, "hold probe failed"
    if out.returncode == 1:
        return False, ""
    return True, (out.stdout or "").strip() or "held"


def refusal(session_id, dry_run=True, lift=False):
    """`(exit_code, token, first_line)` from `restart-worker.py`.

    Two mutually exclusive shapes, and the split IS the operator's 2026-11-12 ruling:

      * unattended (`dry_run`) — `--reason lapse --dry-run`. The token is what the round
        reports; nothing is signalled, so a row that flipped to `idle` between the sweep and
        this call cannot be restarted by a round that was only trying to report a refusal.
      * answered (`lift`) — `--reason lapse --allow-busy-lapse --fresh-prompt-task`. This is
        the ONE call site that may pass the lift, reached only from the operator's
        `ANSWERED:` on this session's own card. `--fresh-prompt-task` is what re-anchors the
        fresh session to this task; without it the worker comes back as a fleet manager.

    The token is read from the script's OWN output, never composed here — a caller that
    printed a constant would pass a criterion that exists to stop exactly that.
    """
    argv = [sys.executable, RESTART, session_id, "--reason", "lapse"]
    if lift:
        argv += ["--allow-busy-lapse", "--fresh-prompt-task"]
    if dry_run:
        argv.append("--dry-run")
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        return None, "cannot-tell", str(error)
    lines = (out.stdout or "").strip().splitlines()
    head = lines[0].strip() if lines else ""
    if out.returncode == 0:
        return 0, "ok", head
    return out.returncode, head, (lines[1].strip() if len(lines) > 1 else "")


def post_card(task, session_id, token_detail):
    """Post ONE card for this session. Returns its item id, or `""`.

    `attention-ask.py post` is idempotent on the dedup key, so a second round reaches the
    same item rather than stacking a duplicate — the "one card at a time per session" rule
    is enforced by the key, not by a reader's care.
    """
    ask = ASK
    if not os.path.exists(ask):
        return ""
    try:
        out = subprocess.run(
            [sys.executable, ask, "post",
             "--dedup-key", f"stuck-tab:{slug_for(task)}",
             # A stable producer id: the asking session reads its own item back, and the
             # fleet round that posts is not always the session that polls on a later tick.
             "--producer-id", f"stalled-tab:{slug_for(task)}",
             "--payload", (f"STALLED TAB WORKER: {task} ({session_id[:8]}) has made no "
                           f"progress for 2+ sweeps. {token_detail} "
                           f"Restart it now? This starts a FRESH session bound to this task."),
             "--option", OPTION_RESTART, "--option", OPTION_LEAVE,
             "--recommend", OPTION_RESTART, "--interrupt-class", "pick"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    # ⚠️ The id is on the `ITEM_ID:` line, and `post` prints a SECOND line
    # (`POLL: python3 attention-ask.py poll <id>`) after it. Reading the last line yields
    # that command, `poll` then answers `cannot-tell` forever, and the leg reports `carded`
    # every round while never restarting anything — a silent no-op that reads as success.
    for line in (out.stdout or "").splitlines():
        if line.startswith("ITEM_ID:"):
            return line[len("ITEM_ID:"):].strip()
    return ""


def poll_card(item_id):
    """`(verdict, content)` for this item — `content` is what the operator actually chose.

    Only `ANSWERED` releases the gate. A declined or hidden card is NOT an answer, and
    neither is a probe that failed — `attention-ask.py`'s own five terminals own that rule.

    ⚠️ **`ANSWERED` alone is not permission to kill.** The card offers `restart it` and
    `leave it`, and `attention-ask.py` prints `ANSWERED: <content>`. Acting on the terminal
    alone means an operator who chose **leave it** gets their worker killed — the card's
    safest answer becoming the most destructive one. The caller compares `content`.
    """
    ask = ASK
    if not item_id or not os.path.exists(ask):
        return "cannot-tell", ""
    try:
        out = subprocess.run(
            [sys.executable, ask, "poll", item_id],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return "cannot-tell", ""
    lines = (out.stdout or "").strip().splitlines()
    head = lines[0].strip() if lines else ""
    for word in ("ANSWERED", "DECLINED", "HIDDEN", "OPEN"):
        if head.startswith(word):
            if word != "ANSWERED":
                return word, ""
            return word, head[len("ANSWERED:"):].strip() if head.startswith("ANSWERED:") else ""
    return "cannot-tell", ""


def escalate(task, session_id, restarts):
    """One escalation card once the cap is spent. Returns True when it was posted."""
    ask = ASK
    if not os.path.exists(ask):
        return False
    try:
        out = subprocess.run(
            [sys.executable, ask, "post",
             "--dedup-key", f"stuck-tab-capped:{slug_for(task)}",
             "--payload", (f"STALLED TAB WORKER CAP: {task} ({session_id[:8]}) has been "
                           f"restarted {restarts} times with no progress. No further restart "
                           f"will run automatically. This needs a person."),
             "--option", "acknowledge", "--recommend", "acknowledge",
             "--interrupt-class", "ack"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Report a stalled tab worker's refusal and hand its restart to the "
                    "operator through one card.",
    )
    ap.add_argument("--vault", required=True)
    ap.add_argument("--task", required=True, help="the task name (the page's stem)")
    ap.add_argument("--dir", default="25 Tasks", help="the vault's tasks directory")
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--stall-count", type=int, default=0,
                    help="the sweep's `stall_count`; the row is only acted on at >= 2")
    args = ap.parse_args(argv)

    if args.stall_count < 2:
        # Not this leg's row. Printing a verdict rather than nothing keeps "the leg ran and
        # found nothing" distinguishable from "the leg never ran".
        print("skipped")
        print(f"stall_count {args.stall_count} < 2")
        return 0

    held, reason = is_held(args.session_id)
    if held:
        print("held")
        print(reason)
        return 0

    state, state_error = read_state(args.vault, args.task)
    if state_error is not None:
        # Fail closed: a corrupt store could hide a spent cap or a consumed card, and both
        # of those exist to stop a restart. Refusing to act is the only safe reading.
        print("cannot-tell")
        print(state_error)
        return 0

    stamp = task_progress_stamp(args.vault, args.dir, args.task)
    if stamp is not None and state.get("progress_at") is not None and stamp > state["progress_at"]:
        # Progress since the last restart resets the cap — the same reset condition
        # `worker-keepalive.py` uses, and the reason a capped row is not capped forever.
        state["restarts"] = 0
        if not write_state(args.vault, args.task, state):
            print("cannot-tell")
            print("the cap reset could not be recorded")
            return 0

    code, token, detail = refusal(args.session_id)
    if code is None:
        print("cannot-tell")
        print(detail)
        return 0

    if code == 0:
        # A `--dry-run` that passes means the row is no longer busy — the stall cleared
        # between the sweep and this call. Leave it; never act on a row that is not stalled.
        print("skipped")
        print("not-busy (the dry run would have restarted it)")
        return 0

    if token != "busy-target":
        # Every other refusal is the row's answer for this round. The token is quoted from
        # the invocation, never composed here.
        print("skipped")
        print(token)
        return 0

    if state["restarts"] >= RESTART_CAP:
        escalate(args.task, args.session_id, state["restarts"])
        print("capped")
        print(f"{state['restarts']} restarts with no progress")
        return 0

    item = post_card(args.task, args.session_id, detail)
    if not item:
        print("cannot-tell")
        print("the card could not be posted")
        return 0

    verdict, answer = poll_card(item)
    if verdict != "ANSWERED":
        print("carded")
        print(f"card {item} is {verdict}")
        return 0

    if item in state["consumed"]:
        # Once per card, marked before the act: an answer must never become a loop.
        print("carded")
        print(f"card {item} was already consumed")
        return 0

    if answer != OPTION_RESTART:
        # `ANSWERED` alone is not permission to kill — the operator chose LEAVE. Record it
        # as consumed so the same answer is not re-read every round, and do nothing else.
        state["consumed"].append(item)
        if not write_state(args.vault, args.task, state):
            print("cannot-tell")
            print("the answer could not be recorded")
            return 0
        print("carded")
        print(f"card {item} answered {answer!r} — not restarting")
        return 0

    state["consumed"].append(item)
    state["restarts"] += 1
    state["progress_at"] = stamp
    if not write_state(args.vault, args.task, state):
        # ⚠️ The marker is written BEFORE the act. If it cannot be written, the act must not
        # run: the card still reads `ANSWERED`, so a restart here would repeat every round —
        # the exact loop the marker exists to prevent.
        print("cannot-tell")
        print("the restart could not be recorded, so nothing was restarted")
        return 0

    # The real restart: the operator's answer IS the judgement the ruling requires, so this
    # is the only call site that passes `--allow-busy-lapse`.
    code, token, detail = refusal(args.session_id, dry_run=False, lift=True)
    if code is None or code != 0:
        print("cannot-tell")
        print(f"restart refused: {token} {detail}".strip())
        return 1
    print("restart")
    print(detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
