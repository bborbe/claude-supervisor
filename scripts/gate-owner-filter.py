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

  2. SESSION -> SPAWNER  the spawn ledger's row for that session, read from the
     attention store's `GET /api/1.0/session-ledger`. ⚠️ The field is
     `spawner_session_id` on the wire and was `parent_session` in the file
     record; `evaluate` accepts both, so a reader asking for one name does not
     silently get `None`.

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

## GOAL SCOPE — the durable input, and why the spawn edge alone cannot answer "mine"

Every hop above reads a LIVE store, and every one of them fails open. So the filter's answer
for one gate depended on the liveness of a manager that had nothing to do with it: measured
2026-10-29, goal manager `49b471f1` was handed **five gates raised by five sessions in four
other goals** in one afternoon (panes 269/`695c311f`, 260/`0eacc5e4`, 272/`2767ba96`,
278/`0b7d28a7`, 279/`51e78933`), each resolved by hand to a task whose `goals:` names another
goal. The same five read `peer-manager` when the spawn chain resolved, so the filter's verdict
for a gate flipped with the health of a manager that did not own it — and it could never name
the real owner, because `verdict()` has no notion of goals or tasks at all.

`session-scope.py` supplies that notion from the VAULT, which is durable: `--self` resolves to
a goal page's `claude_session_id`, the manager's scope is
that goal's `# Tasks` list, and every gated session resolves to the page it is stamped on. A
pane whose page is outside the scope is **EXCLUDED, not dropped**: the row stays in
`--explain --json` carrying `foreign-goal:<goal>` (or `owner-unknown` when the task declares no
goal), and only the bare feed omits it. That is the rule the manager runbook states — *a row
that disappears from a sweep is indistinguishable from a row that got fixed* — applied here:
an exclusion must render differently from a drop, and `counts_line()` reports it in its own
buckets.

⚠️ **THE SCOPE CHECK SITS ABOVE THE SPAWNER PATH, AND IT NAMES THE OWNER INSTEAD OF KEEPING THE
SPAWN EDGE'S REASON.** For a positively-foreign pane the two can disagree — the spawn edge may
say `peer-manager`, `dead-manager` or `unowned` for the same row, and which one it says is
exactly the fact that proved unstable across the five measured firings. The durable answer
wins: one reason, naming who owns the gate.

⚠️ **UNRESOLVED SCOPE IS NOT "NOTHING IS MINE".** A manager with no page of its own, an
unreadable vault list, a session stamped on two pages and a session with no page at all all
leave the filter exactly as it was before this input existed, and the reason is printed once
on stderr as a `SCOPE` line. The alternative — excluding on a failed read — is a fleet-wide
silence produced by a missing page, the direction every hop here fails open to avoid.

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

## ROLE — the operator's own workspace, and why it is excluded rather than dropped

`verdict()` had no notion of the spawn's declared `role`, so a `role: human` row — the
operator's own session, opened into the Direct window by `/supervisor:open --flagged`
(`commands/open.md` § Step 0.6: *"A `role: human` row is never opened as a WORKER — it is
opened into the Direct window instead"*) — was judged by the spawn edge alone. When the
manager was its spawner that read `own-worker`, the branch that exists for the panes a
manager EXISTS to see. Measured 2026-10-27 on Fleet Manager `ac24db61`: pane 2165
(`Check Prometheus Alerts - 2026-10-27`, `role: human`) fired the feed **three times**
across three consecutive 30-minute windows, each firing a full manager turn spent
re-establishing that the gate was the operator's own and relaying nothing.

A human row is answered in its own tab, so no manager ever acts on it, and the exclusion is
a third EXCLUDE reason beside the two the vault supplies. EXCLUDE and not DROP, for the
reason § GOAL SCOPE gives: the row must stay visible in `--explain` / `--json` naming what
it is, and only the bare feed omits it. `counts_line()` therefore reports it in its own
`excluded(role-human)` bucket — folded into `dropped(...)` it would read as a spawn-edge
drop, and printed nowhere at all it would be the silence `--explain` exists to prevent.

⚠️ **THE PREDICATE FAILS CLOSED, IN THE OPPOSITE DIRECTION FROM `is_manager`'s.** `is_human`
matches ONLY an explicit `role: "human"`; a record with no `role`, `role: null`, or no
record at all is NOT human. The two defaults point opposite ways because the failure
directions do: `is_manager` defaults to worker so a pre-field record cannot drop the whole
fleet's gates, and this defaults to not-human so an unrecorded session cannot be silently
excluded — which is that same fleet-wide silence reached from the other side.

⚠️ **It sits ABOVE the `own-worker` branch and above the scope block.** Above `own-worker`
because that branch returns EMIT for every pane this watcher spawned, which is exactly the
reported case; above the scope block so the reason always names the durable fact rather
than whichever goal page happens to claim the row. The match is deliberately NOT scoped to
this watcher's own spawns: `role` is a global ledger fact, so a human row spawned by another
manager is excluded too — a human row is never a manager's to route, whoever spawned it.

⚠️ A filter that silently matches nothing is indistinguishable from a quiet
fleet (a broken watcher is indistinguishable from a quiet one). So `--explain`
prints the reason for every verdict, and every run prints a
`gates: N  emit: N  dropped(peer-manager): N  dropped(peer-manager-own): N
dropped(claimed): N  excluded(foreign-goal): N  excluded(owner-unknown): N
excluded(role-human): N` line on
stderr — which is what makes an empty result legible as "nothing to filter" rather
than "the filter is broken". `excluded` is a separate family from `dropped` on
purpose: see § GOAL SCOPE.
"""

import argparse
import datetime
import glob
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time

STATE = os.path.expanduser("~/.claude/state/attention")
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
#: A THIRD outcome, and it exists so an exclusion can never be read as a drop. A row is
#: EXCLUDE when the gated session's own page belongs to another goal (`foreign-goal:<goal>`)
#: or to no goal at all (`owner-unknown`), or — since 2026-11-02 — when its own ledger
#: `role` is `human` (`role-human`), the operator's workspace: see § ROLE. The row stays in
#: `--explain` / `--json` with that owner named; only the bare feed omits it. Measured
#: 2026-10-29: a manager was handed five gates from five other goals in one afternoon, and
#: the filter had no vocabulary to say whose they were — `verdict()` has no notion of goals
#: or tasks, so its only answers were a liveness-derived drop or an emit.
EXCLUDE = "exclude"

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

# ⚠️ THE PRODUCER THIS FILTER POLLS IN `--interval` MODE, AND WHY THE LOOP LIVES
# HERE RATHER THAN IN A SHELL WRAPPER. The rule's arm is a `Monitor` over
# `who-needs-me.py --section needs-you | gate-owner-filter.py --feed --self <sid>`,
# and `--feed` reads stdin ONCE -- so the Monitor fires on the first read and the
# process exits, leaving the manager's push channel dead with no error. A shell
# `while true; do … | gate-owner-filter.py --feed …; sleep N; done` re-emits every
# kept pane on every poll, which turns a push channel into a 1/NHz poll of the
# manager's whole turn budget -- the cost this filter exists to remove.
#
# So the loop is here, and it is a CHANGE-DETECTOR: a pane wakes the manager when
# it ENTERS the kept set, never on a re-poll. That was unsafe while hop 3 could
# not see a peer manager's own pane -- peer churn reached the kept set and a naive
# diff fired on it, which is the measured cause of Defect 1's three foreign wakes
# (2026-10-01). Hop 3b drops those panes, so what remains in the kept set is the
# manager's own gates and its workers'.
#
# Resolved as a SIBLING of this file, not by name: the two scripts ship side by
# side, and a bare `who-needs-me.py` would resolve against whatever cwd the
# manager's pane happens to hold. `--producer` overrides it.
# ⚠️ `realpath`, not `abspath`: `abspath` normalises `..` but does NOT resolve a
# symlink, so a symlinked install would look for the producer beside the LINK
# rather than beside the script and raise `FileNotFoundError` at the first poll —
# a failure that only appears once the loop is running, which is the worst place
# for it. The intent stated above is "the two scripts ship side by side", and only
# `realpath` answers that for every way the script can be reached.
PRODUCER = (
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "who-needs-me.py")
    + " --section needs-you"
)

# ⚠️ THE ARM'S MEMORY, AND WHY IT IS A FILE RATHER THAN `previous = None`. The loop
# below is a change-detector WITHIN an arm; across arms it had no memory at all, so
# the first poll of every arm treated every held gate as newly entered. Measured
# 2026-10-12 on the Fleet Manager: panes 1065, 1151, 1175, 1094, 1193, 1191, 1196
# re-reported ~8 times, each a full manager turn over a ~600k-token context, for no
# state change. A `Monitor` is bounded at 30 min and re-armed, so the cost recurs
# every arm for as long as a gate stays open.
#
# Keyed by `--self`, so two managers never share one file: this is the watcher's own
# record of what IT has already surfaced, not a fleet-wide ledger.
FEED_STATE_DIR = os.environ.get("GATE_FEED_STATE_DIR") or os.path.expanduser(
    "~/.claude/state/gate-feed"
)

# ⚠️ THE BACKSTOP, AND WHY A BARE "EMIT ONCE" WOULD BE A SILENT STALL. Persisting
# the emit is exactly the shape that loses a message: the arm prints a pane, the
# `Monitor`'s notification is lost (the arm died between the write and the manager's
# turn), and a state file that records it as delivered never retries -- dedup plus
# event-only delivery, with nothing left to re-drive it. The re-arm used to be that
# backstop (`65 Runbooks/Manager Session.md`, both copies: *"the re-arm is the
# documented backstop for a wake that was lost"*), and persisting the state removes
# it. `manager-attention-watch.py`'s `AGED` rung is NOT a full replacement: it fires
# on the TRACKED set, while this filter's kept set is wider -- unowned panes, a dead
# spawner's panes, peer non-managers -- and a gate in that residual class would have
# no re-drive at all.
#
# So a still-open gate is re-emitted once its stored entry is older than this, which
# restores the backstop at a bounded cost: one turn per gate per hour instead of one
# per <=30-minute arm. `--reemit-after 0` disables the re-emit and accepts the
# residual class going unre-driven.
REEMIT_SECONDS = int(os.environ.get("GATE_FEED_REEMIT_SECONDS") or 3600)

# ⚠️ WHERE AN UNOWNED GATE GOES, AND WHY IT IS A DESTINATION RATHER THAN A SILENCE.
# `unowned` used to EMIT to every manager, on the rule that "unresolvable is never
# foreign" -- right in principle, because an unowned gate might be the reader's own.
# It is wrong for the producer class this row is named for: a `Monitor` producer
# (`sweep-gate`, `fleet-keeper`, `drive-escalate`) has NO spawner and NO session id,
# which is exactly the two inputs that produce `unowned` (:789, :816) -- so no manager
# can ever be its owner, and every manager inherits all of its traffic. Measured
# 2026-11-11 on one goal manager: >10 foreign wakes in ~2 h, each a full model round,
# which kept the REPL busy and starved the round cadence itself (no round in 3 h 14 min
# against a 15-minute cadence).
#
# So the class is ROUTED to exactly one destination: the live Fleet Manager. The
# fail-open limb is the whole safety property -- with no live Fleet Manager the gate is
# emitted to every manager exactly as before, because an unresolvable destination is not
# a licence to silence a gate. A fix that dropped the class outright would trade a flood
# for a black hole, and the gates nobody owns would be answered by nobody.
#
# The fleet tier manages no subject page, so `owner.py`'s page resolution cannot answer
# "who is the Fleet Manager" -- `scripts/fleet-register.py` writes that registration and
# this is the reader for it. Same env overrides as the writer, deliberately: a writer
# pointed at one directory while every reader reads another is a silent no-op.
WORKER_MANAGER_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_WORKER_MANAGER_DIR")
    or os.environ.get("WORKER_MANAGER_STATE_DIR")
    or "~/.claude/state/worker-manager"
)
FLEET_BRANCH = "fleet"
UNOWNED = "unowned"
#: The pane's spawner IS a manager, but that manager is no longer live. The old rule emitted
#: on the reasoning that "a dead manager cannot act on its own gate, so nothing duplicates
#: this wake" -- right about duplication, wrong about cost: it hands the gate to EVERY
#: manager instead of the one whose job is unowned work. Measured 2026-11-11 in SC1's
#: after-window: one orphaned worker pane (`088ab282`, spawned 2026-11-07, ledger still
#: `running`) was the only foreign wake left, and it arrived through this branch.
DEAD_MANAGER = "dead-manager"
#: A claim recorded by a manager that is no longer live. The third sibling of `unowned` and
#: `dead-manager`: the claimant is gone, so nobody can act on the pane. Not observed in the
#: SC1 windows measured on 2026-11-11, which is exactly why it is routed here rather than
#: left to fire on some later hour and fail the same criterion the other two failed.
CLAIM_DEAD = "claim-dead"
#: Appended to a routed reason, so `--explain` and the ledger keep WHICH class was routed
#: while the counts line can bucket them together. `unowned` -> `unowned-fleet`.
FLEET_SUFFIX = "-fleet"
#: The routed spelling of `unowned`, kept as its own name because it is the one the SC2
#: criterion quotes.
UNOWNED_FLEET = UNOWNED + FLEET_SUFFIX

#: One manager's per-decision ledger. Shares the per-session root the tracked-set arm
#: writes its `events.jsonl` into, so both arms of the same manager sit together.
EMISSIONS_ROOT = os.environ.get("GATE_EMISSIONS_DIR") or os.path.expanduser(
    "~/.claude/state/manager-attention-watch"
)
#: ⚠️ The ledger is append-only and lives for the life of the manager session, so it needs a
#: bound. Measured 2026-11-11 on a live manager: 276 records in 15.6 minutes, i.e. ~25 k
#: records/day (~6 MB/day) with no rotation at all — the sibling `gate-feed/<sid>.json` is
#: pruned by `emit_decisions`, this file is not. Rotation is by SIZE and keeps the newest
#: lines, so a window cut after a rotation still reads the recent past and only loses history
#: older than the bound.
EMISSIONS_MAX_BYTES = int(os.environ.get("GATE_EMISSIONS_MAX_BYTES") or 5_000_000)
EMISSIONS_KEEP_LINES = int(os.environ.get("GATE_EMISSIONS_KEEP_LINES") or 20000)


_SPAWN_LEDGER = None
_SESSION_SCOPE = None
_OWNER = None
#: Distinguishes "the caller did not supply a fleet manager" from "the caller supplied
#: `None`, meaning none resolved". Without it a test could not force the fail-open limb,
#: and `None` would silently mean "go and look it up".
_UNSET = object()


def _owner():
    """Import `owner.py` — the ONE ownership definition (`docs/session-tiers.md` § Keep-alive).

    Loaded lazily and cached, the same shape `_session_scope()` uses, so a run with `--no-scope`
    never pays for it. ⚠️ **This file must not carry a second definition.** Its § GOAL SCOPE row
    used to decide "a manager owns this" from the vault alone; the verdict itself now comes from
    `scripts/owner.py`, and this file only asks.
    """
    global _OWNER
    if _OWNER is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "owner.py")
        spec = importlib.util.spec_from_file_location("owner", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _OWNER = mod
    return _OWNER


def scope_input(self_id, vault_paths=None):
    """This watcher's vault scope, or `None` when it must not exclude anything.

    Two conditions, both required: the session must be an **owner** by
    `docs/session-tiers.md` § Keep-alive (resolved by `scripts/owner.py`), and the vault must
    resolve it a scope.

    ⚠️ **The ownership gate is what stops a stood-down manager's watcher from filtering.** A
    manager that ran `/supervisor:stop` keeps its session, so its attention watcher keeps
    running and keeps excluding other goals' gates as foreign — while the round that would make
    it a manager is exactly what stopped. Answering that with the one definition rather than a
    second one local to this file is the point of the resolver.

    ⚠️ **`cannot-tell` does NOT drop the scope.** Only a definite `not-owner` does; an
    unreadable probe leaves the filter exactly as it was, which is the direction every hop in
    this file fails.
    """
    if not self_id:
        return _session_scope().load(None, vault_paths)
    ownership = _owner().of_session(self_id)
    if ownership.verdict == _owner().NOT_OWNER:
        return _session_scope().Scope(
            {}, {}, set(), [],
            f"session {self_id[:8]} is stamped as a manager but is not an owner "
            f"({ownership.reason}: {ownership.describe()})",
        )
    return _session_scope().load(self_id, vault_paths, ownership)


def _session_scope():
    """Import `session-scope.py` (hyphenated filename -> importlib) — the ONE vault reader.

    Loaded lazily and cached, the same shape `_spawn_ledger()` uses, so a run that disables
    the input (`--no-scope`) or has no `--self` to resolve never imports it at all — and the
    ~7 s vault walk is skipped by `load()` itself in the second case.
    """
    global _SESSION_SCOPE
    if _SESSION_SCOPE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-scope.py")
        spec = importlib.util.spec_from_file_location("session_scope", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SESSION_SCOPE = mod
    return _SESSION_SCOPE


def _spawn_ledger():
    """Import `spawn-ledger.py` (hyphenated filename -> importlib) — the ONE ledger reader.

    ⚠️ **The ledger moved to the attention store's endpoint on 2026-10-14, and this file
    DELEGATES rather than reading the store itself.** `spawn-ledger.py` was extracted so
    `worker-sessions.py` and `session-liveness.py` could share one reader; this file was one
    of the five that kept its own, and writing a second endpoint client here would recreate
    exactly the drift the extraction removed. What this file does NOT adopt is that module's
    `None`-on-unreadable answer — see `load_ledger_checked` for why the two callers need
    different ones.
    """
    global _SPAWN_LEDGER
    if _SPAWN_LEDGER is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spawn-ledger.py")
        spec = importlib.util.spec_from_file_location("spawn_ledger", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SPAWN_LEDGER = mod
    return _SPAWN_LEDGER


def load_ledger_checked(endpoint=None):
    """Spawn records keyed by session id, or None when the store could not be read.

    ⚠️ **`{}` and `None` are different answers and the two callers need different ones.**
    `{}` is "read it, nobody is recorded"; `None` is "could not read it" — an unreachable
    controller, a non-200, or a body that is not a row array. `load_ledger` COLLAPSES them,
    because this module must fail OPEN (an unreadable ledger resolves every spawner as a
    manager, and keeping every gate is the safe direction there). `adopt-orphans.py` must
    NOT collapse them: it refuses on an unreadable store, because adopting on the strength
    of spawn edges it could not read would hand one manager's workers to another.
    """
    return _spawn_ledger().read_ledger(endpoint)


def load_ledger(endpoint=None):
    """Spawn records keyed by session id, or {} when the store is unreadable.

    An unreadable ledger is NOT an empty one -- it means every spawner resolves
    as a manager, which would drop the whole fleet. The caller must treat {}
    as unknown and fail open (keep everything) rather than as "no spawns".

    ⚠️ **The collapse is deliberate and is this function's whole job**, not an oversight:
    it is the fail-open rule stated above, applied. A caller that needs to tell the two
    apart calls `load_ledger_checked` instead.
    """
    return load_ledger_checked(endpoint) or {}


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


def is_human(session_id, ledger):
    """The gated session is the OPERATOR'S own workspace, not a delegate.

    `role: human` is what `spawn_agent` writes for a row opened into the Direct window
    (`commands/open.md` § Step 0.6), so its gate is answered in that tab and no manager ever
    acts on it. Measured 2026-10-27 on Fleet Manager `ac24db61`: pane 2165
    (`Check Prometheus Alerts - 2026-10-27`, `role: human`) fired the feed three times across
    three consecutive 30-minute windows — see the module header § ROLE.

    ⚠️ **ONLY an explicit `human` counts, and the default is the OPPOSITE of `is_manager`'s.**
    A record with no `role`, `role: null`, or no record at all is NOT human. The two defaults
    point opposite ways because the failure directions do: `is_manager` defaults to worker so
    a pre-field record cannot drop the whole fleet's gates, and this defaults to not-human so
    an unrecorded session cannot be silently excluded — that same fleet-wide silence, reached
    from the other side. `role` is a positive assertion here, never an absence.
    """
    if not session_id:
        return False
    record = ledger.get(session_id) or {}
    return record.get("role") == "human"


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
            # Same guard, same reason as `load_ledger` above: valid-but-non-object JSON
            # parses and then has no `.get`. A registry entry is Claude Code's file, not
            # ours, so a shape this reader did not anticipate is a live possibility rather
            # than a hypothetical -- and crashing here takes the liveness read down with it.
            if not isinstance(rec, dict):
                continue
        except (OSError, ValueError):
            continue
        sid = rec.get("sessionId") or rec.get("session_id")
        if sid:
            out.add(sid)
    return out


def fleet_manager(live, directory=None):
    """The live Fleet Manager's session id, or None when none resolves.

    Reads the registration `scripts/fleet-register.py` writes. Newest `resolved_at` wins,
    and a registration whose session is not live is SKIPPED rather than used: routing the
    unowned class to a dead session would be a black hole with extra steps — the gate would
    leave every other manager's feed and arrive nowhere.

    `live is None` (the registry could not be read) resolves nothing, which sends the
    caller down the fail-open limb. That is the same "unknown is not empty" rule every
    other ownership input here takes.
    """
    if live is None:
        return None
    best, best_at = None, ""
    for path in glob.glob(os.path.join(directory or WORKER_MANAGER_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                rec = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("branch") != FLEET_BRANCH:
            continue
        sid = os.path.basename(path)[: -len(".json")]
        if sid not in live:
            continue
        at = str(rec.get("resolved_at") or "")
        if at >= best_at:
            best, best_at = sid, at
    return best


def route_to_fleet(self_id, fleet, reason):
    """`(verdict, reason)` for a gate no manager can act on, so exactly one should see it.

    Every class the caller routes here shares one property: the reader cannot answer it and
    no other manager owns it, so leaving it in every feed is cost without work. The reason
    is preserved with `FLEET_SUFFIX` appended, so a routed row still says WHICH class it was
    while the counts line buckets them together.

    Four answers, and every one of them is deliberate:

      * no `--self` — a diagnostic run, not a manager's arm: emit, exactly as before.
        There is no reader to route FOR, and a `--pane` probe that printed nothing would
        read as "this pane is fine".
      * no live Fleet Manager — emit. The fail-open limb: an unresolvable destination is
        not a licence to silence a gate.
      * this watcher IS the Fleet Manager — emit. It is the destination.
      * anything else — drop, so the row says where it went.
    """
    if not self_id or not fleet:
        return EMIT, reason
    routed = reason + FLEET_SUFFIX
    if self_id == fleet:
        return EMIT, routed
    return DROP, routed


def route_unowned(self_id, fleet):
    """The `unowned` class, routed. Kept as its own name because SC2 quotes it."""
    return route_to_fleet(self_id, fleet, UNOWNED)


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
                    # Third instance of the same class: a line that is valid JSON but not an
                    # object (`null`, a bare number, an array) parses and then has no `.get`.
                    # The log is append-only and shared, so one such line would otherwise take
                    # the whole pane->session hop down and fail the filter open for every gate.
                    if not isinstance(rec, dict):
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


def newest_item_for_pane(pane, items):
    """The newest event-log record for `pane`, or None.

    Split out of `session_for_pane` so the fingerprint reads the SAME record the
    session hop resolves. Taking the session from one record and the gate's text
    from another would let a pane's identity and its text disagree — and the
    fingerprint is exactly the thing that must not do that.
    """
    hits = [r for r in items if str(r.get("pane")) == str(pane)]
    if not hits:
        return None
    hits.sort(key=lambda r: r.get("ts") or 0)
    return hits[-1]


def session_for_pane(pane, items):
    """The session that raised the gate on `pane`, or None.

    Pane ids are reused, so the newest item for the pane wins — regardless of
    whether that item is still open, for the reason `log_items` records.
    Absent stays absent: a pane with no item at all returns None and the caller
    treats it as unowned, which is a real answer rather than a failure.
    """
    record = newest_item_for_pane(pane, items)
    if record is None:
        return None
    return str(record.get("session_id") or "") or None


def gate_text(record):
    """The gate's own words, read from the record the pane resolves to.

    `detail` is the payload the feed's own rows render from, so it is the gate's
    identity in the terms `who-needs-me.py` publishes. The fallbacks exist because
    a record need not carry every field — and a gate whose text cannot be read
    still has an identity (its pane), just a coarser one.
    """
    if not record:
        return ""
    detail = record.get("detail")
    if detail:
        return str(detail)
    questions = record.get("questions")
    if questions:
        return " ".join(str(q) for q in questions)
    return str(record.get("kind") or "")


def normalise(text):
    """Collapse whitespace so a re-rendered gate keeps one identity.

    The same rule `notify-gate.py` applies to its own gate key, and for the same
    reason: a gate re-rendered with different wrapping is the SAME gate, and a
    fingerprint taken over the raw text would read it as new and re-wake the
    manager for a gate it has already been told about.
    """
    return " ".join(str(text).split())


def gate_fingerprint(pane, items):
    """A stable identity for the gate on `pane`: its pane id plus its own text.

    ⚠️ Seeded on the state that actually CHANGES, never on a timestamp or a poll
    counter. A clock-derived fingerprint differs on every poll and re-emits
    everything — the defect this change removes, reintroduced one layer down. A
    counter-derived one re-emits everything on every restart. Pane plus normalised
    text changes exactly when the gate does: a new question on the same pane is a
    new gate, and a re-render of the same question is not.
    """
    # A headless key IS the store item id, so it already names exactly one gate.
    # Its text comes from a per-poll store read that may fail, and mixing it in
    # would flip the fingerprint on every flap and re-wake the manager each time.
    if is_headless_key(pane):
        return hashlib.sha256(str(pane).encode("utf-8")).hexdigest()[:16]
    record = newest_item_for_pane(pane, items)
    payload = f"{pane}\x1f{normalise(gate_text(record))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def feed_state_path(self_id, state_dir=None):
    """`<state_dir>/<self-session-id>.json` — one arm's memory, per manager."""
    return os.path.join(state_dir or FEED_STATE_DIR, f"{self_id}.json")


def load_feed_state(path):
    """The stored `{pane: {"fingerprint", "emitted_at"}}`, or `{}`.

    ⚠️ An ABSENT or UNREADABLE store reads as FIRST RUN — every kept pane emits —
    and never as "nothing changed". The two are indistinguishable from the file
    alone, and choosing the quiet one would mean a store that was never written, or
    was lost, silently suppresses every gate it should have carried with no way
    back. The fail-open direction here is a wake rather than a silence, the same
    asymmetry `load_claims` and the spawner hops take.

    ⚠️ This is deliberately the OPPOSITE of `drive-ledger.py`, which REFUSES a
    corrupt ledger rather than reading it as empty — and the difference is the
    failure direction, not a disagreement. There, an empty read re-emits
    everything, so refusing is the safe side. Here an empty read does exactly the
    same thing, and refusing would only turn a recoverable wake into a dead arm.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError):
        return {}
    gates = data.get("gates") if isinstance(data, dict) else None
    return gates if isinstance(gates, dict) else {}


def save_feed_state(path, self_id, gates):
    """Persist atomically (makedirs -> pid-suffixed tmp -> `os.replace`).

    The same discipline `manager-predispatch.py` documents: a plain
    `open(path, "w")` truncates first, so a crash or a serialisation error
    part-way through leaves a truncated file where a valid one stood, and the next
    arm then reads it as first run. `os.replace` is atomic within a filesystem, so
    a reader sees either the old file or the new one.

    ⚠️ The pid suffix is not decoration. Every manager shares one
    `FEED_STATE_DIR`, so a bare `<path>.tmp` would let one arm's half-written file
    be renamed over another's good one — a corruption the keying by `--self`
    otherwise prevents.

    Returns the `OSError` on failure and `None` on success, so the caller can
    report a store it cannot write rather than leaving the arm looking healthy
    while the change silently does nothing.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {"self": self_id, "gates": gates}, handle, indent=2, sort_keys=True
            )
        os.replace(tmp, path)
        return None
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return exc


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
        # Unresolved, NOT foreign: the routing decision needs this watcher's own id and the
        # live Fleet Manager, neither of which is an input here, so `verdict()` applies it
        # via `route_unowned()`. Returning a verdict here would either duplicate that rule
        # or let the two drift.
        return EMIT, UNOWNED
    if not is_manager(spawner, ledger):
        return EMIT, "worker-spawner"
    if live is None:
        return EMIT, "liveness-unknown"
    if spawner not in live:
        # Routed by `verdict()` via `route_to_fleet`, for the same reason `unowned` is: the
        # spawner is gone, so nobody can act on this gate and every manager would inherit it.
        return EMIT, DEAD_MANAGER
    return DROP, "peer-manager"


def verdict(session_id, spawner, ledger, live, self_id, claims=None, scope=None, fleet=None):
    """(emit|drop|exclude, reason) for one gated session.

    The keep-set is wide on purpose: drop ONLY on a live peer manager -- reached
    by the spawn edge (its spawner), by the gated session being one itself, or by
    a claim a live peer manager holds. EXCLUDE is the third answer and it has two
    sources: the vault (the gated session's own page belongs to another goal) and
    the ledger's `role: human` (the operator's own workspace, which no manager
    routes — see § ROLE).

    ⚠️ **The scope check sits ABOVE the spawner path, deliberately.** For a
    positively-foreign pane the spawn edge may say `peer-manager`, `dead-manager`
    or `unowned` for the same row -- and which one it says is the fact that proved
    unstable across the five measured firings. The durable answer wins, and it is
    the only one that names the real owner.
    """
    if not session_id:
        return route_unowned(self_id, fleet)
    if self_id and session_id == self_id:
        return EMIT, "self"

    # THE OPERATOR'S OWN WORKSPACE. A `role: human` row is opened into the Direct window
    # (`commands/open.md` § Step 0.6), so its gate is answered there and a manager never acts
    # on it -- yet the spawn edge emits it, and when this watcher IS the spawner it emits
    # `own-worker`, the branch that exists for the panes the manager EXISTS to see. Placed
    # above that branch for exactly that reason, and above the scope block so the reason
    # always names the durable fact rather than whichever goal page happens to claim the row.
    # `is_human` fails closed -- only an explicit `human` matches -- so a record with no
    # `role`, a `null` role or no record at all keeps every existing verdict. See § ROLE.
    if is_human(session_id, ledger):
        return EXCLUDE, "role-human"

    if self_id and spawner == self_id:
        # Above both the spawner path and the claim branch, deliberately: a
        # manager's own workers are the panes it EXISTS to see, so neither a
        # peer spawner nor a peer's claim may silence them. The PREDECESSOR
        # warning above records a measured incident of a manager silently losing
        # its own workers; this keeps that from returning through either door.
        return EMIT, "own-worker"

    # A pane this watcher ADOPTED is one it EXISTS to see, and the claim block below
    # promises exactly that with its `own-claim` arm. Computed HERE, above the scope
    # block, because that block is the first thing that can silence a pane: a foreign
    # scope page plus a live claim is precisely the row the adopting manager undertook
    # to route, so the durable answer must not outrank the claim. Measured contract, not
    # a hypothetical: `ownership-claim.py` records the adoption precisely so the adopting
    # manager keeps seeing a spawnerless pane, and hop 3b sits below this for the same
    # reason the claim block does.
    adopted_by_self = bool(claims and self_id and claims.get(session_id) == self_id)

    # THE DURABLE INPUT. `scope` is `None` when the vault could not be read, when `--no-scope`
    # was passed, or when this manager has no page of its own -- and every one of those leaves
    # the filter exactly as it was. It reaches only panes the two branches above did not claim:
    # never one this watcher spawned (`own-worker`) and never its own session (`self`). For a
    # positively-foreign pane it SUPERSEDES the spawn edge's verdict, reason included -- the
    # pane leaves the feed either way, but it leaves named instead of mislabelled.
    if scope is not None and not adopted_by_self:
        excluded, why = scope.exclusion(session_id, self_id)
        if excluded:
            return EXCLUDE, why

    call, reason = spawner_verdict(spawner, ledger, live)
    if call == DROP:
        return call, reason
    # ⚠️ The routed limb is applied LAST, after hop 3b and after the claim block, and the
    # placement is load-bearing rather than tidy. An earlier cut returned the routed drop
    # HERE, and that silently broke the adoption contract: a goal manager that adopted a
    # spawnerless pane (`own-claim`) lost the gate the moment a Fleet Manager was live,
    # because the routed drop outranked the claim. That is the same hazard the
    # `adopted_by_self` guards exist to prevent, reached through a new door. The file's own
    # asymmetry is "a claim may only ADD a drop, never remove one" -- so a new drop must
    # never be inserted AHEAD of the claim either, or the two rules contradict.
    #
    # Sitting here, every prior hop has already had its say: `peer-manager`, `own-worker`,
    # `self`, the scope exclusion, `peer-manager-own`, `claimed` and `own-claim` all return
    # before this line. What reaches it is a pane nobody owns -- which is exactly the class
    # to route.

    # HOP 3b -- the GATED SESSION is itself the manager. Hop 3 reads the spawn
    # edge, and a manager's own gate has none: an operator-started manager was
    # never spawned, so hop 2 resolves no spawner and the spawner path just
    # emitted `unowned`. Consulted only where that path emits, exactly as the
    # claim below is, because like a claim it can only ADD a drop -- never turn
    # a resolved `peer-manager` drop back into an emit. Same predicate as hop 3,
    # applied one hop differently; the module header carries the measured cases
    # and why this keys on ledger membership rather than on an empty spawner.
    # A pane this watcher ADOPTED is one it EXISTS to see, and the claim block
    # below promises exactly that with its `own-claim` arm. `adopted_by_self` is
    # computed above, at the scope block -- the first place that could silence
    # such a pane -- so hop 3b reuses it rather than re-deriving the predicate.
    # Without the guard hop 3b would drop an adopted pane before the claim was
    # ever read, silencing the gate for the one manager that undertook to route
    # it, and the peer's own watcher exits at the `self` check, so nobody is left
    # to report it.
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
                # THE THIRD SIBLING of `unowned` and `dead-manager`, and the same defect:
                # the manager that CLAIMED this pane is gone, so nobody can act on it and
                # the old rule handed it to every manager. Routed like its siblings.
                return route_to_fleet(self_id, fleet, CLAIM_DEAD)
            return DROP, "claimed"
    # Nothing above resolved this pane, so the routed limb decides — LAST, for the reason
    # in the comment above hop 3b.
    if reason in (UNOWNED, DEAD_MANAGER):
        return route_to_fleet(self_id, fleet, reason)
    return call, reason


def evaluate(pane, ledger, live, items, self_id, claims=None, scope=None, fleet=None):
    """(verdict, reason, session_id, spawner) for one pane."""
    session_id = session_for_pane(pane, items)
    # ⚠️ **Two spellings, one field, and only one of them is on the wire.** The file record
    # called the spawn edge `parent_session`; the endpoint's row calls it
    # `spawner_session_id`. The read accepts BOTH rather than the wire name alone, matching
    # `server/registry-rehydrate.mjs`'s `toAgentRecord`, which made the same move and reads
    # `record.parent_session ?? record.spawner_session_id`. ⚠️ Asking for only one name gets
    # `None` rather than an error, and `None` here is not a missing field — it is the
    # "no spawner" answer that decides whether a gate is emitted at all, so a wrong name
    # would silently drop the whole fleet's gates.
    record = ledger.get(session_id) or {}
    spawner = (record.get("spawner_session_id") or record.get("parent_session")) if session_id else None
    call, reason = verdict(session_id, spawner, ledger, live, self_id, claims, scope, fleet)
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

    ⚠️ **A pane-less headless row is a gate too, keyed on its store item id.**
    `who-needs-me.py` renders a headless worker's answerable gate `[ hst]` — a
    headless worker owns no pane by construction — and the digit-only match used
    to skip it, so the filter read `gates: 0` over a feed carrying the gate and
    the armed watcher never woke. Measured 2026-11-03 01:06: 11 of 13 live gates
    were `[ hst]`. The row's own identity is the store item id its `answer:`
    continuation line names (`hst` is rendered ONLY when that line exists), so
    the key is `hst:<store item id>` — never a pane, which this row cannot have.
    `headless_items` resolves the session behind that key from the store.
    """
    import re

    lines = list(stream)
    out, marks = [], {}
    for index, line in enumerate(lines):
        match = re.match(r"\s*\[\s*(\d+)\s*\]", line)
        if match:
            pane = match.group(1)
        elif re.match(r"\s*\[\s*hst\s*\]", line):
            pane = headless_key(lines, index)
            if pane is None:
                continue
        else:
            continue
        if pane not in marks:
            marks[pane] = REPLAY_MARK in line
            out.append(pane)
    return [(pane, marks[pane]) for pane in out]


HEADLESS_PREFIX = "hst:"


def headless_key(lines, index):
    """`hst:<store item id>` for the `[ hst]` row at `lines[index]`, or None.

    Reads the row's continuation lines only — up to the next `[...]` row marker —
    so an `answer:` line can never be borrowed from a neighbouring row.
    """
    import re

    for line in lines[index + 1:]:
        if re.match(r"\s*\[", line):
            return None
        match = re.search(r"attention-answer\.py answer (\S+)", line)
        if match:
            return HEADLESS_PREFIX + match.group(1)
    return None


def is_headless_key(pane):
    return str(pane).startswith(HEADLESS_PREFIX)


# What one store read says about one `hst:<id>` key. Three values, because a two-valued
# read ("a record, or none") collapsed two different facts into one.
STORE_OPEN = "open"          # the item is on the store's open list
STORE_NOT_OPEN = "not-open"  # a GOOD read, and the item is not on it: closed, answered or pruned
STORE_UNREAD = "unread"      # the read itself failed — nothing is known about the item

# The two fixed wake-line tokens a headless row can carry instead of a session id. They
# differ in BOTH halves (`session=` vs `store=`, `unattributed` vs `unreachable`), so
# `grep -F` separates them and neither is a substring of the other.
UNATTRIBUTED_TOKEN = "session=unattributed"
STORE_FAILED_TOKEN = "store=unreachable"


def owner_of(lines, index):
    """The session id a pane-less row declares on its `owner:` line, or None.

    A row with no pane cannot be joined to its session through the event log —
    `session_for_pane` has no pane to match. `who-needs-me.py` knows the owner (it
    read it from the store, or from the log on the fallback branch), so it renders
    it on the row's continuation block; this reads it back.

    The scan stops at the next `[...]` row marker, so an `owner:` line can never be
    borrowed from a neighbouring row — the same guard `headless_key` applies.

    ⚠️ **The alternative is a SECOND attention-store read.** `headless_items`
    re-reads the store to recover exactly this mapping, and a transient 3 s
    timeout there blanked every row's feed verdict for a whole manager round
    (measured 2026-11-08 05:06:29Z: `gated=0 · clear=0 · unread=11` for 11 rows,
    while the feed itself was answering). Carrying the owner on the row removes
    that read and the failure with it.
    """
    import re

    for line in lines[index + 1:]:
        if re.match(r"\s*\[", line):
            return None
        match = re.search(r"^\s*owner:\s*(\S+)\s*$", line)
        if match:
            return match.group(1)
    return None


def headless_items_checked(keys, endpoint=None):
    """`(records, error)` — `headless_items`, with a store failure named.

    `headless_items` returns `[]` both for an unreachable store and for a read
    that matched nothing, so its caller cannot tell a failed read from a real
    absence. That ambiguity is fine on the emit path, which fails OPEN by design
    (silence must not reintroduce a dropped gate), and it is fatal on the readings
    path, which must never render a failed read as a verdict about a row.

    `error` is None on a read that ran, and a message naming the probe otherwise.
    """
    wanted = [str(k) for k in keys if is_headless_key(k)]
    if not wanted:
        return [], None
    import urllib.request

    base = (endpoint or os.environ.get("ATTENTION_STORE_URL") or "http://localhost:18080").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/1.0/attention", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8") or "[]")
    except (OSError, ValueError) as exc:
        return [], f"attention store read failed: {type(exc).__name__}: {exc}"
    if not isinstance(data, list):
        return [], "attention store returned a non-list body"
    ids = {key[len(HEADLESS_PREFIX):] for key in wanted}
    out, now = [], time.time()
    for item in data:
        if not isinstance(item, dict) or item.get("item_id") not in ids:
            continue
        out.append(
            {
                "pane": HEADLESS_PREFIX + item["item_id"],
                "session_id": item.get("producer_id") or "",
                "detail": item.get("payload") or "",
                "ts": now,
            }
        )
    return out, None


def headless_items(keys, endpoint=None):
    """Synthetic event-log records for the `hst:<id>` keys, read from the store.

    Each record carries `pane: hst:<id>` so the pane-keyed hops
    (`session_for_pane`, `gate_fingerprint`) resolve it unchanged: `session_id`
    from the store's `producer_id`, the text from its `payload`.

    ⚠️ **An unreachable store fails OPEN** — no record, so the session reads
    `unowned` and the gate EMITS. Silence is the failure this route removes; a
    store read that failed must not reintroduce it. Callers that must tell a
    failed read from an empty one use `headless_items_checked` instead — or
    `store_states`, which is that distinction keyed per row.
    """
    return headless_items_checked(keys, endpoint)[0]


def store_states(keys, records, error):
    """Per-key `STORE_OPEN` / `STORE_NOT_OPEN` / `STORE_UNREAD`, from ONE checked read.

    ⚠️ **`GET /api/1.0/attention` returns OPEN items only** (`attention-card-lookup.py`),
    so a key absent from a successful read is a card that is no longer open — closed,
    answered or pruned — and is not a gate. Measured 2026-11-05 to 11-07: 3 of 3, then
    7 of 10, then 18 of 18 wake rows on one goal manager's arm were dead or foreign, and
    the arm was stopped rather than re-armed.

    ⚠️ **A failed read is `STORE_UNREAD`, never `STORE_NOT_OPEN`.** Reading it as "every
    card closed" would drop every headless gate — the fleet-wide silence every hop in
    this file fails open to avoid. `error` is `headless_items_checked`'s second value.
    """
    wanted = [str(k) for k in keys if is_headless_key(k)]
    if error:
        return {key: STORE_UNREAD for key in wanted}
    open_panes = {r["pane"] for r in records}
    return {
        key: STORE_OPEN if key in open_panes else STORE_NOT_OPEN for key in wanted
    }


def panes_from_feed(stream):
    """Pane ids alone — `feed_rows` projected to its first element."""
    return [pane for pane, _ in feed_rows(stream)]


def refusal_exit(refusal):
    """Print the refusal and the do-not-arm warning; the caller exits non-zero.

    ⚠️ **No `gates:` line on this path, deliberately.** A marker printed beside
    the false claim is not a fix — `gates: 0` still reads as a measurement, and a
    manager parsing it cannot tell it from a quiet fleet. The line that lies is
    the line that must not be printed.
    """
    sys.stderr.write(f"gate-owner-filter: upstream refused — {refusal}\n")
    sys.stderr.write(
        "gate-owner-filter: refusing to report a gate count from a feed that "
        "was never read. This exit is non-zero on purpose — do not arm a "
        "watcher on this pipeline's output.\n"
    )
    return 1


def producer_exit(producer, returncode):
    """A producer that FAILED is not a quiet fleet; say so and exit non-zero.

    ⚠️ **This is the `feed_refusal` distinction one layer up, and it was missing.**
    `feed_refusal` separates "read, and empty" from "never read" by a marker on
    stdout — but a producer that dies without printing anything (a crash, a
    missing interpreter, a killed child) leaves stdout empty *and* writes no
    marker, so it landed in the first case and was certified as a healthy empty
    queue by `gates: 0` on every poll, forever. That is precisely the false
    reading `refusal_exit` exists to prevent, re-introduced above it: a broken
    transport made byte-identical to a quiet one. The `LIVENESS` line makes it
    *visible* to a human reading the log, but it does not distinguish the two —
    only the exit status does, and until now nothing consulted it.

    ⚠️ **Any non-zero status ends the loop, even when rows were printed.** A
    producer that emits a partial feed and then fails has produced a feed whose
    completeness is unknown, and acting on a partial gate list is how a manager
    misses the one gate it was armed for. The arm is cheap to re-establish and
    the failure is cheap to see; a silently-partial feed is neither.
    """
    sys.stderr.write(
        f"gate-owner-filter: producer exited {returncode} — {producer}\n"
    )
    sys.stderr.write(
        "gate-owner-filter: refusing to report a gate count from a producer that "
        "failed. A failed producer and a quiet fleet leave the same rows on the "
        "wire; this exit is non-zero on purpose — re-arm once the producer "
        "answers.\n"
    )
    return 1


NOT_OPEN_REASON = "not-open"


def evaluate_panes(panes, ledger, live, items, self_id, claims, scope=None, states=None,
                   fleet=_UNSET):
    """One row per `(pane, replayed)` pair.

    `states` is `store_states()`'s answer. A headless key the store read
    successfully and did NOT find open is dropped as `not-open` before any ownership hop:
    a closed or answered card is not a gate, whoever owned it. A key the store could not
    read keeps its ownership verdict and carries `store: unread`, so `render` can say so —
    that is the fail-open branch, and it must stay visibly distinct from a live gate.

    `fleet` is resolved ONCE here rather than per row: it is a directory glob plus a
    liveness intersection, and a poll can carry dozens of panes. Passing `None` explicitly
    means "no Fleet Manager resolved" and takes the fail-open limb; omitting it means
    "resolve it".
    """
    states = states or {}
    if fleet is _UNSET:
        fleet = fleet_manager(live)
    rows = []
    for pane, replayed in panes:
        call, reason, session_id, spawner = evaluate(
            pane, ledger, live, items, self_id, claims, scope, fleet
        )
        state = states.get(pane)
        if state == STORE_NOT_OPEN:
            call, reason = DROP, NOT_OPEN_REASON
        rows.append(
            {
                "pane": pane,
                "replayed": replayed,
                "verdict": call,
                "reason": reason,
                "session_id": session_id,
                "spawner": spawner,
                "store": state,
            }
        )
    return rows


def headless_suffix(row):
    """The wake-line tail for one row: exactly one of three tokens on a headless key.

    | tail                      | means                                 | manager's action           |
    |---------------------------|---------------------------------------|----------------------------|
    | `session=<id>`            | open gate, owner resolved             | route to that session      |
    | `session=unattributed`    | open gate, no `producer_id` on record | candidate to confirm; never foreign |
    | `store=unreachable`       | the store read failed (fail-open)     | the store is down — check the store |

    A closed or answered card never reaches here: `evaluate_panes` dropped it as `not-open`.

    ⚠️ **`session=unresolved` is retired, and it was the defect.** It covered both the
    failed read and a card the read found closed, so the line could not say which.
    """
    if not is_headless_key(row["pane"]):
        return ""
    if row.get("store") == STORE_UNREAD:
        return f"  {STORE_FAILED_TOKEN}"
    if row.get("session_id"):
        return f"  session={row['session_id']}"
    return f"  {UNATTRIBUTED_TOKEN}"


def render(rows, args, new_panes=None):
    """Print one poll's rows in the shape the caller asked for.

    `new_panes` is poll mode's change-detector: the panes that ENTERED the kept
    set on this poll. `None` is one-shot mode, where every kept pane is news by
    definition. It narrows only the watcher's own bare-pane shape (`--feed`
    without `--explain`/`--json`) — a diagnostic run prints every row, because
    watching the verdicts is what it is for.
    """
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
        return

    if args.explain:
        # ⚠️ The reason column is `16` wide by default and WIDENED to the longest reason on
        # this run. `:16` was exact while `peer-manager-own` and `liveness-unknown` were the
        # longest reasons (measured 2026-10-04: `pane` at column 23 on every row, both
        # included) — but a scope exclusion's reason NAMES THE OWNER
        # (`foreign-goal:Every Recurring Task Is Structurally Valid and Linked to a Runbook`),
        # which is unbounded, and a fixed field would shift every later column by its overrun
        # in the one diagnostic whose whole job is legibility.
        # ⚠️ Both widths keep `4` / `16` as their FLOOR, so a run with no exclusion is
        # byte-identical to the render that shipped — the no-regression requirement. `exclude`
        # is 7 characters and would otherwise overrun the verdict field by three on exactly
        # the rows a reader is trying to line up against the others.
        vwidth = max([4] + [len(r["verdict"]) for r in rows])
        width = max([16] + [len(r["reason"]) for r in rows])
        for r in rows:
            print(
                f"{r['verdict']:{vwidth}} {r['reason']:{width}} pane {r['pane']:>5}  "
                f"session={str(r['session_id'])[:8]:8} spawner={str(r['spawner'])[:8]:8}"
            )
        return

    if args.feed:
        # The watcher's own shape: the panes worth a turn, nothing else.
        # ⚠️ A replayed row carries the feed's own mark, because this line is
        # the watcher's entire input: it prints a pane id and nothing else, so
        # a replayed row was byte-identical to a live one and the watcher woke
        # on gates already answered. A LIVE row renders exactly as it always
        # did — the no-regression requirement is that a responsive store's
        # output is unchanged, and a mark that was always present would fail it
        # (the same rule `who-needs-me.py`'s own render states).
        for r in rows:
            if r["verdict"] != EMIT:
                continue
            if new_panes is not None and r["pane"] not in new_panes:
                continue
            # A headless key names the store item but no pane to jump to, so the
            # wake line also names the session it belongs to — the one handle the
            # manager can act on. A pane row renders exactly as before.
            print(f"{r['pane']}{headless_suffix(r)}{'  ' + REPLAY_MARK if r['replayed'] else ''}")
        return

    for r in rows:
        print(r["verdict"])


def counts_line(rows):
    """The stderr summary, split by reason.

    Loud on the count, so an empty drop set reads as "nothing to filter" rather
    than as a filter that never matched. Split by reason, not lumped: `dropped`
    covers three rules, and a run driven entirely by claims — or by hop 3b —
    would otherwise report panes as peer-manager drops that are not: the exact
    misread this line exists to prevent.

    ⚠️ **`excluded(...)` is its own family, never folded into `dropped(...)`.**
    A drop says "a live manager owns this"; an exclusion says "the vault says
    another goal owns this" — a different input and a different fact. Folding them
    would make a scope exclusion read as a spawn-edge drop, which is the exact
    misattribution the exclusion exists to end. The two buckets are split by
    owner-named vs owner-unknown for the same reason the dropped ones are split by
    rule, and a third — `excluded(role-human)` — is the ledger's `role`, not the
    vault: the operator's own workspace, which no manager routes (see § ROLE). It
    gets its own bucket for the same reason: a row excluded because the operator
    owns it is a different fact from one excluded because another goal does.
    """
    kept = [r for r in rows if r["verdict"] == EMIT]
    dropped = [r for r in rows if r["verdict"] == DROP]
    dropped_peer = [r for r in dropped if r["reason"] == "peer-manager"]
    dropped_own = [r for r in dropped if r["reason"] == "peer-manager-own"]
    dropped_claimed = [r for r in dropped if r["reason"] == "claimed"]
    dropped_not_open = [r for r in dropped if r["reason"] == NOT_OPEN_REASON]
    # ONE bucket for every routed class, because the bucket answers "how much left this
    # feed for the Fleet Manager", not "which hop found it". The specific reason stays on
    # the row (`--explain`, `--json`, and the ledger's `reason`), so nothing is lost.
    dropped_fleet = [r for r in dropped if r["reason"].endswith(FLEET_SUFFIX)]
    excluded = [r for r in rows if r["verdict"] == EXCLUDE]
    excluded_goal = [r for r in excluded if r["reason"].startswith("foreign-goal:")]
    excluded_unknown = [r for r in excluded if r["reason"] == "owner-unknown"]
    excluded_human = [r for r in excluded if r["reason"] == "role-human"]
    return (
        f"gates: {len(rows)}  emit: {len(kept)}  "
        f"dropped(peer-manager): {len(dropped_peer)}  "
        f"dropped(peer-manager-own): {len(dropped_own)}  "
        f"dropped(claimed): {len(dropped_claimed)}  "
        f"dropped(not-open): {len(dropped_not_open)}  "
        f"dropped(fleet-routed): {len(dropped_fleet)}  "
        f"excluded(foreign-goal): {len(excluded_goal)}  "
        f"excluded(owner-unknown): {len(excluded_unknown)}  "
        f"excluded(role-human): {len(excluded_human)}"
    )


def one_shot(panes, args, ledger, live, items, claims, missing=(), scope=None):
    """Read once, print, exit — the original contract, unchanged.

    `missing` names the panes `--pane` asked for that the feed did not carry. Under
    `--explain` each is named, because the alternative is indistinguishable from a
    filter that silently matched nothing: a `--pane 1832` run printing no row and
    `gates: 0` reads as "this pane is fine" whether or not the pane was ever in the
    feed. Naming it separates "the feed does not carry it" from "the feed was never
    consulted" — the defect this flag carried.
    """
    keys = [pane for pane, _ in panes]
    records, error = headless_items_checked(keys, args.endpoint)
    rows = evaluate_panes(panes, ledger, live, items + records, args.self_id, claims, scope,
                          store_states(keys, records, error))
    render(rows, args)
    if args.explain:
        for pane in missing:
            print(f"no feed gate for pane {pane}")
    ledger_error = log_emissions(rows, items + records, scope, args.self_id)
    if ledger_error is not None:
        print(f"STATE  emissions ledger unwritable: {ledger_error}", file=sys.stderr)
    print(counts_line(rows), file=sys.stderr)
    return 0


def emit_decisions(kept, stored, items, now, reemit_after):
    """-> (the panes to emit now, the state to persist).

    Three ways a kept pane is news, and the third is the backstop:

      * no stored entry — it entered the kept set this arm, or the store is new;
      * a DIFFERENT fingerprint — the gate's own text changed, so this is a new
        gate on a reused pane rather than the one already surfaced;
      * the entry is older than `reemit_after` — the wake may have been lost, and
        this is the only thing that re-drives it.

    ⚠️ Entries for panes NOT in the kept set are dropped, and that is a deliberate
    divergence from the rule that pruning is by AGE and LIVENESS rather than by
    sweep-absence. That rule protects a SHARED ledger, where "absent from my sweep"
    is a claim about one reader's partial view and pruning would let one sweep
    delete another's marks. This store is per-session and single-writer, so there
    is no other reader to protect — and the failure direction of a wrong prune here
    is a re-emit, a wake, never a silence.

    ⚠️ `reemit_after <= 0` disables the third branch entirely, which is the escape
    hatch if the hourly re-emit ever proves noisier than the lost wakes it insures
    against.
    """
    new_panes, next_state = set(), {}
    for pane in kept:
        fingerprint = gate_fingerprint(pane, items)
        prior = stored.get(pane)
        if not isinstance(prior, dict):
            prior = None
        try:
            age = now - float(prior["emitted_at"])
        except (KeyError, TypeError, ValueError):
            age = 0.0
        expired = reemit_after > 0 and prior is not None and age >= reemit_after
        if prior is None or prior.get("fingerprint") != fingerprint or expired:
            new_panes.add(pane)
            next_state[pane] = {"fingerprint": fingerprint, "emitted_at": now}
        else:
            next_state[pane] = prior
    return new_panes, next_state


def emissions_path(self_id, root=None):
    """This manager's per-decision ledger, or None when there is no manager to key it by."""
    if not self_id:
        return None
    return os.path.join(root or EMISSIONS_ROOT, self_id, "feed-emissions.jsonl")


def gate_owner(scope, session_id):
    """`(task, goal)` the gated session's own page declares, or `(None, None)`.

    Resolved from `session-scope.py`'s reverse index rather than a second vault walk:
    `Scope.sessions` is already `{session_id: [Entry]}` over every task page in every
    configured vault. A session stamped on two pages, or on none, is unattributed — the
    same answer `Scope.exclusion` gives, for the same reason, and the same fail-open
    direction.

    ⚠️ **This is a RESOLUTION, never a copy.** The attention record carries `item_id`,
    `session_id`, `pane`, `detail`, `context` and no `task` field, so a ledger that
    copied one would record nothing.
    """
    entries = (scope.sessions.get(session_id) if scope is not None else None) or []
    titles = {entry.title for entry in entries}
    if len(titles) != 1:
        return None, None
    entry = entries[0]
    return entry.title, (entry.goals[0] if entry.goals else None)


def trim_emissions(path, keep=None):
    """Keep only the newest `keep` lines. Called when the file passes its size bound.

    Trim IN PLACE rather than rotate to a sibling: the window is cut by `ts` from one
    path, so a second file would make the operator join two to answer one question. The
    cost of losing the oldest history is bounded and stated; an unbounded file's is not.

    The rewrite goes through a temp file and `os.replace`, matching every other state
    writer here — a plain truncating write would leave the ledger half-empty if the
    process died mid-rewrite.
    """
    limit = keep or EMISSIONS_KEEP_LINES
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    if len(lines) <= limit:
        return False
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.writelines(lines[-limit:])
    os.replace(tmp, path)
    return True


def log_emissions(rows, items, scope, self_id, now=None, root=None):
    """Append one record per DECISION to this manager's `feed-emissions.jsonl`.

    The feed arm's only history. `gate-feed/<sid>.json` is a pruned current-state map —
    `emit_decisions` drops every entry whose pane has left the kept set — so it cannot
    answer "how many foreign gates did this manager's feed carry in the last hour", which
    is the number this ledger exists to produce. Append-only, one line per row per poll.

    ⚠️ **`ts` is not decoration, and it is why this function exists at all.** Without it
    every count is a whole-file count against a file that lives as long as the manager
    session, so a `0` could come from history rather than from the window being quoted.
    The window is cut from this field.

    A write that fails returns the error and is never raised: a ledger that cannot be
    written must not take the wake path down with it.
    """
    path = emissions_path(self_id, root)
    if not path:
        return None
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for row in rows:
                record = newest_item_for_pane(row["pane"], items)
                task, goal = gate_owner(scope, row.get("session_id"))
                handle.write(
                    json.dumps(
                        {
                            "ts": stamp,
                            "decision": row["verdict"],
                            "reason": row["reason"],
                            "item_id": (record or {}).get("item_id"),
                            "pane": row["pane"],
                            "session_id": row.get("session_id"),
                            "spawner": row.get("spawner"),
                            "task": task,
                            "goal": goal,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        # Cheap stat, not a read: the trim only pays its cost once per bound crossing.
        if os.path.getsize(path) > EMISSIONS_MAX_BYTES:
            trim_emissions(path)
        return None
    except (OSError, TypeError, ValueError) as exc:
        # ⚠️ Wider than `OSError` on purpose. A ledger record is built from parsed store
        # data (`newest_item_for_pane` sorts on `ts`, `json.dumps` serialises whatever the
        # store returned), so a malformed record raises `TypeError`/`ValueError` — and this
        # call sits on the wake path, where a raise kills the arm instead of costing one
        # line of instrumentation. The docstring promises the ledger cannot take the wake
        # path down; this is what makes that true.
        return exc


def poll(args, items, scope=None):
    """Poll the producer, emitting only panes that ENTER the kept set.

    ⚠️ **The ownership inputs are re-read on EVERY poll, and that is the point of
    the loop rather than an optimisation to skip.** An arm runs for up to 30
    minutes, and every input this filter consults is one that is *supposed* to
    change inside that window: a peer manager records an ownership claim, a
    manager exits so its panes stop being peer-owned, a worker is spawned and
    gains a ledger record. A loop that read them once at arm time would judge a
    fresh feed against a frozen world, and both failure modes are the ones this
    filter exists to remove — a claim recorded after arming is invisible so the
    claimed pane is emitted (the foreign wake Defect 1 is named for), and a peer
    manager that dies mid-arm stays in the stale `live` set so its own gate is
    still dropped and is then owned by nobody, contradicting the invariant stated
    at `verdict()` ("a DEAD peer manager's own gate falls through and emits").

    ⚠️ **`items` is the one exception, and the exception is measured rather than
    assumed.** `log_items` globs and parses ~1 589 event-log files into ~25 000
    items: **195.8 ms**, against 37.4 ms for the ledger, 11.7 ms for the claims
    file and 1.3 ms for the registry (measured 2026-10-07 on the live machine).
    Everything cheap is re-read per poll; the one genuinely expensive source is
    read once per *arm*, not per poll. ⚠️ **The residual is named rather than
    hidden:** a pane whose first appearance in the event log lands mid-arm does
    not resolve its session until the next re-arm and reads `unowned` until then.
    That is the conservative direction — it emits, so a manager may spend one
    extra turn — and it is bounded by the `Monitor`'s 30-minute re-arm.

    ⚠️ **`scope` is the SECOND arm-time read, and its residual is the same one `items`
    carries.** It walks every configured vault (~7 s, 35 000 task pages, measured
    2026-10-29), so it is built once in `main()` and handed in. A task page stamped mid-arm
    is absent from the map until the next re-arm, which reads as unattributed and KEEPS the
    gate. ⚠️ It is also the input that can EXCLUDE, so the staleness has a second face worth
    naming: a page whose `goals:` moved mid-arm keeps its old owner until the re-arm. Both
    faces resolve on the same bound, and neither can silence a pane that was never
    attributed at all.

    ⚠️ **A `LIVENESS` line goes to stderr on EVERY poll, and it is the point of
    the loop rather than decoration.** A change-detector is silent when nothing
    changes, and silence is byte-identical to a dead arm — the defect
    `manager-attention-watch.py` closed for its own arm with the same record. The
    line carries the counts, so an arm that is up and finding nothing reads
    differently from one that never ran.

    ⚠️ **A refusal ends the loop rather than being retried.** The one-shot
    contract is "do not arm a watcher on this pipeline's output", and a refused
    read means the transport is broken, not that the fleet is quiet; a loop that
    swallowed it would keep a dead push channel looking alive for as long as the
    `Monitor`'s 30-minute bound. Exiting non-zero is what makes the manager see
    it — and the `LIVENESS` line already written for the previous poll is what
    keeps a genuine quiet arm distinguishable from a lapsed one.
    """
    state_path = feed_state_path(args.self_id, args.feed_state_dir)
    store_warned = False
    emissions_warned = False
    polls = 0
    while True:
        proc = subprocess.run(
            shlex.split(args.producer), capture_output=True, text=True
        )
        lines = proc.stdout.splitlines(keepends=True)
        refusal = feed_refusal(lines)
        if refusal:
            return refusal_exit(refusal)
        if proc.returncode != 0:
            return producer_exit(args.producer, proc.returncode)

        # ⚠️ Re-read INSIDE the loop, never hoisted — see the docstring. The
        # ordering matters too: the marker is checked before the status, because
        # a refusal is the more specific verdict and `who-needs-me.py` exits
        # non-zero on that path as well.
        feed = feed_rows(lines)
        # Headless rows resolve through the store, re-read per poll: a headless gate is
        # new by definition, so the arm-time `items` snapshot never carries it.
        keys = [pane for pane, _ in feed]
        records, error = headless_items_checked(keys, args.endpoint)
        poll_items = items + records
        rows = evaluate_panes(
            feed,
            load_ledger(args.endpoint),
            live_ids(args.registry_dir),
            poll_items,
            args.self_id,
            load_claims(args.claims_file),
            scope,
            store_states(keys, records, error),
        )
        kept = [r["pane"] for r in rows if r["verdict"] == EMIT]

        # ⚠️ The store is re-read EVERY poll rather than hoisted. Nothing else
        # writes this file, but a hoisted read would let the `--max-polls` loop and
        # a restarted arm disagree about the same file — and the read is a ~0.2 ms
        # open against a loop that already spawns a producer per poll.
        now = time.time()
        new_panes, next_state = emit_decisions(
            kept, load_feed_state(state_path), poll_items, now, args.reemit_after
        )
        # ⚠️ A store this arm cannot write is reported ONCE, never swallowed.
        # Silent failure here is the worst shape available: the arm looks healthy,
        # the LIVENESS line keeps advancing, and the change does nothing at all.
        if not store_warned and save_feed_state(state_path, args.self_id, next_state):
            store_warned = True
            print(
                f"STATE  unwritable: {state_path} — not persisting, so this arm "
                f"re-emits every kept pane on each re-arm",
                file=sys.stderr,
            )

        render(rows, args, new_panes=new_panes)

        # ⚠️ Written BEFORE the sleep, so its age is the arm's last heartbeat and
        # a stalled producer reads as a gap rather than as a quiet poll.
        state = "change" if new_panes else "no change"
        print(
            f"LIVENESS  {counts_line(rows)}  new: {len(new_panes)}  {state}",
            file=sys.stderr,
        )
        sys.stderr.flush()
        sys.stdout.flush()

        # ⚠️ AFTER the heartbeat, deliberately. The ledger is instrumentation: if building a
        # record raises despite `log_emissions`'s own catch, the poll's LIVENESS line must
        # already be on disk so the arm reads as alive-with-a-gap rather than as dead.
        #
        # Written per poll, not per wake: the ledger records DECISIONS, and a suppressed gate
        # is exactly the decision this row exists to make visible. A ledger written only on
        # emit would read `0` on a quiet hour against a completely unfixed filter — the
        # failure the criterion's candidate floor was added for.
        # Attempted EVERY poll, reported ONCE: a transient failure that clears must resume
        # the ledger rather than leave a permanent hole the window cannot see.
        emissions_error = log_emissions(rows, poll_items, scope, args.self_id)
        if emissions_error is not None and not emissions_warned:
            emissions_warned = True
            print(
                f"STATE  emissions ledger unwritable: {emissions_error} — the "
                f"window's counts will be unavailable",
                file=sys.stderr,
            )

        # ⚠️ The bound is checked BEFORE the sleep, so `--max-polls 1` is a real
        # single poll rather than one poll plus an interval of dead waiting. It
        # exists for the same reason its sibling in `manager-attention-watch.py`
        # does: an unbounded loop is testable only by killing it, and a test that
        # races the clock pins nothing.
        polls += 1
        if args.max_polls is not None and polls >= args.max_polls:
            return 0
        time.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(description="Drop gates a manager does not own.")
    parser.add_argument("--self", dest="self_id", default=None, help="this watcher's session id")
    parser.add_argument("--pane", action="append", default=[], help="evaluate one pane (repeatable)")
    parser.add_argument("--feed", action="store_true", help="read who-needs-me.py output on stdin")
    parser.add_argument("--explain", action="store_true", help="print the reason for every verdict")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--endpoint", default=None, help="override the attention store base URL")
    parser.add_argument("--ledger-dir", default=None, help="ACCEPTED AND IGNORED — the ledger comes from the endpoint")
    parser.add_argument("--registry-dir", default=REGISTRY)
    parser.add_argument("--state-dir", default=STATE)
    parser.add_argument("--claims-file", default=CLAIMS)
    parser.add_argument(
        "--scope-vault",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "vault ROOT to read the goal scope from, instead of every vault in "
            "vault-cli's config (repeatable; for fixtures and operator overrides)"
        ),
    )
    parser.add_argument(
        "--no-scope",
        action="store_true",
        help=(
            "skip the vault scope read entirely — the filter then behaves exactly "
            "as it did before that input existed (no row is excluded by the SCOPE; "
            "the `role: human` exclusion is not a scope input and still applies)"
        ),
    )
    parser.add_argument(
        "--feed-state-dir",
        default=FEED_STATE_DIR,
        help=(
            "where the arm's per-session memory lives; the file is named after "
            "--self, so two managers never share one"
        ),
    )
    parser.add_argument(
        "--reemit-after",
        type=int,
        default=REEMIT_SECONDS,
        metavar="SECONDS",
        help=(
            "re-emit a still-open gate once its stored entry is this old, so a "
            "lost wake is retried; 0 disables it (only with --interval)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "poll the producer every SECONDS and emit only panes that ENTER the "
            "kept set; omit to read the feed once and exit"
        ),
    )
    parser.add_argument(
        "--producer",
        default=PRODUCER,
        help="the feed command re-run on each poll (only with --interval)",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=None,
        metavar="N",
        help="stop after N polls (only with --interval); unbounded by default",
    )
    args = parser.parse_args()

    # ⚠️ `--ledger-dir` is kept and IGNORED rather than removed, the same call
    # `worker-sessions.py` made for `--dir` / `--heartbeat-dir`: callers and tests pass it, so
    # removing it breaks them, while accepting it in silence would let a caller believe it had
    # redirected a read that still went to the endpoint.
    if args.ledger_dir:
        print(
            "warning: --ledger-dir is accepted and IGNORED — the ledger is read from the "
            "session-ledger endpoint; use --endpoint to point at the store",
            file=sys.stderr,
        )

    ledger = load_ledger(args.endpoint)
    live = live_ids(args.registry_dir)
    items = log_items(args.state_dir)
    claims = load_claims(args.claims_file)

    # ⚠️ **The scope is read ONCE, here, and its failure is announced rather than
    # swallowed.** It is the only input that can EXCLUDE a row, so a scope that silently
    # failed would leave the defect this change fixes in place while the filter looked
    # healthy — the same shape `STATE unwritable` exists for. The `SCOPE` line is written
    # once per process, never per poll, so it does not turn the stderr heartbeat into a
    # notification stream.
    scope = None
    if not args.no_scope:
        scope = scope_input(args.self_id, args.scope_vault or None)
        if not scope.resolved:
            print(
                f"SCOPE  unresolved: {scope.reason} — no row is excluded by the scope; "
                f"every gate emits exactly as before, except a `role: human` row, which "
                f"is excluded by the role rule and not by this input",
                file=sys.stderr,
            )
            scope = None

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
    # ⚠️ `--pane` FILTERS the feed; it never manufactures a gate. Building the list
    # from the flag made every named pane a gate its caller owned, with the feed never
    # consulted at all: measured 2026-10-16, `--self beac5fd0 --pane 1832 --explain`
    # read `gates: 1  emit: 1  reason: own-worker` while the authoritative path over the
    # same pane (`who-needs-me.py --section needs-you | gate-owner-filter.py --feed
    # --self beac5fd0`) read `gates: 0  emit: 0`. `--explain` reads as a diagnostic, so
    # a manager checking "is this pane blocked?" got a confident affirmative for a pane
    # that needed nothing -- the one direction that fails by INVENTING a gate rather
    # than dropping one. `wanted` is the filter; the feed read below supplies the set.
    wanted = [str(pane).strip() for pane in args.pane]
    if args.interval is not None and args.pane:
        parser.error("--interval polls the feed; it cannot be combined with --pane")
    if args.interval is not None and not args.feed:
        parser.error("--interval re-reads the feed each poll; pass --feed as well")
    # ⚠️ `--self` is what keys the state file, so an interval arm without it has
    # nowhere to persist and would silently keep the PRE-fix behaviour — re-emitting
    # the whole kept set on every re-arm while looking exactly like a fixed one. The
    # documented arm passes it, and refusing is what keeps that failure visible.
    if args.interval is not None and not args.self_id:
        parser.error(
            "--interval keys its state file on --self; pass --self <this session id>"
        )
    # ⚠️ The lower bound is not cosmetic. `time.sleep` rejects a negative value
    # outright, so `--interval -1` reached the loop and died with an unhandled
    # `ValueError` traceback — a crash rather than a usage error, on an argument
    # that reads as a plausible typo. `0` is accepted but ONLY bounded, because
    # unbounded it is a busy-loop that re-spawns the producer at maximum rate;
    # the test suite uses exactly this spelling, which is why it must stay legal
    # rather than be rejected outright.
    if args.interval is not None and args.interval < 0:
        parser.error(
            "--interval is a poll period in seconds; a negative value cannot be slept"
        )
    if args.interval == 0 and args.max_polls is None:
        parser.error(
            "--interval 0 polls with no sleep; bound it with --max-polls, or pass a "
            "real period. Never arm a Monitor with 0."
        )
    # ⚠️ The diagnostic shapes print EVERY row on every poll, so a `Monitor` over
    # one would fire on each pass — the turn-budget cost this loop's change-detector
    # exists to remove, reintroduced by the flag a reader reaches for to debug it.
    # `new_panes` narrows only the bare `--feed` shape, so the two are refused here
    # rather than silently ignoring the change-detector.
    if args.interval is not None and (args.explain or args.json):
        parser.error(
            "--interval emits only newly-entered panes; --explain/--json print every "
            "row on every poll and would wake a Monitor each time. Run the diagnostic "
            "shape one-shot instead."
        )
    if args.max_polls is not None and args.interval is None:
        parser.error("--max-polls bounds the --interval loop; pass --interval as well")
    if args.feed and args.interval is None:
        # Read stdin ONCE and answer both questions from the same lines: whether the
        # upstream refused, and which panes it emitted. Two reads of a pipe cannot
        # both see the data, and a refusal is a verdict about the whole read.
        # ⚠️ In `--interval` mode the feed is re-produced by `--producer` on every
        # poll instead: a pipe can be read once, and a poll needs it N times. That
        # is the whole reason the loop lives here rather than in a shell wrapper.
        lines = sys.stdin.readlines()
        refusal = feed_refusal(lines)
        if refusal:
            return refusal_exit(refusal)
        feed = feed_rows(lines)
    elif wanted:
        # `--pane` WITHOUT `--feed` still has to ask the feed, or the filter has nothing
        # to filter and the flag reverts to manufacturing the very gate this change
        # removes. Produced once, with the same refusal / failed-producer handling the
        # poll loop uses: a producer that died and a quiet fleet leave the same rows on
        # the wire, and only one of them is a measurement.
        proc = subprocess.run(
            shlex.split(args.producer), capture_output=True, text=True
        )
        lines = proc.stdout.splitlines(keepends=True)
        refusal = feed_refusal(lines)
        if refusal:
            return refusal_exit(refusal)
        if proc.returncode != 0:
            return producer_exit(args.producer, proc.returncode)
        feed = feed_rows(lines)
    else:
        feed = []

    if wanted:
        seen = {pane for pane, _ in feed}
        panes = [(pane, replayed) for pane, replayed in feed if pane in wanted]
        missing = [pane for pane in wanted if pane not in seen]
    else:
        panes, missing = feed, []

    if not args.pane and not args.feed:
        parser.error("pass --pane, --feed, or both")

    if args.interval is not None:
        # `poll` re-reads everything cheap itself; only the expensive `items`
        # glob is handed over, because it is the one source whose cost makes a
        # per-poll read a real trade rather than a free one.
        return poll(args, items, scope)
    return one_shot(panes, args, ledger, live, items, claims, missing=missing, scope=scope)

if __name__ == "__main__":
    sys.exit(main())
