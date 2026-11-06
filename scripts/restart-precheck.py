#!/usr/bin/env python3
"""`restart-precheck.py` — the read-only half of `/supervisor:restart`.

`restart-worker.py` owns the kill+resume leg and refuses on seven conditions about the
TARGET. This script owns the two things it does not, both of which must be answered
BEFORE anything is signalled:

  1. **Is the target's worktree carrying tracked changes, and is that dirt its own?**
     A kill reverts nothing on disk, but it does strand the work a live session was
     mid-way through. Untracked paths are reported and do NOT refuse: real worker
     trees always carry scratch. Tracked changes refuse **only when the target is the
     sole live session in that worktree** — see `other_occupants()` for the fleet
     measurement that made sharing, not dirt, the discriminator.
  2. **What did the session die holding?** That answer picks whether anything is typed
     into the resumed pane afterwards. `docs/fleet-surface.md` § Spawn a worker states
     the two axes; the classifier below implements the cause-of-death half.

The second answer is derived, never re-invented: `closer_from_transcript()`,
`is_parked_verb()` and `LIVE_WINDOW` are imported from `who-needs-me.py`, the script
that already owns "what does this session's transcript tail say". Two copies of that
read would drift, and the drift would be invisible — both would keep returning a
string.

Classifications:

  mid-work       no `👤 You:` closer, a `nothing` closer, or a parked `later (on …)`.
                 Nothing is waiting on a human, so the restart note is DELIVERED.
                 The 2026-09-20 restart-request death lands here by construction: a
                 harness `restart Claude Code` gate is not a `👤 You:` line.
  gate-held      the transcript tail carries a real gate. The resumed pane replays it,
                 so nothing is typed — typing into a pane showing `Enter to select`
                 turns any keystroke into a menu selection.
  gate-unanswerable
                 a headless (`sdk-cli`, no pane) session whose closer names no channel
                 an answer can still travel by. A headless session's only channel is an
                 attention-store card, so its gate is answerable only when the closer
                 names a full store id whose card is `open`, or whose answer the store
                 records as `delivered`. Everything else — a truncated id, a card closed
                 without an answer, an answer never delivered, a command or pane with no
                 route back — is a gate nobody can release; holding it only keeps the
                 slot forever. ⚠️ Fails closed: an unreadable store keeps `gate-held`.
  undetermined   the transcript could not be read. Fail closed: nothing is typed.

Exit codes: 0 classified, 1 refusal (`unknown-session-id`, `ambiguous-pid`,
`dirty-worktree`), 2 usage error. The refusal token is the first line of output, the
same contract `restart-worker.py` uses.

⚠️ **This script is read-only.** It signals nothing and spawns nothing, which is what
makes it safe to run as a dry check on any session.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# The store the board answers through — the same env names `attention-ask.py` reads.
STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "5"))

# A full store item id. ⚠️ Shape only: `attention-ask.py` `STORE_ITEM_ID_LEN` is 32 and
# `poll` refuses anything shorter, so a shorter hex run is not a channel even when a
# prefix lookup would resolve it.
STORE_ID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")

HEADLESS_ENTRYPOINT = "sdk-cli"

OPEN_LIST_LIMIT = 1000


class StoreUnreadable(Exception):
    """The store could not answer — the caller must fail closed."""


def store_get(path):
    """GET one store path → parsed JSON, `None` on 404, `StoreUnreadable` otherwise."""
    headers = {}
    token = os.environ.get("ATTENTION_STORE_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{STORE}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=STORE_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise StoreUnreadable(f"store returned {err.code}") from err
    except Exception as err:
        raise StoreUnreadable(str(err)) from err


def card_is_live_channel(item_id, get=store_get):
    """True when an answer can still reach the asker through this card.

    Open → the board can still answer it. Answered → only when the store records the
    answer as `delivered`: an answered record with no observed delivery is the
    `attention-answer.py` trap (fixed: it used to print a `DELIVERY: supervisor poll`
    line naming a route without observing it), so the store reads answered and the
    worker never hears it.
    Closed without an answer → a clear, not an answer (`answered-attribution.py`).
    Absent → nothing left to answer.
    """
    item = get(f"/api/1.0/attention/{item_id}")
    if item is None:
        return False
    if not isinstance(item, dict):
        raise StoreUnreadable(f"item {item_id} is not an object")
    if item.get("state") == "open":
        return reaches_headless(item)
    if not item.get("answered_at"):
        return False
    # ⚠️ A missing or malformed delivery report must not read as "never delivered" — that
    # would release a gate on an unreadable store. Only a well-formed report decides.
    report = get(f"/api/1.0/attention/{item_id}/attempt")
    if not isinstance(report, dict) or "status" not in report:
        raise StoreUnreadable(f"no readable delivery report for {item_id}")
    return report.get("status") == "delivered"


def gate_is_answerable(rec, closer, get=store_get):
    """Whether any channel can still release `closer`'s gate for session `rec`.

    A session with a pane is always answerable — the operator can type into it; that is
    the behaviour before this check and it is deliberately unchanged. A headless session
    has only the store.
    """
    if (rec.get("entrypoint") or "") != HEADLESS_ENTRYPOINT:
        return True
    ids = STORE_ID_RE.findall((closer or "").lower())
    try:
        if has_open_own_card(rec.get("sessionId"), get):
            return True
        return any(card_is_live_channel(i, get) for i in ids)
    except Exception:
        # ⚠️ Deliberately broad: ANY failure reading the store — transport, protocol,
        # a shape this code does not expect — fails closed to `gate-held`. A crash here
        # would also abort `close-worker.py`, and a narrow list is exactly how an
        # unanticipated error class would slip through as a release.
        return True


def has_open_own_card(sid, get=store_get):
    """Whether the store holds an open card this session produced.

    ⚠️ The `Stop` hook mirrors every waiting closer into a card produced by the session
    itself, and that card's id is never in the closer text. So a headless closer naming
    no id can still be answerable — through its own mirror. `GET /api/1.0/attention`
    returns open items only; it ignores a `producer_id` filter, so the match is here.
    """
    if not sid:
        return False
    listing = get(f"/api/1.0/attention?limit={OPEN_LIST_LIMIT}")
    if isinstance(listing, dict) and isinstance(listing.get("items"), list):
        listing = listing["items"]
    if not isinstance(listing, list):
        # A 404 or an unexpected shape is not "no open cards" — fail closed.
        raise StoreUnreadable("open-card listing unreadable")
    if len(listing) >= OPEN_LIST_LIMIT:
        # A full page may have cut off this session's own mirror card.
        raise StoreUnreadable(f"open-card listing hit limit={OPEN_LIST_LIMIT}")
    if not all(isinstance(i, dict) for i in listing):
        raise StoreUnreadable("open-card listing holds a non-object entry")
    return any(i.get("producer_id") == sid and i.get("state", "open") == "open"
               and reaches_headless(i) for i in listing)


def reaches_headless(item):
    """Whether an answer to this open card can be delivered to a pane-less session.

    ⚠️ A `message`-class answer is delivered by `SendMessage`, which refuses a headless
    worker — measured 2026-11-05 on witness `78c15080`: the board answered its mirror
    card and the store recorded the delivery attempt as `failed`
    (`supervisor:message-delivery`). So an open `message` card is no channel for it. Any
    other or absent mechanism is kept as a channel: failing closed keeps `gate-held`.
    """
    return item.get("answer_mechanism") != "message"

SESSIONS_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSIONS_DIR") or "~/.claude/sessions"
)


def load_sibling(filename):
    """Import a sibling script by path — the repo's own convention for sharing code."""
    name = os.path.basename(filename).replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def refuse(reason, detail):
    """Print the reason token, then the sentence. Returns exit code 1."""
    print(reason)
    print(f"❌ {reason}: {detail}")
    return 1


def pid_is_alive(pid):
    """A pid we are not allowed to signal still exists — EPERM means "there, not yours"."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def other_occupants(entries, cwd, sid):
    """How many OTHER live sessions share this worktree.

    The number decides whether a dirty worktree is evidence about the target at all.
    Measured 2026-09-25 against the live fleet: 19 of 25 interactive sessions sat in a
    dirty worktree, and all 17 primary-vault sessions were dirty by the SAME three
    tracked files — mine and two siblings', not the target's own work. A refusal on
    that reading would have blocked 76% of the fleet on dirt that provably belonged to
    somebody else. Sharing is the discriminator, so it is measured rather than assumed.
    """
    if not cwd:
        return 0
    return sum(
        1 for _, rec in entries
        if rec.get("cwd") == cwd
        and rec.get("sessionId") != sid
        and pid_is_alive(rec.get("pid"))
    )


def worktree_probe(cwd):
    """`(verdict, tracked_lines, untracked_count)` for the session's own cwd.

    `not-a-repo` and `unknown` are distinct answers from `clean`, and neither refuses:
    a session whose cwd is not a git worktree has no tracked work to strand, and an
    unreadable probe proves nothing about one. Only `dirty` refuses.
    """
    if not cwd:
        return "unknown", [], 0
    try:
        done = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return "unknown", [], 0
    if done.returncode != 0:
        return "not-a-repo", [], 0
    tracked, untracked = [], 0
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            tracked.append(line)
    return ("dirty" if tracked else "clean"), tracked, untracked


def classify(rec, wnm, get=store_get):
    """The cause-of-death branch, derived from the transcript tail.

    `undetermined` is checked FIRST and on its own signal — an absent transcript — so
    that "the file is not there" can never be read as "the file says nothing", which
    is the same blind-is-not-unreadable error the runbook records for the headless
    liveness probe.

    ⚠️ **The record is re-keyed, and that is not cosmetic.** `who-needs-me.py` reads
    records from the attention store, which spells the field `session_id`; the session
    registry spells the same value `sessionId`. Handing the registry record straight
    through raises `KeyError: 'session_id'` on the transcript glob — measured on the
    first live run of this script. The shim carries the one key `who-needs-me.py`
    needs; nothing else about the record is its business.
    """
    sid = rec.get("sessionId")
    if wnm.session_transcript_age(sid) == float("inf"):
        return "undetermined", "(no transcript)"
    closer = wnm.closer_from_transcript({"session_id": sid})
    if not closer or closer.startswith("nothing") or wnm.is_parked_verb(closer):
        return "mid-work", closer or "(none)"
    if not gate_is_answerable(rec, closer, get):
        return "gate-unanswerable", closer
    return "gate-held", closer


def main():
    parser = argparse.ArgumentParser(
        description="Read-only pre-kill probe for /supervisor:restart.",
    )
    parser.add_argument("session_id")
    parser.add_argument("--sessions-dir", default=None)
    args = parser.parse_args()

    sid = (args.session_id or "").strip()
    if not sid:
        print("❌ usage: restart-precheck.py <session-id>", file=sys.stderr)
        return 2

    restart_worker = load_sibling("restart-worker.py")
    who_needs_me = load_sibling("who-needs-me.py")

    entries = restart_worker.registry_entries(args.sessions_dir or SESSIONS_DIR)
    if entries is None:
        return refuse("unknown-session-id", f"the registry at {SESSIONS_DIR} could not be read")

    path, rec = restart_worker.find_target(entries, sid)
    if path == "ambiguous":
        return refuse("ambiguous-pid", f"{len(rec)} registry entries claim session {sid}")
    if rec is None:
        return refuse("unknown-session-id", f"no registry entry carries session id {sid}")

    cwd = (rec.get("cwd") or "").strip()
    verdict, tracked, untracked = worktree_probe(cwd)
    others = other_occupants(entries, cwd, sid)
    age = who_needs_me.session_transcript_age(sid)
    branch, closer = classify(rec, who_needs_me)

    # The report is built first and emitted LAST, so that a refusal's token can be the
    # first line of stdout — the contract `restart-worker.py` sets and the one a caller
    # branches on. Printing the report eagerly put the token on line 8 instead, which a
    # first-line reader reads as success. Asserted by
    # `test_solo_dirty_worktree_refuses`; the diagnostic is kept, only reordered.
    report = [f"🔄 restart-precheck · session {sid[:8]} · pid {rec.get('pid')} · {rec.get('name')!r}"]
    report.append(f"   status: {rec.get('status')} · kind: {rec.get('kind')}")
    report.append(f"   cwd: {cwd or '(none)'}")
    report.append(f"   worktree: {verdict}" + (f" ({len(tracked)} tracked)" if tracked else ""))
    report.extend(f"      {line}" for line in tracked)
    if untracked:
        report.append(f"   untracked: {untracked} (noted, does not refuse)")
    if verdict == "dirty":
        report.append(f"   occupants: {others} other live session(s) share this worktree")
    if age == float("inf"):
        # `session_transcript_age` returns `inf` for a session with no transcript, and
        # `int(inf)` raises OverflowError — measured on the first fixture run, where it
        # crashed the report *before* the classifier could return `undetermined`. The
        # absent case is a real answer, so it gets its own rendering rather than a cast.
        age_txt, fresh = "absent", "absent"
    else:
        age_txt = f"{int(age)}s ago"
        fresh = "fresh" if age < who_needs_me.LIVE_WINDOW else "stale"
    report.append(f"   transcript: {age_txt} ({fresh}, LIVE_WINDOW {who_needs_me.LIVE_WINDOW}s)")
    report.append(f"   closer: {closer}")
    report.append(f"   classification: {branch}")

    if verdict == "dirty" and others == 0:
        code = refuse(
            "dirty-worktree",
            f"session {sid} is the only live session in {cwd} and that worktree has "
            f"{len(tracked)} tracked change(s); commit or stash them, or the restart "
            "strands that work",
        )
        print("\n".join(report))
        return code

    print("\n".join(report))
    if verdict == "dirty":
        print(
            f"   ⚠️ dirt noted, not refused — {others} other live session(s) share this "
            "worktree, so it is not this target's to own"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
