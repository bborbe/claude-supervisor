#!/usr/bin/env python3
"""Decide the heal for a STUCK headless worker — and never perform it itself.

A headless worker whose permission channel dies ("Stream closed") cannot be
restarted the way a tab worker can. `scripts/restart-worker.py` refuses a headless
target by design — *"a headless worker has no reliable resume guard, so its
liveness stays undetermined"* — and the resume it *does* perform is a
`wezterm cli spawn`, which is a tab. So the heal runs through the supervisor's
`spawn_agent` MCP tool, and **only a Claude session can call that**.

That constraint is the whole design. `heal()` takes its effects as ARGUMENTS
rather than performing them, so the decision is testable without a supervisor, a
vault or a pane, and `main()` prints the action for a manager to execute. A script
that tried to resume a worker itself would have to reimplement an MCP client to do
it, and would then be a second resume mechanism beside the one that already
refuses this case.

The ladder, and why it stops:

  not yet resumed → resume headless once. A first death is a transient; the
                    channel restarts and the worker carries on in the same id.
  resumed already → reopen as an INTERACTIVE tab. The first resume did not hold,
                    so the cause is the channel, not the blip.

⚠️ **`idle-no-wait` is not a death and takes the same ladder.** A headless worker
whose turn ended with no declared wait and whose transcript has not moved for two
fleet rounds is idle rather than dead: the fleet's own end-of-life
(`reap-idle-headless.py`) closes its process at 15 minutes and nothing resumed it,
so the task sits with open boxes and no live owner. The remedy is the same fresh
process on the SAME session id, then a tab if that did not hold. The decision
lives here rather than in the drive leg so `fleet-drive` and the attention
watcher cannot drift into two ladders.

⚠️ There is no third rung, and that is structural rather than an omission. The
spawn ledger is keyed by session id and a resume keeps the SAME id, so a resume
OVERWRITES its record in place: the ledger can say "resumed at least once" and
never "resumed twice". The bound therefore holds through the second rung itself —
the interactive reopen is a new TAB session, which the watcher reads as
`stuck-tab:` and never feeds back here, and the dead headless session has no
heartbeat left to raise another gate with. A failed reopen is reported and not
retried: the operator decides, the ladder does not loop.

A parked JUDGEMENT gate is not a death and is never healed: it is reported, and
`answer_permission` is never called. Auto-answering a judgement gate is out of
scope for this task and would be the worst possible direction — it settles a
question the operator was asked, on their behalf, silently.

⚠️ A `🔴 BLOCKED` closer carrying an answerable `👤 You:` verb is that same parked
shape and does not arrive here at all: the watcher files it as a park reason
(`registry:waiting` / `idle+closer`), not as a `stuck-tab:` cause. Only the
VERBLESS closer — `👤 You: nothing`, where no card was minted and nobody was told —
still reaches `--kind blocked-closer`, and it stays report-only like any other
non-`HEALABLE_KINDS` kind.
"""
import argparse
import importlib.util
import os
import sys

ACTION_RESUME_HEADLESS = "resume-headless"
ACTION_REOPEN_INTERACTIVE = "reopen-interactive"
ACTION_REPORT_ONLY = "report-only"

# The deaths this heals. A parked gate is not a death — see the module docstring —
# and an unrecognised kind is reported rather than guessed at.
#
# ⚠️ **`permission-channel` is a death, not a park, and the ladder is the same one.**
# `stream-closed` is the same failure read at its most immediate (the LAST tool
# aborted); `permission-channel` is the same transport read over a window, for the
# shape where reads keep working between aborts. The fix measured on both is a fresh
# process: 2026-10-28 session `b6e447e4` reported "writes abort with `Stream
# closed`; reads still work" and a resume restored the channel. Reporting it
# instead — the treatment every non-`stream-closed` kind gets — would leave the one
# session that holds a live repair unable to run it, which is the defect this
# cause exists to name.
KIND_STREAM_CLOSED = "stream-closed"
KIND_PERMISSION_CHANNEL = "permission-channel"
# ⚠️ **`idle-no-wait` is the fleet round's cause, not the watcher's.** The watcher
# raises `stuck:` only on a death it can observe; a worker that simply stopped —
# turn ended, no declared wait, transcript frozen for two rounds — raises no gate at
# all, which is the gap this kind closes. It is healable for the same reason the
# deaths are: one fresh process on the same session id, bounded by the same ladder.
KIND_IDLE_NO_WAIT = "idle-no-wait"
HEALABLE_KINDS = (KIND_STREAM_CLOSED, KIND_PERMISSION_CHANNEL, KIND_IDLE_NO_WAIT)

#: The refusal a restart answers with when the target carries an open operator card.
#: Its own token, not a report-only verdict: a report-only line reads as "nothing to do",
#: while this is "do not touch it", and the two must not look alike.
REFUSAL_OPEN_CARD = "open-card"
REFUSAL_STORE_UNREACHABLE = "store-unreachable"

_SPAWN_LEDGER = None
_WHO_NEEDS_ME = None


def _who_needs_me():
    """Import `who-needs-me.py` — the one reader of the store's OPEN items."""
    global _WHO_NEEDS_ME
    if _WHO_NEEDS_ME is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "who-needs-me.py")
        spec = importlib.util.spec_from_file_location("who_needs_me", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _WHO_NEEDS_ME = mod
    return _WHO_NEEDS_ME


def open_card(session_id, endpoint=None):
    """(item id or None, error or None) for an OPEN attention card on this session.

    ⚠️ **An open card is a refusal, not a report.** Restarting a session that is
    waiting on an operator discards the question they were about to answer — the same
    reason `close-worker.py` carries `gate-held` and `restart-worker.py` carries
    `held-session`. The ladder's rungs are all *actions*, so this check sits ahead of
    `decide()`: a session with a live card is never resumed, whatever the cause.

    ⚠️ **An unreadable store is a refusal too, never a clearance.** A gate that
    cannot be read is not a gate that is absent, and the fail-open direction here
    restarts a worker whose operator is mid-answer.
    """
    wn = _who_needs_me()
    if endpoint:
        wn.STORE = endpoint
    try:
        items = wn.store_items()
    except Exception as exc:  # StoreUnreachable, and any parse failure behind it
        return None, str(exc)
    for item in items:
        if not isinstance(item, dict):
            continue
        # ⚠️ **`producer_id` is the store's own field for the asking session.** Reading
        # `session_id` here was the defect: that name exists only on `who-needs-me.py`'s
        # NORMALISED rows (`normalize_store_item`), while `store_items()` hands back the
        # raw wire shape, so the gate matched nothing and refused nothing — a no-op that
        # its own fixture could not catch, because the fixture invented the normalised
        # spelling. `session_id` stays as a fallback for a caller that passes normalised
        # rows.
        producer = str(item.get("producer_id") or item.get("session_id") or "")
        if not producer:
            continue
        # ⚠️ **A PREFIX, not equality.** `--session` is an 8-char display handle — the
        # form the watcher prints and the manager holds — while `producer_id` is a full
        # uuid. Equality would never match, which is the second half of the same no-op.
        if producer == session_id or producer.startswith(session_id):
            return item.get("item_id") or item.get("id"), None
    return None, None


def _spawn_ledger():
    """Import `spawn-ledger.py` (hyphenated filename -> importlib) — the ONE ledger reader.

    ⚠️ **The ledger moved to the attention store's endpoint on 2026-10-14, and this file
    delegates rather than resolving a directory of its own.** It used to mirror
    `server/config.mjs`'s resolution a third time; a second copy of a store's address is what
    drifts, and this one drifts silently in the direction that answers "never resumed" for a
    session that has in fact been resumed — which resumes it again.
    """
    global _SPAWN_LEDGER
    if _SPAWN_LEDGER is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spawn-ledger.py")
        spec = importlib.util.spec_from_file_location("spawn_ledger", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SPAWN_LEDGER = mod
    return _SPAWN_LEDGER


def decide(kind, deaths):
    """The action for a death of `kind`, given `deaths` prior headless resumes.

    ⚠️ `deaths` counts HEADLESS RESUMES, not deaths, and from the real ledger it is
    only ever 0 or 1 (see the module docstring). Any non-zero value is the
    interactive rung — never a second headless resume.

    ⚠️ A kind outside `HEALABLE_KINDS` is REPORT-ONLY, never a resume and never
    an answer. A parked judgement gate reaching a resume would restart a worker
    that is working correctly, and reaching an answer would settle the operator's
    question for them.
    """
    if kind not in HEALABLE_KINDS:
        return ACTION_REPORT_ONLY
    if deaths == 0:
        return ACTION_RESUME_HEADLESS
    return ACTION_REOPEN_INTERACTIVE


def deaths_from_ledger(session_id, endpoint=None):
    """How many times `session_id` has already been resumed.

    Read from the spawn ledger, which is the only store that keeps this: the
    session registry forgets a session exactly when it exits, and the transcript
    records the conversation rather than the spawns. A resume keeps the SAME
    session id, so the chain is counted by `resumed_from`, not by distinct ids.

    ⚠️ A missing or unreadable ledger returns 0 rather than raising. Zero means
    "resume headless", which is the first rung — the recoverable direction. The
    opposite default would skip straight to reporting a worker that has never been
    resumed.

    ⚠️ **The store is the attention store's endpoint now, and the rows arrive as a map keyed
    by session id** — the shape this scan already wanted, where the file version had to
    rebuild it from a directory listing.
    """
    ledger = _spawn_ledger().read_ledger(endpoint) or {}
    n = 0
    for rec in ledger.values():
        if not isinstance(rec, dict):
            # Well-formed JSON that is not a record — skip it, never crash on it. The shared
            # reader already drops a non-dict row, so this is a belt-and-braces guard rather
            # than the only one.
            continue
        # ⚠️ A PREFIX is a legal lookup and is the form a manager actually holds:
        # the watcher prints 8 chars, so an exact match would silently count 0 and
        # resume a session that had already been resumed — the loop the bound
        # exists to stop, reached through the join rather than the ladder.
        rf = str(rec.get("resumed_from") or "")
        if rf and rf.startswith(session_id):
            n += 1
    return n


def heal(session_id, task, kind, deaths, *, spawn_headless, spawn_interactive,
         set_task_mode, post_card, jump_link):
    """Run the action `decide()` names, through the injected effects.

    Every effect is a callable the caller supplies, because every one of them is a
    tool only a manager session holds: the two spawns and `set_task_mode` are the
    manager's own, and `post_card` / `jump_link` are shell helpers. Injecting them
    is what lets a test assert "exactly one headless resume, and the session id
    unchanged" with no supervisor in the loop.

    Returns the action taken, so a caller can log it without re-deriving it.
    """
    action = decide(kind, deaths)
    if action == ACTION_RESUME_HEADLESS:
        # Same session id — the resume continues the conversation rather than
        # starting a new one, which is what keeps the task's claude_session_id
        # pointing at the work.
        spawn_headless(session_id)
        return action
    if action == ACTION_REOPEN_INTERACTIVE:
        # Mode first, then the tab: a tab opened before the task says
        # `interactive` is a tab the next reader will treat as headless.
        set_task_mode(task, "interactive")
        try:
            # A fresh tab for the TASK, never a resume of the session id: the server
            # refuses `resume` with `interactive:true` (server/tab.mjs
            # resumeSupportError) — the tab launcher is never handed the flag.
            pane = spawn_interactive(task)
        except Exception:
            # Never leave the task saying `interactive` with no tab behind it: put
            # the mode back and tell the operator, then let the failure surface.
            set_task_mode(task, "headless")
            try:
                post_card(f"{task} died twice with a closed permission channel — "
                          f"the interactive reopen FAILED; mode restored to "
                          f"headless", None)
            except Exception:
                pass  # the spawn failure below is the one the operator needs
            raise
        post_card(f"{task} died twice with a closed permission channel — reopened "
                  f"as an interactive tab", jump_link(pane))
        return action
    post_card(f"{task} is STUCK ({kind}) and past the heal ladder — no resume "
              f"attempted", None)
    return action


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", required=True, help="the stuck session id")
    ap.add_argument("--task", required=True, help="its task name")
    # Required, not defaulted: a default of `stream-closed` would turn every
    # caller that forgot it into a headless resume — the one rung that acts.
    # Constrained, so a mistyped or un-split value (`stuck:stream-closed`) is an
    # error rather than a silent report-only that reads like a real verdict.
    #
    # ⚠️ **`blocked-closer` here means the VERBLESS closer only** — a `🔴 BLOCKED`
    # panel whose `👤 You:` line says `nothing` (or is absent). A closer carrying an
    # answerable verb is a park, not a death, so the watcher files it as
    # `registry:waiting` / `idle+closer` and it never reaches this script as a
    # `stuck-tab:` cause. The choice stays because the verbless class still exists,
    # and `decide()` already reports it rather than resuming or answering it.
    ap.add_argument("--kind", required=True,
                    choices=HEALABLE_KINDS + ("blocked-closer", "permission-failures"),
                    help="the stuck cause: the reason after `stuck:` (`blocked-closer` "
                         "is the VERBLESS closer — a verb-carrying one is a park, "
                         "not a stuck cause)")
    ap.add_argument("--endpoint", default=None, help="override the attention store base URL")
    ap.add_argument("--ledger-dir", default=None,
                    help="ACCEPTED AND IGNORED — the ledger comes from the endpoint")
    args = ap.parse_args(argv)
    # ⚠️ Kept and IGNORED rather than removed, the same call `worker-sessions.py` made for
    # `--dir` / `--heartbeat-dir`: the manager's own call sites pass it, so removing it breaks
    # them, while accepting it in silence would let a caller believe it had redirected a read
    # that still went to the endpoint — and the read it would have redirected is the one that
    # decides whether a worker is resumed again.
    if args.ledger_dir:
        sys.stderr.write(
            "warning: --ledger-dir is accepted and IGNORED — the ledger is read from the "
            "session-ledger endpoint; use --endpoint to point at the store\n"
        )
    args.session = args.session.strip()
    if len(args.session) < 8:
        # A short or empty id prefix-matches every ledger record.
        ap.error("--session must be at least 8 characters of the session id")

    # ⚠️ **The card check runs BEFORE the ladder.** Every rung is an action, and a
    # session with a live operator card is waiting on a person: restarting it discards
    # the question they were about to answer. Two lines, token first, so a caller can
    # branch on the first line alone — the same shape the rest of this repo's refusals
    # use.
    card, store_error = open_card(args.session, args.endpoint)
    if store_error is not None:
        print(REFUSAL_STORE_UNREACHABLE)
        print(
            "%s: the attention store could not be read (%s) -- restart refused rather "
            "than cleared" % (args.session, store_error)
        )
        return 1
    if card:
        print(REFUSAL_OPEN_CARD)
        print(
            "%s: open operator card %s -- restart refused, the session is waiting on a "
            "person" % (args.session, card)
        )
        return 1

    deaths = deaths_from_ledger(args.session, args.endpoint)
    action = decide(args.kind, deaths)
    # The decision only. The manager holds the tools that perform it; printing the
    # action is how the two halves meet, and it keeps this script runnable on a
    # host with no supervisor at all.
    # The task is echoed so a mistyped name is visible on the line itself.
    print(f"HEAL {args.session} [{args.kind}] task={args.task!r} resumes={deaths} -> {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
