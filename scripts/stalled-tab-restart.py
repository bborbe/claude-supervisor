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
import fcntl
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


def answer_value(content):
    """`option: restart it` -> `restart it`.

    ⚠️ **The store does not print the bare label.** `attention-ask.py` renders an answer
    through `describe_one`, whose shape is `{kind}: {value}` — so an option click arrives as
    `option: restart it`. Comparing the raw content against `restart it` never matches, and
    because the mismatch falls into the leave-it branch, every operator approval is recorded
    as a decline and **no restart ever runs**. The kind prefix is stripped here, once.
    """
    content = (content or "").strip()
    for kind in ("option: ", "values: ", "text: "):
        if content.startswith(kind):
            return content[len(kind):].strip()
    return content


def slug_for(task):
    """A filesystem-safe key for a task name, stable across rounds."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in task.lower())
    return cleaned.strip("-")[:60] or hashlib.sha1(task.encode()).hexdigest()[:12]


def state_path(vault, task):
    key = hashlib.sha1(os.path.abspath(vault).encode()).hexdigest()[:10]
    return os.path.join(STATE_DIR, key, f"{slug_for(task)}.json")


def fresh_state():
    return {"restarts": 0, "progress_at": None, "consumed": [], "declined": False,
            "declined_at": None, "escalated": None, "card_item": None}


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
    # ⚠️ `progress_at` is compared with `>` against a float mtime. A JSON-valid string (or
    # list, or dict) raises TypeError there — an unhandled crash, not the fail-closed
    # `cannot-tell` this store discipline requires. It is validated with the rest.
    progress_at = data.get("progress_at")
    if progress_at is not None and (
        not isinstance(progress_at, (int, float)) or isinstance(progress_at, bool)
    ):
        return None, f"state progress_at is {type(progress_at).__name__}, not a number"
    return {"restarts": restarts, "progress_at": progress_at,
            "consumed": consumed, "declined": bool(data.get("declined")),
            "declined_at": data.get("declined_at"),
            "card_item": data.get("card_item"),
            "escalated": data.get("escalated")}, None


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
        # ⚠️ The FIRST line is the header — `🔄 restart-worker · session <sid> · pid <pid>` —
        # and it names the session that just DIED. The line that says what happened is the
        # last `✅` one, and it is the only one carrying the new pane and the new session id.
        # Returning the header made the leg report the old sid as though it were the result,
        # which is precisely the confusion the restart row warns about.
        ok = next((ln.strip() for ln in reversed(lines) if ln.strip().startswith("✅")), head)
        return 0, "ok", ok
    return out.returncode, head, (lines[1].strip() if len(lines) > 1 else "")


def card_key(task, state):
    """The dedup key, carrying a generation so a repeat stall is re-asked.

    ⚠️ **A bare `stuck-tab:<slug>` would ask the operator once, ever.** The store dedups on
    producer + key, so after the first card is answered and consumed, a later stall of the
    same task would resolve to that same item, hit the "already consumed" branch, and the
    operator would never be asked again — the leg would go quiet on exactly the row it
    exists for.

    ⚠️ **The generation is the RESTART count only — never the consumed count.** Folding
    `len(consumed)` in looks right and is not: answering `leave it` consumes the card, which
    would change the key and mint a fresh card on the very next round. The operator would be
    asked again and again after saying no. `restarts` changes only when a restart actually
    ran, which is exactly the boundary a new episode starts at.
    """
    return f"stuck-tab:{slug_for(task)}:{state['restarts']}"


def post_card(task, session_id, token_detail, state):
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
             "--dedup-key", card_key(task, state),
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
             # The generation is the restart count, exactly as `card_key`'s is: after a
             # progress reset the cap can be spent again, and a bare slug would resolve to
             # the already-consumed escalation and never re-alert.
             "--dedup-key", f"stuck-tab-capped:{slug_for(task)}:{restarts}",
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

    return with_lock(args)


def with_lock(args):
    """Serialise two rounds on one task before acting.

    ⚠️ **The store is a read-modify-write across a subprocess boundary.** Between
    `read_state` and `write_state` this script spawns `attention-ask.py` and
    `restart-worker.py`, which takes seconds; two rounds overlapping on the same task would
    both read `restarts: 0`, both see an `ANSWERED` card, and both restart the worker — the
    cap bypassed by timing rather than by logic. A non-blocking lock makes the loser report
    `cannot-tell` and try again next round, which is the same direction every other
    undecidable probe in this file takes.
    """
    lock_path = state_path(args.vault, args.task) + ".lock"
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        handle = open(lock_path, "w", encoding="utf-8")
    except OSError as error:
        print("cannot-tell")
        print(f"the task lock could not be opened: {error}")
        return 0
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("cannot-tell")
            print("another round holds this task's lock")
            return 0
        return act(args)
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def act(args):
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
        # ⚠️ A stale decline must not survive the generation change. Left in place, a
        # `leave it` recorded at generation N silences every later episode that reaches N
        # again — the leg goes permanently quiet on the one row it exists for, which is the
        # failure this whole file is written to prevent.
        state["declined"] = False
        state["declined_at"] = None
        state["escalated"] = None
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
        # ⚠️ **The escalation is remembered in OUR store, for the same reason a decline is:**
        # the attention store's suppression is open-scoped, so once `acknowledge` is answered
        # the fixed key mints a NEW card on the next round and the operator is re-alerted
        # every tick for a condition they already acknowledged. One escalation per spent cap.
        if state.get("escalated") == state["restarts"]:
            print("capped")
            print(f"{state['restarts']} restarts with no progress (already escalated)")
            return 0
        # A failed escalation post must not print `capped` as though the operator had been
        # told — the verdict's whole content is "one card was posted, and nothing more will
        # happen". A silent failure there is the row going quiet at its worst moment.
        if not escalate(args.task, args.session_id, state["restarts"]):
            print("cannot-tell")
            print(f"{state['restarts']} restarts with no progress, and the escalation "
                  "card could not be posted")
            return 0
        state["escalated"] = state["restarts"]
        write_state(args.vault, args.task, state)
        print("capped")
        print(f"{state['restarts']} restarts with no progress")
        return 0

    # ⚠️ **The store's suppression is OPEN-scoped, so a `leave it` is not remembered for us.**
    # Once the card is answered the same dedup key posts a NEW item, and without this the
    # operator is asked again every round — the exact failure the key generation was supposed
    # to prevent and does not. The decline has to live in our own store, scoped to the
    # episode: a restart starts a new generation, which is when asking again is correct.
    # ⚠️ **Anchored on the task page's mtime, not the restart generation.** A generation
    # comparison has no way out of the gen-0 case: a decline recorded before any restart
    # leaves `declined == restarts == 0` and the cap reset never runs, because it needs a
    # `progress_at` only a restart sets — so the leg goes permanently quiet on that row.
    # The page moving is the real "this is a new episode" signal, and it exists from round 1.
    # ⚠️ The flag and the mtime are BOTH needed. `declined_at` alone loses the decline when
    # the task page cannot be resolved at answer time (`stamp is None`), and the operator is
    # then re-asked every round — the failure this branch exists to prevent, reachable
    # through an unreadable page rather than through a stale generation.
    if state.get("declined"):
        declined_at = state.get("declined_at")
        if declined_at is None or stamp is None or stamp <= declined_at:
            print("carded")
            print("the operator declined this episode and the task has not moved since")
            return 0

    # ⚠️ **Poll the card a PREVIOUS round posted — never the one this round just made.**
    # Posting and polling in the same round can only ever read `OPEN`: the operator has not
    # answered yet. And because the store's dedup is open-scoped, the next round's post mints
    # a NEW item, so the answered one is never polled — `ANSWERED` is unreachable, the
    # operator is re-asked every round, and no restart ever runs. That is a no-op of the
    # feature's whole mechanism, so the posted id is remembered in our own store and polled
    # on the round AFTER it was posted.
    item = state.get("card_item")
    if not item or item in state["consumed"]:
        item = post_card(args.task, args.session_id, detail, state)
        if not item:
            print("cannot-tell")
            print("the card could not be posted")
            return 0
        state["card_item"] = item
        if not write_state(args.vault, args.task, state):
            print("cannot-tell")
            print("the card id could not be recorded, so its answer could never be read")
            return 0
        print("carded")
        print(f"card {item} posted; its answer is read next round")
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

    if answer_value(answer) != OPTION_RESTART:
        # `ANSWERED` alone is not permission to kill — the operator chose LEAVE. Record it
        # as consumed so the same answer is not re-read every round, and do nothing else.
        state["consumed"].append(item)
        # Remembered in OUR store: the attention store drops the suppression once the card
        # is answered, so without this the next round posts a fresh card and asks again.
        state["declined"] = True
        state["declined_at"] = stamp
        # The card is spent, so the next episode posts a fresh one rather than polling this.
        state["card_item"] = None
        if not write_state(args.vault, args.task, state):
            print("cannot-tell")
            print("the answer could not be recorded")
            return 0
        print("carded")
        print(f"card {item} answered {answer!r} — not restarting")
        return 0

    state["consumed"].append(item)
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
        # ⚠️ The cap is charged AFTER the restart, never before. A refused restart that
        # still spent the cap would silently reduce how many attempts the operator gets for
        # a failure they never caused — and the refusal may be one a later round can clear.
        print("cannot-tell")
        print(f"restart refused: {token} {detail}".strip())
        return 1

    state["restarts"] += 1
    state["progress_at"] = stamp
    # The generation advanced, so the previous episode's decline and escalation are spent,
    # and its card is spent with them — the next stall posts a new one.
    state["declined"] = False
    state["declined_at"] = None
    state["card_item"] = None
    state["escalated"] = None
    if not write_state(args.vault, args.task, state):
        # The consumed marker already landed, so this cannot become a loop; what is lost is
        # the cap charge. Reported rather than swallowed: the next round reads the stale
        # count and would offer one more restart than the cap allows.
        print("restart")
        print(detail)
        print("⚠️ the cap charge could not be recorded — the next round may allow one more")
        return 0
    print("restart")
    print(detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
