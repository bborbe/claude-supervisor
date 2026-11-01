#!/usr/bin/env python3
"""manager-quiet.py — the zero-runnable-tasks check for ONE manager's subject.

A subject manager whose subject has nothing left to drive keeps paying a full-context
model call every tick until the operator stands it down by hand. Measured 2026-10-15:
two managers (goal `Recurring Operator Gates …`, goal `Drive Rounds Report …`) each
carried a hand-written `<slug>.stopped` marker, both at the 900 s default cadence,
because nothing in the loop could tell "this subject is quiet" from "this tick had no
news". This script is that check.

**It is a COUNTER, not a verdict.** One quiet tick is ordinary — a round may land
between two writes. Only `QUIET_TICKS_TO_STAND_DOWN` consecutive quiet ticks mean the
subject has gone quiet, and the count lives on disk because ticks outlive the process.
⚠️ **A tick is identified by `<slug>.cadence`'s mtime, never by the call** — the count
records which tick it belongs to, so a second read of the same tick is inert:
`<slug>.quiet` in the liveness state dir, beside the `<slug>.cadence` / `<slug>.arms` /
`<slug>.stopped` files `manager-liveness.py` already owns. Same namespace, same
`slug()` — deliberately, so the two readers cannot disagree about which subject a file
describes.

⚠️ **It fails toward ARMED, never toward stood-down.** A task this check cannot prove
quiet is counted **runnable**, and one runnable task resets the counter to zero. The
asymmetric cost is the whole reason: a manager left armed on a quiet subject wastes a
model call, while a manager stood down on a subject that still had work silently stops
driving it and nothing announces the loss. So every unreadable status, unparseable
date and unresolved blocker is read as *work may remain*.

⚠️ **`clock-bound` is only as mechanical as `defer_date`.** The criterion names three
quiet shapes — `blocked_by` an open task, deferred to a future date, clock-bound — and
the first two are frontmatter reads. The third is not: a body saying "re-measure on
2026-10-22" is prose, and no reader here parses prose into a date. It is therefore
covered only where the author also set `defer_date` (the vault's own convention for
exactly this), and elsewhere it lands in the *runnable* bucket above — the safe
direction. A subject whose clock-bound work is not deferred stays armed, which is a
missed stand-down, not a false one.

**Quiet shapes**, each read from frontmatter or the blocker resolver, never inferred:

  * `status` in `completed` / `aborted`  -> TERMINAL, excluded from the set entirely
  * `status` in `backlog` / `hold`       -> not surfaced; the manager drives none
  * `phase` is `todo`                    -> waiting-approval; the operator's boundary
  * `defer_date` on a future date        -> deferred
  * `blocked_by` with an unmet entry     -> blocked (`blocked_by_verdict`, reused)
  * anything else                        -> RUNNABLE

⚠️ **`status: next` is RUNNABLE, and that is deliberate.** `vault-cli task approve`
writes `phase: todo -> planning` and leaves `status` at `next` — "the row stays queued,
not owned" (commit `1804f59`, 2026-10-09). Counting `next` quiet here made a subject
full of approved rows read as *empty* to the stand-down counter: two ticks later the
manager retired itself and drove none of them, which is the loss this script exists to
prevent. The approval boundary is `phase: todo`, not `status: next`, because `phase` is
the field `approve` actually moves. This mirrors `sweep-gate.py`'s `_classify`, whose
ready-to-start arm is `status == "next" or (in_progress and phase == "planning")` — the
runbook's rule is that the two renderers must agree, and this file was the third copy
that did not.

The readers are `manager-predispatch.py`'s, loaded by path rather than re-implemented:
`split_frontmatter`, `fm_scalar`, `blocked_by_verdict`, `resolve_tracked`. A second
copy of "what is a task" is how the sweep, the gate and this check would come to
disagree about a row.

**Usage**

    manager-quiet.py --subject "<goal or topic>" --vault <path> [--today YYYY-MM-DD]
    manager-quiet.py --subject "<goal or topic>" --vault <path> --reset

**Output contract** — one line on stdout, `<verdict>` TAB `<n_runnable>` TAB `<n_quiet>`
TAB `<consecutive_quiet>`, where `<verdict>` is `armed` or `stood-down`. `armed` means
the caller must leave the loop alone. `stood-down` means `QUIET_TICKS_TO_STAND_DOWN`
consecutive quiet ticks were reached and the caller may disarm. An unreadable subject
exits 2 with a reason on stderr and **writes nothing** — a failed read must not advance
a counter toward a stand-down.
"""
import argparse
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: `defer_date` is an instant, not a date — see `defer-date.py`.
defer_date = _load("defer_date", "defer-date.py")

QUIET_TICKS_TO_STAND_DOWN = 2

STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_LIVENESS_STATE_DIR", "~/.claude/state/sweep-gate")
)

TERMINAL_STATUSES = frozenset({"completed", "aborted"})
# Not surfaced: the manager drives none of these, so they are quiet rather than runnable.
# `backlog` is someday and `hold` is paused with no resume date — the vault's own Task
# Status Semantics.
#
# ⚠️ **`next` is NOT in this set.** `vault-cli task approve` writes `phase: todo ->
# planning` and deliberately leaves `status` at `next` ("the row stays queued, not
# owned"), so an approved, never-started row carries `status: next`. Counting it quiet
# here made a subject full of approved rows read as empty, and the stand-down counter
# then retired the manager that had just approved them. The approval boundary moved to
# `UNAPPROVED_PHASE` below, which is the field `approve` actually writes.
UNSURFACED_STATUSES = frozenset({"backlog", "hold"})

# The operator's approval boundary, mirroring `sweep-gate.py`'s `_classify`: a row at
# `phase: todo` is waiting on a human by construction — agents file there and stop, and
# the operator's `todo -> planning` flip IS the approval. It must read quiet even though
# it now carries `status: next`, which the set above no longer covers.
UNAPPROVED_PHASE = "todo"

EXIT_OK = 0
EXIT_UNREADABLE = 2

_HERE = os.path.dirname(os.path.abspath(__file__))


_PREDISPATCH = None


def load_predispatch():
    """`manager-predispatch.py`, loaded by path and memoized.

    Its filename is hyphenated, so it is not importable as a module name — the same
    reason `scripts/tests/*` load these by `spec_from_file_location`. Importing runs
    only module-level constants; every reader this uses is a pure function.

    ⚠️ **The memo is load-bearing, not a micro-optimization.** `exec_module` re-executes
    the module body on every call — unlike a normal `import`, which caches in
    `sys.modules` — and `classify` calls this **once per tracked task**, so an
    unmemoized loader re-parsed `manager-predispatch.py` once per row per tick. It also
    reset that module's own module-level state on every call, so two readers within one
    tick could in principle observe different constants.
    """
    global _PREDISPATCH
    if _PREDISPATCH is None:
        spec = importlib.util.spec_from_file_location(
            "manager_predispatch", os.path.join(_HERE, "manager-predispatch.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PREDISPATCH = mod
    return _PREDISPATCH


def slug(subject: str) -> str:
    """Same slug as `manager-liveness.py`, `sweep-gate.py` and `stop-probe.py`.

    ⚠️ **Duplicated on purpose, and pinned by a test.** The four files must agree or a
    stand-down would be recorded against a slug nothing else reads; they live in
    different plugins' namespaces, so a shared import is not available. The test
    asserts this function and `manager-liveness.py`'s produce the same value for the
    same input.
    """
    return re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")


def quiet_path(s: str, state_dir: str | None = None) -> str:
    return os.path.join(state_dir or STATE_DIR, f"{s}.quiet")


def cadence_path(s: str, state_dir: str | None = None) -> str:
    return os.path.join(state_dir or STATE_DIR, f"{s}.cadence")


def tick_key(s: str, state_dir: str | None = None) -> int | None:
    """The identity of the CURRENT tick: the mtime of `<slug>.cadence`.

    The manager arms at the head of every round, so that file's mtime marks the tick a
    call belongs to — which is what makes a second read of the same tick recognisable.
    `None` when there is no cadence file: a subject with no loop has no tick to be
    counted against, and the caller falls back rather than inventing one.
    """
    try:
        return int(os.path.getmtime(cadence_path(s, state_dir)))
    except OSError:
        return None


def read_quiet_state(s: str, state_dir: str | None = None) -> tuple:
    """`(count, tick)` for one subject; `(0, None)` when absent or unreadable.

    Unreadable reads as 0, never as a large number: a corrupt counter must restart the
    count, not hand the caller a stand-down it did not earn. A bare integer is the
    pre-tick-key format and reads as `tick = None`, so a counter written by an older
    version still counts — it simply cannot tell a re-read from a new tick.
    """
    try:
        with open(quiet_path(s, state_dir), encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return 0, None
    try:
        return max(0, int(raw)), None
    except ValueError:
        pass
    try:
        d = json.loads(raw)
        return max(0, int(d["count"])), int(d["tick"])
    except (ValueError, KeyError, TypeError):
        return 0, None


def read_quiet_count(s: str, state_dir: str | None = None) -> int:
    """The persisted consecutive-quiet-tick count; 0 when absent or unreadable."""
    return read_quiet_state(s, state_dir)[0]


def write_quiet_state(s: str, n: int, tick: int, state_dir: str | None = None) -> None:
    p = quiet_path(s, state_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"tick": int(tick), "count": int(n)}) + "\n")
    os.replace(tmp, p)


def write_quiet_count(s: str, n: int, state_dir: str | None = None) -> None:
    write_quiet_state(s, n, 0, state_dir)


def _date_key(value):
    """The instant a `defer_date` names, or None — `defer-date.py` is the one parser.

    ⚠️ **Both spellings reach here.** The vault normalizes an *unquoted* date scalar to
    RFC3339 (`2026-10-22T00:00:00Z`) and keeps the date-only form only when quoted, and
    a full timestamp carries a time of day — all three are read as the same kind of
    thing, an aware datetime, so the comparison below is against an instant rather than
    a date string.
    """
    parsed, _ = defer_date.parse_defer_date(value)
    return parsed


def _date_text(value) -> str:
    """The form a reader is shown: `YYYY-MM-DD`, or ISO 8601 when a time is set."""
    return defer_date.display(value)


def classify(fm: str, tasks_dir: str, index, today_key) -> dict:
    """`{"kind", "defer", "blocked"}` for one task's frontmatter.

    `kind` is `terminal` | `quiet` | `runnable`. `defer` carries the row's future
    `defer_date` when that is *why* it is quiet, and `blocked` its unmet blocker names —
    both exist so the reopen trigger can be named from the **same read that decided the
    verdict**, rather than re-derived later from a second one that could disagree.

    ⚠️ **Every branch below reads a field; none infers.** An absent or unparseable
    value falls through to `runnable` — see the module docstring on failing toward
    armed.
    """
    pd = load_predispatch()
    status = (pd.fm_scalar(fm, "status") or "").strip().lower()
    if status in TERMINAL_STATUSES:
        return {"kind": "terminal", "defer": None, "blocked": []}
    if status in UNSURFACED_STATUSES:
        return {"kind": "quiet", "defer": None, "blocked": []}
    phase = (pd.fm_scalar(fm, "phase") or "").strip().lower()
    if phase == UNAPPROVED_PHASE:
        return {"kind": "quiet", "defer": None, "blocked": []}
    raw_due = pd.fm_scalar(fm, "defer_date")
    state, detail = defer_date.defer_state(raw_due, today_key)
    if state == "invalid":
        # Fail toward ARMED, as this module has always documented: a defer this check
        # cannot read is not one it can rule the row out on, so the row stays runnable —
        # and the reason is said rather than swallowed, so the garbage is still visible.
        print(f"manager-quiet: {detail} — not counted as a defer", file=sys.stderr)
    if state == "deferred":
        return {"kind": "quiet", "defer": _date_text(raw_due), "blocked": []}
    verdict = pd.blocked_by_verdict(fm, tasks_dir, index)
    if verdict["blocked"]:
        return {"kind": "quiet", "defer": None, "blocked": list(verdict["unmet"])}
    return {"kind": "runnable", "defer": None, "blocked": []}


def survey(vault: str, members: list, today_key) -> dict:
    """`{"runnable", "quiet", "terminal", "defer", "blocked"}` for one subject's set.

    The task set is `manager-predispatch.py`'s `resolve_tracked` — a member may be a
    goal (admits tasks whose `goals:` name it) or a task named directly — so this check
    and the sweep see the same rows. `defer` and `blocked` are keyed by task name and
    carry only the rows that are quiet *for that reason*.
    """
    pd = load_predispatch()
    tasks_dir = os.path.join(vault, pd.TASKS_DIRNAME)
    index = pd.task_index(tasks_dir)
    out = {"runnable": [], "quiet": [], "terminal": [], "defer": {}, "blocked": {}}
    for task in pd.resolve_tracked(vault, members):
        path = os.path.join(tasks_dir, task["name"] + ".md")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                fm = pd.split_frontmatter(fh.read())
        except OSError:
            # An unreadable row is work this check cannot rule out -> runnable.
            out["runnable"].append(task["name"])
            continue
        verdict = classify(fm or "", tasks_dir, index, today_key)
        out[verdict["kind"]].append(task["name"])
        if verdict["defer"]:
            out["defer"][task["name"]] = verdict["defer"]
        if verdict["blocked"]:
            out["blocked"][task["name"]] = verdict["blocked"]
    return out


def _defer_instant(text):
    """Sort key for a rendered `defer_date` — the instant it names, never the string."""
    parsed, _ = defer_date.parse_defer_date(text)
    return parsed or defer_date.as_now()


def reopen_trigger(seen: dict) -> str:
    """What would make this subject live again — the line SC3 requires.

    The **earliest** future `defer_date` when any quiet row carries one, because that is
    the first date the subject can change on its own; otherwise the blocking task names;
    otherwise an explicit `no named trigger`. ⚠️ **The no-trigger case says so in words
    rather than returning `""`** — an empty tail would satisfy SC3's literal probe while
    naming nothing, and a reader cannot tell "no trigger exists" from "the trigger
    failed to compute".
    """
    # Sorted by the INSTANT, not by the rendered string: two rows in different UTC
    # offsets sort by their digits otherwise, naming the wrong "earliest".
    dates = sorted((d for d in seen["defer"].values() if d), key=_defer_instant)
    if dates:
        return dates[0]
    if seen["blocked"]:
        # The BLOCKERS, never the blocked rows: the subject reopens when a blocker
        # completes, so naming the row that is waiting would name the wrong end of the
        # edge — and a reader acting on it would go and look at a task that is fine.
        names = sorted({b for blockers in seen["blocked"].values() for b in blockers})
        return "blocked by " + ", ".join(names)
    return "no named trigger"


def tick(subject: str, vault: str, today_key, state_dir: str | None = None) -> dict:
    """Record one tick and return the verdict. This is the whole rule.

    A runnable task resets the counter to 0 — the reset is what makes the count
    *consecutive*, and a counter that only ever incremented would stand a subject down
    two ticks after its last quiet one, however much work arrived in between.

    **A tick is identified by `<slug>.cadence`'s mtime, not by the call.** A manager
    that reads its subject twice in one tick must not count two ticks: see the counter
    block below.
    """
    pd = load_predispatch()
    bad_vault = pd.vault_root_error(vault)
    if bad_vault:
        return {"error": bad_vault}
    try:
        # `resolve_subject` returns `(branch, members)` and RAISES on both-match and
        # no-match — never a silent preference, which is the failure both commands'
        # resolution section exists to prevent. A refusal here must not touch the
        # counter, so it is caught before any write.
        _branch, members = pd.resolve_subject(vault, subject)
    except ValueError as exc:
        return {"error": str(exc)}
    s = slug(subject)
    seen = survey(vault, members, today_key)
    prior, prior_tick = read_quiet_state(s, state_dir)
    key = tick_key(s, state_dir)
    if seen["runnable"]:
        count = 0
    elif key is not None and key == prior_tick:
        # A SECOND read of the SAME tick. The count already reflects this tick, so this
        # read is INERT — never an extra tick. Counting invocations instead of ticks
        # stands a subject down EARLY, and early is the one direction that loses work
        # silently: the rule's whole asymmetry is that a manager left armed costs a model
        # call, while one stood down on live work stops driving it and nothing announces
        # the loss. Found 2026-10-16 by a goal manager that read its subject twice in one
        # tick and watched the counter go 1 -> 2 -> 3.
        count = prior
    else:
        count = prior + 1
    write_quiet_state(s, count, key if key is not None else (prior_tick or 0), state_dir)
    return {
        "subject": subject,
        "slug": s,
        "runnable": seen["runnable"],
        "quiet": seen["quiet"],
        "terminal": seen["terminal"],
        "consecutive_quiet": count,
        "verdict": "stood-down" if count >= QUIET_TICKS_TO_STAND_DOWN else "armed",
        "trigger": reopen_trigger(seen),
    }


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Zero-runnable-tasks check for one subject.")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--today", help="YYYY-MM-DD; defaults to the local date")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--reset", action="store_true", help="clear the counter and exit")
    a = ap.parse_args(argv)

    if a.reset:
        write_quiet_count(slug(a.subject), 0, a.state_dir)
        return EXIT_OK

    # Default to the CURRENT INSTANT: local midnight would keep a row quiet until the
    # next day even after its defer time passed.
    today_key = _date_key(a.today) if a.today else defer_date.as_now()
    result = tick(a.subject, a.vault, today_key, a.state_dir)
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return EXIT_UNREADABLE
    print(
        f"{result['verdict']}\t{len(result['runnable'])}\t"
        f"{len(result['quiet'])}\t{result['consecutive_quiet']}"
    )
    if result["verdict"] == "stood-down":
        # SC3's line. Emitted HERE rather than by the caller so the trigger and the
        # verdict come from one read of the tree — a step that recomputed the trigger
        # could name a different date than the one the stand-down was decided on.
        print(f"Stood down: zero runnable tasks; reopen on {result['trigger']}")
    if result["runnable"]:
        # The reason a subject is still armed is the row that kept it armed; a bare
        # count would send a reader looking for which task it was.
        print("runnable: " + ", ".join(result["runnable"]), file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
