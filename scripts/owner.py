#!/usr/bin/env python3
"""Does this session OWN this subject? The one definition, and the one read behind it.

`docs/session-tiers.md` § Keep-alive states the definition in prose:

> **Owner:** a manager owns a subject only when its session reads `LIVE` **and** its loop reads
> `OK` or `STALE`. A loop reading `STOPPED`, `SLEEPING`, `UNARMED` or `INVALID`, or a probe that
> cannot answer, is **not** an owner.

This module is that sentence as code, and the only place in the tree that decides it. Before it,
four scripts each answered "who owns this" from their own combination of a page's
`claude_session_id` and a liveness probe, and they disagreed:

| Site | What it called an owner (read 2026-11-08) |
|---|---|
| `worker-keepalive.py` `goal_manager_live()` | a goal page whose `claude_session_id` reads `LIVE` — session only, loop never read |
| `topic-keepalive.py` | session liveness **and** `manager-liveness.py --state` cadence, combined inline |
| `gate-owner-filter.py` § GOAL SCOPE | the goal page's `claude_session_id` plus task-page stamps, from the vault |
| `session-scope.py` | a page's `claude_session_id` stamp taken as ownership, loop liveness never read |

The disagreement was a live hole, not a tidiness complaint: a goal manager that ran
`/supervisor:stop` keeps its session with its loop `STOPPED`. `worker-keepalive.py` deferred to
it — *"the goal tier resumes it"* — but the goal round is exactly what stopped, so the worker had
no keeper and nothing said so. Zero instances on 2026-11-08, and the code allowed it.

## Three answers, and the third is not a synonym for the second

  owner         the session is `LIVE` and its loop is `OK` or `STALE` — it runs the round
  not-owner     a definite answer: nothing owns this subject right now
  cannot-tell   the probe could not decide, **or** the loop says the manager is deliberately not
                running (`SLEEPING`, `UNARMED`, `INVALID`)

⚠️ **`cannot-tell` never collapses into `not-owner`.** `not-owner` is what licenses a caller to
hand the subject to another keeper — the fleet leg resuming a worker, a manager being resumed or
started. Answering `not-owner` on a failed read therefore licenses a **second writer** on a
subject whose first is merely unmeasurable, which is the failure every probe in this repo is
written to avoid (`topic-keepalive.py`'s `UNKNOWN` is never death; `fleet-keeper.py` reports a
failed probe and never treats it as fine).

## The table, in priority order

```
  loop STOPPED                   not-owner    loop-stopped         a stand-down, whoever the session is
  loop SLEEPING                  cannot-tell  loop-sleeping        date-gated: nothing due before its instant
  session NONE                   not-owner    no-session           nothing recorded to own the subject
  session UNKNOWN/AMBIG/MALFORM  cannot-tell  session-undecidable  the instrument could not answer
  session ABSENT                 not-owner    session-absent       the conversation is gone
  loop INVALID                   cannot-tell  loop-invalid         a record exists and cannot be read
  loop UNREADABLE                cannot-tell  loop-unreadable      the loop probe itself failed
  loop UNARMED                   cannot-tell  loop-unarmed          nothing has ever armed it
  session LIVE, loop OK/STALE    owner        live-armed           the definition
  session LIVE, loop not read    cannot-tell  loop-unread          a caller that named no subject
```

⚠️ **`STOPPED` outranks the session, and `no-session` outranks the remaining loop states.** A
deliberate stand-down owns nothing whatever its session reads — that is the hole this module
closes. And a subject that records no session at all is owned by nobody, so a stale or unreadable
cadence file beside it is not a reason to withhold the answer a caller acts on: the manager was
never recorded, so there is no first writer for a second to collide with. `ABSENT` outranks the
unreadable loop states for the same reason, and `SLEEPING` outranks `ABSENT` because a date-gated
subject whose session died is still not due until its instant.

## Reading a session's loop when the caller only has an id

The loop is per SUBJECT, and its record is keyed by `<branch>--<slug>`. A caller holding only a
session id (the gate filter's spawner, its claim holder) gets the subject from the manager's own
registration file in `MANAGER_DIR` (`<session-id>.json`, below), which carries `subject`,
`branch` and `vault` — written by the tier resolution that already decided which manager a
session is, so this module and that decision cannot disagree. No registration means no subject to
read, which is `loop-unread`: `cannot-tell`, never `not-owner`.

## Cost

One `session-liveness.py --check` (an HTTP probe, 30 s timeout) and one `manager-liveness.py
--state` (files only) per resolution. That is why `gate-owner-filter.py` asks once per arm about
its own `--self` and never per poll — a caller on a hot path must cache, not re-resolve.

## Usage

    owner.py --session <id> [--subject <name> --branch goal|topic|fleet]
    owner.py --subject <name> --branch goal [--vault <path>] [--dir <dir>]

Prints `<verdict>\\t<reason>\\t<session>\\t<loop>\\t<detail>` and exits 0 for every verdict it
can answer — the verdict IS the answer, and a non-zero exit would make `not-owner` read as a
failure to every `if` that wraps it. Exit 2 means the arguments did not resolve to anything
readable.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

SCRIPTS = os.path.dirname(os.path.abspath(__file__))

OWNER = "owner"
NOT_OWNER = "not-owner"
CANNOT_TELL = "cannot-tell"

#: No value at all — distinct from every probe word, so a caller can tell "not read" from
#: "read and unknown".
NONE = "NONE"

#: `scripts/session-liveness.py`'s documented contract is the EXIT CODE, not the text:
#: `LIVE` (0) / `ABSENT` (1) / `UNKNOWN` (2) / `AMBIGUOUS` (3) / `MALFORMED` (4).
VERDICT_BY_EXIT = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "AMBIGUOUS", 4: "MALFORMED"}
UNDECIDABLE_SESSION = ("UNKNOWN", "AMBIGUOUS", "MALFORMED")

#: `ABSENT` is believed only when the instrument also says so in words. ⚠️ Exit 1 is `ABSENT`'s
#: code and every Python traceback's code, and `ABSENT` is the one verdict that authorises a
#: resume — so a crashed instrument would read as a dead manager and a second writer would be
#: started over a live one. `session-liveness.py --check` prints `ABSENT — <message>` to stderr
#: on every genuine `ABSENT`; matched per LINE and on a word boundary, so `ABSENTEE` and a prose
#: mention do not count.
_ABSENT_WORD = re.compile(r"ABSENT\b")

#: `manager-liveness.py --state`'s vocabulary, plus this module's own word for "the probe could
#: not answer at all". ⚠️ `UNREADABLE` is NOT one of the five: it is a fact about the instrument,
#: not about the subject, and folding it into `INVALID` (a record that exists and cannot be
#: read) would report a crashed probe as a broken record.
LOOP_STATES = ("OK", "STALE", "STOPPED", "SLEEPING", "UNARMED", "INVALID")
PROBE_FAILED = "UNREADABLE"

#: `manager-liveness.py`'s three tiers. A loop record carries its branch in the slug
#: (`goal--<base>`), because a topic and a goal may share a name.
BRANCHES = ("goal", "topic", "fleet")

PROBE_TIMEOUT = 30  # seconds, on both probes: a hung liveness probe must not wedge a tick

MANAGER_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_WORKER_MANAGER_DIR")
    or os.environ.get("WORKER_MANAGER_STATE_DIR")
    or "~/.claude/state/worker-manager"
)

#: Vault folder names that hold subject pages. Both generations are listed so a vault left on the
#: pre-2026-09-13 names (`23 Goals` / `23 Topics`) still resolves, exactly as `session-scope.py`'s
#: `PROBE_GOALS` does.
SUBJECT_DIRS = {
    "goal": ("24 Goals", "23 Goals", "goals"),
    "topic": ("23 Topics", "topics"),
}


@dataclass(frozen=True)
class Ownership:
    """One answer: the verdict, why, and the two facts it was built from.

    `session` and `loop` are the raw probe words (`NONE` when not read), never a second opinion
    about ownership — a caller that must act differently on `SLEEPING` than on `INVALID` reads
    them and does not re-derive the verdict from them.
    """

    verdict: str
    reason: str
    session: str = NONE
    loop: str = NONE
    session_id: str = ""
    detail: str = ""

    @property
    def is_owner(self) -> bool:
        return self.verdict == OWNER

    @property
    def is_cannot_tell(self) -> bool:
        return self.verdict == CANNOT_TELL

    def describe(self) -> str:
        """`session LIVE, loop OK` — the two facts in one phrase.

        ⚠️ **Callers embed this verbatim in the line they print, and a round log is grepped for
        it.** A caller's report of an owner has to carry the evidence the verdict rests on, so a
        reader — or a later audit of a round log — can tell an owner-by-definition from a jump
        taken for some other reason. `worker-keepalive.py` prints `goal manager <id> is an owner
        (session LIVE, loop OK)` for exactly that reason.
        """
        return f"session {self.session}, loop {self.loop}"

    def __str__(self) -> str:
        return f"{self.verdict} ({self.reason}: {self.describe()})"


def verdict_of(session: str, loop: str) -> tuple:
    """`(verdict, reason)` for one `(session, loop)` pair — `docs/session-tiers.md` § Keep-alive.

    The whole point of this module is that this function is the ONLY place the two signals are
    combined. A caller that branches on `session` and `loop` itself to decide ownership is the
    defect the module exists to remove; a caller that branches on them to choose an ACTION
    (`leave` a stopped manager alone, `restart` a live one whose loop is `INVALID`) is fine —
    the facts are public, the verdict is not.
    """
    session = session or NONE
    loop = loop or NONE
    # 1. A deliberate stand-down owns nothing, whatever its session reads. This is the case the
    #    module exists for: the session lingers, the round is stopped, and a worker deferred to
    #    it is kept by nobody.
    if loop == "STOPPED":
        return NOT_OWNER, "loop-stopped"
    # 2. A date-gated subject is not re-arming until its instant BY DESIGN. It owns nothing now
    #    and will own again by itself, so a caller must not hand its workers to another keeper.
    if loop == "SLEEPING":
        return CANNOT_TELL, "loop-sleeping"
    # 3. Nothing is recorded to own the subject at all. Outranking the remaining loop states is
    #    deliberate: a never-started subject beside a stale cadence file is still unowned, and
    #    the action a caller takes on it (open it, resume its worker) creates the FIRST writer
    #    rather than a second.
    if session == NONE:
        return NOT_OWNER, "no-session"
    # 4. The instrument could not answer. Never death, never ownership.
    if session in UNDECIDABLE_SESSION:
        return CANNOT_TELL, "session-undecidable"
    # 5. The conversation is gone. Nothing owns the subject, and there is no first writer left
    #    for a second to collide with — so this is a definite answer even beside a loop record
    #    that cannot be read. (SLEEPING is checked above it deliberately: a date-gated subject
    #    whose session died is still not due, so its caller must not hand the work on yet.)
    if session == "ABSENT":
        return NOT_OWNER, "session-absent"
    # 6-8. The loop could not be read, for three different reasons, all of which are cannot-tell:
    #      a record that exists and cannot be parsed, a probe that crashed, and a subject nothing
    #      has ever armed.
    if loop == "INVALID":
        return CANNOT_TELL, "loop-invalid"
    if loop == PROBE_FAILED:
        return CANNOT_TELL, "loop-unreadable"
    if loop == "UNARMED":
        return CANNOT_TELL, "loop-unarmed"
    # 9. THE DEFINITION.
    if session == "LIVE" and loop in ("OK", "STALE"):
        return OWNER, "live-armed"
    # 10. A caller that named a session but no subject has no loop to read. That is not an owner
    #     on the evidence, and it is not "no owner" either.
    return CANNOT_TELL, "loop-unread"


# ---------------------------------------------------------------------------------------------
# The probes.
# ---------------------------------------------------------------------------------------------


def session_verdict(sid: str) -> str:
    """`LIVE` / `ABSENT` / `UNKNOWN` / `AMBIGUOUS` / `MALFORMED`, from the instrument.

    ⚠️ **Read the exit code, never the text.** `session-liveness.py` prints `LIVE` to stdout and
    every other verdict to stderr, so a stdout scan collapses them all to `UNKNOWN` — which is
    what made `ABSENT` unreachable in `topic-keepalive.py` until 2026-10-30, and `ABSENT` is the
    only verdict that licenses a resume.

    ⚠️ **An unrunnable or hung probe is `UNKNOWN` too**, and `ABSENT` additionally needs the
    instrument's own word on stderr — see `_ABSENT_WORD`. Both are cannot-tell, never death.
    """
    try:
        probe = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "session-liveness.py"), "--check", sid],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    verdict = VERDICT_BY_EXIT.get(probe.returncode, "UNKNOWN")
    if verdict == "ABSENT" and not any(
        _ABSENT_WORD.match(line) for line in (probe.stderr or "").splitlines()
    ):
        return "UNKNOWN"
    return verdict


def loop_state(subject: str, branch: str = "goal") -> tuple:
    """`(state, detail)` for one subject's loop, or `(PROBE_FAILED, "")` when it could not answer.

    ⚠️ **Bounded and guarded, because a keep-alive tick runs this for every subject it holds.**
    Without the timeout a stuck `manager-liveness.py` blocks the tick forever — worse than the
    abort the guard prevents, because no verdict is ever printed at all.

    ⚠️ **The detail is returned, not dropped.** `--state` prints `<STATE>\\t<slug>\\t<detail>` and
    the detail is the only place a reader learns WHAT a state is waiting for — for a sleep it
    carries the instant.
    """
    if not subject or branch not in BRANCHES:
        return NONE, ""
    try:
        probe = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "manager-liveness.py"),
             "--state", "--subject", subject, "--branch", branch],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return PROBE_FAILED, ""
    # `--state` exits 0 for every state it can answer, so a non-zero exit is the child dying.
    if probe.returncode != 0:
        return PROBE_FAILED, ""
    fields = ((probe.stdout.splitlines() or [""])[0]).split("\t")
    state = fields[0].strip()
    detail = fields[2].strip() if len(fields) > 2 else ""
    if state not in LOOP_STATES:
        return "INVALID", detail
    return state, detail


def registration(sid: str) -> tuple:
    """`(subject, branch, reason)` for a manager session — `(None, None, reason)` on a failed read.

    The file is written by the tier resolution at arm time (`docs/subject-resolution.md`
    § Recording, and `scripts/fleet-register.py` for the fleet tier), so it carries the same
    subject the manager itself resolved — not a guess from a session name. ⚠️ The id is joined
    into a path, so it must be a bare filename: a value carrying `/`, `\\` or `..` would read an
    arbitrary `*.json` outside the registration dir.
    """
    if not isinstance(sid, str) or not sid:
        return None, None, "no session id to resolve"
    if sid in (".", "..") or "/" in sid or "\\" in sid or "\x00" in sid:
        return None, None, f"session id {sid!r} is not a bare filename"
    path = os.path.join(MANAGER_DIR, f"{sid}.json")
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None, None, f"no manager registration at {path}"
    except (OSError, ValueError) as err:
        return None, None, f"{path} is unreadable ({err})"
    if not isinstance(record, dict):
        return None, None, f"{path} is not a registration object"
    subject = record.get("subject")
    branch = record.get("branch")
    # ⚠️ **`isinstance(subject, str)` is load-bearing, not defensive dressing.** A record whose
    # `subject` is a list or a number is truthy, so a bare `not subject` check lets it through
    # and it reaches `os.path.join` / `subprocess.run` as a non-string: an uncaught `TypeError`
    # out of the resolver, which is neither a verdict nor `cannot-tell`.
    if not isinstance(subject, str) or not subject or branch not in BRANCHES:
        return None, None, f"{path} carries no usable subject/branch"
    return subject, branch, ""


def frontmatter(text: str) -> str:
    """The page's frontmatter block, or `""` when it has none."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def scalar(fm: str, key: str) -> str:
    """One frontmatter scalar, or `""`.

    ⚠️ `[ \\t]*`, never `\\s*`: `\\s` matches the newline, so an EMPTY value makes the regex
    swallow the line break and return the NEXT key's line as this key's value — a page carrying
    `claude_session_id:` with no id would read as having one.
    """
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", fm, re.MULTILINE)
    return m.group(1).strip().strip("\"'") if m else ""


def page_session_id(fm: str) -> str:
    """The manager a goal or topic page records, or `""`.

    `claude_session_id` is the only field a goal or topic page records its manager in; the
    retired `manager_session_id` fallback was removed once both vaults were migrated.
    """
    return scalar(fm, "claude_session_id")


# ---------------------------------------------------------------------------------------------
# The resolutions.
# ---------------------------------------------------------------------------------------------


def of_session(sid: str, subject: str = "", branch: str = "goal") -> Ownership:
    """Resolve a session id, reading its loop from its own registration when no subject is given."""
    if not sid:
        loop, detail = (loop_state(subject, branch) if subject else (NONE, ""))
        verdict, reason = verdict_of(NONE, loop)
        return Ownership(verdict, reason, NONE, loop, "", detail)
    if not subject:
        resolved, branch, why = registration(sid)
        if not resolved:
            session = session_verdict(sid)
            verdict, reason = verdict_of(session, NONE)
            # A live session with no registration is not an owner on evidence we do not have.
            # The registration's own reason rides in `detail`, so a reader can act on it.
            return Ownership(verdict, reason, session, NONE, sid, why)
        subject = resolved
    session = session_verdict(sid)
    loop, detail = loop_state(subject, branch)
    verdict, reason = verdict_of(session, loop)
    return Ownership(verdict, reason, session, loop, sid, detail)


def of_page(fm: str, subject: str, branch: str = "goal") -> Ownership:
    """Resolve the manager a goal or topic page records, reading that subject's loop."""
    sid = page_session_id(fm)
    loop, detail = loop_state(subject, branch)
    session = session_verdict(sid) if sid else NONE
    verdict, reason = verdict_of(session, loop)
    return Ownership(verdict, reason, session, loop, sid, detail)


def locate(vault: str, subject: str, branch: str = "goal", directory: str = "") -> tuple:
    """`(path, reason)` — the subject page, or why it could not be shown that there is none.

    ⚠️ **An unreadable FOLDER is not an absent page, and the two must not collapse.** The
    caller answers `not-owner` when no page exists, and `not-owner` licenses a second keeper;
    a directory this process cannot list tells us nothing about whether the subject has a
    manager, so it is reported as a reason and the caller answers `cannot-tell`. A folder that
    genuinely is not there (`FileNotFoundError`) IS a known absence — the vault may keep its
    goals elsewhere — so it stays on the not-owner path.
    """
    if branch not in SUBJECT_DIRS:
        return "", ""
    roots = [directory] if directory else list(SUBJECT_DIRS[branch])
    for root in roots:
        base = os.path.join(vault, root)
        try:
            entries = os.listdir(base)
        except FileNotFoundError:
            continue
        except OSError as err:
            return "", f"{base} is unreadable ({err})"
        for entry in entries:
            if entry.lower() == subject.lower() + ".md":
                return os.path.join(base, entry), ""
    return "", ""


def page_path(vault: str, subject: str, branch: str = "goal", directory: str = "") -> str:
    """The subject page's path, matched by exact basename — never a fuzzy or prefix match.

    ⚠️ **Exact basename, as `docs/subject-resolution.md` requires:** two subjects may differ only
    in punctuation, and a prefix match would read one goal's manager as another's.

    ⚠️ **`fleet` has no subject page, and returns none.** The fleet manager manages no page
    (`scripts/fleet-register.py`), so falling back to the goal folders for that branch would
    resolve a GOAL page as if it were the fleet's subject.

    ⚠️ **A caller that decides an OWNERSHIP verdict must use `locate()` instead.** This returns
    `""` for both "no such page" and "the folder could not be read", and only the first of those
    is a known absence.
    """
    return locate(vault, subject, branch, directory)[0]


def of_subject(vault: str, subject: str, branch: str = "goal", directory: str = "") -> Ownership:
    """Resolve a subject named by the caller, reading its page and its loop.

    ⚠️ **"Could not read it" is `cannot-tell`, never `not-owner`, at both hops.** `not-owner` is
    the answer that licenses a caller to hand the subject to another keeper, so answering it
    because a folder or a page would not open is precisely the second writer over a possibly
    live manager that this module exists to withhold. Only a page that was READ and records no
    session is `not-owner` (`no-session`), and only a folder that is genuinely absent is a known
    absence.
    """
    path, why = locate(vault, subject, branch, directory)
    if why:
        loop, detail = loop_state(subject, branch)
        return Ownership(CANNOT_TELL, "page-unreadable", NONE, loop, "", why)
    if not path:
        loop, detail = loop_state(subject, branch)
        verdict, reason = verdict_of(NONE, loop)
        return Ownership(verdict, reason, NONE, loop, "",
                         detail or f"no page for {subject} under {branch}")
    # ⚠️ **A page that exists but cannot be READ is a failed probe, not a crash and not an
    # absence.** `locate()` only proves a directory entry matched; the read that follows can
    # still fail on permissions, a symlink to nowhere, or a page that is not UTF-8. Letting
    # that raise would turn a caller's whole tick into a traceback, and answering `not-owner`
    # would license a second writer on a subject whose manager is merely unmeasurable.
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as err:
        loop, detail = loop_state(subject, branch)
        return Ownership(CANNOT_TELL, "page-unreadable", NONE, loop, "",
                         f"{path} is unreadable ({err})")
    return of_page(frontmatter(text), subject, branch)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", help="a manager session id; its subject comes from its registration")
    ap.add_argument("--subject", help="a goal or topic name")
    ap.add_argument("--branch", choices=BRANCHES, default="goal")
    ap.add_argument("--vault", help="the vault ROOT path, to read the subject's page")
    ap.add_argument("--dir", help="override the subject directory")
    a = ap.parse_args(argv)

    if a.subject and a.vault:
        o = of_subject(a.vault, a.subject, a.branch, a.dir or "")
    elif a.session:
        o = of_session(a.session, a.subject or "", a.branch)
    else:
        # ⚠️ **`--subject` without `--vault` is a REFUSAL, never a verdict.** The page is
        # where the subject's manager is recorded, so a run that cannot read it has not
        # established that nobody owns the subject — and answering `not-owner` from the loop
        # state alone is exactly the licence to start a second writer that this module exists
        # to withhold. Exit 2 is the cannot-tell direction, matching the flag-less call.
        print("owner.py needs --session, or --subject with --vault", file=sys.stderr)
        return 2
    print("\t".join((o.verdict, o.reason, o.session, o.loop, o.detail)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
