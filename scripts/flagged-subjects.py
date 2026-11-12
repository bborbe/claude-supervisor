#!/usr/bin/env python3
"""Select flagged goals and topics whose manager is not live, bounded by free manager slots.

Why this exists
---------------
The fleet round's flagged-open leg ran `/supervisor:open --flagged`, and that batch selects
**tasks only** (`commands/open.md` § the `--flagged` batch). A flagged **goal** or **topic**
with no live manager therefore stayed closed until the operator opened it by hand, even when
the fleet had free slots — measured 2026-11-08: 12 goals carried `flag: true` at
`status: in_progress`, six had a live manager and six did not, and no leg of the round
surfaced those six. The operator's ruling is that a flag means *open me* at every tier.

This script is the selection half of that leg. It decides **which** flagged subjects the
round should open and in what order; the leg runs `/supervisor:open --goal` / `--topic` for
each `OPEN` line, so there is no second spawn path here.

What counts as a subject
------------------------
A goal page or a topic page carrying `flag: true`, not terminal (`status` in
completed/aborted/hold is out, and so is a `completed:` date — closure leaves `status`
untouched on some pages), and not at `phase: todo` (the operator's approval inbox: the flag
says *open me*, but `commands/open.md` § Step 2B/2C refuses an unapproved page, so this
script reports it as `WAITING` and opens nothing).

`status: in_progress` is required for a **goal** (the operator's own criterion). A **topic**
only has to be non-terminal — topic pages in the wild carry `status: in_progress` but the
criterion is stated for goals, and an absent status is not a closure.

"Live manager" is answered by the session registry, never by the page field alone
-------------------------------------------------------------------------------
`session-liveness.py --list --json` is the reader. Each record carries `name` and
`formerNames`, and a subject is managed when any of those names, manager suffix stripped,
matches the subject's title — the same rule `scripts/fleet-board.py` owns
(`_norm_session_name` + `_matches_one_title`), imported rather than restated.

A goal is also managed when a live manager names a **topic that lists it** — the two-hop
chain — so the goal→topic map is read from the topic pages and both titles are tested.

⚠️ **A page `claude_session_id` is read too, but only as the second answer.** When the
registry names nothing, the page id is checked with `session-liveness.py --check`: `LIVE`
means the manager is running under a name the registry no longer carries (`RENAMED`),
`ABSENT` is a **resume** (`commands/open.md` § Step 2B item 3) and is therefore still
openable, and `UNKNOWN`/`AMBIGUOUS` is reported as `UNKNOWN` and never opened — an
unreadable verdict is not permission to spawn.

No open attention card
----------------------
A goal already carrying an open `fleet-propose-manager:<goal>` or `fleet-approve-manager:<goal>`
card is `CARDED`: the operator is being asked about it, and opening it under the question
would race the ask. An unreadable attention store is `REFUSED`, never "no cards" — the same
`UNKNOWN`-is-not-zero rule the rest of this plugin follows.

The slot gate
-------------
The free-slot count is `hard cap − live managers`, and both numbers come from the code that
owns them: the cap from `resolveMaxManagers` via `scripts/propose-managers.py:resolve_cap()`
(the operator's ceiling, `spawn.maxManagersHard`), the live count from that file's own
`live_manager_labels()` over one `fleet-board.py --json` read (the board's manager
classification, Fleet Manager excluded). No number is restated here.

⚠️ **`hard is None` is UNLIMITED, not zero** — the operator's `spawn.maxManagersSoft: 0` off
switch resolves `hardLimit: null`, and reading that as "no free slot" turns the off switch
into permanent silence.

Ordering — the one rule, stated once
------------------------------------
**Topics first, then goals.** The tiers multiply downwards: a topic manager can open its own
goals, and a goal manager can open its workers, so a manager slot spent high in the tree
covers more subjects than the same slot spent low. Goals are ranked by `priority` (1 = high),
then by approved live work (more first), then by name — the ranking
`propose-managers.py:qualify()` already applies.

⚠️ **Tasks are not in this script at all.** They spend the **worker** budget
(`spawn.maxWorkerSoft`), a different cap from the manager one, so tasks and managers never
compete for the same slot. `/supervisor:open --flagged` keeps opening them, unchanged — the
ordering question between tasks, goals and topics therefore has no shared budget to decide.

⚠️ **A goal under a topic this run is opening is not opened itself** — the topic manager owns
it the moment it starts, and opening both would double-manage the same work. Such a goal is
reported as `COVERED`, not silently dropped.

Output
------
One line per subject, pipe-separated, so a probe can assert on the first field:

    OPEN     | <vault> | topic | <name>
    OPEN     | <vault> | goal  | <name>
    DEFERRED | <vault> | goal  | <name> | <reason>
    COVERED  | <vault> | goal  | <name> | topic <topic> opened this round
    MANAGED  | <vault> | goal  | <name>
    WAITING  | <vault> | goal  | <name> | phase: todo
    CARDED   | <vault> | goal  | <name> | open card <dedup key>
    UNKNOWN  | <vault> | goal  | <name> | <reason>
    SUMMARY  | free=<n|unlimited> open=<n> deferred=<n> covered=<n> managed=<n> waiting=<n> carded=<n> unknown=<n>

`--json` prints the same computation as one object and nothing else.

Exit codes
----------
  0  the selection was computed (whether or not anything is openable)
  2  REFUSED — the vault set, the session registry, the manager cap, the live-manager count,
     the attention store, or a vault's goals/topics directory could not be read. Never read
     as "nothing to open".
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# A subject in one of these states is closed or paused: a manager must not be opened on it.
TERMINAL_STATUSES = {"completed", "aborted", "hold"}

# `session-liveness.py --check` exit codes. LIVE is the only one that means "a manager runs";
# ABSENT (1) is a resume, and the rest — UNKNOWN (2), AMBIGUOUS (3), MALFORMED (4) — are
# "cannot tell", which is never a spawn.
LIVE, ABSENT = 0, 1


def _load(name, filename):
    """Import a sibling script as a module — never a copy of its rules."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pm = _load("propose_managers", "propose-managers.py")
board = pm._board()

#: The one flag predicate — see `scripts/flag_provenance.py`. Before the operator's
#: 2026-11-10 ruling this module read `flag` with no provenance at all, so an agent-set
#: goal flag opened a manager here while the same write on a task was refused.
flag_provenance = _load("flag_provenance", "flag_provenance.py")


def live_sessions():
    """Every live session record — `name` and `formerNames` included — or None when unreadable."""
    try:
        proc = subprocess.run(
            ["python3", os.path.join(HERE, "session-liveness.py"), "--list", "--json"],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout)
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def session_state(session_id):
    """`session-liveness.py --check` exit code, or None when the probe could not run."""
    try:
        proc = subprocess.run(
            ["python3", os.path.join(HERE, "session-liveness.py"), "--check", session_id],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    return proc.returncode


def open_card_keys():
    """The dedup keys of the attention store's OPEN cards, or None when it could not be read."""
    try:
        answer = _load("attention_answer", "attention-answer.py")
        items = answer.fetch_open()
    except Exception:
        return None
    if not isinstance(items, list):
        return None
    return {str(item.get("dedup_key") or "") for item in items if isinstance(item, dict)}


def page_rows(directory):
    """`(title, frontmatter block)` for every page — or None when the directory cannot be read.

    ⚠️ **`None` is "unreadable", never "empty".** An unreadable `goals_dir` or `topics_dir`
    that returned `[]` would render as a clean round with nothing to open — the exact
    "a suppressed leg and a leg that opened nothing must never print the same" failure. The
    caller turns `None` into exit 2 REFUSED. A vault with no such directory at all is
    legitimate and returns `[]`.
    """
    rows = []
    if not directory:
        return rows
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    for filename in names:
        if not filename.endswith(".md"):
            continue
        try:
            with open(os.path.join(directory, filename), encoding="utf-8") as handle:
                block = pm.frontmatter(handle.read())
        except OSError:
            continue
        if block:
            rows.append((filename[:-3], block))
    return rows


def subjects(vault, kind):
    """The flagged, non-terminal pages of one kind in one vault — or None when unreadable."""
    directory = vault.get("goals_dir") if kind == "goal" else vault.get("topics_dir")
    rows = page_rows(directory)
    if rows is None:
        return None
    out = []
    for name, block in rows:
        if not flag_provenance.flag_admits(block, kind):
            continue
        status = (pm.field(block, "status") or "").strip().lower()
        if status in TERMINAL_STATUSES:
            continue
        if kind == "goal" and status != "in_progress":
            continue
        # A `completed:` date is terminal even when `status` was never flipped.
        if (pm.field(block, "completed") or "").strip():
            continue
        out.append({
            "kind": kind,
            "name": name,
            "vault": vault["name"],
            "phase": (pm.field(block, "phase") or "").strip().lower(),
            "page_id": pm.field(block, "claude_session_id"),
        })
    return out


def classify(subject, labels, registrations, topics, card_keys):
    """The verdict for one subject: managed / waiting-approval / carded / unknown / open."""
    titles = [subject["name"]]
    if subject["kind"] == "goal":
        titles += sorted(topics.get(subject["name"], ()))
    if pm.managed(titles, labels, board, registrations=registrations,
                  vault=subject["vault"]):
        return "managed", ""

    # The registry names nothing. The page id is the second answer, never the first.
    page_id = (subject.get("page_id") or "").strip()
    if page_id:
        state = session_state(page_id)
        if state == LIVE:
            return "managed", "page session id %s is LIVE under a renamed session" % page_id[:8]
        if state != ABSENT:
            # UNKNOWN, AMBIGUOUS, MALFORMED and a probe that could not run at all are all
            # "cannot tell" — and an unreadable verdict is not permission to spawn.
            return "unknown", "page session id %s could not be read" % page_id[:8]
        # ABSENT is a resume, not an absence — fall through and let it open.

    if subject["phase"] == "todo":
        return "waiting-approval", "phase: todo"
    if subject["kind"] == "goal" and card_keys is not None:
        for key in (pm.dedup_key(subject["name"]), pm.approve_dedup_key(subject["name"])):
            if key in card_keys:
                return "carded", "open card %s" % key
    return "open", ""


def approved_live_counts(roots):
    """`{(vault name, goal): approved live-phase task count}` — the ranking's second key."""
    counts = {}
    for row in pm.scan(roots):
        if row["startability"] != pm.STARTABLE:
            continue
        key = (row["vault"]["name"], row["goal"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def rank(subject, counts, goals_dirs):
    """Topics before goals; goals by priority, then approved work, then name."""
    if subject["kind"] == "topic":
        return (0, 0, 0, subject["name"].lower())
    priority = pm.goal_priority(goals_dirs.get(subject["vault"]), subject["name"])
    return (1, priority if priority is not None else 99,
            -counts.get((subject["vault"], subject["name"]), 0), subject["name"].lower())


def select(roots, labels, registrations, card_keys, free):
    """The ordered verdicts for every flagged subject — a pure function over read inputs."""
    counts = approved_live_counts(roots)
    goals_dirs = {vault["name"]: vault.get("goals_dir") for vault in roots}
    verdicts = []
    for vault in roots:
        topics = pm.topics_by_goal(vault)
        topic_rows = subjects(vault, "topic")
        goal_rows = subjects(vault, "goal")
        if topic_rows is None or goal_rows is None:
            # A read failure is not "nothing to open" — the caller reports it as REFUSED.
            return None
        for subject in topic_rows:
            state, reason = classify(subject, labels, registrations, {}, card_keys)
            verdicts.append({"subject": subject, "state": state, "reason": reason})
        for subject in goal_rows:
            state, reason = classify(subject, labels, registrations, topics, card_keys)
            verdicts.append({"subject": subject, "state": state, "reason": reason})
    verdicts.sort(key=lambda v: rank(v["subject"], counts, goals_dirs))

    topics_by_vault = {vault["name"]: pm.topics_by_goal(vault) for vault in roots}
    opened_topics = set()
    budget = free
    for verdict in verdicts:
        subject = verdict["subject"]
        if verdict["state"] != "open":
            continue
        if subject["kind"] == "goal":
            owning = topics_by_vault.get(subject["vault"], {}).get(subject["name"], set())
            covered = sorted(name for name in owning
                             if (subject["vault"], name) in opened_topics)
            if covered:
                verdict["state"] = "covered"
                verdict["reason"] = "topic %s opened this round" % covered[0]
                continue
        if budget is not None and budget <= 0:
            verdict["state"] = "deferred"
            verdict["reason"] = "manager cap"
            continue
        if budget is not None:
            budget -= 1
        if subject["kind"] == "topic":
            opened_topics.add((subject["vault"], subject["name"]))
    return verdicts


def render(verdicts, free):
    """The pipe-separated lines, plus the SUMMARY — one shape, so probes can assert on it."""
    lines = []
    tally = {}
    for verdict in verdicts:
        subject = verdict["subject"]
        state = verdict["state"]
        tally[state] = tally.get(state, 0) + 1
        if state == "open":
            lines.append("OPEN | %s | %s | %s" % (subject["vault"], subject["kind"], subject["name"]))
        elif state == "waiting-approval":
            lines.append("WAITING | %s | %s | %s | phase: todo" % (
                subject["vault"], subject["kind"], subject["name"]))
        else:
            lines.append("%s | %s | %s | %s | %s" % (
                state.upper(), subject["vault"], subject["kind"], subject["name"], verdict["reason"]))
    lines.append("SUMMARY | free=%s open=%d deferred=%d covered=%d managed=%d waiting=%d carded=%d unknown=%d" % (
        "unlimited" if free is None else free,
        tally.get("open", 0), tally.get("deferred", 0), tally.get("covered", 0),
        tally.get("managed", 0), tally.get("waiting-approval", 0),
        tally.get("carded", 0), tally.get("unknown", 0)))
    return lines


def board_json():
    """`fleet-board.py --json`'s stdout — ONE read, or None when it could not be taken."""
    try:
        proc = subprocess.run(
            ["python3", os.path.join(HERE, "fleet-board.py"), "--json"],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or None


def manager_identity(raw, sessions):
    """`(labels, count, registrations)` from one board read — manager sessions only.

    ⚠️ **Manager sessions only, and that is the whole point.** The registry lists every live
    session, workers included, and a worker is routinely named after the very page its task
    belongs to. Passing those names as manager labels would let a worker's title mark its
    goal as managed — suppressing exactly the open this leg exists to make. So the board's
    `role` decides membership, and the registry only widens each member's names with its
    `formerNames` (the rename history the board's single `label` does not carry).
    """
    labels = pm.live_manager_labels(raw)
    if labels is None:
        return None
    registrations = pm.live_registrations(raw)
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {}
    manager_ids = set()
    for session in parsed.get("sessions", []) if isinstance(parsed, dict) else []:
        if not isinstance(session, dict) or session.get("role") != "manager":
            continue
        if (session.get("label") or "").strip() == pm.ROOT_NAME:
            continue
        session_id = session.get("session_id")
        if session_id:
            manager_ids.add(session_id)
    widened = list(labels)
    for record in sessions:
        if not isinstance(record, dict) or record.get("sessionId") not in manager_ids:
            continue
        for name in [record.get("name")] + list(record.get("formerNames") or []):
            if name:
                widened.append(name)
    return widened, len(labels), registrations


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Select flagged goals and topics whose manager is not live.")
    parser.add_argument("--json", action="store_true",
                        help="print the whole computation as JSON and nothing else")
    parser.add_argument("--limit", type=int, default=None,
                        help="override the free-slot count (diagnosis only; never the round's gate)")
    args = parser.parse_args(argv)

    roots = pm.vaults()
    if roots is None:
        print("UNKNOWN — the vault set could not be read", file=sys.stderr)
        return 2

    sessions = live_sessions()
    if sessions is None:
        print("UNKNOWN — the session registry could not be read; liveness is unknown", file=sys.stderr)
        return 2

    cap = pm.resolve_cap()
    if cap is None:
        print("UNKNOWN — the manager cap could not be resolved", file=sys.stderr)
        return 2
    _, hard, _, _ = cap

    raw = board_json()
    managers = manager_identity(raw, sessions) if raw else None
    if managers is None:
        print("UNKNOWN — the live-manager count could not be taken", file=sys.stderr)
        return 2
    labels, manager_count, registrations = managers

    card_keys = open_card_keys()
    if card_keys is None:
        print("UNKNOWN — the attention store could not be read; open cards are unknown",
              file=sys.stderr)
        return 2

    if args.limit is not None:
        free = args.limit
    elif hard is None:
        free = None
    else:
        free = max(0, int(hard) - manager_count)

    verdicts = select(roots, labels, registrations, card_keys, free)
    if verdicts is None:
        print("UNKNOWN — a goals or topics directory could not be read; the subject set is "
              "unknown, never empty", file=sys.stderr)
        return 2

    if args.json:
        json.dump({
            "free": free,
            "hard": hard,
            "live_managers": manager_count,
            "subjects": [
                {"vault": v["subject"]["vault"], "kind": v["subject"]["kind"],
                 "name": v["subject"]["name"], "state": v["state"], "reason": v["reason"]}
                for v in verdicts
            ],
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    for line in render(verdicts, free):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
