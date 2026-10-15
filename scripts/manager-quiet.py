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
subject has gone quiet, and the count lives on disk because a tick is one process:
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
  * `status` in `next` / `backlog` / `hold` -> not surfaced; the manager drives none
  * `defer_date` on a future date        -> deferred
  * `blocked_by` with an unmet entry     -> blocked (`blocked_by_verdict`, reused)
  * anything else                        -> RUNNABLE

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
import os
import re
import sys
import time

QUIET_TICKS_TO_STAND_DOWN = 2

STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_LIVENESS_STATE_DIR", "~/.claude/state/sweep-gate")
)

TERMINAL_STATUSES = frozenset({"completed", "aborted"})
# Not surfaced: the manager drives none of these, so they are quiet rather than runnable.
# `next` is queued-not-surfaced, `backlog` is someday, `hold` is paused with no resume
# date — the vault's own Task Status Semantics.
UNSURFACED_STATUSES = frozenset({"next", "backlog", "hold"})

EXIT_OK = 0
EXIT_UNREADABLE = 2

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_predispatch():
    """`manager-predispatch.py`, loaded by path.

    Its filename is hyphenated, so it is not importable as a module name — the same
    reason `scripts/tests/*` load these by `spec_from_file_location`. Importing runs
    only module-level constants; every reader this uses is a pure function.
    """
    spec = importlib.util.spec_from_file_location(
        "manager_predispatch", os.path.join(_HERE, "manager-predispatch.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def read_quiet_count(s: str, state_dir: str | None = None) -> int:
    """The persisted consecutive-quiet-tick count; 0 when absent or unreadable.

    Unreadable reads as 0, never as a large number: a corrupt counter must restart the
    count, not hand the caller a stand-down it did not earn.
    """
    try:
        with open(quiet_path(s, state_dir), encoding="utf-8") as fh:
            return max(0, int(fh.read().strip()))
    except (OSError, ValueError):
        return 0


def write_quiet_count(s: str, n: int, state_dir: str | None = None) -> None:
    p = quiet_path(s, state_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f"{n}\n")
    os.replace(tmp, p)


def parse_date(value: str):
    """`YYYY-MM-DD` from a `defer_date` scalar, or None when it is not a date.

    ⚠️ **Both spellings reach here.** The vault normalizes an *unquoted* date scalar to
    RFC3339 (`2026-10-22T00:00:00Z`) and keeps the date-only form only when quoted, so
    a reader that accepts one spelling silently ignores half the vault. The first ten
    characters are taken when they match the date shape; anything else is not a date and
    the caller treats it as absent.
    """
    text = (value or "").strip().strip("'\"")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    try:
        return time.struct_time(
            (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0, 0, 0, -1)
        )
    except ValueError:
        return None


def _date_key(value: str):
    """A `(y, m, d)` tuple, or None — comparable without a timezone in the picture."""
    t = parse_date(value)
    return (t.tm_year, t.tm_mon, t.tm_mday) if t else None


def classify(fm: str, tasks_dir: str, index, today_key) -> str:
    """`terminal` | `quiet` | `runnable` for one task's frontmatter.

    ⚠️ **Every branch below reads a field; none infers.** An absent or unparseable
    value falls through to `runnable` — see the module docstring on failing toward
    armed.
    """
    pd = load_predispatch()
    status = (pd.fm_scalar(fm, "status") or "").strip().lower()
    if status in TERMINAL_STATUSES:
        return "terminal"
    if status in UNSURFACED_STATUSES:
        return "quiet"
    due = _date_key(pd.fm_scalar(fm, "defer_date"))
    if due is not None and today_key is not None and due > today_key:
        return "quiet"
    if pd.blocked_by_verdict(fm, tasks_dir, index)["blocked"]:
        return "quiet"
    return "runnable"


def survey(vault: str, members: list, today_key) -> dict:
    """`{"runnable": [...], "quiet": [...], "terminal": [...]}` for one subject's set.

    The task set is `manager-predispatch.py`'s `resolve_tracked` — a member may be a
    goal (admits tasks whose `goals:` name it) or a task named directly — so this check
    and the sweep see the same rows.
    """
    pd = load_predispatch()
    tasks_dir = os.path.join(vault, pd.TASKS_DIRNAME)
    index = pd.task_index(tasks_dir)
    out = {"runnable": [], "quiet": [], "terminal": []}
    for task in pd.resolve_tracked(vault, members):
        path = os.path.join(tasks_dir, task["name"] + ".md")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                fm = pd.split_frontmatter(fh.read())
        except OSError:
            # An unreadable row is work this check cannot rule out -> runnable.
            out["runnable"].append(task["name"])
            continue
        out[classify(fm or "", tasks_dir, index, today_key)].append(task["name"])
    return out


def tick(subject: str, vault: str, today_key, state_dir: str | None = None) -> dict:
    """Record one tick and return the verdict. This is the whole rule.

    A runnable task resets the counter to 0 — the reset is what makes the count
    *consecutive*, and a counter that only ever incremented would stand a subject down
    two ticks after its last quiet one, however much work arrived in between.
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
    if seen["runnable"]:
        count = 0
    else:
        count = read_quiet_count(s, state_dir) + 1
    write_quiet_count(s, count, state_dir)
    return {
        "subject": subject,
        "slug": s,
        "runnable": seen["runnable"],
        "quiet": seen["quiet"],
        "terminal": seen["terminal"],
        "consecutive_quiet": count,
        "verdict": "stood-down" if count >= QUIET_TICKS_TO_STAND_DOWN else "armed",
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

    today_key = _date_key(a.today) if a.today else time.localtime()[:3]
    result = tick(a.subject, a.vault, today_key, a.state_dir)
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return EXIT_UNREADABLE
    print(
        f"{result['verdict']}\t{len(result['runnable'])}\t"
        f"{len(result['quiet'])}\t{result['consecutive_quiet']}"
    )
    if result["runnable"]:
        # The reason a subject is still armed is the row that kept it armed; a bare
        # count would send a reader looking for which task it was.
        print("runnable: " + ", ".join(result["runnable"]), file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
