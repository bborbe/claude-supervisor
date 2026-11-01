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
produced; the tail re-arm reads the receipt back and prints
`INCOMPLETE ROUND — skipped: <step> — <why>` for every field that does not resolve
to an artifact produced THIS round.

Three verbs, one receipt per subject. `--subject` is an alias for `--topic` (the fleet
tier's name for it), and `--branch` selects the tier: `""` (the default) is the goal or
topic round, `fleet` is the fleet round, whose slug carries the branch exactly as
`manager-liveness.py` writes it (`fleet--fleet-manager`).

  --start  --topic T            begin a round: writes `round_started_at` and clears
                                every field, so a receipt can only ever describe the
                                round that wrote it. Run at the tick's head.
  --record --topic T --step S --path P
                                step S produced artifact P. Steps are `needs-you`
                                (the file the round persisted
                                `who-needs-me.py --section needs-you` to), `sweep`
                                (the sweep reader's payload), `watcher` (the
                                tracked-set arm's `events.jsonl`) and `readings`
                                (the `<slug>.readings` document
                                `scripts/round-readings.py` writes).
                                The **fleet tier** (`--branch fleet`) owes a different
                                pair instead: `re-arm` (the `fleet--<subject>.arms` ring
                                `manager-liveness.py --arm` appends to) and `digest` (the
                                sweep reader's rendered payload under
                                `~/.claude/state/fleet-handback/`).
  --check  --topic T            validate against the round window. Prints
                                `INCOMPLETE ROUND — skipped: <step> — <why>` per
                                unresolved field and exits 11 if any; prints nothing
                                and exits 0 on a full round. Run at the tail re-arm,
                                validating the round that just ran.

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
its docstring asserted that the two checks were aligned, and the assertion was false in
three ways: the multiple was `2x` against liveness's `1x`; the slack was 60 s against
liveness's 180 s; and the statistic was the UPPER median (`gaps[len//2]`) against the
true `median()`, which differs on every even gap count — so even the number being
multiplied was not the same one. (Liveness's period is the p90 capped at `PERIOD_CAP` x
that median, not the median itself.) Measured 2026-10-21 over the retained `.arms`
history, the two limits differed on 46 of 46 subjects, and the band between them was
the defect: a manager liveness had already called stale could still have its round's
fields judged current. The receipt now imports the rule, so agreement holds by
construction rather than by assertion.
"""
import argparse, datetime, importlib.util, json, math, os, re, sys, time

STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_RECEIPT_STATE_DIR", "~/.claude/state/manager-receipts")
)
EXIT_SKIPPED = 11
# The debt's own code, distinct from EXIT_SKIPPED so a leg chained `--debt && <leg>`
# is stopped by a debt it owes rather than by a verdict it cannot act on.
EXIT_DEBT_OPEN = 12

# The positive marker a skipped round prints.
#
# It is POSITIVE on purpose. The rounds measured on 2026-10-31 that skipped their
# digest already omitted `Snapshot written` from their render, so an absence-based
# signal would have passed on exactly the rounds this one exists to catch. This
# string cannot exist at the pre-change revision, which is what makes it evidence.
SENTINEL = "INCOMPLETE ROUND"

# The steps whose artifacts the receipt must name. Order is the print order.
#
# `readings` is step 2's own output — `scripts/round-readings.py` writes the four
# verdict families to `<slug>.readings`. It is the step the 12:17 round skipped while
# still rendering a complete-looking table, so it is the one this receipt most needs
# to name.
STEPS = ("needs-you", "sweep", "watcher", "readings")

# The fleet round's own two load-bearing steps, in their own tuple rather than appended
# to STEPS. A goal or topic round owes no digest, so a shared tuple would make every such
# round report `skipped: digest` forever — a permanent false red on a healthy round, which
# is the failure mode this whole check exists to avoid. The fleet round is the only tier
# that owes both a per-round arm and a per-round digest.
FLEET_STEPS = ("re-arm", "digest")
ALL_STEPS = STEPS + FLEET_STEPS

# The branches this receipt knows: `""` (the goal/topic tier, which carries no prefix) and
# `fleet`. `manager-liveness.py:110-112` validates its own set before building the same
# slug, and this module mirrors the validation so an unknown branch cannot interpolate into
# the state path.
#
# ⚠️ The two sets are deliberately NOT identical, and the asymmetry has a consequence worth
# stating rather than discovering: a goal or topic round passes no branch here, so its
# receipt is keyed on the bare `<base>`, while `manager-liveness.py` writes
# `goal--<base>` / `topic--<base>`. `liveness_slug()` bridges that by resolving the record
# from an existing prefixed twin, so the observed period DOES engage for those tiers and the
# reported interval is the fallback only. (Until 2026-10-21 it did not, and every goal and
# topic round silently used the reported 900 s. The alternative remedy — having those call
# sites pass `--branch` — is unnecessary while the record itself names the branch, and would
# leave a caller that forgets it reading the wrong file again.)
BRANCHES = ("", "fleet")


def steps_for(branch: str) -> tuple:
    """The steps a round of `branch` owes. No branch is the goal/topic tier."""
    return FLEET_STEPS if branch == "fleet" else STEPS

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

    ⚠️ **A goal or topic receipt slug is the bare base; the record is not.** Since the
    goal/topic split `manager-liveness.py` writes `goal--<base>` / `topic--<base>`, while
    a goal or topic round passes no `--branch` here and so keys its receipt on the bare
    base. Reading the bare base therefore found no history at all for every subject whose
    record had been prefixed — measured 2026-10-21: **4 of the 9** live receipts, each
    silently falling back to the reported interval while a full `.arms` history sat under
    the prefixed name. The receipt's `base_slug()` and liveness's are the same transform,
    which is what makes the prefixed names derivable from `s` alone.

    ⚠️ **An already-prefixed slug is returned unchanged.** A fleet round (or any caller
    that passed `--branch`) has `slug()` put the branch on the front itself, so `s` is
    already `fleet--<base>` — the correct liveness name. Probing `goal--fleet--<base>`
    first would find nothing and fall through to the right answer by luck; say it
    explicitly instead, so the fallback stays a fallback.

    Falls back to `s` — a pre-split record, or none — so `resolve()` can still apply its
    own legacy lookup rather than this function inventing a name.

    ⚠️ **Residual: a name that is both a goal and a topic resolves to `goal--`.** The
    probe walks `ml.BRANCHES` in order and takes the first record that exists, so a
    subject present under both prefixes gets `goal--<base>` on the topic tier as well.
    The arm side uses the same order (`detect_branch`), so the two agree with each
    other — which is the property this file needs — but `manager-liveness.py --check`
    classifies both records, so "the receipt's limit is the liveness limit" holds per
    record, not per subject. Named rather than left to be discovered; it is the same
    slug ambiguity the branch prefix exists to narrow, and it is not introduced here.
    """
    ml = liveness()
    if any(s.startswith(f"{b}--") for b in ml.BRANCHES if b):
        return s
    for branch in ml.BRANCHES:
        name = f"{branch}--{s}"
        if os.path.exists(ml.path(name, "cadence")) or os.path.exists(ml.path(name, "arms")):
            return name
    return s


def base_slug(subject: str) -> str:
    # Same base transform as sweep-gate.py, stop-probe.py and manager-liveness.py.
    return re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")


def slug(subject: str, branch: str = "") -> str:
    """The state slug, carrying the BRANCH exactly as `manager-liveness.py:101` does.

    A fleet round writes its liveness under `fleet--fleet-manager`, so a receipt keyed
    on the bare `fleet-manager` reads a file the arm step never wrote and reports
    `skipped: re-arm` on a round that armed perfectly. Every goal/topic caller passes no
    branch and keeps the unprefixed slug it has always used.
    """
    if branch not in BRANCHES:
        raise ValueError(f"branch must be one of {BRANCHES}, got {branch!r}")
    base = base_slug(subject)
    return f"{branch}--{base}" if branch else base


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


# --- the debt -----------------------------------------------------------------
#
# The debt lives BESIDE the receipt, never in it. `start()` clears `fields`, so a
# debt stored there would be wiped by the very round that must pay it — the carry
# would be unimplementable, and a round could skip the same step forever while every
# individual receipt read clean. A separate file is what makes the skip outlive the
# round that caused it, and it is what lets `--debt` fire on a round that never ran
# its tail at all (measured: two of the five 2026-10-31 skips never ran `--check`).


def debt_path(s: str) -> str:
    return os.path.join(STATE_DIR, f"{s}.debt")


def abandoned_path(s: str) -> str:
    return os.path.join(STATE_DIR, f"{s}.abandoned")


def read_debt(s: str) -> list:
    try:
        with open(debt_path(s), encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return []


def write_debt(s: str, steps) -> None:
    """Replace the debt with `steps`; an empty set removes the file entirely."""
    steps = list(dict.fromkeys(steps))
    if not steps:
        try:
            os.unlink(debt_path(s))
        except OSError:
            pass
        return
    write_atomic(debt_path(s), "\n".join(steps) + "\n")


def read_abandoned(s: str) -> list:
    """`[(epoch, step), …]` — when each step was paid, and which."""
    try:
        with open(abandoned_path(s), encoding="utf-8") as fh:
            out = []
            for line in fh:
                parts = line.split()
                if len(parts) != 2:
                    continue
                try:
                    out.append((float(parts[0]), parts[1]))
                except ValueError:
                    continue
            return out
    except OSError:
        return []


def start(topic: str, branch: str = "") -> int:
    s = slug(topic, branch)
    # Fields are CLEARED, not carried: a receipt that survived its own round would
    # let a skipped round inherit the previous round's artifacts and pass.
    write_atomic(path(s), json.dumps({"topic": topic, "round_started_at": time.time(), "fields": {}}) + "\n")
    print(f"started {s}")
    return 0


def record(topic: str, branch: str, step: str, artifact: str) -> int:
    s = slug(topic, branch)
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


READINGS_VERDICTS = {
    "liveness": {"LIVE", "ABSENT", "UNKNOWN"},
    "in_flight": {"in-flight", "quiet", "unread"},
    "feed": {"gated", "clear", "unread"},
    "external_wait": {"external", "none", "unread"},
}


def readings_shape(text: str) -> bool:
    """The readings document `scripts/round-readings.py` writes — not merely JSON.

    The producer's own shape is asserted rather than "it parses": the defect this check
    exists to catch is a round that wrote *something* plausible and did none of the
    work. A `{}`, an all-`UNKNOWN` row and a hand-written stub all parse as JSON, so
    the predicate requires the four verdict keys on **every** row and each value inside
    its declared vocabulary — the two facts a fabricated file is least likely to carry
    consistently.

    An empty `rows` map is accepted: a tracked set with no resolvable task is a real
    (if alarming) outcome, and the reader refuses on its own terms. It is not accepted
    as a *substitute* for a populated one, because nothing here can tell the two apart
    — that is the honest bound, and `--verify` plus the reader's own refusal are the
    checks that do.
    """
    try:
        doc = json.loads(text)
    except ValueError:
        return False
    if not isinstance(doc, dict):
        return False
    if not isinstance(doc.get("recorded_at"), (int, float)):
        return False
    if doc.get("feed_state") not in ("read", "unread"):
        return False
    rows = doc.get("rows")
    if not isinstance(rows, dict):
        return False
    for row in rows.values():
        if not isinstance(row, dict):
            return False
        for key, allowed in READINGS_VERDICTS.items():
            if row.get(key) not in allowed:
                return False
    return True


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


def rearm_shape(artifact: str, started: float, now: float) -> bool:
    """An arm epoch INSIDE the round window — the step, not the file's existence.

    `manager-liveness.py --arm` appends an epoch to `<slug>.arms`, so the ring is the
    arm step's own record. But a ring inherited from an earlier round, or one emptied by
    `manager-liveness.py:345` at the first arm after a stand-down, would pass an
    existence test and prove nothing about THIS round. The newest epoch must fall inside
    it — the same rule `watcher_shape` applies to its own log.
    """
    try:
        with open(artifact, encoding="utf-8") as fh:
            vals = [float(x) for x in fh.read().split()]
    except (OSError, ValueError):
        return False
    # The ring stores WHOLE seconds, so both bounds are floored to that resolution: an arm
    # stamped in the same wall-clock second as `round_started_at` can round *below* it, and
    # one stamped in the same second the check runs can read as *after* a fractional `now`.
    # Flooring both is the honest cost of a one-second timestamp — the same flooring
    # `watcher_shape` documents for its own log. The residual is that an arm up to ~1s
    # before the window can pass; unreachable in practice, since a previous round's arm is
    # minutes earlier and the round opens its window before it arms.
    return any(int(started) <= v <= int(now) + 1 for v in vals)


def digest_shape(text: str) -> bool:
    """The sweep reader's rendered digest, by its own opening line.

    `agents/fleet-sweep-reader.md` renders `DIGEST <round timestamp> · <N> sessions ·
    snapshot written: <swept_at>` as the file's first line. A truncated file loses it
    and a stub written by anything else never had it, so these three ASCII markers are
    the shape — not "it parses", and not "it is non-empty".
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return (line.startswith("DIGEST ")
                and "sessions" in line
                and "snapshot written:" in line)
    return False


def check(topic: str, branch: str = "", now: float = None) -> int:
    now = time.time() if now is None else now
    s = slug(topic, branch)
    d = read_receipt(s)
    started, limit, basis = window(s, now)

    if started is None:
        print(f"{SENTINEL} — skipped: receipt — no receipt for {s}; the round did not write one")
        return EXIT_SKIPPED
    if (now - started) > limit:
        # The round's own window lapsed: every field is unjudgeable, so name the
        # window rather than each step — a stale receipt fails all of them at once.
        # ⚠️ `math.ceil`, not `int`. `observed_period()` interpolates a p90, so the limit
        # can be fractional; truncating it lets a round that lapsed by under a second
        # print `started 1180s ago, limit 1180s` — a line that reads as NOT lapsed while
        # the round was refused. Rounding up can overstate by <1 s, which is the safe
        # direction: the printed limit is never below the enforced one.
        print(f"{SENTINEL} — skipped: receipt — round started {int(now - started)}s ago, limit {math.ceil(limit)}s {basis}")
        return EXIT_SKIPPED

    # A paid step is satisfied only for the round it was paid in. The record is
    # timestamped for that reason: an abandonment that survived every later round
    # would silently excuse the step forever, which is the same permanent-green
    # failure a debt stored in `fields` would have had in the other direction.
    # `int(started)`, not `started`: the record stores a float, but a payment made in
    # the same wall-clock second the round opened can read fractionally below a
    # fractional `round_started_at`. Flooring the start is the same one-second cost
    # `rearm_shape` documents, and the alternative is a recovery that silently fails
    # whenever the operator is fast.
    abandoned = {step for ts, step in read_abandoned(s) if ts >= int(started)}
    fields = d.get("fields") or {}
    skipped = []
    for step in steps_for(branch):
        if step in abandoned:
            continue
        artifact = fields.get(step)
        if not artifact:
            skipped.append((step, "no artifact recorded"))
            continue
        if step == "watcher":
            if not watcher_shape(artifact, started, now):
                skipped.append((step, f"no events.jsonl record inside the round window ({artifact})"))
            continue
        if step == "re-arm":
            if not rearm_shape(artifact, started, now):
                skipped.append((step, f"no arm epoch inside the round window ({artifact})"))
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
        if step == "needs-you":
            ok = needs_you_shape(text)
        elif step == "readings":
            ok = readings_shape(text)
        elif step == "digest":
            ok = digest_shape(text)
        else:
            ok = sweep_shape(text)
        if not ok:
            skipped.append((step, f"not the producer's shape ({artifact})"))

    # The debt is rewritten on EVERY check, not appended to: a round that did its
    # work pays the previous round's debt simply by passing, and a round that skips
    # again re-opens it. `--start` deliberately does not touch this file.
    #
    # FLEET ONLY. The goal/topic tier shares `--check`, but nothing there chains
    # `--debt` — only `drive.md`, `keepalive.md` and `flagged-open.md` do — so
    # writing the file for that tier would create state no command reads, and an
    # operator would find a `<base>.debt` whose gate nothing consults. The sentinel
    # still fires there; only the carry is fleet-scoped, which is what it was
    # designed for.
    if branch == "fleet":
        write_debt(s, [step for step, _ in skipped])

    for step, why in skipped:
        print(f"{SENTINEL} — skipped: {step} — {why}")
    return EXIT_SKIPPED if skipped else 0


def debt(topic: str, branch: str = "") -> int:
    """Print the open debt; exit EXIT_DEBT_OPEN while any step is unpaid.

    A read of the file, never of the tail's output — which is the whole point. The
    rounds that skipped and never ran `--check` (13:11 and 13:27 on 2026-10-31) left
    no tail line to read, and this still fires on them.
    """
    s = slug(topic, branch)
    steps = read_debt(s)
    for step in steps:
        print(f"debt: {step}")
    return EXIT_DEBT_OPEN if steps else 0


def abandon(topic: str, branch: str, step: str) -> int:
    """Pay ONE skipped step: drop it from the debt, record when it was paid.

    This is the task's single recovery verb. It is deliberately not a way to silence
    the arm: blocking the arm is what makes `fleet-keeper.py` (launchd, 60 s) see a
    false lapse and resurrect the round, so the recovery has to be an explicit,
    recorded statement that this step will not be done — never a refusal to re-arm.
    """
    s = slug(topic, branch)
    steps = read_debt(s)
    if step not in steps:
        print(f"no open debt for {step} on {s}", file=sys.stderr)
        return 2
    write_debt(s, [x for x in steps if x != step])
    # `write_debt`'s empty branch unlinks and swallows an OSError, while its
    # non-empty branch goes through `write_atomic` and raises. Reporting success on
    # the silent path would leave `--debt` exiting 12 while this verb claimed the
    # step was paid — so read the result back rather than trusting the write.
    if step in read_debt(s):
        print(f"could not pay {step} on {s}: the debt file is unchanged", file=sys.stderr)
        return 2
    with open(abandoned_path(s), "a", encoding="utf-8") as fh:
        fh.write("%.3f %s\n" % (time.time(), step))
    print(f"abandoned {s} {step}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Per-round receipt for manager loops.",
        epilog=(
            "A skipped round prints `INCOMPLETE ROUND — skipped: <step> — <why>` and "
            "exits 11; on the fleet tier the skipped steps are also written to "
            "`<slug>.debt` beside the receipt. `--debt` exits 12 while that file is "
            "non-empty, so a leg chained `--debt && <leg>` cannot run ahead of the "
            "step that is owed. `--start` does not clear it: the debt outlives the "
            "round that caused it. `--abandon --step <step>` is the one recovery verb "
            "— it pays a step the round genuinely will not do and records it in "
            "`<slug>.abandoned`, which `--check` honours for the round it was paid in "
            "and no later one, so re-running that same round's `--check` exits 0 while "
            "an old payment cannot excuse the step forever."
        ),
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", action="store_true", help="begin a round (manager, tick head)")
    mode.add_argument("--record", action="store_true", help="record a step's artifact")
    mode.add_argument("--check", action="store_true", help="validate the round (manager, tail)")
    mode.add_argument("--debt", action="store_true",
                      help="print the open debt; exits 12 while any step is unpaid, 0 when clear")
    mode.add_argument("--abandon", action="store_true",
                      help="pay ONE skipped step (needs --step): removes it from the debt "
                           "and records it in <slug>.abandoned, which --check treats as satisfied")
    ap.add_argument("--topic")
    ap.add_argument("--subject", help="alias for --topic; the fleet tier's name for it")
    ap.add_argument("--branch", default="",
                    help="the session tier; `fleet` selects the fleet round's steps")
    ap.add_argument("--step", choices=ALL_STEPS)
    ap.add_argument("--path", help="the artifact the step produced")
    a = ap.parse_args()
    subject = a.subject or a.topic
    # Validate here as well as in `slug()`: the CLI owes an argparse usage error, not a
    # traceback, and the tier check keeps a fleet step off a goal receipt where
    # `steps_for()` would never look for it.
    if a.branch not in BRANCHES:
        ap.error(f"--branch must be one of {BRANCHES}, got {a.branch!r}")
    if a.step and a.step not in steps_for(a.branch):
        ap.error(f"--step {a.step!r} is not a {a.branch or 'goal/topic'} step")
    if not subject:
        ap.error("--topic or --subject is required with every mode")
    if a.check:
        return check(subject, a.branch)
    if a.debt:
        return debt(subject, a.branch)
    if a.abandon:
        if not a.step:
            ap.error("--abandon needs --step")
        return abandon(subject, a.branch, a.step)
    if a.start:
        return start(subject, a.branch)
    if not a.step or not a.path:
        ap.error("--record needs --step and --path")
    return record(subject, a.branch, a.step, a.path)


if __name__ == "__main__":
    sys.exit(main())
