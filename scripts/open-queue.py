#!/usr/bin/env python3
"""Rank the approved queue and select the rows a fleet round may open this tick.

Why this exists
---------------
The fleet round has read the approved-but-unstarted queue on every tick since
2026-10-01 and has never opened a row of it: `commands/steps/readings.md` printed
`approved, not started: N` and stated the design outright — *"The line reports; it
never opens."* The opening was delegated to the owning goal manager.

Measured 2026-10-30 09:45 on the live fleet: **133 approved rows waiting, 8 of 8
delegates reading `STALE` / `UNARMED` / `STOPPED`, 17 free worker slots.** A
measure-then-delegate design with no live delegate is a measure-then-drop, and the
operator's ruling of 2026-10-30 is that the missing hop is filled by the round itself:

    "we dont want to auto open topic-manager, goal-manager ... but flag are fine"

So this script answers one question — *which rows may this tick open, in what order* —
and nothing else. It opens nothing: the round does, through `/supervisor:open`, whose
own gates (approval, readiness, the cap) stay exactly where they are.

The population is not re-derived here
--------------------------------------
A row is in the queue because `scripts/approved-not-started.py` says so. This script
**calls it** rather than re-reading `approved_at`, `status` and `claude_session_id`
itself: two readers of one predicate is the defect `session-liveness.py`'s header
records, and it does not become acceptable by being written twice in one repo. The
queue is `--tasks-dir` handed to that script, and its rows arrive already filtered by
the live-session rule `readings.md` documents.

This script adds only what that one does not answer:

  * the **guards** — a row this round may not open even though it is approved and
    unstarted (see below);
  * the **rank** — operator-flag, then priority, then age;
  * the **capacity** — how many rows the free slots admit;
  * the **skipped list**, with a reason per row, so a round that opens three of 133
    names the other 130 rather than printing `skipped: capacity`.

The guards, and why each is here
--------------------------------
`approved-not-started.py` answers "approved, and no live session owns it". That is
necessary and not sufficient — further conditions decide whether opening the row is
the round's act:

  * **`phase: todo`** — the operator's approval inbox. `commands/open.md` Gate 1
    refuses it, and this leg does not become an approval path by being automated.
  * **a `blocked_by`** — the row names a dependency that is not met.
  * **a future `defer_date`** — defer-UNTIL, not a due date: the row is deliberately
    parked, and `next-focus.py` reads the same distinction.
  * **an unreadable task file** — refused, because an absent guard read is not a clean
    one. Opening on an unknown guard state is the failure this branch exists to stop.

⚠️ **These guards are absences, and an absence passes on a round that opens nothing.**
The positive control lives in the test suite (`test_open_queue.py`'s control row) and in
the round itself, which prints the opened set in order.

Rank
----
1. **flag first** — `flag: true`, **whoever set it**. Operator ruling 2026-10-30: *"in the
   end it should not matter how set the flag … if flag, should be open if claude session
   limit allow it"* — so `flag_set_by` is a note here, not a gate, and a bare `flag: true`
   with no setter ranks exactly as an operator's does. ⚠️ **This is deliberately wider than
   `commands/open.md` Step 0.5's batch selector**, which keeps its provenance gate because
   that batch writes `approved_by: operator`; this leg only ranks a row for opening, and
   `docs/session-tiers.md` § rule 2 owns the split.
2. **`priority` ascending** — 1 before 2 before 3; a row carrying no `priority` sorts
   after every row that does, because an unset field is not a high priority.
3. **oldest `approved_at` first** — the tiebreak, so the queue drains in the order it
   was approved rather than the order the directory happens to list.

Ranked, never bulk
------------------
The 2026-10-01 ruling — *"approving and starting all of them is not a good idea"* — is
why the cap is applied **after** the rank and why the skipped rows are printed. A round
that opens `free` arbitrary rows and prints `skipped: capacity` satisfies a count and
violates the ruling; the count is not the claim, the order is.

Capacity
--------
`live` comes from `worker-sessions.py --count` and `target` from `worker-target.py` —
the two instruments `docs/fleet-surface.md` § Spawn a worker item 5 names, never a
number restated here.

⚠️ **A target of `unlimited` (the soft key set to `0`) opens nothing.** The cap is the
only bound this leg has to rank against; admitting every eligible row because the bound
was removed is bulk opening by construction, which the operator's 2026-10-01 ruling
forbids. The leg refuses and names the config state rather than inventing a per-round
number — the operator sets a target.

⚠️ **`unknown` is a third answer, never `0`.** An unreadable capacity store prints
`unknown` and exits non-zero: *"no free slots"* and *"I could not count"* must not
render the same, because only one of them is a fact about the fleet. Same rule, same
reason as `approved-not-started.py`.

Exit codes: 0 selected (including zero rows) · 2 a read failed, reason on stderr.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
APPROVED = os.path.join(HERE, "approved-not-started.py")
WORKER_SESSIONS = os.path.join(HERE, "worker-sessions.py")
WORKER_TARGET = os.path.join(HERE, "worker-target.py")

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Sorts every row carrying no priority after every row that has one. A number, not a
# sentinel string, so the key stays a tuple of comparables.
NO_PRIORITY = 10**6


def frontmatter(text):
    """Return the frontmatter block, or '' when the file has none."""
    match = FRONTMATTER.match(text)
    return match.group(1) if match else ""


def field(block, key):
    r"""First value of a scalar frontmatter key, or None.

    ⚠️ **`[ \t]*`, never `\s*`.** `\s` matches `\n`, so `^key:\s*(.*)$` lets an EMPTY
    `key:` swallow the newline and capture the NEXT line as its value — `phase:` followed
    by `status: in_progress` reads `phase` as `status: in_progress`, and a `defer_date:`
    with no value reads the following key and compares it as a date. Bounded to the
    horizontal whitespace a YAML scalar actually uses.
    """
    match = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", block, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def read_frontmatter(tasks_dir, name):
    """The frontmatter block of one task file, or None when it cannot be read.

    None is distinct from '': the caller refuses on None, because an unreadable file
    would otherwise yield no guard fields and make a blocked or deferred row look
    openable.
    """
    path = os.path.join(tasks_dir, name + ".md")
    try:
        with open(path, encoding="utf-8") as handle:
            return frontmatter(handle.read())
    except OSError:
        return None


def is_flagged(block):
    """`flag: true` — **whoever set it**. Provenance is not read.

    Operator ruling 2026-10-30: *"in the end it should not matter how set the flag … if flag,
    should be open if claude session limit allow it"*. `flag_set_by` is a note, not a gate,
    so a bare `flag: true` with no setter ranks exactly as one written `--by operator`. The
    flag still only decides WHICH already-approved row goes first — the approval is
    `approved_at`, which this leg requires on every row it opens.
    """
    return (field(block, "flag") or "").strip().lower() in ("true", "yes", "on")


def priority_of(block):
    """The row's `priority` as an int, or None when absent/unparseable.

    None sorts last: an unset priority is not a high one.
    """
    raw = (field(block, "priority") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def has_blocked_by(block):
    """True when the row declares any blocker, in **either** frontmatter shape.

    ⚠️ **`blocked_by` is written as a multi-line YAML block in this vault** — a bare
    `blocked_by:` followed by indented `- '[[…]]'` items — so a scalar-only read returns
    None for the shape the vault actually uses and the guard never fires. That is the
    measured defect `scripts/next-focus.py` records for its own hold-goal class: *"An
    inline-only read returns '' for it … the value's absence read as 'no blocker'"*.
    There the cost was a proposal that never fired; here it is a spawn, so the guard
    reads both forms.
    """
    match = re.search(r"^blocked_by:[ \t]*(.*)$", block, re.MULTILINE)
    if not match:
        return False
    inline = match.group(1).strip().strip('"').strip("'")
    if inline:
        # An inline value is the WHOLE answer — including `[]`, which is an empty list
        # and not a blocker. Scanning on from here would read the next key's indented
        # list (`tags:`, `depends_on:`) as a blocker and refuse an eligible row.
        return inline not in ("[]", "[ ]")
    for line in block[match.end():].splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            if line.strip().startswith("-"):
                return True  # a block-list item
            continue
        break
    return False


def guards(block, today):
    """The reason this row may not open, or None when it may.

    The first failing guard is the reason reported, so the operator is told one thing
    rather than a list.
    """
    if block is None:
        return "unreadable task file"
    if (field(block, "phase") or "").strip() == "todo":
        return "waiting approval"
    if has_blocked_by(block):
        return "blocked"
    defer = (field(block, "defer_date") or "").strip()[:10]
    if defer and defer > today:
        return "deferred"
    return None


def rank_key(row):
    """Sort key: operator-flag first, then priority asc, then oldest approval."""
    # ⚠️ `-age_seconds`, never the raw `approved_at` string. `approved-not-started.py`
    # re-emits it via `datetime.isoformat()`, which preserves each row's own UTC offset,
    # so two rows in different offsets compare as strings and sort by the digits rather
    # than by the instant. `age_seconds` is already normalised and sorts correctly.
    return (
        0 if row["flagged"] else 1,
        row["priority"] if row["priority"] is not None else NO_PRIORITY,
        -float(row["age_seconds"]),
    )


def run_json(cmd):
    """Run a helper that emits JSON; return (value, error).

    A non-zero exit or unparseable stdout is an error, never an empty result — the
    `unknown`-is-not-`0` rule.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        return None, "cannot run %s: %s" % (os.path.basename(cmd[1]), exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, detail[-1] if detail else "exit %d" % proc.returncode
    try:
        return json.loads(proc.stdout), None
    except ValueError:
        return None, "unparseable output from %s" % os.path.basename(cmd[1])


def run_scalar(cmd):
    """Run a helper that emits one token; return (value, error)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        return None, "cannot run %s: %s" % (os.path.basename(cmd[1]), exc)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return None, detail[-1] if detail else "exit %d" % proc.returncode
    return proc.stdout.strip(), None


def collect(tasks_dir, today, approved_json=None):
    """The ranked eligible rows plus the skipped ones with their reasons.

    `approved_json` is injectable so the suite can drive this without a live registry;
    production passes None and the script calls `approved-not-started.py` itself.
    """
    if approved_json is None:
        payload, err = run_json([sys.executable, APPROVED, "--tasks-dir", tasks_dir, "--json"])
        if err:
            return None, None, err
    else:
        payload = approved_json

    rows = []
    skipped = []
    for entry in payload.get("rows", []):
        name = entry["name"]
        block = read_frontmatter(tasks_dir, name)
        reason = guards(block, today)
        if reason:
            skipped.append({"name": name, "reason": reason})
            continue
        rows.append(
            {
                "name": name,
                "approved_at": entry.get("approved_at", ""),
                "age_seconds": entry.get("age_seconds", 0),
                "manager": entry.get("manager"),
                "flagged": is_flagged(block),
                "priority": priority_of(block),
            }
        )
    rows.sort(key=rank_key)
    return rows, skipped, None


def capacity():
    """(live, target) — `target` is None for `unlimited`; (None, None) on a failed read."""
    live_raw, live_err = run_scalar([sys.executable, WORKER_SESSIONS, "--count"])
    target_raw, target_err = run_scalar([sys.executable, WORKER_TARGET])
    if live_err or target_err:
        return None, None
    try:
        live = int(live_raw)
    except ValueError:
        return None, None
    if target_raw == "unlimited":
        return live, None
    try:
        return live, int(target_raw)
    except ValueError:
        return None, None


def select(rows, live, target):
    """Split ranked rows into (opened, skipped-for-capacity).

    ⚠️ **`target is None` (the soft key set to `0`, i.e. unlimited) admits NOTHING.**
    "Unlimited" switches the server's cap off, and a cap is the only thing this leg has
    to bound a round against: admitting every eligible row because the bound was removed
    is bulk opening by construction, which the operator's 2026-10-01 ruling forbids
    ("approving and starting all of them is not a good idea"). So the leg refuses and
    names the config state instead of guessing a number — the operator can set a target.
    """
    if target is None:
        return [], list(rows)
    free = max(0, target - live)
    return rows[:free], rows[free:]


def render(opened, skipped, live, target):
    """The round's block.

    Three forms, each of which the round prints verbatim. Zero free slots and an
    unlimited target each have their own line: a leg that opened nothing because the
    fleet was full, because the target was removed, and because the queue was empty
    must not render the same.
    """
    if target is None:
        return "open leg: target unlimited — selected 0 (workers unlimited)"

    if live is not None and max(0, target - live) == 0:
        return "open leg: no free slots — selected 0 (workers %d/%d)" % (live, target)

    head = "open leg: selected %d (workers %d/%d)" % (len(opened), live, target)
    lines = [head]
    for index, row in enumerate(opened, start=1):
        if row["flagged"]:
            why = "flagged"
        elif row["priority"] is not None:
            why = "priority %d" % row["priority"]
        else:
            why = "no priority"
        lines.append("  %d. %s — %s" % (index, row["name"], why))
    if skipped:
        counts = {}
        for row in skipped:
            counts[row["reason"]] = counts.get(row["reason"], 0) + 1
        detail = " · ".join("%d %s" % (n, reason) for reason, n in sorted(counts.items()))
        lines.append("  skipped: %d — %s" % (len(skipped), detail))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rank the approved queue and select the rows a fleet round may open"
    )
    parser.add_argument("--tasks-dir", required=True, help="the vault's task directory")
    parser.add_argument("--now", default=None, help="ISO date for the defer guard (default: today)")
    parser.add_argument("--vault", default=None,
                        help="vault name, used to compose each row's ready-to-run action")
    parser.add_argument("--json", action="store_true", help="emit the selection as JSON")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.tasks_dir):
        # Both streams: the exit code is the signal, but a round that captured only
        # stdout would otherwise render nothing at all, which reads as a clean round.
        print("open leg: unknown")
        print("❌ tasks dir not found: %s" % args.tasks_dir, file=sys.stderr)
        return 2

    today = (args.now or date.today().isoformat())[:10]
    rows, skipped, err = collect(args.tasks_dir, today)
    if err:
        print("open leg: unknown")
        print("⚠️ approved queue unreadable — %s" % err, file=sys.stderr)
        return 2

    live, target = capacity()
    if live is None:
        print("open leg: unknown")
        print("⚠️ capacity unreadable — cannot tell free slots from a full fleet", file=sys.stderr)
        return 2

    opened, capacity_skipped = select(rows, live, target)
    all_skipped = skipped + [{"name": r["name"], "reason": "capacity"} for r in capacity_skipped]

    if args.json:
        # ⚠️ `action` is pre-quoted with `shlex.quote`, mirroring `next-focus.py`'s
        # `action` field. A row name is a task-file stem, and the vault is
        # worker-writable, so a `"` in it breaks naive quoting and a `$(…)` or backtick
        # executes in the session's shell. The round takes this string verbatim.
        vault = ' --vault %s' % shlex.quote(args.vault) if args.vault else ""
        json.dump(
            {
                "selected": [
                    {
                        "name": r["name"],
                        "action": "/supervisor:open %s --task%s"
                        % (shlex.quote(r["name"]), vault),
                    }
                    for r in opened
                ],
                "skipped": all_skipped,
                "live": live,
                "target": target,
                # The rendered block rides the JSON, as `next-focus.py` does, so one
                # invocation yields both the block to print and the quoted actions —
                # two reads could disagree, and a step that needs both would otherwise
                # have to run the leg twice.
                "render": render(opened, all_skipped, live, target),
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    print(render(opened, all_skipped, live, target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
