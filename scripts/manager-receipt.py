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

The spend-per-change readout — the round's own `tokens spent ÷ cells moved` line:

  --tokens  --topic T           print the round's output-token spend: the parent
                                transcript PLUS `<session-id>/subagents/*.jsonl`,
                                windowed `round_started_at` → now and deduped by
                                `message.id`. The parent alone omits both legs.
  --snapshot --topic T          copy the tier's sweep payload (`manager-predispatch/
                                <slug>.table` on the goal tier, `manager-receipts/
                                <slug>.sweep` on the topic tier) to `<payload>.prev` —
                                run at the sweep leg's HEAD, BEFORE the leg rewrites it.
  --count   --topic T           print the moved-row count: goal rows whose
                                `Status`/`Phase`/`Met` changed since `.prev`;
                                `n/a (baseline)` on the first tick, never `0`.
                                Run AFTER the sweep and BEFORE the print.

Two more verbs carry the topic round's keep-alive, a CONDITIONAL step owed only for a
verdict the round actually read:

  --record-verdict --topic T --goal G --vault V --verdict resume|restart --session ID
                                written by `topic-keepalive.py --receipt T` as it
                                prints the verdict, never by hand.
  --record-outcome --topic T --goal G --outcome O
                                what the round did: `executed`, `refused:<token>` or
                                `deferred:busy-target`. `executed` resolves only
                                against the work's own record inside the round window
                                — the `--record-resume` stamp for a resume, a
                                `kind=spawn` exit record for a restart — so writing
                                the word alone does not clear the step.

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
import argparse, datetime, glob, importlib.util, json, math, os, re, sys, time

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
STEPS = ("needs-you", "sweep", "watcher", "readings", "drive")

# The steps a TOPIC round owes, and the reason this tuple exists at all.
#
# `steps_for()` used to return `STEPS` for every non-fleet round, so one list judged a
# goal round and a topic round alike. The two are not alike: a topic round has no drive
# row at all (`commands/topic-drive.md` row 5 is *Keep alive*), so adding `drive` to
# `STEPS` alone would print `skipped: drive` on every topic round forever — a permanent
# false red, the same failure mode the `FLEET_STEPS` comment below names for the fleet
# tier.
#
# `sweep` stays IN this tuple even though the topic sweep leg records nothing yet. The
# topic round does sweep every tick (`commands/topic-drive.md` row 3), so dropping the
# step would let a round that skips its sweep report complete — this check's own defect,
# one tier over. The missing record call is the topic sweep's to add, not this tuple's
# to excuse.
TOPIC_STEPS = ("needs-you", "readings", "sweep", "watcher")

# The fleet round's own two load-bearing steps, in their own tuple rather than appended
# to STEPS. A goal or topic round owes no digest, so a shared tuple would make every such
# round report `skipped: digest` forever — a permanent false red on a healthy round, which
# is the failure mode this whole check exists to avoid. The fleet round is the only tier
# that owes both a per-round arm and a per-round digest.
FLEET_STEPS = ("re-arm", "digest")
ALL_STEPS = STEPS + FLEET_STEPS

# The tiers a round can belong to. This is NOT `BRANCHES`: `branch` is interpolated into
# the state slug by `slug()`, so a third branch value would re-key every receipt already
# on disk from `<base>` to `topic--<base>` and orphan it. The tier therefore selects the
# step tuple only, and never reaches `slug()`.
TIERS = ("goal", "topic")

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


def steps_for(branch: str, tier: str = "goal") -> tuple:
    """The steps a round of `branch`/`tier` owes.

    No branch is the goal/topic tier, and `tier` separates those two: a goal round owes
    `STEPS` (which carries `drive`), a topic round owes `TOPIC_STEPS` (which does not).
    `tier` is deliberately NOT a `branch` value — see `TIERS`.
    """
    if branch == "fleet":
        return FLEET_STEPS
    return TOPIC_STEPS if tier == "topic" else STEPS


# --- the keep-alive verdicts --------------------------------------------------
#
# Measured 2026-11-08 on a topic manager (session eab58671, ticks 15:55 and 16:15):
# `topic-keepalive.py` answered `resume` for three goal managers on two consecutive ticks
# and the round executed none, attributing the skip first to `spawn.maxWorkerSoft` and then
# to the per-topic N = 3 — neither of which gates a resume. Every field above resolved, so
# `--check` reported a full round while three managers stayed down.
#
# ⚠️ **A CONDITIONAL step, never a member of `STEPS` or `TOPIC_STEPS`.** A round owes a
# keep-alive outcome only for a verdict it actually read: a goal round reads none, and a
# topic round whose goals all read `jump` / `leave` reads none either. A fixed tuple entry
# would print `skipped: keepalive` on those rounds forever — the permanent false red this
# file refuses elsewhere. The verdict list is what makes the debt: no verdict, nothing owed.
#
# ⚠️ **The verdict is written by the probe, the outcome by the round.** `topic-keepalive.py
# --receipt <topic>` appends the entry the moment it prints `resume` / `restart`, so a
# round cannot omit a verdict by forgetting to record it; the round then records what it
# did with `--record-outcome`.
KEEPALIVE = "keepalive"
OWING_VERDICTS = ("resume", "restart")

# `refused:<token>` must name a refusal the verdict's OWN command can emit — a free-form
# token, or one borrowed from another path, would let a round label an unexecuted resume
# and pass. So the vocabulary is per verdict:
#
#   resume  → `/supervisor:open --goal`, Step 2B item 3 → Step 3. That path's refusals:
#             `⌛ waiting-approval` (item 0), the registry read's "cannot tell" (item 1),
#             `start-rate.mjs` exit 1 (Step 3.1) and `resolve-launcher.py` non-zero (3.1).
#   restart → `/supervisor:restart`: `restart-worker.py`'s first-line tokens (its module
#             docstring, plus `environment-fault` and `hold-unreadable`, which it returns
#             when the hold store cannot be read), and `restart-precheck.py`'s
#             `dirty-worktree`.
#
# ⚠️ **No cap token on either path, by design.** The resume path never reaches Step 1.5's
# Gate 3, so `spawn.maxWorkerSoft` and the per-topic N cannot refuse it — and they are
# exactly the excuses the 2026-11-08 round gave. A vocabulary carrying them would let that
# round pass.
#
# `busy-target` is a restart deferral (`deferred:busy-target`), never a refusal and never
# valid on a resume: the round is told to wait a tick for a mid-turn session, and a resume
# targets a session that is ABSENT, so it cannot be mid-turn.
REFUSAL_TOKENS = {
    "resume": ("waiting-approval", "cannot-tell", "start-rate", "launcher-refused"),
    "restart": ("unknown-session-id", "ambiguous-pid", "headless-target", "held-session",
                "hold-unreadable", "role-undetermined", "stale-load-path",
                "environment-fault", "dirty-worktree"),
}
DEFER_TOKENS = {"resume": (), "restart": ("busy-target",)}


def outcome_valid(outcome: str, verdict: str) -> bool:
    """`executed`, or a refusal / deferral token the verdict's own command emits."""
    if outcome == "executed":
        return True
    kind, _, token = (outcome or "").partition(":")
    if kind == "refused":
        return token in REFUSAL_TOKENS.get(verdict, ())
    if kind == "deferred":
        return token in DEFER_TOKENS.get(verdict, ())
    return False


def _same_session(recorded: str, seen: str) -> bool:
    """Equal, or one an 8+-char prefix of the other — `/supervisor:restart` takes a prefix."""
    if not recorded or not seen:
        return False
    if recorded == seen:
        return True
    short, long_ = sorted((recorded, seen), key=len)
    return len(short) >= 8 and long_.startswith(short)


_KEEPALIVE_MOD = None


def keepalive_module():
    """`topic-keepalive.py`, imported — the ONE definition of where a resume is recorded."""
    global _KEEPALIVE_MOD
    if _KEEPALIVE_MOD is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topic-keepalive.py")
        spec = importlib.util.spec_from_file_location("topic_keepalive", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _KEEPALIVE_MOD = mod
    return _KEEPALIVE_MOD


def session_exit_dir() -> str:
    # Same override and default as `scripts/session-exit-record.py:state_dir()`.
    return os.path.expanduser(
        os.environ.get("SUPERVISOR_SESSION_EXIT_DIR") or "~/.claude/state/session-exit"
    )


def resume_recorded(vault: str, goal: str, session: str, started: float, now: float) -> bool:
    """A `--record-resume` stamp for this goal AND the verdict's session, in the round window.

    `topic-keepalive.py --record-resume` writes it only once `session-liveness.py` reads the
    resumed id `LIVE`, so the stamp is the resume's own proof — not the round's say-so.
    ⚠️ **Matched on the session too.** A stamp alone proves only that SOME live id was
    handed over — the round's own session would do — so the stamp must name the id the
    verdict said was `ABSENT`.
    """
    if not vault:
        return False
    km = keepalive_module()
    rows = km.load_sessions(km.resume_path(vault, goal, "goal"))
    return any(int(started) <= t <= now + 1 and _same_session(session, s) for t, s in rows)


def restart_spawned(session: str, started: float, now: float) -> bool:
    """A `kind=spawn` line for `session` in the exit-record store, inside the round window.

    `restart-worker.py` wires `session-exit-record.py append --kind spawn --session <id>`
    into the relaunched pane before the launcher runs, so the line exists only when the
    restart actually spawned the session. Timestamps are UTC, second resolution.
    """
    if not session:
        return False
    directory = session_exit_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    for name in names:
        if not (name.startswith("pane-") and name.endswith(".log")):
            continue
        p = os.path.join(directory, name)
        try:
            # The store is append-only, so a log last written before the round opened
            # cannot hold an in-window line — skip it rather than read the whole history.
            if os.path.getmtime(p) < int(started):
                continue
            with open(p, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            fields = line.rstrip("\n").split("\t")
            if "kind=spawn" not in fields:
                continue
            seen = next((f[len("session="):] for f in fields if f.startswith("session=")), "")
            if not _same_session(session, seen):
                continue
            try:
                ts = datetime.datetime.strptime(fields[0], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except ValueError:
                continue
            if int(started) <= ts <= now + 1:
                return True
    return False


def keepalive_skips(d: dict, started: float, now: float) -> list:
    """`[(KEEPALIVE, why), …]` — one per verdict the round read and did not account for."""
    out = []
    for entry in d.get(KEEPALIVE) or []:
        if not isinstance(entry, dict):
            continue
        goal = entry.get("goal") or "?"
        verdict = entry.get("verdict") or "?"
        outcome = entry.get("outcome") or ""
        if not outcome:
            out.append((KEEPALIVE, f"{goal}: `{verdict}` verdict has no outcome"))
            continue
        if not outcome_valid(outcome, verdict):
            out.append((KEEPALIVE, f"{goal}: `{outcome}` is not a known outcome for `{verdict}`"))
            continue
        if outcome != "executed":
            continue
        if verdict == "resume":
            ok = resume_recorded(entry.get("vault") or "", goal, entry.get("session") or "",
                                 started, now)
            proof = "no --record-resume stamp for its session"
        else:
            ok = restart_spawned(entry.get("session") or "", started, now)
            proof = "no kind=spawn exit record for its session"
        if not ok:
            out.append((KEEPALIVE, f"{goal}: `{verdict}` marked executed but {proof} inside the round window"))
    return out

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
    """The open steps, or `[]` when there is genuinely no debt file.

    ⚠️ **Only `FileNotFoundError` means "no debt".** Every other `OSError` — an
    unreadable file, a permission-denied state dir — propagates, because this is
    the file a `--debt && <leg>` gate is chained on and swallowing it would be a
    fail-open on the one check built to fail closed: the leg would run while a
    debt exists. The caller decides how to report it; `--debt` exits non-zero.
    """
    try:
        with open(debt_path(s), encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except FileNotFoundError:
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
                    ts = float(parts[0])
                except ValueError:
                    continue
                # ⚠️ `float()` accepts `inf`, and `ts >= int(started)` has no upper
                # bound — one tampered `inf` line would excuse the step on every later
                # round, the permanent green the round-scoping exists to prevent.
                # Nothing here writes a non-finite value, so this is a tamper guard.
                if not math.isfinite(ts):
                    continue
                out.append((ts, parts[1]))
            return out
    except OSError:
        return []


def gated_path(s: str) -> str:
    return os.path.join(STATE_DIR, f"{s}.gated")


def read_gated(s: str) -> list:
    """`[(epoch, step), …]` — which steps the frozen-tree predicate suppressed, and when.

    A **different claim** from `.abandoned`, and kept in its own file for that reason: an
    abandonment is a recovery (a human or manager decided a leg was owed and waived), while
    a gate is the predicate deciding the leg was never owed this tick. Collapsing the two
    would make "the round skipped a leg by design" indistinguishable from "the round
    skipped a leg it owed", which is exactly the distinction the criteria grade on.

    Same tamper guards as `read_abandoned`: a non-finite epoch would excuse the step on
    every later round, the permanent green the round-scoping exists to prevent.
    """
    try:
        with open(gated_path(s), encoding="utf-8") as fh:
            out = []
            for line in fh:
                parts = line.split()
                if len(parts) != 2:
                    continue
                try:
                    ts = float(parts[0])
                except ValueError:
                    continue
                if not math.isfinite(ts):
                    continue
                out.append((ts, parts[1]))
            return out
    except OSError:
        return []


_BOX_TABLE = None


def box_table():
    """`box-table.py`, imported — the ONE renderer, for the fallback box.

    `docs/sweep-output.md` § Rendering names this script as the only renderer
    (*"Never hand-draw the box"*), and it is also that file's documented fallback for
    a sweep that cannot classify. Imported rather than re-implemented for the reason
    its own docstring gives: display width, VS16 and OSC 8 are handled there, and a
    second implementation drifts on every one of them.
    """
    global _BOX_TABLE
    if _BOX_TABLE is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "box-table.py")
        spec = importlib.util.spec_from_file_location("box_table", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BOX_TABLE = mod
    return _BOX_TABLE


#: The goal tier's frame — `Topic / Goal / Task | Session | Status | Phase | Met`
#: (`docs/sweep-output.md` § The frame). ⚠️ **NOT the topic frame:** the topic tier
#: renders `Goal | Status | Phase | Met | Manager`. Recognising only one of them is
#: the defect the sibling task found in `_sweep_rows`, where a goal-tier `--count`
#: read `0` on every tick.
GOAL_FRAME_HEADER = ("Topic / Goal / Task", "Session", "Status", "Phase", "Met")
GOAL_FRAME_WIDTHS = (42, 10, 34, 9, 17)
#: The topic tier's frame, measured off the payload `manager-receipts/<slug>.sweep`
#: itself (2026-11-12): content widths 42 · 20 · 10 · 9 · 16.
TOPIC_FRAME_HEADER = ("Goal", "Status", "Phase", "Met", "Manager")
TOPIC_FRAME_WIDTHS = (42, 20, 10, 9, 16)


def tracked_names(subject: str) -> list[str]:
    """The tracked set the round resolved, names only — `[]` when it never ran.

    Read from `manager-predispatch.tracked_path()` (the caller's own input file), so
    the fallback names the rows this round is actually watching rather than a set
    re-derived here. Names only, by design: a gated tick read no statuses, and
    inventing them would print a table claiming reads it never made.
    """
    try:
        with open(predispatch().tracked_path(subject), encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return []


def _fallback_box(subject: str, tier: str, names: list[str]) -> str:
    """The full frame built from the tracked set, for a tick with no payload to replay.

    One row per tracked name with every other cell `—` (this frame's value for
    *nothing recorded*), and on the goal tier the subject's own row first with its
    rows indented beneath it — the shape a real goal payload renders.
    """
    if tier == "topic":
        header, widths = TOPIC_FRAME_HEADER, TOPIC_FRAME_WIDTHS
        rows = [[n, "—", "—", "—", "—"] for n in names]
    else:
        header, widths = GOAL_FRAME_HEADER, GOAL_FRAME_WIDTHS
        rows = [[subject, "—", "—", "—", "—"]]
        rows += [["   " + n, "—", "—", "—", "—"] for n in names]
    return box_table().render(
        {"header": list(header), "rows": rows, "widths": list(widths)}, link=False
    )


def _payload_body(lines: list[str]) -> list[str]:
    """The payload's box, with the payload's OWN leading marker dropped.

    ⚠️ **Never `lines[1:]`.** The two tiers' payloads differ: the topic payload opens
    with a marker line (`16:41 ✓ … — 53 goals`), while the goal payload — written by
    `--write-payload` — opens with the box's own TOP BORDER. Dropping the first line
    unconditionally removed that border, so a gated goal tick printed a box with no
    top edge: the frame the operator's rulings require, one cell short. The marker is
    whatever precedes the first `┌`, so the split holds for both shapes.
    """
    i = 0
    while i < len(lines) and not lines[i].lstrip().startswith("┌"):
        i += 1
    return lines[i:]


def render_cached(topic: str, branch: str = "", now: float = None, tier: str = "goal") -> int:
    """Print the cached sweep payload with a FRESH marker for this tick.

    The operator's rulings require the table on every tick, including a gated one where
    the reader never ran. The cached payload's own first line carries the tick that
    produced it, so replaying it verbatim prints a header claiming a time this tick did
    not run — and that makes a dead loop and a gated one look identical, which is the
    ambiguity the marker exists to remove. The first line is therefore replaced, never
    kept.

    ⚠️ **A tick with no payload to replay still prints a full box**, built from the
    tracked set by `_fallback_box`, with the marker naming the absence. Before this,
    the renderer exited 1 and printed nothing, so a subject that had never swept — or
    whose payload path disagreed with its producer's — printed no table at all, which
    is the failure `Manager Session` § Sweep output records from the first
    implementation. An empty box would read as *the sweep ran and had no rows*; the
    explicit `no cached table yet` is what keeps "no table yet" visible.
    """
    s = slug(topic, branch)
    path = payload_file(topic, tier)
    stamp = time.strftime("%H:%M", time.localtime(time.time() if now is None else now))
    try:
        with open(path, encoding="utf-8") as fh:
            body = _payload_body(fh.read().splitlines())
    except OSError:
        body = []
        why = f"no cached table yet at {path}"
    else:
        why = f"the cached payload for {s} carries no box"

    if not body:
        names = tracked_names(topic)
        print(f"{stamp} ✓ {topic} — sweep gated this tick, {why} — "
              f"{len(names)} row(s) from the tracked set, statuses not read")
        print(_fallback_box(topic, tier, names))
        return 0

    # The marker names the gate explicitly: a reader comparing ticks must be able to
    # tell "the table is here because nothing changed" from "the table is here and the
    # round ran". Never claim the goal/slot counts — this tick did not read them.
    print(f"{stamp} ✓ {topic} — sweep gated this tick, table from cache")
    for line in body:
        print(line)
    return 0


# --- the spend-per-change readout ---------------------------------------------
#
# The goal this serves — "a manager round costs what it changes" — needs the round to
# print `tokens spent ÷ cells moved`. Two independent producers feed that line, and
# neither may certify itself:
#
#   * `--tokens` sums `usage.output_tokens` over the round's PARENT transcript plus its
#     `subagents/*.jsonl`. The two legs are subagent dispatches, so the parent alone
#     omits both of them. One assistant message spans several records that repeat
#     `usage` verbatim, so the sum is deduped by `message.id` (measured 2.33x inflation
#     on 2026-11-11, 2.06x on an independent 40-transcript probe 2026-11-12).
#   * `--snapshot` copies the sweep payload before the sweep leg rewrites it, and
#     `--count` diffs the new payload against that copy afterwards. The order is
#     load-bearing: snapshot → sweep → diff. A diff taken after re-snapshotting the
#     same payload reads `0` forever, so the denominator could never fail.
#
# The residual limit, stated rather than implied: the diff removes the sweep leg's
# self-REPORT (its `N change` first line), not its self-RENDERING. A leg that renders
# no moved cell still yields `0`. The metric's trust boundary is the leg's render.

PROJECTS_ROOT = os.path.expanduser(
    os.environ.get("MANAGER_RECEIPT_PROJECTS_ROOT", "~/.claude/projects")
)


_PREDISPATCH = None


def predispatch():
    """`manager-predispatch.py`, imported — the ONE definition of the payload path.

    ⚠️ **Imported, never restated.** The sweep reader's `--write-payload` resolves its
    destination through `payload_path()`, so a copy of that rule here would drift from
    the file the reader actually writes. Measured 2026-11-12: `render_cached()` opened
    `~/.claude/state/manager-receipts/<slug>.sweep`, and no goal-tier round writes that
    file — the payload lands at `~/.claude/state/manager-predispatch/<slug>.table`. The
    counter and the render must read the producer's own path, not a remembered one.
    """
    global _PREDISPATCH
    if _PREDISPATCH is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manager-predispatch.py")
        spec = importlib.util.spec_from_file_location("manager_predispatch", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PREDISPATCH = mod
    return _PREDISPATCH


def payload_file(subject: str, tier: str = "goal") -> str:
    """The sweep payload the round at this tier actually produces.

    Goal tier: `manager-predispatch.payload_path` — `<predispatch>/<slug>.table`, the
    file `--write-payload` writes. Topic tier: `~/.claude/state/manager-receipts/
    <slug>.sweep`, the path `commands/steps/topic-sweep.md` names for its reader. The
    two are different files by design; a single hardcoded name is what made the
    counter read a path nothing wrote.
    """
    if tier == "topic":
        return os.path.join(STATE_DIR, f"{slug(subject)}.sweep")
    return predispatch().payload_path(subject)


def prev_file(subject: str, tier: str = "goal") -> str:
    """The snapshot the counter diffs against — beside the payload it snapshots."""
    return payload_file(subject, tier) + ".prev"


def _sweep_rows(text: str):
    """`{row name: (status, phase, met)}`, or `None` when no header is recognised.

    **Two frames are real, and both must parse.** The topic tier renders
    `Goal | Status | Phase | Met | Manager`; the goal tier renders
    `Topic / Goal / Task | Session | Status | Phase | Met`. Recognising only the
    first made a goal-tier `--count` read `0` on every tick — the denominator could
    never fire, which is the defect the metric exists to expose. Columns are found
    by NAME, so a frame with a different first column still works.

    The header line and the `Manager` / `Session` column are excluded: both churn on
    ticks where no tracked goal moved (`STALE → OK` flips are liveness, not change),
    and the metric is the table's goal cells.

    `None` is a SHAPE FAILURE, never "no rows". A caller that read it as `0 moved`
    would restore the silent zero; `count_moved` refuses instead.
    """
    rows = {}
    header = None
    for ln in text.splitlines():
        stripped = OSC8_RE.sub("", ln).strip()
        if not (stripped.startswith("│") and stripped.endswith("│")):
            continue
        cells = [c.strip() for c in stripped.strip("│").split("│")]
        if header is None:
            if "Status" in cells and "Phase" in cells and "Met" in cells:
                header = cells
            continue
        if len(cells) < len(header):
            continue
        rows[cells[0]] = tuple(cells[header.index(name)]
                               for name in ("Status", "Phase", "Met"))
    return rows if header is not None else None


def snapshot(topic: str, branch: str = "", tier: str = "goal") -> int:
    """Copy the sweep payload to `<payload>.prev` BEFORE the sweep leg rewrites it.

    Called at the sweep leg's head. The copy is what `--count` diffs against after the
    sweep; taking it after the sweep would copy the new payload onto itself and make
    the denominator `0` forever.
    """
    s = slug(topic, branch)
    try:
        with open(payload_file(topic, tier), encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        # No prior payload is the first tick, not an error: `--count` will print
        # `n/a (baseline)` because `.prev` does not exist.
        print(f"no sweep payload to snapshot for {s}: {e}", file=sys.stderr)
        return 0
    write_atomic(prev_file(topic, tier), text)
    print(f"snapshot {s}")
    return 0


def count_moved(topic: str, branch: str = "", tier: str = "goal") -> int:
    """Print the moved-row count: goal rows whose Status/Phase/Met changed.

    Prints `n/a (baseline)` when no `.prev` exists (the first tick), never `0` — a
    baseline is not a measurement of no change. Otherwise prints the integer, so a
    no-change tick prints `0`.
    """
    s = slug(topic, branch)
    prev = prev_file(topic, tier)
    if not os.path.exists(prev):
        print("n/a (baseline)")
        return 0
    try:
        with open(prev, encoding="utf-8") as fh:
            before = _sweep_rows(fh.read())
        with open(payload_file(topic, tier), encoding="utf-8") as fh:
            after = _sweep_rows(fh.read())
    except OSError as e:
        print(f"cannot read the sweep payload for {s}: {e}", file=sys.stderr)
        return 2
    if before is None or after is None:
        # A frame we cannot parse must not read as `0 moved` — that is the silent
        # zero this counter exists to avoid, and it would let the criterion pass
        # forever on a shape change.
        print(f"cannot parse the sweep frame for {s}: no Status/Phase/Met header — "
              f"refusing to report 0 moved", file=sys.stderr)
        return 2
    moved = sum(1 for goal, cells in after.items()
                if goal in before and before[goal] != cells)
    print(moved)
    return 0


def _record_epoch(rec: dict):
    ts = rec.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _transcript_files(session: str) -> list:
    """Every transcript file for `session`: the parent plus each subagent file.

    The parent is `<projects-root>/*/<session>.jsonl`; a session's subagent files live
    in the sibling directory `<session>/subagents/*.jsonl`. Both are searched across
    every project dir because a round may run from any cwd and the session id is
    globally unique.
    """
    out = []
    for project in sorted(glob.glob(os.path.join(PROJECTS_ROOT, "*"))):
        parent = os.path.join(project, f"{session}.jsonl")
        if os.path.isfile(parent):
            out.append(parent)
        sub = os.path.join(project, session, "subagents")
        if os.path.isdir(sub):
            out.extend(sorted(glob.glob(os.path.join(sub, "*.jsonl"))))
    return out


def spend_tokens(topic: str, branch: str = "", session: str = "", now: float = None) -> int:
    """Print the round's output-token spend: parent transcript PLUS subagent files.

    Windowed `round_started_at` → now and deduped by `message.id`. The parent alone
    omits both legs (they are subagent dispatches), and one assistant message spans
    several records that repeat `usage` verbatim, so both halves are mandatory.
    """
    s = slug(topic, branch)
    d = read_receipt(s)
    started = d.get("round_started_at")
    if not started:
        print(f"no round in progress for {s} — run --start first", file=sys.stderr)
        return 2
    session = session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session:
        print("--tokens needs a session: pass --session or set $CLAUDE_CODE_SESSION_ID",
              file=sys.stderr)
        return 2
    now = time.time() if now is None else now
    files = _transcript_files(session)
    if not files:
        # A missing transcript must not read as "the round spent nothing" — that is a
        # silent zero, and the numerator would then never fail.
        print(f"no transcript files for session {session} — refusing to report 0 tokens",
              file=sys.stderr)
        return 2
    seen = set()
    total = 0
    for fn in files:
        try:
            with open(fn, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = _record_epoch(rec)
                    if ts is None or ts < started or ts > now:
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    mid = msg.get("id")
                    if not mid or mid in seen:
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    out = usage.get("output_tokens")
                    if not isinstance(out, int) or isinstance(out, bool):
                        continue
                    seen.add(mid)
                    total += out
        except OSError:
            continue
    print(total)
    return 0


def gate(topic: str, branch: str, step: str) -> int:
    """Record that the frozen-tree predicate suppressed `step` for this round.

    ⚠️ **Only the predicate may call this.** A leg recorded here is excused from the
    round's `--check` with no artifact, so writing it for a leg that was owed would let a
    round report complete while skipping work — the failure the receipt exists to catch.
    The caller states the predicate's verdict; this function does not re-derive it.
    """
    s = slug(topic, branch)
    with open(gated_path(s), "a", encoding="utf-8") as fh:
        fh.write(f"{time.time():.3f} {step}\n")
    print(f"gated {s}: {step}")
    return 0


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


def record_verdict(topic: str, goal: str, vault: str, verdict: str, session: str) -> int:
    """Append one keep-alive verdict the round now owes an outcome for.

    Called by `topic-keepalive.py --receipt` at the moment it prints the verdict. Lives in
    the receipt rather than the round's `fields`, under its own key, because a round can
    read several verdicts and every one of them is owed separately.
    """
    if verdict not in OWING_VERDICTS:
        print(f"verdict must be one of {OWING_VERDICTS}, got {verdict!r}", file=sys.stderr)
        return 2
    if not session:
        # `executed` is proved against this id; an entry without one could never be paid.
        print("--record-verdict needs the verdict's --session id", file=sys.stderr)
        return 2
    s = slug(topic)
    d = read_receipt(s)
    if not d.get("round_started_at"):
        print(f"no round in progress for {s} — run --start first", file=sys.stderr)
        return 2
    entries = d.setdefault(KEEPALIVE, [])
    # A probe re-run in the same round reads the same verdict again; one owed entry per
    # (goal, verdict, session) while it is open, or one outcome would leave a twin red.
    for e in entries:
        if (isinstance(e, dict) and not e.get("outcome")
                and base_slug(e.get("goal") or "") == base_slug(goal)
                and e.get("verdict") == verdict and e.get("session") == session):
            print(f"already owed {s} {KEEPALIVE} {goal} {verdict}")
            return 0
    entries.append({
        "goal": goal, "vault": vault, "verdict": verdict, "session": session,
        "read_at": time.time(), "outcome": "",
    })
    write_atomic(path(s), json.dumps(d) + "\n")
    print(f"recorded {s} {KEEPALIVE} {goal} {verdict}")
    return 0


def record_outcome(topic: str, goal: str, outcome: str, verdict: str = "") -> int:
    """Account for the newest open verdict on `goal`: `executed`, `refused:<t>`, `deferred:<t>`.

    Refuses an outcome outside that verdict's vocabulary here, at the write, so the round
    learns of a bad token while it can still correct it — `--check` would only report it at
    the tail. The goal is matched on its slug, as the probe matches goal pages
    case-insensitively.
    """
    s = slug(topic)
    d = read_receipt(s)
    if not d.get("round_started_at"):
        print(f"no round in progress for {s} — run --start first", file=sys.stderr)
        return 2
    open_entries = [e for e in d.get(KEEPALIVE) or []
                    if isinstance(e, dict) and not e.get("outcome")
                    and base_slug(e.get("goal") or "") == base_slug(goal)
                    and (not verdict or e.get("verdict") == verdict)]
    if not open_entries:
        which = f" `{verdict}`" if verdict else ""
        print(f"no open{which} keep-alive verdict for {goal!r} on {s}", file=sys.stderr)
        return 2
    entry = open_entries[-1]
    verdict = entry.get("verdict") or ""
    if not outcome_valid(outcome, verdict):
        refusals = ", ".join(REFUSAL_TOKENS.get(verdict, ())) or "none"
        defers = ", ".join(DEFER_TOKENS.get(verdict, ())) or "none"
        print(f"unknown outcome {outcome!r} for a `{verdict}` verdict: use `executed`, "
              f"`refused:<token>` ({refusals}) or `deferred:<token>` ({defers})",
              file=sys.stderr)
        return 2
    entry["outcome"] = outcome
    write_atomic(path(s), json.dumps(d) + "\n")
    print(f"recorded {s} {KEEPALIVE} {goal} {outcome}")
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
    "in_flight": {"in-flight", "parked", "quiet", "unread"},
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
    # `partial` is a read that RAN but could not attribute every gate it saw: the
    # rows it did resolve keep their verdict and only the rest are `unread`. It is a
    # completed reading, not a skipped step — rejecting it here flagged every such
    # round `INCOMPLETE ROUND — skipped: readings` and recorded false debt for work
    # the round actually did.
    if doc.get("feed_state") not in ("read", "partial", "unread"):
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


def drive_shape(text: str) -> bool:
    """The drive leg's own report: the action lines it hands back.

    `commands/steps/goal-drive.md` requires the manager to "reproduce them under the
    table, including `Not resumed` and `Escalated`", so a real drive report carries at
    least one of the leg's action headers. The test is therefore a POSITIVE token test
    rather than non-emptiness: a `touch`ed file, an arbitrary string and a round-written
    stub must all fail, exactly as they do for every other step.

    ⚠️ The predicate is deliberately loose about WHICH header, because a leg that found
    nothing still reports — it says so under `To open` / `To resume` with no rows. A
    predicate requiring a non-empty row set would report `skipped: drive` on a healthy
    round that had nothing to do, which is the permanent-false-red this whole file
    exists to avoid.
    """
    body = text.strip()
    if not body:
        return False
    markers = ("To open", "To resume", "Not resumed", "Escalated", "Nudged", "Reaped")
    return any(marker in body for marker in markers)


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


def _tail_epochs_beside(artifact: str) -> set:
    """The `<slug>.tail` store beside a `<slug>.arms` ring, as epoch keys.

    `manager-liveness.py`'s `arm()` writes the ring as TWO columns and keeps the tail
    marker in this sibling store — a session still running a pre-upgrade copy rebuilds
    the ring from two-tuples and would erase a third-column marker on every arm it
    takes. `parse_arm_lines` still recognises the retired column, so a reader that
    honours only the column sees `is_tail` False on every ring this build writes and
    silently counts tail beats as head beats.
    """
    if not artifact.endswith(".arms"):
        return set()
    try:
        with open(artifact[: -len(".arms")] + ".tail", encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    except OSError:
        return set()


def rearm_shape(artifact: str, started: float, now: float) -> bool:
    """An arm epoch INSIDE the round window — the step, not the file's existence.

    `manager-liveness.py --arm` appends an epoch to `<slug>.arms`, so the ring is the
    arm step's own record. But a ring inherited from an earlier round, or one emptied by
    `manager-liveness.py:345` at the first arm after a stand-down, would pass an
    existence test and prove nothing about THIS round. The newest epoch must fall inside
    it — the same rule `watcher_shape` applies to its own log.

    ⚠️ Parsed through `manager-liveness.parse_arm_lines`, never `fh.read().split()`.
    Each line carries `<epoch> <interval>` now, and a flat split puts the interval on
    the epoch list — where a small number lands inside a narrow window and reports an
    arm that never happened. This file asks only *when*, so it wants the epochs.

    ⚠️ **HEAD beats only — a tail beat is a different write and must not pay this step.**
    Since the round grew a closing beat, a round writes an epoch at its head AND one at
    its tail, and both land in the same ring inside the same window. Counting them alike
    made the tail beat able to CLEAR a head arm that never ran: `--check` correctly reports
    `skipped: re-arm` when the round's head is missing, and then `output-shape.md` item
    13's row-20 `--debt` re-evaluation re-reads the ring, finds the tail epoch inside the
    window, and pays the debt — so the round that skipped its head arm reads complete one
    step later. That is the precise false green this predicate exists to prevent, and it
    is why the tail marker has to be honoured here rather than only in `observed_period`.
    """
    try:
        with open(artifact, encoding="utf-8") as fh:
            pairs = liveness().parse_arm_lines(fh.read())
    except OSError:
        return False
    # Both marker shapes have to be honoured. The store is the one this build writes;
    # the third column survives only as read-only legacy on rings from before the move.
    # Checking the column alone is a no-op against every current ring.
    tail = _tail_epochs_beside(artifact)
    vals = [epoch for epoch, _, column_tail in pairs
            if not column_tail and f"{epoch:.0f}" not in tail]
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


def _skips(s: str, branch: str, d: dict, started: float, limit: float, now: float,
           tier: str = "goal") -> list:
    """`[(step, why), …]` for the round in progress. Pure — writes nothing.

    Shared by `--check` (the tail's verdict) and `--debt` (the legs' gate), because
    both must judge the SAME round. A `--debt` that read the file alone would gate on
    whatever the last check happened to leave, which is the previous round's verdict.
    """
    # A paid step is satisfied only for the round it was paid in. The record is
    # timestamped for that reason: an abandonment that survived every later round
    # would silently excuse the step forever, which is the same permanent-green
    # failure a debt stored in `fields` would have had in the other direction.
    # `int(started)`, not `started`: the record stores a float, but a payment made in
    # the same wall-clock second the round opened can read fractionally below a
    # fractional `round_started_at`. Flooring the start is the same one-second cost
    # `rearm_shape` documents, and the alternative is a recovery that silently fails
    # whenever the operator is fast.
    # ⚠️ Bounded on BOTH sides. The lower bound is the round's own start; the upper
    # bound keeps a record dated in the future from excusing the step until
    # wall-clock caught up — a forward clock jump when `abandon()` stamped it, or a
    # hand-edited line. `math.isfinite` in `read_abandoned` rejects inf/nan; this
    # rejects a finite future.
    #
    # ⚠️ `now + 1`, not `now`, and the slack is deliberate: it is the SAME
    # one-second tolerance `int(started)` already grants at the lower bound, so the
    # two ends of the window are equally forgiving of a sub-second skew between when
    # a payment was stamped and when the round it belongs to opened or was read.
    # `abandon()` writes `"%.3f"`, so a payment made immediately before a read can
    # land fractionally after that read's `now`; without the slack that recovery
    # fails whenever the operator is fast. A record dated a minute or a day ahead
    # still fails, which is what the guard is for.
    abandoned = {step for ts, step in read_abandoned(s) if int(started) <= ts <= now + 1}
    # A gated step is excused on the same round-scoped window as a paid one: the gate
    # applies to the round that took the verdict, never to a later round that must
    # re-evaluate the predicate for itself.
    gated = {step for ts, step in read_gated(s) if int(started) <= ts <= now + 1}
    fields = d.get("fields") or {}
    skipped = []
    for step in steps_for(branch, tier):
        if step in abandoned or step in gated:
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
        elif step == "drive":
            ok = drive_shape(text)
        else:
            ok = sweep_shape(text)
        if not ok:
            skipped.append((step, f"not the producer's shape ({artifact})"))
    # The fleet tier reads no goal verdicts; only the goal/topic receipt can carry them.
    if branch != "fleet":
        skipped.extend(keepalive_skips(d, started, now))
    return skipped


def _write_debt_for(s: str, branch: str, skipped: list) -> bool:
    """Rewrite the debt from `skipped`. Returns False when the write failed.

    Rewritten on EVERY evaluation, never appended to: a round that did its work pays
    the previous round's debt simply by passing, and a round that skips again re-opens
    it. `--start` deliberately does not touch this file.

    FLEET ONLY. The goal/topic tier shares `--check`, but nothing there chains
    `--debt` — only `drive.md`, `keepalive.md` and `flagged-open.md` do — so writing
    the file for that tier would create state no command reads. The sentinel still
    fires there; only the carry is fleet-scoped, which is what it was designed for.
    """
    if branch != "fleet":
        return True
    try:
        write_debt(s, [step for step, _ in skipped])
        return True
    except OSError as e:
        # A read-only or full state dir must not turn a verdict into a traceback: the
        # caller reads the exit code. The debt is left as it was — stale, not cleared.
        print(f"could not write {debt_path(s)}: {e}", file=sys.stderr)
        return False


def check(topic: str, branch: str = "", now: float = None, tier: str = "goal") -> int:
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

    skipped = _skips(s, branch, d, started, limit, now, tier)
    _write_debt_for(s, branch, skipped)

    for step, why in skipped:
        print(f"{SENTINEL} — skipped: {step} — {why}")
    return EXIT_SKIPPED if skipped else 0


def debt(topic: str, branch: str = "", now: float = None, only=None) -> int:
    """Print the open debt; exit EXIT_DEBT_OPEN while any step is unpaid.

    ⚠️ **`only` scopes the GATE, never the record.** A leg passes the steps it actually
    consumes, and only those can stop it; the debt file is still rewritten with every
    step the round owes, so `--check` and the other gated legs see exactly what they
    saw before. Measured 2026-11-05 on the Fleet Manager: 2 of 25 keepalive gate
    calls exited 12 on `debt: digest` because the round reached row 5b while the
    sweep reader was still running in the background (06:57:39; digest recorded at
    07:05:35). The keepalive procedures read `park-event.py`, `park-nudge.py`,
    `worker-keepalive.py` and `topic-keepalive.py`, never the digest, so a digest
    owed is no reason to skip them.

    ⚠️ **It RE-EVALUATES the round in progress, it does not merely read the file.**
    The gate runs at router rows 5 / 5b / 5c, and the tail's `--check` is row 19 — so
    a file-only read would gate every round on the *previous* round's verdict. That is
    wrong in both directions: a round that has already swept its digest would still be
    blocked by a debt it has paid, and the round that actually skipped would not be
    blocked at all. Judging the current receipt fixes both, and it is what makes
    `--debt && <leg>` a prevention rather than a one-round penalty.

    The file is still what the verdict is written to, so a round that never runs the
    tail but does run its legs still records its own skip. When the round cannot be
    judged at all — no receipt, or its window lapsed — the carried file is the only
    evidence there is, and it is what gets read.
    """
    s = slug(topic, branch)
    now = time.time() if now is None else now
    started, limit, _basis = window(s, now)
    if started is not None and (now - started) <= limit:
        steps = [step for step, _ in _skips(s, branch, read_receipt(s), started, limit, now)]
        if not _write_debt_for(s, branch, [(step, "") for step in steps]):
            return 2
    else:
        try:
            steps = read_debt(s)
        except OSError as e:
            # Unreadable, not empty. Exiting 0 here would run a leg while a debt is
            # owed, which is the exact failure the gate exists to stop.
            print(f"cannot read {debt_path(s)}: {e}", file=sys.stderr)
            return 2
    if only:
        steps = [step for step in steps if step in only]
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
    try:
        steps = read_debt(s)
    except OSError as e:
        print(f"cannot read {debt_path(s)}: {e}", file=sys.stderr)
        return 2
    if step not in steps:
        print(f"no open debt for {step} on {s}", file=sys.stderr)
        return 2
    write_debt(s, [x for x in steps if x != step])
    # `write_debt`'s empty branch unlinks and swallows an OSError, while its
    # non-empty branch goes through `write_atomic` and raises. Reporting success on
    # the silent path would leave `--debt` exiting 12 while this verb claimed the
    # step was paid — so read the result back rather than trusting the write.
    try:
        still_owed = step in read_debt(s)
    except OSError as e:
        print(f"cannot re-read {debt_path(s)}: {e}", file=sys.stderr)
        return 2
    if still_owed:
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
                           "and records it in <slug>.abandoned, which --check honours for "
                           "the round it was paid in and no later one")
    mode.add_argument("--gate", action="store_true",
                      help="record that the frozen-tree predicate suppressed ONE step "
                           "(needs --step): writes <slug>.gated, which --check honours for "
                           "THIS round only, so the round reports complete instead of "
                           "carrying debt for a leg it never owed. A different claim from "
                           "--abandon: that one pays an owed leg, this one records that the "
                           "leg was not owed. Only the predicate may call it")
    mode.add_argument("--render-cached", action="store_true",
                      help="print the cached sweep payload with a FRESH timestamped marker "
                           "for this tick — the gated tick's table, per the operator's "
                           "2026-09-15 and 2026-09-20 rulings. The payload's own first line "
                           "is stale and is replaced, never replayed. With no payload to "
                           "replay it still prints the marker plus a full box built from the "
                           "tracked set, naming the absence — never a silent empty tick")
    mode.add_argument("--tokens", action="store_true",
                      help="print the round's output-token spend: parent transcript plus "
                           "subagents/*.jsonl, windowed round_started_at → now, deduped by "
                           "message.id")
    mode.add_argument("--snapshot", action="store_true",
                      help="copy the tier's sweep payload to <payload>.prev — run at the "
                           "sweep leg's HEAD, before the leg rewrites the payload")
    mode.add_argument("--count", action="store_true",
                      help="print the moved-row count: goal rows whose Status/Phase/Met "
                           "changed since <payload>.prev; `n/a (baseline)` on the first tick")
    mode.add_argument("--record-verdict", action="store_true",
                      help="topic tier: a `resume`/`restart` keep-alive verdict the round now "
                           "owes an outcome for (needs --goal --vault --verdict --session); "
                           "topic-keepalive.py --receipt writes it")
    mode.add_argument("--record-outcome", action="store_true",
                      help="topic tier: what the round did with --goal's open verdict "
                           "(needs --goal --outcome)")
    ap.add_argument("--goal", help="with --record-verdict/--record-outcome: the goal")
    ap.add_argument("--vault", help="with --record-verdict: the vault root the probe read")
    ap.add_argument("--verdict", choices=OWING_VERDICTS, help="with --record-verdict")
    ap.add_argument("--session", default="",
                    help="with --record-verdict: the recorded id; with --tokens: the "
                         "transcript's session (defaults to $CLAUDE_CODE_SESSION_ID)")
    ap.add_argument("--outcome",
                    help="with --record-outcome: `executed`, `refused:<token>` or "
                         "`deferred:busy-target`")
    ap.add_argument("--topic")
    ap.add_argument("--subject", help="alias for --topic; the fleet tier's name for it")
    ap.add_argument("--branch", default="",
                    help="the session tier; `fleet` selects the fleet round's steps")
    ap.add_argument("--tier", default="goal", choices=TIERS,
                    help="which round owes which steps: `goal` (the default, owes `drive`) "
                         "or `topic` (owes no drive leg). Selects the step tuple only and "
                         "never reaches the state slug, so it cannot orphan a receipt")
    ap.add_argument("--step", choices=ALL_STEPS)
    ap.add_argument("--path", help="the artifact the step produced")
    ap.add_argument("--only", action="append", choices=FLEET_STEPS, metavar="STEP",
                    help="with --debt: gate only on this step (repeatable). The debt "
                         "file still records every owed step; only the exit code and "
                         "the printed lines are scoped")
    a = ap.parse_args()
    subject = a.subject or a.topic
    # Validate here as well as in `slug()`: the CLI owes an argparse usage error, not a
    # traceback, and the tier check keeps a fleet step off a goal receipt where
    # `steps_for()` would never look for it.
    if a.branch not in BRANCHES:
        ap.error(f"--branch must be one of {BRANCHES}, got {a.branch!r}")
    # `--tier` selects the goal/topic step tuple and nothing else, so pairing it with the
    # fleet branch is a silent no-op — the fleet round is the only tier that owes neither
    # tuple. Refuse rather than accept a flag that changes nothing: a caller that passes
    # it believes it selected something.
    if a.tier != "goal" and a.branch == "fleet":
        ap.error("--tier selects the goal/topic tuple and means nothing with --branch fleet")
    if a.step and a.step not in steps_for(a.branch, a.tier):
        ap.error(f"--step {a.step!r} is not a {a.branch or a.tier} step")
    if not subject:
        ap.error("--topic or --subject is required with every mode")
    if a.only and not a.debt:
        ap.error("--only scopes the --debt gate and means nothing with any other mode")
    if a.check:
        return check(subject, a.branch, tier=a.tier)
    if a.render_cached:
        return render_cached(subject, a.branch, tier=a.tier)
    if a.tokens:
        return spend_tokens(subject, a.branch, session=a.session or "")
    if a.snapshot:
        return snapshot(subject, a.branch, tier=a.tier)
    if a.count:
        return count_moved(subject, a.branch, tier=a.tier)
    # `--debt` and `--abandon` are fleet-only by construction: `check()` writes the
    # file only for `--branch fleet`, so the goal/topic tier would read an empty file
    # and exit 0 — a gate that always passes. Refusing is the only honest answer; a
    # leg that forgot `--branch fleet` would otherwise run while a debt is owed.
    if a.debt:
        if a.branch != "fleet":
            ap.error("--debt is fleet-only: the goal/topic tier writes no debt, so an "
                     "empty read there would be a false clear. Pass --branch fleet.")
        return debt(subject, a.branch, only=a.only)
    if a.abandon:
        if a.branch != "fleet":
            ap.error("--abandon is fleet-only: only the fleet tier opens a debt. "
                     "Pass --branch fleet.")
        if not a.step:
            ap.error("--abandon needs --step")
        return abandon(subject, a.branch, a.step)
    if a.gate:
        # Deliberately NOT fleet-scoped, unlike `--abandon`. The predicate applies to
        # every tier, and the goal/topic tier is where it is most needed — `--abandon`
        # refuses there because no debt file is written, but `--gate` writes a state
        # `--check` honours directly, so it is that tier's only recovery path.
        if not a.step:
            ap.error("--gate needs --step")
        return gate(subject, a.branch, a.step)
    if a.record_verdict or a.record_outcome:
        # Goal verdicts ride the goal/topic receipt only; the fleet tier owes none.
        if a.branch:
            ap.error("--record-verdict/--record-outcome are topic-tier: pass no --branch")
        if not a.goal:
            ap.error("--record-verdict/--record-outcome need --goal")
        if a.record_verdict:
            if not a.vault or not a.verdict:
                ap.error("--record-verdict needs --vault and --verdict")
            return record_verdict(subject, a.goal, a.vault, a.verdict, a.session)
        if not a.outcome:
            ap.error("--record-outcome needs --outcome")
        return record_outcome(subject, a.goal, a.outcome, a.verdict or "")
    if a.start:
        return start(subject, a.branch)
    if not a.step or not a.path:
        ap.error("--record needs --step and --path")
    return record(subject, a.branch, a.step, a.path)


if __name__ == "__main__":
    sys.exit(main())
