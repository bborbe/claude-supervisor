#!/usr/bin/env python3
"""Drop gates a manager does not own, so its model wakes only for its own.

The attention watcher a manager arms at startup is fleet-wide: `who-needs-me.py`
reports every open gate on the machine, and every emitted line costs the manager
a full turn — a re-sent session context per wake. Measured 2026-09-25 on Fleet
Manager session `64b4a415`: ~12 wakes in one session for panes owned by other
live managers, at a measured median of 345,344 cache-read tokens per turn
(a measured no-change sweep) — roughly 4.1M tokens
in a single session, recurring every loop.

Ownership is resolvable without guessing, through four hops:

  1. PANE -> SESSION  the open item's `pane` and `session_id` in
     `~/.claude/state/attention/*.events.jsonl`. NOT the `*.needs.json` store:
     that is the retired pattern and carries nothing for the live feed
     (measured 2026-09-25: 0 of 9 feed panes had a record). NOT the HTTP
     attention store either -- it is authoritative for WHICH items are open and
     resolves producer liveness server-side, but carries no `pane`; the reader
     joins it to this log on `dedup_key` for the event-time fields.

  2. SESSION -> SPAWNER  `parent_session` in the spawn ledger
     (`~/.local/state/claude-supervisor/sessions/<sid>.json`).

  3. SPAWNER IS A MANAGER  when it has NO ledger record of its own, OR when its
     record declares `role: "manager"`. The first arm is the common one -- a
     manager started by `/supervisor:open` runs in a plain wezterm tab and is
     never itself spawned. The second arm exists because that is not the only
     way to start one: `spawn_agent(role="manager")` DOES write a record, so
     reading any record as proof of "worker" filed such a manager as a worker
     and every peer re-escalated its workers' gates. Measured 2026-09-25: 600
     records, 15 distinct parents, 11 with no own record (managers) against 4
     with one (worker-spawned workers). This is a property of the spawn edge,
     not a session-name convention -- and it is NOT `manager-liveness.py`, which
     tracks a loop's cadence by topic slug and never answers whether a session
     is a manager.

  4. SPAWNER IS LIVE  the session registry (`~/.claude/sessions/<pid>.json`,
     `sessionId` + `status`). A dead manager's panes are nobody's to route, so
     they are kept rather than dropped.

The keep-set is the complement and is deliberately wide: a gate is dropped ONLY
when a live manager other than this watcher's own owns it -- reached at hop 3
(its SPAWNER is such a manager) or at hop 3b (the gated session IS such a
manager itself). Panes this session spawned, panes whose spawner is a worker,
panes whose spawner is dead, and unowned panes all emit.

⚠️ HOP 3b EXISTS BECAUSE HOP 3 CANNOT REACH A MANAGER'S OWN GATE. A manager that
raises a gate on itself was operator-started, so it has no ledger record, hop 2
resolves no spawner, and `spawner_verdict` reads the empty spawner `unowned` and
keeps it -- while that same manager's WORKERS drop correctly one hop out.
Measured 2026-09-27 on goal-manager session `cd816ba7`: one `--explain` run
dropped `ca3ea901`'s two workers (panes 2326, 2327) and emitted `ca3ea901`'s own
pane 2076 as `unowned` in the same pass -- succeeding one hop out and failing
exactly the hop this adds. Re-measured 2026-10-01 with the actors named: session
`9ecc3e19` was simultaneously the SPAWNER of a dropped gate and the SUBJECT of an
emitted one. Cost on one topic manager: 5-13 foreign wakes per 30-minute arm,
zero true positives. It is consulted only where the spawner path emits -- like
the claim below, hop 3b can only ADD a drop, never turn a resolved
`peer-manager` drop back into an emit.

⚠️ HOP 3b KEYS ON `is_manager(session_id, ledger)`, NOT ON `spawner is None`.
The two are not the same test, and the difference is a live case rather than a
pedantic one: a WORKER whose spawn chain never resolved to a registered session
carries `parent_session: null` too (see `CLAUDE.md` § The spawn ledger), but it
has a ledger record of its OWN, so it is not a manager and must keep emitting.
Keying 3b on the empty spawner would drop every such worker.

⚠️ **HOP 3b DROPS ON "NOT A WORKER, AND LIVE", AND THAT CLASS IS WIDER THAN
"MANAGER".** What makes the drop correct is NOT the shape hop 4 protects — hop 4
*keeps* a gate nobody can act on, this *drops* one — but that the gated session is
live and is therefore **its own reporter**: a live session raises its own gate and
sees its own closer, so a second watcher surfacing it buys nothing. That is what
the liveness guard encodes, and it is why a DEAD peer manager's gate still emits.

`is_manager` admits two arms, and both are read here: a session with **no ledger
record at all**, and — since 2026-10-06 — a session whose record declares
`role: "manager"`. The second arm is what lets a manager opened through the worker
path (`spawn_agent(role="manager")`, which writes a record) be recognised at all;
before it, only the record's *presence* was read, so such a manager was filed as a
worker. An operator-started manager is only the most common member of the first
arm. Two others are named here rather than left to be discovered:

  * a worker whose session id never resolved — `CLAUDE.md` § The spawn ledger:
    *"A worker whose session id never resolved gets no record rather than one
    filed under a key nothing would look up, and the server logs that rather than
    staying quiet."* It is absent from the ledger **and** resolves no spawner, so
    it satisfies both halves of the branch; before this change it emitted.
  * an operator-opened interactive session that never went through `spawn_agent`
    — no record, for the same reason a manager has none.

**No predicate fix is available** for either: by the evidence this hop has, a
record-less live session is indistinguishable from a manager, and the alternative
— dropping the branch — restores the peer-manager wake the branch exists to
remove. The compensating control is that the condition is *discoverable* rather
than invisible: the server logs an unresolved id at spawn, per the same paragraph.
Named here rather than left to be rediscovered, for the reason the PREDECESSOR
section above states its own limitation.

⚠️ HOP 3b REQUIRES LIVENESS, so a DEAD peer manager's own gate is KEPT -- hop 4's
rule read one hop differently. A dead manager cannot act on its own gate, so the
duplication that justifies the drop is absent and dropping would leave the gate
with no owner. Absence from the registry is not proof of death either (measured
2026-10-04: a live session read `UNREGISTERED` because its registry record keys
the id differently), and the asymmetry is the one `load_claims` states -- one
wasted wake against a fleet-wide silence.

A CLAIM overrides that, and it is the one ownership input that does NOT come
from the spawn edge. `scripts/ownership-claim.py` records which manager has
adopted a gated session (`~/.claude/state/ownership-claims.json`, keyed on the
GATED session id -- the id `session_for_pane` already resolves, so the claim
joins with no new hop). A claim held by a LIVE manager other than this watcher
drops the pane as `claimed`; a claim held by this watcher, or by a manager that
is gone, or read against an unreadable registry, EMITS -- the same fail-open the
spawner hops take, for the same reason.

⚠️ **A claim may only ADD a drop, never remove one.** The spawner path is
resolved FIRST, and the claim is consulted only where that path emits. Running
the claim's fail-open outcomes ahead of the spawner path would convert a
resolved `peer-manager` drop into an emit whenever any claim existed for the
pane -- the fail-open rule protects an *unresolvable* input, and does not reach
a verdict the spawner path already resolved.

Without this input a pane whose session
has no spawner is emitted to every manager forever, because nothing about the
pane changes when a manager adopts it: measured 2026-10-02, 5+ wakes in one hour
for gates the manager could not act on, 1 of them actionable, after the Fleet
Manager had taken ownership of those panes by `SendMessage` -- a channel no
record reads. The `--only-file` allowlist is NOT the answer and stays barred:
it trades this defect for a worse one, since it drops genuinely unowned panes.

⚠️ A PREDECESSOR MANAGER IS NOT A PEER MANAGER, and this filter cannot tell them
apart. A PEER manager serves a DIFFERENT subject; a PREDECESSOR served the SAME
subject and handed it over. After a handover the outgoing manager is still live
and still owns the spawn records of every worker it started, so hop 3 reads it as
a manager and hop 4 reads it as live -- and its workers drop as `peer-manager`
although they belong to the subject this watcher serves. Measured 2026-09-26 on a
Manager Layer handover: `gates: 6  emit: 1  dropped(peer-manager): 5`, four of
them that topic's own workers (spawner `1217e759`, the outgoing manager) and one
a genuine peer (Fleet Manager `64b4a415`).

The window is BOUNDED AND SELF-CLOSING, and that is the compensating control. Hop
4 keeps a dead manager's panes, so the blindness lasts exactly from the handover
until the outgoing session exits. Measured 2026-09-27: those same four panes read
`emit dead-manager` once `1217e759` had exited, with no code change. Until then,
read `who-needs-me.py` directly on the first tick after taking a topic over --
the watcher is the push channel, not the only channel.

No predicate fix is available, and the honest answer is this documented
limitation rather than a code change. Two candidates were considered and
rejected:

  * TOPIC-SCOPED -- would need a subject for the watcher and for each spawner.
    The only sources are `~/.claude/state/worker-manager/<sid>.json` (absent for
    a manager that armed no loop: the successor in the measurement above had
    none) and the registry `name` (the subject by convention only, and
    unnormalized -- `Sample Manager` against `Unattended
    Execution`). Both reintroduce the session-name convention hop 3 exists to
    avoid.
  * INHERITANCE -- re-pointing `parent_session` falsifies provenance, and while
    both managers are live it moves the blindness to the other session, which
    would then read its own workers as `peer-manager`. Inert in its target
    window.

⚠️ A filter that silently matches nothing is indistinguishable from a quiet
fleet (a broken watcher is indistinguishable from a quiet one). So `--explain`
prints the reason for every verdict, and every run prints a
`gates: N  emit: N  dropped(peer-manager): N  dropped(peer-manager-own): N
dropped(claimed): N` line on stderr — which is what makes an empty result legible
as "nothing to filter" rather than "the filter is broken".
"""

import argparse
import glob
import json
import os
import sys

STATE = os.path.expanduser("~/.claude/state/attention")
LEDGER = os.path.expanduser("~/.local/state/claude-supervisor/sessions")
REGISTRY = os.path.expanduser("~/.claude/sessions")
# Same env override the writer (`ownership-claim.py`) honours, deliberately: a
# writer pointed at one file while every reader reads another is a SILENT
# no-op -- the claim is recorded, `list` reports it held, and no watcher ever
# consults it. The two resolutions must not drift.
CLAIMS = os.environ.get("SUPERVISOR_OWNERSHIP_CLAIMS") or os.path.expanduser(
    "~/.claude/state/ownership-claims.json"
)

EMIT = "emit"
DROP = "drop"

# The token `who-needs-me.py` writes to its STDOUT when it refuses a read, so the
# refusal survives the pipe that carries its output here. Kept as a literal rather
# than imported: the two scripts are shipped side by side and may be updated
# independently, and a filter that could not recognise a NEWER writer's refusal would
# silently re-collapse it into the empty feed it is meant to be told apart from.
REFUSAL_MARKER = "WHO-NEEDS-ME-REFUSED"

# The mark `who-needs-me.py` stamps on a row it returned from the event-log
# fallback, matched literally against the feed line. It is the feed's own
# spelling -- keep the two in lockstep, or the filter carries a signal the feed
# no longer emits and every row reads live.
REPLAY_MARK = "⟳replay"


def load_ledger(ledger_dir=LEDGER):
    """Spawn records keyed by session id, or {} when the directory is unreadable.

    An unreadable ledger is NOT an empty one -- it means every spawner resolves
    as a manager, which would drop the whole fleet. The caller must treat {}
    as unknown and fail open (keep everything) rather than as "no spawns".
    """
    out = {}
    for path in glob.glob(os.path.join(ledger_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                rec = json.load(handle)
        except (OSError, ValueError):
            continue
        sid = rec.get("session_id") or os.path.basename(path)[: -len(".json")]
        out[sid] = rec
    return out


def load_claims(path=CLAIMS):
    """Open ownership claims, keyed by gated session id, or {} when unreadable.

    An unreadable store is NOT an empty one, and here the two happen to agree:
    both fail open. That is deliberate -- a claim store that cannot be read must
    never silence a manager's watcher, and the cost of a missed drop is one
    wasted wake against a fleet-wide silence.

    Only entries carrying a `manager` are returned; a malformed entry is skipped
    rather than read as a claim held by nobody. Read directly rather than by
    shelling out to `ownership-claim.py`, so the filter keeps its single-process
    shape and stays runnable when the script is absent.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    claims = data.get("claims")
    if not isinstance(claims, dict):
        return {}
    out = {}
    for session_id, entry in claims.items():
        if isinstance(entry, dict) and entry.get("manager"):
            out[str(session_id)] = str(entry["manager"])
    return out


def is_manager(session_id, ledger):
    """A spawner is a manager when it was never itself spawned, or was spawned AS one.

    The spawn edge alone cannot answer this. `/supervisor:open` starts a topic
    manager in a plain wezterm tab, which writes no ledger record, so absence is the
    common case -- but `spawn_agent(role="manager")` is a documented call that DOES
    write one, and reading any record as proof of "worker" made such a manager
    indistinguishable from the workers it spawns. Measured 2026-10-06: the CDB in
    Weldall Manager (`1d379d23`, spawned by the Fleet Manager) had a record, so every
    peer resolved its workers `worker-spawner` and re-escalated gates it had already
    handled -- the same gates escalated twice.

    ONLY an explicit `manager` counts, and a record with no `role` reads as a
    worker. That default is NOT "no pre-field manager has a record" -- the very
    manager this fixes had one. It is that a pre-field record cannot be told
    apart: the field is absent on every worker AND every manager spawned before
    it existed alike. Defaulting the other way would flip every pre-field record
    to manager, and workers outnumber managers by orders of magnitude, so it
    would drop the whole fleet's gates -- the fleet-wide silence every other hop
    here fails open to avoid. The residue is a known gap, not a claim that no
    such manager exists: a pre-change `spawn_agent(role="manager")` manager still
    reads as a worker until it is re-spawned or its record gains the field.
    """
    if not session_id:
        return False
    record = ledger.get(session_id)
    if record is None:
        return True
    return record.get("role") == "manager"


def live_ids(registry_dir=REGISTRY):
    """Session ids the registry lists, or None when the registry is unreadable.

    None is not "nothing is live": it is unknown, and the caller fails open.
    """
    if not os.path.isdir(registry_dir):
        return None
    out = set()
    for path in glob.glob(os.path.join(registry_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                rec = json.load(handle)
        except (OSError, ValueError):
            continue
        sid = rec.get("sessionId") or rec.get("session_id")
        if sid:
            out.add(sid)
    return out


def log_items(state_dir=STATE):
    """Every item the event logs carry, newest write per item, open or closed.

    The log is append-only and never rewritten: an `open` line carries the item,
    a later `close` line carrying only its `item_id` clears it. Folded here
    rather than read from the record, so the `pane` and `session_id` always come
    from the same line.

    ⚠️ CLOSED items are returned too, and that is load-bearing. This reader is
    the pane→session hop only — whether the gate is still open was already
    decided upstream by `who-needs-me.py`, which is slow (it globs every
    session's log and reads the attention store and transcripts) and can take
    seconds. Requiring the item to still be open here re-derives a fact the
    caller was already given, against a log that may have moved on in between.
    Measured 2026-09-25: a gate the feed reported on pane 1908 had all **29** of
    its items closed by the time this read ran, so the hop returned None, the
    filter failed open, and it emitted a pane owned by another live manager —
    the one case the filter exists to drop.
    """
    items = []
    for path in glob.glob(os.path.join(state_dir, "*.events.jsonl")):
        opened, closed = {}, set()
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    item_id = rec.get("item_id")
                    if not item_id:
                        continue
                    if rec.get("type") == "open":
                        opened[item_id] = rec
                    else:
                        closed.add(item_id)
        except OSError:
            continue
        for item_id, rec in opened.items():
            items.append(dict(rec, state="answered" if item_id in closed else "open"))
    return items


def session_for_pane(pane, items):
    """The session that raised the gate on `pane`, or None.

    Pane ids are reused, so the newest item for the pane wins — regardless of
    whether that item is still open, for the reason `log_items` records.
    Absent stays absent: a pane with no item at all returns None and the caller
    treats it as unowned, which is a real answer rather than a failure.
    """
    hits = [r for r in items if str(r.get("pane")) == str(pane)]
    if not hits:
        return None
    hits.sort(key=lambda r: r.get("ts") or 0)
    return str(hits[-1].get("session_id") or "") or None


def spawner_verdict(spawner, ledger, live):
    """(emit|drop, reason) from the spawn edge alone.

    Split out so the claim input can be consulted ONLY where this path emits.
    A claim may ADD a drop, never remove one: running a claim's fail-open
    outcomes ahead of this path would turn a resolved `peer-manager` drop into
    an emit whenever any claim existed for the pane, which loses drop precision
    and re-creates the peer-owned-wake cost the filter exists to remove. The
    fail-open rationale -- never silence a watcher on an unresolvable input --
    does not reach a verdict this path already resolved.
    """
    if not spawner:
        return EMIT, "unowned"
    if not is_manager(spawner, ledger):
        return EMIT, "worker-spawner"
    if live is None:
        return EMIT, "liveness-unknown"
    if spawner not in live:
        return EMIT, "dead-manager"
    return DROP, "peer-manager"


def verdict(session_id, spawner, ledger, live, self_id, claims=None):
    """(emit|drop, reason) for one gated session.

    The keep-set is wide on purpose: drop ONLY on a live peer manager -- reached
    by the spawn edge (its spawner), by the gated session being one itself, or by
    a claim a live peer manager holds.
    """
    if not session_id:
        return EMIT, "unowned"
    if self_id and session_id == self_id:
        return EMIT, "self"
    if self_id and spawner == self_id:
        # Above both the spawner path and the claim branch, deliberately: a
        # manager's own workers are the panes it EXISTS to see, so neither a
        # peer spawner nor a peer's claim may silence them. The PREDECESSOR
        # warning above records a measured incident of a manager silently losing
        # its own workers; this keeps that from returning through either door.
        return EMIT, "own-worker"

    call, reason = spawner_verdict(spawner, ledger, live)
    if call == DROP:
        return call, reason

    # HOP 3b -- the GATED SESSION is itself the manager. Hop 3 reads the spawn
    # edge, and a manager's own gate has none: an operator-started manager was
    # never spawned, so hop 2 resolves no spawner and the spawner path just
    # emitted `unowned`. Consulted only where that path emits, exactly as the
    # claim below is, because like a claim it can only ADD a drop -- never turn
    # a resolved `peer-manager` drop back into an emit. Same predicate as hop 3,
    # applied one hop differently; the module header carries the measured cases
    # and why this keys on ledger membership rather than on an empty spawner.
    # A pane this watcher ADOPTED is one it EXISTS to see, and the claim block
    # below promises exactly that with its `own-claim` arm. The adoption is
    # checked here as well because hop 3b sits ABOVE that block: without this
    # guard it would drop an adopted pane before the claim was ever read,
    # silencing the gate for the one manager that undertook to route it -- and
    # the peer's own watcher exits at the `self` check, so nobody is left to
    # report it. Measured contract, not a hypothetical: `ownership-claim.py`
    # records the adoption precisely so the adopting manager keeps seeing a
    # spawnerless pane.
    adopted_by_self = bool(claims and self_id and claims.get(session_id) == self_id)
    if not spawner and is_manager(session_id, ledger) and not adopted_by_self:
        # Liveness stays a requirement, so a DEAD peer manager's own gate falls
        # through and emits: it cannot act on its own gate, so nothing
        # duplicates this wake, and dropping it would leave the gate owned by
        # nobody. An unreadable registry (`live is None`) falls through for the
        # same fail-open reason every other hop takes it.
        if live is not None and session_id in live:
            return DROP, "peer-manager-own"

    # The spawner path emits, so the claim can only ADD a drop. Its fail-open
    # outcomes cost nothing here -- they land on an emit either way -- and a
    # pane with no spawner is exactly the case this input exists for, since it
    # is emitted permanently otherwise.
    if claims:
        holder = claims.get(session_id)
        if holder:
            if self_id and holder == self_id:
                return EMIT, "own-claim"
            if live is None:
                return EMIT, "liveness-unknown"
            if holder not in live:
                return EMIT, "claim-dead"
            return DROP, "claimed"
    return call, reason


def evaluate(pane, ledger, live, items, self_id, claims=None):
    """(verdict, reason, session_id, spawner) for one pane."""
    session_id = session_for_pane(pane, items)
    spawner = (ledger.get(session_id) or {}).get("parent_session") if session_id else None
    call, reason = verdict(session_id, spawner, ledger, live, self_id, claims)
    return call, reason, session_id, spawner


def feed_refusal(lines):
    """The upstream refusal line, or `None` when the feed was actually read.

    ⚠️ **This is the third state, and the reason it is a marker and not "no rows".**
    An empty stdin means one of two different things, and the two demand opposite
    responses:

      * the feed was read and held no gates — a REAL answer, which must keep exiting 0
        with `gates: 0`. Collapsing it into an error is the false refusal
        `docs/pane-reads.md` names, and it has already been shipped once here: a
        `Monitor` armed on this pipeline died with `script failed (exit 2)` on every
        quiet tick (measured 2026-10-05, manager loop tick 154).
      * the feed was never read — `who-needs-me.py` refused, and its stdout is empty
        because the refusal went to stderr, which a pipe does not carry. Measured
        2026-10-06 against v0.106.0 with the mux socket unreachable: this filter read
        zero gates and printed `gates: 0  emit: 0 …`, exit 0 — a healthy empty queue
        certified by the success code, for a transport that never answered.

    The marker is what separates them. `who-needs-me.py` writes `REFUSAL_MARKER` to
    stdout precisely so it reaches this reader through the pipe; an unmarked empty
    stdin stays the first case. Matching is on the line's first token, so the message
    that follows it may change without breaking the contract.
    """
    for line in lines:
        if line.lstrip().startswith(REFUSAL_MARKER):
            return line.strip()
    return None


def feed_rows(stream):
    """(pane, replayed) pairs in a `who-needs-me.py` render, first-appearance order.

    Reads the `[<pane>]` row marker rather than any `--pane-id` flag: the flag
    also appears on the activate command of a row whose pane may already be
    gone, so the marker is the row's own identity.

    ⚠️ **The marker is right-aligned inside its brackets**, so the digits are
    padded: a live render carries `[ 417]`, `[  90]`, `[   0]`, never `[417]`.
    Matching `\\[(\\d+)\\]` therefore matched **nothing** on a real feed and the
    caller exited `pass --pane, --feed, or both` against a feed holding eight
    panes — a scoping filter that renders as a filter which found nothing to
    pass. Measured 2026-10-01 against the live feed (8 pane rows, exit 2); the
    fixtures in `tests/test_gate_owner_filter.py` used unpadded ids and so never
    caught it. Tolerate whitespace on both sides.

    ⚠️ **`replayed` is read off the same line, and it is a guard rather than
    noise.** `who-needs-me.py` stamps `⟳replay` on every row it returns from its
    event-log fallback — the branch it takes when the attention store is
    unreachable — precisely so a manager does not act on a gate answered hours
    ago. This function used to return the pane id alone and discard the rest of
    the line, so the filter emitted `864` for a row the feed had rendered
    `5h49m  ⟳replay  ⚙ …` and the watcher woke on an already-answered gate.
    Measured 2026-10-03 (Manager Layer, ticks 89-90): **10 firings in ~70
    minutes, 2 real.** The mark is carried, never re-derived: it is a property
    of the read that produced the row, and `--pane` callers have no line to
    read it from.
    """
    import re

    out, marks = [], {}
    for line in stream:
        match = re.match(r"\s*\[\s*(\d+)\s*\]", line)
        if not match:
            continue
        pane = match.group(1)
        if pane not in marks:
            marks[pane] = REPLAY_MARK in line
            out.append(pane)
    return [(pane, marks[pane]) for pane in out]


def panes_from_feed(stream):
    """Pane ids alone — `feed_rows` projected to its first element."""
    return [pane for pane, _ in feed_rows(stream)]


def main():
    parser = argparse.ArgumentParser(description="Drop gates a manager does not own.")
    parser.add_argument("--self", dest="self_id", default=None, help="this watcher's session id")
    parser.add_argument("--pane", action="append", default=[], help="evaluate one pane (repeatable)")
    parser.add_argument("--feed", action="store_true", help="read who-needs-me.py output on stdin")
    parser.add_argument("--explain", action="store_true", help="print the reason for every verdict")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--ledger-dir", default=LEDGER)
    parser.add_argument("--registry-dir", default=REGISTRY)
    parser.add_argument("--state-dir", default=STATE)
    parser.add_argument("--claims-file", default=CLAIMS)
    args = parser.parse_args()

    ledger = load_ledger(args.ledger_dir)
    live = live_ids(args.registry_dir)
    items = log_items(args.state_dir)
    claims = load_claims(args.claims_file)

    # ⚠️ Guard on the SOURCE, never on the derived row set. An empty `panes`
    # means one of two different things, and only one of them is an operator
    # error: `--feed` with no `[<pane>]` rows is a *successful* read of an
    # empty feed (exit 0, nothing emitted, `gates: 0` on stderr), while a call
    # carrying neither flag is malformed. Testing `panes` collapsed the two, so
    # the documented watcher arm --
    # `who-needs-me.py --section needs-you | gate-owner-filter.py --feed --self <sid>`
    # -- exited 2 with `pass --pane, --feed, or both` on every quiet tick, and a
    # `Monitor` armed with it died with `script failed (exit 2)` within seconds
    # while its sibling watchers stayed up: the manager's push channel was dead
    # exactly when the fleet was quiet, and the failure read as a bad command
    # rather than as a lost watch. Measured 2026-10-05 (manager loop tick 154):
    # the feed read `Needs you (0)` / `Nothing needs you.` and the pipeline died
    # with the usage error, reproduced in both argument orders and against a
    # synthetic empty feed on stdin. Do not restore the derived-set test: it is
    # the same shape as the padding defect `feed_rows` documents above -- a
    # correct extractor whose empty result the caller misreads as a bad call.
    panes = [(pane, False) for pane in args.pane]
    if args.feed:
        # Read stdin ONCE and answer both questions from the same lines: whether the
        # upstream refused, and which panes it emitted. Two reads of a pipe cannot
        # both see the data, and a refusal is a verdict about the whole read.
        lines = sys.stdin.readlines()
        refusal = feed_refusal(lines)
        if refusal:
            # ⚠️ **No `gates:` line on this path, deliberately.** A marker printed
            # beside the false claim is not a fix — `gates: 0` still reads as a
            # measurement, and a manager parsing it cannot tell it from a quiet fleet.
            # The line that lies is the line that must not be printed.
            sys.stderr.write(f"gate-owner-filter: upstream refused — {refusal}\n")
            sys.stderr.write(
                "gate-owner-filter: refusing to report a gate count from a feed that "
                "was never read. This exit is non-zero on purpose — do not arm a "
                "watcher on this pipeline's output.\n"
            )
            return 1
        panes += feed_rows(lines)
    if not args.pane and not args.feed:
        parser.error("pass --pane, --feed, or both")

    rows = []
    for pane, replayed in panes:
        call, reason, session_id, spawner = evaluate(
            pane, ledger, live, items, args.self_id, claims
        )
        rows.append(
            {
                "pane": pane,
                "replayed": replayed,
                "verdict": call,
                "reason": reason,
                "session_id": session_id,
                "spawner": spawner,
            }
        )

    kept = [r for r in rows if r["verdict"] == EMIT]
    dropped = [r for r in rows if r["verdict"] == DROP]

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
        return 0

    if args.explain:
        # ⚠️ `:16` is filled EXACTLY by `peer-manager-own` and `liveness-unknown`,
        # and the `pane` column stays aligned only because no reason exceeds it
        # (measured 2026-10-04: `pane` at column 23 on every row, both reasons
        # included). A reason longer than 16 characters must widen this field, or
        # every row's columns shift by its overrun -- in a diagnostic whose whole
        # job is legibility.
        for r in rows:
            print(
                f"{r['verdict']:4} {r['reason']:16} pane {r['pane']:>5}  "
                f"session={str(r['session_id'])[:8]:8} spawner={str(r['spawner'])[:8]:8}"
            )
    elif args.feed:
        # The watcher's own shape: the panes worth a turn, nothing else.
        # ⚠️ A replayed row carries the feed's own mark, because this line is
        # the watcher's entire input: it prints a pane id and nothing else, so
        # a replayed row was byte-identical to a live one and the watcher woke
        # on gates already answered. A LIVE row renders exactly as it always
        # did -- the no-regression requirement is that a responsive store's
        # output is unchanged, and a mark that was always present would fail it
        # (the same rule `who-needs-me.py`'s own render states).
        for r in kept:
            print(f"{r['pane']}{'  ' + REPLAY_MARK if r['replayed'] else ''}")
    else:
        for r in rows:
            print(r["verdict"])

    # Loud on the count, so an empty drop set reads as "nothing to filter"
    # rather than as a filter that never matched.
    # Split by reason, not lumped: `dropped` now covers three rules, and a run
    # driven entirely by claims -- or by hop 3b -- would otherwise report panes
    # as peer-manager drops that are not: the exact misread this line exists to
    # prevent.
    dropped_peer = [r for r in dropped if r["reason"] == "peer-manager"]
    dropped_own = [r for r in dropped if r["reason"] == "peer-manager-own"]
    dropped_claimed = [r for r in dropped if r["reason"] == "claimed"]
    print(
        f"gates: {len(rows)}  emit: {len(kept)}  "
        f"dropped(peer-manager): {len(dropped_peer)}  "
        f"dropped(peer-manager-own): {len(dropped_own)}  "
        f"dropped(claimed): {len(dropped_claimed)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
