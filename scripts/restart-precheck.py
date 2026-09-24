#!/usr/bin/env python3
"""`restart-precheck.py` — the read-only half of `/supervisor:worker-restart`.

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
  undetermined   the transcript could not be read. Fail closed: nothing is typed.

Exit codes: 0 classified, 1 refusal (`unknown-session-id`, `ambiguous-pid`,
`dirty-worktree`), 2 usage error. The refusal token is the first line of output, the
same contract `restart-worker.py` uses.

⚠️ **This script is read-only.** It signals nothing and spawns nothing, which is what
makes it safe to run as a dry check on any session.
"""
import argparse
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

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
    dirty worktree, and all 17 Personal-vault sessions were dirty by the SAME three
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


def classify(rec, wnm):
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
    return "gate-held", closer


def main():
    parser = argparse.ArgumentParser(
        description="Read-only pre-kill probe for /supervisor:worker-restart.",
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
