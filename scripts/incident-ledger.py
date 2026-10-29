#!/usr/bin/env python3
"""The incident ledger: which worker blocker was opened, whose it is, and whether it ever closed.

Storage: ~/.claude/state/incidents/ledger.json -- ONE file, written by this script and
by nothing else (override SUPERVISOR_INCIDENTS_LEDGER, which is also what lets the
tests and the negative-control runs exercise open/close/share against a throwaway path
instead of the operator's real ledger).

WHY THIS EXISTS. A blocker used to end when its card closed, and a close is not a
resolution. Two mechanisms make a close read as an answer and neither of them is one:

  * the attention store records `open` -> `closed` as CLEARED, not answered --
    `answered_at` and `decision` stay unset, no answer is routed, and the asking
    session stays frozen exactly as it was (`attention-controller`
    pkg/attention-store-attempt.go: "a close is a clear, not an answer -- the
    discriminator is `answered_at`");
  * `scripts/manager-attention-watch.py` emits `CLEARED` when a worker settles at
    `idle` carrying no closer line, which is a transcript-shape settle, not proof the
    worker acted after the answer.

So an incident closes on a VERIFIED RESUME -- the worker's next tool call or task
change after the answer -- and on nothing else. `close` therefore REQUIRES
`--resume-evidence`, and there is deliberately no flag that closes on a card. An
answer that never resumes leaves the incident open, counted in the denominator only.

TWO LEDGERS ALREADY EXIST AND NEITHER ANSWERS THIS QUESTION.

  | store                    | keyed by       | lifetime               | says                                                        |
  |--------------------------|----------------|------------------------|-------------------------------------------------------------|
  | scripts/asked-ledger.py  | blocked session| pruned by age+liveness | who was ASKED, and who resolved the ask                     |
  | scripts/drive-ledger.py  | session        | per round              | nudged / reaped / unverifiable THIS round                   |
  | THIS ledger              | dedup key      | permanent              | which blocker, whose, since when, deadline, and how it ENDED |

`asked-ledger.py` does carry `resolved_by`, but only when the resolver differs from the
asker, and neither it nor `drive-ledger.py` carries an outcome or a deadline -- so
neither can say whether the worker ever resumed. That gap, not a missing field, is why
this is a third store.

⚠️ A STALL IS A DURATION, NOT AN EVENT, AND IT ISSUES NO ATTENTION CARD. Its
`detected_by` is the drive verdict that named it (`drive:<verdict>@<round>`), and
`card_id` stays empty for that kind -- empty at detection, that is: its deadline
escalation still posts one, and that card is identified by `dedup_key`. Do not
fabricate a card id to fill the field: a fabricated id is a join that never resolves
and it reads as provenance.

⚠️ A CORRUPT LEDGER IS REFUSED, NOT READ AS EMPTY -- the rule `drive-ledger.py` states
and for the same reason: a false "empty" is how 37 entries were lost on 2026-09-23.
`read` exits 2 on a file it cannot parse and no writer overwrites one.

Locking: fcntl.flock on a sidecar lock held across the read-modify-write, then a
pid-suffixed tmp and os.replace. A by-hand run can overlap the periodic round, and two
writers sharing one fixed tmp path is how a wipe goes silent.

Subcommands: open | answer | close | resume | escalate | stall | share | list | prune
"""
import argparse
import datetime
import fcntl
import glob
import json
import os
import subprocess
import sys

LEDGER = os.environ.get("SUPERVISOR_INCIDENTS_LEDGER") or os.path.expanduser(
    "~/.claude/state/incidents/ledger.json"
)
LOCK = LEDGER + ".lock"

HERE = os.path.dirname(os.path.abspath(__file__))
ATTENTION_ASK = os.path.join(HERE, "attention-ask.py")

#: Exit code for a ledger this script will not touch: unparseable on disk. Distinct
#: from 1 so a caller can tell "refused" from "crashed" -- a refused run wrote nothing.
REFUSED = 2

KINDS = ("permission", "question", "stall")

#: `pending` is the open state; `timed-out` is an ESCALATION, never a close -- an
#: incident that timed out has no verified resume, so it never reaches `close`.
PENDING = "pending"
TIMED_OUT = "timed-out"
#: A stall incident whose session is GONE is RETIRED, not escalated and not closed:
#: the session no longer exists, so there is nothing to resume and no card the
#: operator could act on. Terminal, so `prune` may drop it; and excluded from
#: `share`'s denominator, because it is an incident with NO POSSIBLE resolution and
#: counting it would depress the one number this ledger exists to report, for good.
SKIPPED_GONE = "skipped-gone"
CLOSE_OUTCOMES = ("manager", "operator", "self-cleared")
ALL_OUTCOMES = (PENDING,) + CLOSE_OUTCOMES + (TIMED_OUT, SKIPPED_GONE)

#: The only outcome that counts as "resolved by the owning manager" in `share`.
RESOLVED_BY_MANAGER = "manager"

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(TS_FORMAT)


def parse_ts(value):
    try:
        return datetime.datetime.strptime(value, TS_FORMAT).replace(
            tzinfo=datetime.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def deadline_seconds():
    """Default deadline for a new incident.

    Pinned rather than left to each caller, so `escalate` has one reproducible clock
    and a test can shorten it instead of waiting 15 minutes per attempt.
    """
    raw = os.environ.get("SUPERVISOR_INCIDENT_DEADLINE_SECONDS", "900")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 900
    return value if value > 0 else 900


def stall_rounds():
    """Consecutive rounds without progress before a session reads as stalled."""
    raw = os.environ.get("SUPERVISOR_INCIDENT_STALL_ROUNDS", "2")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2
    return value if value > 0 else 2


def incident_budget():
    """How many NEW stall incidents one round may open.

    The drive leg caps its own actions for the same reason (`drive-unverifiable.py`,
    `MAX_ACTIONS`): a population that goes unverifiable together must not become a
    flood of cards. Measured 2026-10-22: one round opened 117 stall incidents and
    every one of them became a board card, because the action leg had a cap and
    this leg did not.
    """
    raw = os.environ.get("SUPERVISOR_INCIDENT_STALL_BUDGET", "3")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 3
    return value if value > 0 else 3


def as_session_id(value):
    """A session id as a comparable lowercase string, or "" when it is not one.

    ⚠️ **Guarded, not coerced.** A malformed producer payload (an int, a dict, a
    list) raises `AttributeError` on `.lower()`, which turns one bad row into a dead
    round -- the same silent-off this file refuses one store over. `""` is the
    "cannot judge" answer, and every caller fails OPEN on it rather than reading it
    as gone: an unreadable id is not evidence that its session is absent.
    """
    return value.lower() if isinstance(value, str) else ""


def bounded_names(names, limit=5):
    """A bounded rendering of a name list: the first `limit`, then a remainder count.

    ⚠️ **The list is unbounded in production.** The drive ledger keeps a row for
    every session that was ever unverifiable, so a `", ".join(names)` line grows with
    that graveyard -- measured 2026-10-29: 120 gone rows in one line. The prefix
    stays (a skip that only counted would be indistinguishable from a threshold that
    stopped firing); the tail does not.
    """
    shown = ", ".join(names[:limit])
    if len(names) > limit:
        shown += ", … (+%d more)" % (len(names) - limit)
    return shown


def warn_liveness_unavailable(verb):
    """The ONE warning both legs emit when the probe returns `None`.

    Shared rather than copied: `escalate` must say exactly what `stall` says, and a
    duplicated string is a line that diverges the first time either is edited.
    """
    sys.stderr.write(
        "warning: session liveness unavailable, so a gone session cannot be "
        "told from a stalled one; %s without that filter\n" % verb
    )


def live_sessions():
    """Every session the heartbeat store lists, or None when the probe failed.

    ⚠️ **`None` is NOT an empty set, and the caller fails OPEN on it.** A store
    that cannot be reached must never read as "every session is dead": suppressing
    the whole incident class on an outage is the false-empty this ledger refuses one
    store over (`read_drive_ledger` refuses a corrupt file for exactly this reason).
    So an outage keeps the current behaviour and says so, rather than going quiet.

    ⚠️ A session ABSENT from this set is GONE, not silent. `session-liveness.py`
    separates absent from unknown precisely so a reader can tell them apart, and the
    distinction is the whole point: a stall is a running session that is not moving,
    while a session that no longer exists has nothing to investigate and no card the
    operator could act on.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "session-liveness.py"),
                "--list",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # ⚠️ **`degraded=` is NOT a veto, and treating it as one disables the gate.** The
    # producer marks a row `degraded` when the session IS in the identity registry but
    # the endpoint holds NO live heartbeat row for it (`degraded: not row` on the
    # registry loop, `False` on the endpoint-only loop, `session-liveness.py`
    # `_list_payload`). The row is therefore PRESENT and its session is live, merely
    # uncorroborated — so the count says nothing about a session MISSING from the set.
    # Measured 2026-10-29 on a healthy fleet: `rows=45 registry_live=43 degraded=7` —
    # 7 of the 43 registry rows lack a heartbeat and 2 rows are endpoint-only, so
    # `degraded > 0` is the NORMAL state. A `degraded > 0 → None` rule would return
    # "unavailable" every round, warn every round, and never retire anything — the same
    # silent-off this gate exists to prevent, reached from the third side.
    #
    # The read is unusable in exactly TWO cases, both below: an EMPTY list, and a
    # non-empty list the endpoint corroborated nothing in (partial rollout). Everything
    # else is a usable absent-set.
    # ⚠️ **The `rows=… registry_live=… degraded=…` header goes to STDERR, not stdout**
    # (measured 2026-10-22: with stderr discarded, stdout begins at `[`). The
    # tolerance below is therefore not the shape this reads today — it is a guard for
    # the header moving to stdout, which would otherwise be a one-character change in
    # the producer that silently turns this gate off. Line-based, never
    # `stdout.find("[")`: a bracket in a future header would be read as the payload
    # start, the parse would fail, and the caller would fail OPEN — off, not loud.
    lines = (result.stdout or "").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("[")), None
    )
    if start is None:
        return None
    try:
        rows = json.loads("\n".join(lines[start:]))
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    # ⚠️ **BOTH key spaces, because the two sides do not agree on one.** `--list`
    # returns full registry UUIDs, while the drive ledger keys its entries on the
    # 8-char display handle the sweep digest carries (`agents/fleet-drive.md:44`,
    # `scripts/drive-ledger.py:108`) — every other consumer is prefix-tolerant
    # (`--check`, `--pane-for`, `transcript_for`'s `session_id[:8]`), which is what
    # made the mismatch invisible. Returning only the full id would match no
    # production row at all: every eligible session would read as gone, the incident
    # class would be silently disabled, and each round would print a plausible skip
    # line per row. Lower-cased on both sides for the same reason the rest of the
    # fleet compares ids case-insensitively.
    live = set()
    corroborated = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        session_id = as_session_id(row.get("sessionId"))
        if not session_id:
            continue
        # ⚠️ A row the endpoint corroborates carries `degraded: false`; a registry row
        # with no live heartbeat row carries `degraded: true`. Tracked per row, from the
        # PAYLOAD — never from the `degraded=` header, which counts the same thing but
        # is a display line this function no longer parses.
        if not row.get("degraded"):
            corroborated = True
        live.add(session_id)
        live.add(session_id[:8])
    if not live:
        # ⚠️ **An EMPTY successful probe is "no measurement", not "every session is
        # gone".** `--list --json` exits 0 with `[]` when neither the registry nor the
        # endpoint holds a live row, and returning an empty set would make every
        # eligible row read as gone: the whole stall class goes quiet behind a
        # plausible skip line. That is the same silent-off the fail-open rule above
        # exists to prevent, reached from the fail-closed side.
        # ⚠️ **Nothing here separates "empty fleet" from "empty measurement"** — the
        # producer's `degraded=` counts rows it could not corroborate, not rows it
        # could not see, so an empty payload carries `degraded=0` in both cases. An
        # empty read is therefore never trusted as a fleet reading: it fails open and
        # says so, which is the only direction that cannot suppress a live blocker.
        return None
    if not corroborated:
        # ⚠️ **A non-empty payload the endpoint corroborated NOTHING in is not a fleet
        # reading either.** During a partial rollout the endpoint holds no `live: true`
        # row while the registry still lists sessions, so every row comes back
        # `degraded: true` and the set is registry-only — and a cluster/a2a worker
        # lives ONLY in the endpoint, so it would read as absent and its stall incident
        # would be retired `skipped-gone`, terminally, for a session that is alive.
        # `session-liveness.py` warns about exactly this state at the `--list` empty-row
        # branch. This is the ONE narrow case the count still decides: at least one
        # corroborated row means the endpoint answered, which is all the absent-set
        # needs; zero means it did not, and the read fails open.
        return None
    return live


def caller_id():
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or "unattributed"
    )


def dedup_key(session_id, kind):
    """Stable identity for one blocker on one session.

    The escalation card is keyed on this, which is what lets `escalate` be re-run
    every round without posting a second card for the same incident.
    """
    return "incident:%s:%s" % (session_id, kind)


def _refuse(reason):
    sys.stderr.write("refused: %s\n" % reason)
    sys.exit(REFUSED)


def read_ledger():
    try:
        with open(LEDGER, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"version": 1, "incidents": []}
    except (OSError, ValueError) as err:
        _refuse("%s is unreadable (%s); refusing to read it as empty" % (LEDGER, err))
    if not isinstance(data, dict) or not isinstance(data.get("incidents"), list):
        _refuse("%s is not an incident ledger (no `incidents` list)" % LEDGER)
    return data


def write_ledger(data):
    parent = os.path.dirname(LEDGER)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp.%d" % (LEDGER, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, LEDGER)


class Locked(object):
    """Hold the sidecar lock across a read-modify-write. A round and a hand-run race."""

    def __enter__(self):
        parent = os.path.dirname(LOCK)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.handle = open(LOCK, "a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        return False


def find_open(incidents, key):
    """The one open incident for this dedup key, or None.

    Open means outcome is still `pending`. A closed or escalated record is history:
    a second blocker of the same kind on the same session opens a NEW record, so the
    share reading counts episodes rather than sessions.
    """
    for incident in incidents:
        if incident.get("dedup_key") == key and incident.get("outcome") == PENDING:
            return incident
    return None


def new_incident(args, opened_at):
    key = dedup_key(args.session, args.kind)
    seconds = args.deadline_seconds
    if seconds is None:
        seconds = deadline_seconds()
    opened = parse_ts(opened_at)
    deadline = (opened + datetime.timedelta(seconds=seconds)).strftime(TS_FORMAT)
    detected_by = args.detected_by or (
        "drive:stalled@%s" % now() if args.kind == "stall" else ""
    )
    return {
        "dedup_key": key,
        "session_id": args.session,
        "kind": args.kind,
        "owner": args.owner,
        "opened_at": opened_at,
        "deadline": deadline,
        "outcome": PENDING,
        "closed_at": None,
        "detected_by": detected_by,
        "escalated_by": None,
        "card_id": args.card_id or "",
        "resume_evidence": None,
    }


def cmd_open(args):
    opened_at = now()
    key = dedup_key(args.session, args.kind)
    with Locked():
        data = read_ledger()
        existing = find_open(data["incidents"], key)
        if existing:
            # Idempotent: the same blocker seen twice in a round is one incident,
            # not two, and re-opening would inflate the denominator.
            print(
                "already open %s (owner %s, opened %s, deadline %s)"
                % (
                    key,
                    existing.get("owner", "?"),
                    existing.get("opened_at", "?"),
                    existing.get("deadline", "?"),
                )
            )
            return 0
        incident = new_incident(args, opened_at)
        data["incidents"].append(incident)
        write_ledger(data)
        print(
            "opened %s (kind %s, owner %s, deadline %s, detected_by %s)"
            % (
                key,
                incident["kind"],
                incident["owner"],
                incident["deadline"],
                incident["detected_by"] or "-",
            )
        )
        return 0


def cmd_answer(args):
    """Stamp that a blocker's card was ANSWERED, and by whom.

    This is the pivot the whole ledger turns on: `close` will not fire without it,
    because a resume with no recorded answer is not a resolution — it is a worker
    that started moving again for some other reason. The answer is stamped
    separately from the close precisely so the gap between them is measurable: an
    incident answered and never resumed is the case SC4 exists for.

    ⚠️ `--by` is a CLASS (`manager` / `operator`), not a session id, because it is
    the class the share reading counts and the class a caller can prove. The session
    id is kept beside it in `answered_by_session` for provenance.
    """
    key = dedup_key(args.session, args.kind)
    with Locked():
        data = read_ledger()
        incident = find_open(data["incidents"], key)
        if incident is None:
            print("no open incident for %s" % key)
            return 0
        incident["answered_at"] = now()
        incident["answered_by"] = args.by
        incident["answered_by_session"] = args.by_session or caller_id()
        write_ledger(data)
        print("answered %s (by %s)" % (key, args.by))
        return 0


def cmd_close(args):
    if args.outcome == TIMED_OUT:
        _refuse(
            "`timed-out` is an escalation, not a close. Use `escalate`; a timed-out\n"
            "      incident has no verified resume, so it never reaches close."
        )
    if args.outcome == SKIPPED_GONE:
        _refuse(
            "`skipped-gone` is the ledger's own gone-session retirement, not a verified\n"
            "      resume. `escalate` writes it when the session is absent from the\n"
            "      liveness probe; there is no evidence for a caller to supply."
        )
    evidence = " ".join((args.resume_evidence or "").split())
    if not evidence:
        _refuse(
            "close needs --resume-evidence naming the observed resume (the worker's\n"
            "      next tool call or task change after the answer). A card close is not\n"
            "      a resume: the attention store records `closed` as CLEARED, not\n"
            "      answered, so closing on it would record a resolution nobody earned."
        )
    key = dedup_key(args.session, args.kind)
    with Locked():
        data = read_ledger()
        incident = find_open(data["incidents"], key)
        if not incident:
            print("no open incident for %s" % key)
            return 0
        # A resume with no recorded answer is not a resolution: the worker started
        # moving again for some reason nobody recorded. `answer` owns that stamp,
        # and requiring it here is what keeps the two events measurable apart.
        #
        # ⚠️ `self-cleared` is EXEMPT, and the exemption is the definition rather
        # than a convenience: a blocker that cleared itself is one nobody answered.
        # Requiring an answer there would leave the class unwritable and push a
        # self-clear into `manager`, inflating the one number the goal reads.
        outcome = args.outcome or incident.get("answered_by") or RESOLVED_BY_MANAGER
        if outcome in ("manager", "operator") and not incident.get("answered_at"):
            _refuse(
                "%s has no recorded answer. Run `answer` first: a resume without an\n"
                "      answer is not a resolution, and closing on one would record a\n"
                "      manager-resolved blocker nobody asked for." % key
            )
        incident["outcome"] = outcome
        incident["closed_at"] = now()
        incident["resume_evidence"] = evidence
        write_ledger(data)
        print("closed %s (outcome %s)" % (key, outcome))
        return 0


def past_deadline(incident, at):
    deadline = parse_ts(incident.get("deadline"))
    return deadline is not None and at >= deadline


def post_card(incident):
    """Post the escalation card, keyed on the incident so a re-run cannot duplicate it."""
    payload = (
        "Blocker on session %s (%s) passed its deadline with no verified resume. "
        "Owner: %s. Opened %s, deadline %s, detected by %s."
        % (
            incident.get("session_id"),
            incident.get("kind"),
            incident.get("owner"),
            incident.get("opened_at"),
            incident.get("deadline"),
            incident.get("detected_by") or "unknown",
        )
    )
    command = [
        sys.executable,
        ATTENTION_ASK,
        "post",
        # Raised by the periodic manager loop (see the module docstring: the ledger
        # records worker blockers and the manager sweep is what escalates them), so
        # the card is a manager's escalation even though its subject is a worker.
        "--producer-kind", "manager",
        "--dedup-key",
        incident["dedup_key"],
        "--payload",
        payload,
        "--context",
        "An answered-but-never-resumed blocker counts in the total and not as "
        "resolved. This card is the escalation, not a resolution.",
        "--option",
        "ack",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(
            "warning: could not post escalation for %s: %s\n"
            % (incident["dedup_key"], (result.stderr or result.stdout).strip())
        )
        return False
    return True


def cmd_escalate(args):
    """Escalate every open incident past its deadline.

    The stamp is the discriminator the criterion reads: a periodic round passes
    `--round-id` and stamps `round:<id>`, while a typed run leaves it off and stamps
    `manual:<session-id>`. That is what makes "invoked by the periodic caller" an
    observation instead of an assertion -- a hand-run cannot produce a `round:` stamp
    without inventing a round id, which is the class of dishonesty this ledger exists
    to prevent.
    """
    at = parse_ts(now())
    # ⚠️ The probe runs BEFORE the lock, and only when it can change the outcome:
    # holding the ledger lock across a subprocess plus an HTTP call would stall every
    # other writer for the length of a network round trip.
    pending_due_stall = any(
        incident.get("outcome") == PENDING
        and incident.get("kind") == "stall"
        and past_deadline(incident, at)
        for incident in read_ledger()["incidents"]
    )
    live = live_sessions() if pending_due_stall else None
    if pending_due_stall and live is None:
        # ⚠️ The SAME warning `cmd_stall` emits, because the same silent-off applies:
        # an outage must never read as "every session is gone". The difference is
        # which leg is left unguarded, and this is the leg that posts the cards.
        warn_liveness_unavailable("escalating")
    with Locked():
        data = read_ledger()
        due = [
            incident
            for incident in data["incidents"]
            if incident.get("outcome") == PENDING and past_deadline(incident, at)
        ]
        if not due:
            print("nothing past its deadline")
            return 0
        stamp = "round:%s" % args.round_id if args.round_id else "manual:%s" % caller_id()
        gone = []
        malformed = 0
        for incident in due:
            # ⚠️ A stall incident for a session that is GONE must not become a card:
            # the card can only say "no verified resume" for a session that no longer
            # exists, which is nothing the operator can act on. Gating only `stall`,
            # because a permission or question incident belongs to a live producer
            # that posted it and is polling for the answer.
            session_id = as_session_id(incident.get("session_id"))
            if incident.get("kind") == "stall" and live is not None:
                if not session_id:
                    # ⚠️ A malformed id cannot PROVE the session is gone, so the gate
                    # fails OPEN and the incident escalates: suppressing a live
                    # worker's card is the worse of the two errors, and the guard in
                    # `as_session_id` already kept the round from dying on it.
                    malformed += 1
                elif session_id not in live:
                    # RETIRED, not left pending: a skip is not an escalation, and a
                    # pending record re-lists, re-probes and re-enters `share`'s
                    # denominator every round with no possible resolution.
                    if not args.dry_run:
                        incident["outcome"] = SKIPPED_GONE
                        incident["closed_at"] = now()
                        # The PROBE's own timestamp, not the write's: the judgement is
                        # about the fleet as it stood when `live_sessions()` ran, and a
                        # session that (re)started between the probe and this write is
                        # the one case the record has to be able to explain.
                        incident["resume_evidence"] = (
                            "session absent from the liveness probe taken at %s (%s)"
                            % (at.strftime(TS_FORMAT), stamp)
                        )
                    gone.append(incident["dedup_key"])
                    continue
            if args.dry_run:
                print("would escalate %s (stamp %s)" % (incident["dedup_key"], stamp))
                continue
            if not post_card(incident):
                # Leave the incident pending: it is still un-escalated, and stamping
                # it would record an escalation that never reached the board.
                continue
            incident["outcome"] = TIMED_OUT
            incident["escalated_by"] = stamp
            print("escalated %s (stamp %s)" % (incident["dedup_key"], stamp))
        if malformed:
            sys.stderr.write(
                "warning: %d stall incident(s) carry a malformed session_id, so the "
                "gone gate cannot judge them; escalating unfiltered\n" % malformed
            )
        if gone:
            # `skipped-gone`, never `timed-out`: a `timed-out` stamp would claim an
            # escalation that never happened, and the record is terminal so a later
            # round neither re-lists it nor pays the probe for it again.
            #
            # ⚠️ `would retire` under `--dry-run`, matching `would escalate` above and
            # `would close` in `cmd_resume`: a dry run that printed `retired` would be
            # indistinguishable from a real one in its own output, which is the false
            # signal this ledger exists to remove.
            print(
                "%s %d stall incident(s) for gone sessions: %s"
                % (
                    "would retire" if args.dry_run else "retired",
                    len(gone),
                    bounded_names(gone),
                )
            )
        if not args.dry_run:
            write_ledger(data)
        return 0


def read_drive_ledger(path):
    """{session_id: (verdict, unverifiable_streak)} from the fleet-drive ledger.

    ⚠️ **The streak is READ, never recounted.** `scripts/drive-unverifiable.py`
    already owns that measurement — it advances `unverifiable_streak` per entry,
    consecutive only when `unverifiable_round` equals the ledger's own `round`, and
    resets it on any other verdict. A second counter here would be a second answer
    to the same question, and the two would drift the first time either changed.

    The drive ledger carries two shapes and the live file has carried both at once
    (`drive-ledger.py` documents the measurement): the canonical `{"round": ts,
    "entries": [...]}` and a LEGACY flat map keyed by an eight-character sid. This
    reads both, because a stall detector that silently saw only one would report a
    quiet fleet while half the sessions were invisible.

    ⚠️ A corrupt drive ledger is REFUSED here too. Reading it as "no stalls" is the
    false-empty this ledger exists to avoid, one store over.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        _refuse("%s is unreadable (%s); refusing to read it as no stalls" % (path, err))
    if not isinstance(data, dict):
        _refuse("%s is not a drive ledger" % path)

    def streak_of(row):
        try:
            return int(row.get("unverifiable_streak") or 0)
        except (TypeError, ValueError):
            return 0

    observed = {}
    for row in data.get("entries") or []:
        if not isinstance(row, dict):
            continue
        sid = as_session_id(row.get("session_id"))
        if sid:
            observed[sid] = (row.get("verdict"), streak_of(row))
    for key, row in data.items():
        if key == "entries" or not isinstance(row, dict):
            continue
        sid = as_session_id(row.get("sid") or row.get("session_id") or key)
        if sid and sid not in observed:
            observed[sid] = (row.get("verdict"), streak_of(row))
    return observed


def cmd_stall(args):
    """Open a stall incident for every session the drive leg has called stalled.

    The stall is the one blocker that is a DURATION rather than an event, so it is
    the one that cannot be filed on sight: the incident opens only once the drive
    leg's own `unverifiable_streak` reaches `SUPERVISOR_INCIDENT_STALL_ROUNDS`
    (default 2). A session one bad round in gets nothing, and a session that
    recovers is reset by the drive leg before it ever reaches here — which is the
    whole reason the threshold exists.

    ⚠️ This does not count rounds. `drive-unverifiable.py` does, and reading its
    streak is what keeps one measurement in one place.

    ⚠️ **A session that is GONE is skipped, not filed.** The drive ledger keeps
    retained rows for sessions that have since exited, so a long-dead session can
    reach any streak at all; an incident for one can only ever produce a card saying
    "no verified resume", which is nothing the operator can act on. Measured
    2026-10-22: 117 incidents opened in one round, every one of them for a session
    absent from the heartbeat store.

    ⚠️ **At most `incident_budget()` new incidents a round.** The per-round cap the
    drive leg already applies to its own actions had no counterpart here, so the
    flood arrived through the one door it did not cover.
    """
    required = stall_rounds()
    drive_ledger = args.drive_ledger or os.environ.get(
        "SUPERVISOR_DRIVE_LEDGER"
    ) or os.path.expanduser("~/.claude/state/fleet-drive/ledger.json")
    observed = read_drive_ledger(drive_ledger)
    if not observed:
        # A fresh install or an empty ledger has nothing to filter, so the probe's
        # subprocess + registry read + HTTP call would be paid for nothing.
        print("no session has stalled for %d consecutive rounds" % required)
        return 0
    live = live_sessions()
    if live is None:
        warn_liveness_unavailable("opening")
    eligible = []
    gone = []
    for sid in sorted(observed):
        verdict, streak = observed[sid]
        if verdict != "unverifiable" or streak < required:
            continue
        if live is not None and sid not in live:
            gone.append((sid, streak))
            continue
        eligible.append((sid, streak))
    if gone:
        # One line, not one per row: the drive ledger keeps a row for every session
        # that has ever been unverifiable, so a per-row line grows without bound as
        # that graveyard does -- hence the bounded prefix. The names stay, because a
        # skip that only counted would be indistinguishable from a threshold that
        # stopped firing.
        print(
            "skipped %d gone session(s) past the threshold: %s"
            % (
                len(gone),
                bounded_names(["%s(%d)" % (sid, streak) for sid, streak in gone]),
            )
        )
    # Longest streak first, then session id — the drive leg's own order, so a backlog
    # drains oldest-first and nothing starves behind a steady trickle of new rows.
    eligible.sort(key=lambda row: (-row[1], row[0]))
    budget = incident_budget()
    opened = 0
    with Locked():
        data = read_ledger()
        for sid, streak in eligible:
            key = dedup_key(sid, "stall")
            if find_open(data["incidents"], key):
                print("already open %s (streak %d)" % (key, streak))
                continue
            if opened >= budget:
                print(
                    "held back %s (streak %d): the round's budget of %d is spent"
                    % (sid, streak, budget)
                )
                continue
            at = now()
            incident = {
                "dedup_key": key,
                "session_id": sid,
                "kind": "stall",
                "owner": sid,
                "opened_at": at,
                "deadline": (
                    parse_ts(at) + datetime.timedelta(seconds=deadline_seconds())
                ).strftime(TS_FORMAT),
                "outcome": PENDING,
                "closed_at": None,
                "detected_by": "drive:unverifiable@%s" % (args.round_id or at),
                "escalated_by": None,
                "card_id": "",
                "resume_evidence": None,
                "observations": streak,
            }
            data["incidents"].append(incident)
            opened += 1
            print(
                "opened %s (kind stall, streak %d, deadline %s)"
                % (key, streak, incident["deadline"])
            )
        if opened and not args.dry_run:
            write_ledger(data)
    if opened == 0 and not gone:
        print("no session has stalled for %d consecutive rounds" % required)
    return 0


def parse_any_ts(value):
    """Parse the ISO shapes a transcript timestamp carries.

    Transcript stamps are not this ledger's `TS_FORMAT`: they arrive with
    milliseconds and a `Z` or a numeric offset. Normalising here rather than
    assuming one shape is what keeps a resume from being silently missed — a
    comparison against `None` never fires, and a resume that never fires leaves an
    incident open forever while looking like a worker that did not move.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def transcript_for(projects_root, session_id):
    """The transcript for a session, resolved by id PREFIX — the watcher's own join.

    `manager-attention-watch.py` resolves the same way (`<sid8>*.jsonl` under the
    projects root), so this reuses that convention rather than inventing a second
    one. A full-uuid filename match would miss every transcript whose directory is
    named from a munged cwd.
    """
    if not projects_root or not session_id:
        return None
    candidates = sorted(
        glob.glob(os.path.join(projects_root, "*", session_id[:8] + "*.jsonl"))
    )
    return candidates[0] if candidates else None


def tool_call_after(path, since, limit_bytes=262144):
    """The first (timestamp, tool_name) for a tool_use record dated after `since`.

    THIS is the verified resume, and it is the whole reason a card close cannot
    stand in for it: a card is cleared by whoever answered, while a tool call can
    only be made by the worker itself. Nothing else in the system proves the worker
    moved.

    Read from the TAIL, the same window the rest of this repo uses — a transcript is
    append-only, and the newest records are the ones that matter.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > limit_bytes:
                handle.seek(size - limit_bytes)
                handle.readline()  # drop the partial line the seek landed inside
            for raw in handle:
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                stamp = parse_any_ts(record.get("timestamp"))
                if stamp is None or stamp <= since:
                    continue
                content = (record.get("message") or {}).get("content") or []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        return stamp, block.get("name") or "tool"
    except OSError:
        return None
    return None


def cmd_resume(args):
    """Close every answered incident whose worker has since made a tool call.

    The automatic half of the rule: `close` enforces that a resume needs an answer,
    and this observes the resume so nobody has to assert one by hand. The outcome
    comes from `answered_by`, never from the observer — who answered is a fact about
    the answer, and a resume says nothing about it.

    ⚠️ A resume with no `answered_by` (an incident answered before the field existed)
    is left OPEN rather than defaulted: recording it as `manager` would credit the
    owning manager for a blocker whose answerer nobody recorded, which is precisely
    the inflation this ledger exists to prevent.
    """
    if not args.projects_root:
        _refuse("resume needs --projects-root (~/.claude/projects)")
    closed = 0
    would_close = 0
    with Locked():
        data = read_ledger()
        for incident in data["incidents"]:
            if incident.get("outcome") != PENDING:
                continue
            answered_at = parse_ts(incident.get("answered_at"))
            if answered_at is None:
                continue
            outcome = incident.get("answered_by")
            if outcome not in CLOSE_OUTCOMES:
                continue
            path = transcript_for(args.projects_root, incident.get("session_id"))
            if path is None:
                continue
            hit = tool_call_after(path, answered_at)
            if hit is None:
                continue
            stamp, name = hit
            evidence = "tool call %s at %s, after the answer at %s" % (
                name,
                stamp.strftime(TS_FORMAT),
                incident["answered_at"],
            )
            if args.dry_run:
                # Counted separately from `closed`: a dry run writes nothing, so
                # `closed` stays 0 and the tail would otherwise print "no verified
                # resumes" directly under the "would close" lines it just printed.
                print("would close %s (%s)" % (incident["dedup_key"], evidence))
                would_close += 1
                continue
            incident["outcome"] = outcome
            incident["closed_at"] = now()
            incident["resume_evidence"] = evidence
            closed += 1
            print(
                "closed %s on verified resume (outcome %s)" % (incident["dedup_key"], outcome)
            )
        if closed and not args.dry_run:
            write_ledger(data)
    if closed == 0 and would_close == 0:
        print("no verified resumes")
    return 0


def window_incidents(incidents, days):
    if days is None:
        return list(incidents)
    cutoff = parse_ts(now()) - datetime.timedelta(days=days)
    kept = []
    for incident in incidents:
        opened = parse_ts(incident.get("opened_at"))
        if opened is not None and opened >= cutoff:
            kept.append(incident)
    return kept


def cmd_share(args):
    """The manager-resolved share over a window.

    `pending` never counts as resolved -- that is the whole point. An incident that
    was answered and never resumed sits in the denominator and in no numerator, and
    an empty window prints `0 / 0 / 0%` rather than a vacuous 100%.

    ⚠️ **A `skipped-gone` incident is EXCLUDED from the denominator**, and reported
    beside it instead. Its session no longer exists, so it can never be answered and
    never resumed: counting it would put an incident with no possible numerator into
    the denominator of every all-time reading, permanently depressing the one number
    this ledger exists to report. That is not a resolution that did not happen; it is
    an incident that could not be resolved at all.
    """
    data = read_ledger()
    incidents = window_incidents(data["incidents"], args.days)
    retired = [
        incident
        for incident in incidents
        if (incident.get("outcome") or PENDING) == SKIPPED_GONE
    ]
    incidents = [
        incident
        for incident in incidents
        if (incident.get("outcome") or PENDING) != SKIPPED_GONE
    ]
    total = len(incidents)
    counts = {}
    for incident in incidents:
        outcome = incident.get("outcome") or PENDING
        counts[outcome] = counts.get(outcome, 0) + 1
    resolved = counts.get(RESOLVED_BY_MANAGER, 0)
    percent = (100.0 * resolved / total) if total else 0.0
    window = "all time" if args.days is None else "%d day(s)" % args.days
    print("window: %s" % window)
    print("total: %d" % total)
    print("resolved-by-manager: %d" % resolved)
    print("share: %.1f%%" % percent)
    for outcome in ALL_OUTCOMES:
        # `skipped-gone` is EXCLUDED from `counts` by construction (see above), so
        # printing its row here would always read `skipped-gone 0` directly above the
        # `retired N` paragraph that reports the same incidents. The paragraph is the
        # honest home for them; a zero row beside it reads as a contradiction.
        if outcome == SKIPPED_GONE:
            continue
        print("  %-12s %d" % (outcome, counts.get(outcome, 0)))
    if retired:
        print(
            "\nretired %d gone-session incident(s), excluded from the share -- their "
            "session no longer exists, so no resolution was possible." % len(retired)
        )
    if total == 0 and not retired:
        print(
            "\n⚠️ no incidents in this window -- 0 / 0 / 0% is an empty population, "
            "not a 0% resolution rate."
        )
    return 0


def cmd_list(args):
    data = read_ledger()
    incidents = window_incidents(data["incidents"], args.days)
    if args.open:
        incidents = [i for i in incidents if i.get("outcome") == PENDING]
    if not incidents:
        print("no incidents")
        return 0
    for incident in incidents:
        print(
            "%s  %-10s %-11s owner=%-10s opened=%s deadline=%s%s"
            % (
                incident.get("dedup_key", "?"),
                incident.get("kind", "?"),
                incident.get("outcome", "?"),
                incident.get("owner", "?"),
                incident.get("opened_at", "?"),
                incident.get("deadline", "?"),
                "  escalated_by=%s" % incident["escalated_by"]
                if incident.get("escalated_by")
                else "",
            )
        )
    return 0


def cmd_prune(args):
    """Drop terminal incidents older than --max-age-hours.

    Open incidents are never pruned: an incident with no resolution is the one record
    a reader most needs, and age is not a resolution.

    ⚠️ **`skipped-gone` IS terminal, and this is what retires it from the file.** It
    carries a `closed_at` like any other terminal outcome, so it is pruned on the
    same clock -- without that, a gone session's record would sit in the ledger
    forever, re-listed by `list` and re-counted by nothing else.
    """
    cutoff = parse_ts(now()) - datetime.timedelta(hours=args.max_age_hours)
    with Locked():
        data = read_ledger()
        kept = []
        dropped = 0
        for incident in data["incidents"]:
            if incident.get("outcome") == PENDING:
                kept.append(incident)
                continue
            stamp = parse_ts(incident.get("closed_at") or incident.get("opened_at"))
            if stamp is not None and stamp < cutoff:
                dropped += 1
                continue
            kept.append(incident)
        if dropped:
            data["incidents"] = kept
            write_ledger(data)
    print("pruned %d terminal incident(s) older than %gh" % (dropped, args.max_age_hours))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="The worker-blocker incident ledger (open | close | escalate | share | list | prune)."
    )
    subparsers = parser.add_subparsers(dest="command")

    def add_identity(sub):
        sub.add_argument("--session", required=True, help="the blocked session's id")
        sub.add_argument("--kind", required=True, choices=KINDS)

    open_parser = subparsers.add_parser("open", help="record a blocker incident")
    add_identity(open_parser)
    open_parser.add_argument("--owner", required=True, help="session id that owns the blocker")
    open_parser.add_argument(
        "--detected-by",
        default="",
        help="the detector's own identity: card:<item_id> / question:<item_id> / drive:<verdict>@<round>",
    )
    open_parser.add_argument("--card-id", default="", help="empty for a stall, which issues no card")
    open_parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=None,
        help="override SUPERVISOR_INCIDENT_DEADLINE_SECONDS for this incident",
    )
    open_parser.set_defaults(func=cmd_open)

    close_parser = subparsers.add_parser("close", help="close on a verified resume")
    add_identity(close_parser)
    # `timed-out` is deliberately ACCEPTED by argparse and refused below, rather
    # than dropped from `choices`. A `choices` rejection is a usage error that
    # cannot say why; the refusal names the escalation path the caller wanted.
    close_parser.add_argument(
        "--outcome",
        default=None,
        choices=ALL_OUTCOMES,
        help="defaults to the recorded answered_by class (manager / operator)",
    )
    close_parser.add_argument(
        "--resume-evidence",
        required=True,
        help="the observed resume: the worker's next tool call or task change after the answer",
    )
    close_parser.set_defaults(func=cmd_close)

    answer_parser = subparsers.add_parser(
        "answer", help="stamp that the blocker's card was answered, and by whom"
    )
    add_identity(answer_parser)
    answer_parser.add_argument(
        "--by",
        required=True,
        choices=("manager", "operator"),
        help="the class that answered; the share reading counts only `manager`",
    )
    answer_parser.add_argument(
        "--by-session", default="", help="the answering session's id, for provenance"
    )
    answer_parser.set_defaults(func=cmd_answer)

    escalate_parser = subparsers.add_parser(
        "escalate", help="escalate open incidents past their deadline"
    )
    escalate_parser.add_argument(
        "--round-id",
        default="",
        help="the periodic round's own timestamp; omit for a hand-run, which stamps manual:<session>",
    )
    escalate_parser.add_argument("--dry-run", action="store_true")
    escalate_parser.set_defaults(func=cmd_escalate)

    stall_parser = subparsers.add_parser(
        "stall", help="open a stall incident from the fleet-drive ledger"
    )
    stall_parser.add_argument(
        "--drive-ledger",
        default=None,
        help=(
            "the fleet-drive ledger path (canonical or legacy shape); defaults to "
            "SUPERVISOR_DRIVE_LEDGER, the same override drive-ledger.py honours"
        ),
    )
    stall_parser.add_argument(
        "--round-id", default="", help="the drive round's own timestamp"
    )
    stall_parser.add_argument("--dry-run", action="store_true")
    stall_parser.set_defaults(func=cmd_stall)

    resume_parser = subparsers.add_parser(
        "resume", help="close answered incidents whose worker has since acted"
    )
    resume_parser.add_argument(
        "--projects-root", default=None, help="~/.claude/projects, where transcripts live"
    )
    resume_parser.add_argument("--dry-run", action="store_true")
    resume_parser.set_defaults(func=cmd_resume)

    share_parser = subparsers.add_parser("share", help="the manager-resolved share over a window")
    share_parser.add_argument("--days", type=int, default=None)
    share_parser.set_defaults(func=cmd_share)

    list_parser = subparsers.add_parser("list", help="list incidents")
    list_parser.add_argument("--days", type=int, default=None)
    list_parser.add_argument("--open", action="store_true")
    list_parser.set_defaults(func=cmd_list)

    prune_parser = subparsers.add_parser("prune", help="drop old terminal incidents")
    prune_parser.add_argument("--max-age-hours", type=float, default=24 * 30)
    prune_parser.set_defaults(func=cmd_prune)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
