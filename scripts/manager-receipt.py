#!/usr/bin/env python3
"""Per-round receipt for manager loops: make a round that skipped its steps visible.

`commands/goal-drive.md` is a router map — it names the steps and the condition
under which each is read, and carries no mechanical check that any of them ran. A
round can therefore arm its wakeup, run `manager-liveness.py --arm`, count the
task-file boxes and render exactly like a full one: `manager-liveness.py --check`
proves only that the WAKEUP was armed, never that the round did its work.

Measured 2026-10-17 (goal manager session 956f6f82): from 18:01 to 21:50 local the
session made 15 `ScheduleWakeup` calls and **0** `Agent` dispatches and **0**
`Monitor` arms — the sweep leg never ran and neither watcher was armed — while a
worker held an in-tab question for 34+ minutes unrelayed. Nothing in the round's own
output distinguished it from a full one.

This script is that check. The round records the artifact each load-bearing step
produced; the tail re-arm reads the receipt back and prints `skipped: <step>` for
every field that does not resolve to an artifact produced THIS round.

Three verbs, one receipt per topic:

  --start  --topic T            begin a round: writes `round_started_at` and clears
                                every field, so a receipt can only ever describe the
                                round that wrote it. Run at the tick's head.
  --record --topic T --step S --path P
                                step S produced artifact P. Steps are `needs-you`
                                (the file the round persisted
                                `who-needs-me.py --section needs-you` to), `sweep`
                                (the sweep reader's payload) and `watcher` (the
                                tracked-set arm's `events.jsonl`).
  --check  --topic T            validate against the round window. Prints
                                `skipped: <step>` per unresolved field and exits 11
                                if any; prints nothing and exits 0 on a full round.
                                Run at the tail re-arm, validating the round that
                                just ran.

**A field resolves only when its artifact proves the step RAN, not that a path
exists.** The file must sit inside the round window AND carry the producer's own
shape: a `touch`ed empty file, an arbitrary non-empty string and a round-written
stub all fail. Existence-plus-recency is satisfied by the very shortcut this check
exists to catch — measured by audit 2026-10-18, where a probe keyed on mtime passed
against a round that never ran the read.

**The honest bound.** The receipt is written by the round, so no probe here can tell
a fabricated receipt from a real one — only raise what fabrication costs. What the
check does guarantee is that a round cannot skip a step *silently*: omitting it is
now a visible act rather than an invisible one.

Staleness is `manager-liveness.py`'s rule, applied to this round: the SAME
`observed_period()` and the SAME `SLACK`, IMPORTED from that file rather than
restated here. The limit is `period + SLACK` — 1x the observed period plus 180 s —
falling back to `interval + 180` when there is too little history.

**One rule, one definition.** Until 2026-10-21 this file carried its own basis while
its docstring asserted that the two checks were aligned, and the assertion was false
twice over: the multiple was `2x` against liveness's `1x`, and the statistic was the
UPPER median (`gaps[len//2]`) against liveness's true `median()`, which differs on
every even gap count. Measured 2026-10-21 over the retained `.arms` history, the two
limits differed on 46 of 46 subjects, and the band between them was the defect: a
manager liveness had already called stale could still have its round's fields judged
current. The receipt now imports the rule, so agreement holds by construction rather
than by assertion.
"""
import argparse, datetime, importlib.util, json, os, re, sys, time

STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_RECEIPT_STATE_DIR", "~/.claude/state/manager-receipts")
)
EXIT_SKIPPED = 11

# The steps whose artifacts the receipt must name. Order is the print order.
STEPS = ("needs-you", "sweep", "watcher")

# OSC 8 hyperlink: ESC ] 8 ; params ; URI ST. The payload `--write-payload` stores is
# already link-free, but a frame captured from the screen carries them, and they are
# zero-width — so they must never decide a shape question either way.
OSC8_RE = re.compile(r"\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07)")

_LIVENESS = None


def liveness():
    """`manager-liveness.py`, imported — the ONE definition of the staleness rule.

    ⚠️ **Imported, never restated.** This file used to carry its own copy of the
    period arithmetic and claimed the two agreed; they did not (see the module
    docstring). Delegating makes the receipt's limit literally the liveness limit —
    `observed_period()`, `SLACK`, the branch-prefixed record names and the
    pre-split fallback all come from the module that also writes them, so there is
    nothing left to drift.

    Hyphenated filename, so this goes through importlib — the same shape
    `check-spawn-ledger.py` and `attention-ask.py` use for their sibling scripts.
    """
    global _LIVENESS
    if _LIVENESS is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manager-liveness.py")
        spec = importlib.util.spec_from_file_location("manager_liveness", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIVENESS = mod
    return _LIVENESS


def liveness_slug(s: str) -> str:
    """The liveness record's name for this receipt's slug.

    ⚠️ **The receipt slug is the bare base; the record usually is not.** Since the
    goal/topic split `manager-liveness.py` writes `goal--<base>` / `topic--<base>`,
    and this file's `slug()` never carried a branch. Reading the bare base therefore
    found no history at all for every subject whose record had been prefixed —
    measured 2026-10-21: **4 of the 9** live receipts, each silently falling back to
    the reported interval while a full `.arms` history sat under the prefixed name.
    The receipt's `slug()` and liveness's `base_slug()` are the same transform, which
    is what makes the prefixed names derivable from `s` alone.

    Falls back to `s` — a pre-split record, or none — so `resolve()` can still apply
    its own legacy lookup rather than this function inventing a name.
    """
    ml = liveness()
    for branch in ml.BRANCHES:
        name = f"{branch}--{s}"
        if os.path.exists(ml.path(name, "cadence")) or os.path.exists(ml.path(name, "arms")):
            return name
    return s


def slug(topic: str) -> str:
    # Same slug as sweep-gate.py, stop-probe.py and manager-liveness.py.
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")


def path(s: str) -> str:
    return os.path.join(STATE_DIR, f"{s}.json")


def write_atomic(p: str, text: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, p)


def read_receipt(s: str) -> dict:
    try:
        with open(path(s), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def start(topic: str) -> int:
    s = slug(topic)
    # Fields are CLEARED, not carried: a receipt that survived its own round would
    # let a skipped round inherit the previous round's artifacts and pass.
    write_atomic(path(s), json.dumps({"topic": topic, "round_started_at": time.time(), "fields": {}}) + "\n")
    print(f"started {s}")
    return 0


def record(topic: str, step: str, artifact: str) -> int:
    s = slug(topic)
    d = read_receipt(s)
    if not d.get("round_started_at"):
        # Refuse rather than invent a window: a receipt with no start cannot say
        # which round an artifact belongs to, which is the whole question.
        print(f"no round in progress for {s} — run --start first", file=sys.stderr)
        return 2
    fields = d.setdefault("fields", {})
    fields[step] = artifact
    write_atomic(path(s), json.dumps(d) + "\n")
    print(f"recorded {s} {step}")
    return 0


def window(s: str, now: float) -> tuple:
    """(started_at, limit, basis) for the round in progress.

    The limit is `manager-liveness.py`'s own, computed by its own function: the
    observed period plus its SLACK, or the declared interval plus the same SLACK
    when there is too little history. Nothing here re-derives a statistic or a
    multiple — the basis string NAMES the rule that ran (`1x+180`) so the printed
    line, not just the number, says which one applied.

    `now` is accepted rather than read so the boundary is testable without racing
    the clock; `check()` passes its own.
    """
    d = read_receipt(s)
    started = d.get("round_started_at")
    if not started:
        return None, None, "no receipt"
    ml = liveness()
    live = liveness_slug(s)
    period = ml.observed_period(live)
    if period:
        return started, period + ml.SLACK, f"period={int(period)}s(observed,1x+{ml.SLACK})"
    try:
        with open(ml.resolve(live, "cadence")[0], encoding="utf-8") as fh:
            interval = int(fh.read().strip())
    except (OSError, ValueError):
        interval = 900
    return started, interval + ml.SLACK, f"period={interval}s(reported,1x+{ml.SLACK})"


def in_window(mtime: float, started: float, now: float, limit: float) -> bool:
    """Produced THIS round, and not so long ago that the round itself lapsed."""
    return started <= mtime <= now and (now - started) <= limit


def needs_you_shape(text: str) -> bool:
    """The producer's own section shape, not merely non-empty.

    `who-needs-me.py --section needs-you` prints `Needs you (<n>)` and its rows, or
    `Nothing needs you.` when the section is empty. Anything else is a stub.
    """
    body = text.strip()
    return body.startswith("Needs you (") or "Nothing needs you." in body


def _frame_top(line: str) -> bool:
    return line.startswith("┌") and line.endswith("┐")


def _frame_bottom(line: str) -> bool:
    return line.startswith("└") and line.endswith("┘")


def _liveness_marker(line: str) -> bool:
    """The tick's leading liveness marker: timestamp + `✓` + topic + counts.

    `docs/sweep-output.md` § Print the full table on every tick mandates it as
    "always the first line of the tick's output" and calls it "part of the frame,
    not decoration". The `✓` is the glyph that spec names as the marker's
    distinguishing shape, so it is the test here rather than "any leading line" —
    a stub that prepends arbitrary prose must still fail.
    """
    return "✓" in line


def sweep_shape(text: str) -> bool:
    """The sweep reader's payload: the RENDERED box-table `--write-payload` stores.

    `--write-payload` (`manager-predispatch.py`) does `table = sys.stdin.read()` and
    writes what it was handed, so `--save` can read the table back and the operator
    can see it. It is not JSON, and never was: the predicate this replaced required
    `"header" in json.loads(text)`, which no artifact the producer writes can
    satisfy, so every round reported `skipped: sweep` whether or not the leg ran
    (measured 2026-10-18, round 19:49–20:05 on goal manager session `beac5fd0`).

    **Two payload shapes are real, and both are accepted.** The tick's output is a
    liveness marker followed by the frame — "The marker leads, and the box hangs two
    spaces under it" (`docs/sweep-output.md`) — so a payload captured from a tick
    starts with the marker, while one captured from the frame alone does not.
    Measured 2026-10-18/19 on consecutive rounds twenty minutes apart: same code,
    same install, one payload of each shape. A predicate requiring the frame at
    line 0 flaps between green and red on rounds identical in substance, which is
    the defect this predicate exists to close. The marker is accepted only as
    line 0 and only with a real frame starting on the next line; stripping it is
    deliberately the producer's business, not this predicate's.

    The frame's own contract is `box-table.py:render()`: a top rule opening `┌` and
    closing `┐`, `│`-framed rows, and a bottom rule opening `└` and closing `┘`.
    Both rules are required — a truncated file loses its bottom rule, and an empty
    file has neither — so the shape still refuses the stub this check exists to
    catch, while accepting every real payload.
    """
    lines = [ln.strip() for ln in OSC8_RE.sub("", text).splitlines() if ln.strip()]
    if len(lines) >= 2 and _liveness_marker(lines[0]) and _frame_top(lines[1]):
        lines = lines[1:]
    if len(lines) < 4:
        return False
    if not _frame_top(lines[0]):
        return False
    if not _frame_bottom(lines[-1]):
        return False
    return any(ln.startswith("│") and ln.endswith("│") for ln in lines[1:-1])


def record_ts(value):
    """A record's `ts` as epoch seconds, or None when it is not a timestamp.

    `manager-attention-watch.py:1445` writes `time.strftime("%Y-%m-%dT%H:%M:%S%z")`
    — an ISO-8601 string with a numeric offset. The predicate this replaced accepted
    only `isinstance(ts, (int, float))`, so it matched **0** of the 526 records in
    the measured log and every round reported `skipped: watcher` even while the arm
    demonstrably polled inside the window.

    A bare number stays accepted: it costs nothing and keeps a hand-written or older
    log readable. Anything else — a torn line's junk, `"soon"`, a missing key —
    returns None and is ignored, so one bad record never masks a good one.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return None


def watcher_shape(artifact: str, started: float, now: float) -> bool:
    """The tracked-set arm's newest `events.jsonl` record, inside the round window.

    Read from the RECORD's own `ts`, never the file's mtime: the arm appends on every
    poll, so the file's mtime only says it was touched, while the newest record's ts
    says the arm was still polling during this round.

    The lower bound is floored to the producer's own resolution: `ts` carries whole
    seconds, so a record appended in the same wall-clock second the round opened
    would otherwise compare as *before* a fractional `round_started_at` and be
    rejected by up to one second. Flooring widens the window by at most that second,
    which is the honest cost of a one-second timestamp.
    """
    started = int(started)
    newest = None
    try:
        with open(artifact, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = record_ts(rec.get("ts"))
                if ts is not None:
                    newest = ts if newest is None else max(newest, ts)
    except OSError:
        return False
    return newest is not None and started <= newest <= now


def check(topic: str, now: float = None) -> int:
    now = time.time() if now is None else now
    s = slug(topic)
    d = read_receipt(s)
    started, limit, basis = window(s, now)

    if started is None:
        print(f"skipped: receipt — no receipt for {s}; the round did not write one")
        return EXIT_SKIPPED
    if (now - started) > limit:
        # The round's own window lapsed: every field is unjudgeable, so name the
        # window rather than each step — a stale receipt fails all of them at once.
        print(f"skipped: receipt — round started {int(now - started)}s ago, limit {int(limit)}s {basis}")
        return EXIT_SKIPPED

    fields = d.get("fields") or {}
    skipped = []
    for step in STEPS:
        artifact = fields.get(step)
        if not artifact:
            skipped.append((step, "no artifact recorded"))
            continue
        if step == "watcher":
            if not watcher_shape(artifact, started, now):
                skipped.append((step, f"no events.jsonl record inside the round window ({artifact})"))
            continue
        try:
            mtime = os.path.getmtime(artifact)
        except OSError:
            skipped.append((step, f"missing ({artifact})"))
            continue
        if not in_window(mtime, started, now, limit):
            skipped.append((step, f"not produced this round ({artifact})"))
            continue
        try:
            with open(artifact, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            skipped.append((step, f"unreadable ({artifact})"))
            continue
        ok = needs_you_shape(text) if step == "needs-you" else sweep_shape(text)
        if not ok:
            skipped.append((step, f"not the producer's shape ({artifact})"))

    for step, why in skipped:
        print(f"skipped: {step} — {why}")
    return EXIT_SKIPPED if skipped else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-round receipt for manager loops.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", action="store_true", help="begin a round (manager, tick head)")
    mode.add_argument("--record", action="store_true", help="record a step's artifact")
    mode.add_argument("--check", action="store_true", help="validate the round (manager, tail)")
    ap.add_argument("--topic")
    ap.add_argument("--step", choices=STEPS)
    ap.add_argument("--path", help="the artifact the step produced")
    a = ap.parse_args()
    if a.check:
        return check(a.topic) if a.topic else (ap.error("--check requires --topic") or 2)
    if not a.topic:
        ap.error("--topic is required with --start/--record")
    if a.start:
        return start(a.topic)
    if not a.step or not a.path:
        ap.error("--record needs --step and --path")
    return record(a.topic, a.step, a.path)


if __name__ == "__main__":
    sys.exit(main())
