#!/usr/bin/env python3
"""Model-free staleness for the fleet round: report a loop that stopped producing digests.

The fleet drive leg acts only on a digest round — no round, no `CLASSIFICATION` set, so
no `parked` candidate, no `revive` verdict and no nudge. A loop that silently stops
producing rounds is therefore indistinguishable, from the operator's side, from a fleet
with nothing to do. Measured 2026-10-20: the retained digests run
`2026-10-18T12:52:46Z` then nothing until `2026-10-19T21:58:31Z` — a **33h05m gap**
spanning the 20:14:10Z day-end census — and four of the six `idle-with-open-work` rows
at that census were never dispatched to at all. Nothing reported it, because nothing
watches digest production: `grep -rln 'fleet-handback'` over this repo returns only
`CHANGELOG.md`, `agents/fleet-drive.md`, `agents/fleet-sweep-reader.md` and
`commands/steps/sweep.md`, and `~/.claude/scripts/` holds no digest-staleness check.

Two host-external precedents exist (`com.bborbe.sweep-gate-notify` at 900 s and
`com.bborbe.manager-liveness-notify` at 60 s), and this is a **third** rather than an
extension of either:

  * `sweep-gate-notify`'s unit is a *subject* resolved under `23 Topics/`; the fleet
    digest is one global artifact. Routing it through `sweep-gate-subjects.py` would
    enter the fail-open publish path that tick script's own header warns about.
  * Its 900 s interval is disqualifying: at that cadence a 1080 s limit is observable
    only on the *next* tick, up to 1800 s after the last round.
  * `manager-liveness-notify` has the right shape — 60 s polling, one report per lapse
    — but reads per-subject manager cadence under `~/.claude/state/worker-manager/`,
    a different artifact with a different parser.

Verbs, one file each, all under STATE_DIR:

  --check   Read the newest digest's own `snapshot written:` line, age it against LIMIT,
            and print exactly one verdict line:

              OK         digest=<ts> age=<s> limit=<s>
              STALE      digest=<ts> age=<s> limit=<s> round=<file>
              SUPPRESSED digest=<ts> … reason=already-reported
              RECOVERED  digest=<ts> age=<s> limit=<s>
              UNOBSERVED gap=<s> — this watcher did not run across the gap; baseline reset

            Exit 10 on each STALE report — once per lapse interval, not once per
            transition. The notified marker holds the
            baseline it reported and the lapse interval reached, so a FLAT age notifies
            once and the following healthy round clears it (exit 0, RECOVERED). A
            RISING age re-fires once per further LIMIT — a lapse that never recovers is
            still a lapse, and silencing it after one report is how a 6h24m gap went
            unreported. A second lapse after a recovery reports again.
            Exit 2 on any failure to read the digest directory — never 0, so a broken
            read can never read as "healthy".

  --state   ONE line, `<STATE>\\t<detail>`, exit 0 whatever the state: the state IS the
            answer, and a non-zero exit would make a healthy watcher look like a failure
            to every `if` that wraps it. `--check` structurally cannot answer this — it
            prints only the transition.

  --stale   The watcher's own liveness: age of STATE_DIR/heartbeat against 2 x TICK.
            Prints OK / STALE / MISSING and exits 1 on either of the last two. This is
            the answer to "is the watcher itself alive", which `--check` cannot give —
            a dead watcher prints nothing, and silence is not evidence.

⚠️ **The resume guard is the quiet-hours rule, and it is derived rather than configured.**
The retained set carries three separate ~8 h gaps (`22:16Z→06:11Z`, `21:32Z→06:04Z`,
`21:58Z→06:09Z`). Whether the cause is a deliberate stand-down or the host sleeping is
not settled, and it does not have to be: a watcher that was itself not running across a
gap cannot tell a lapsed loop from a sleeping host, so attributing the gap to the loop
would be a guess. `--check` therefore compares its OWN previous tick: when that is older
than 2 x TICK the tick is a resume, it prints UNOBSERVED, and it resets the baseline
instead of reporting. The first tick ever is a resume for the same reason — a fresh
install must not fire on the history it inherited.

⚠️ **The newest round is chosen by its parsed timestamp, falling back to mtime.** Digest
filenames do not carry the snapshot time reliably — `2026-10-20T08-43-00-02-00.digest`
holds `snapshot written: 2026-10-20T06:43:35Z` and `round14-2026-10-20T13-10Z.digest`
holds `11:39:16Z` — so ordering by name would judge a stale round by a fresh filename.

⚠️ **Nine of the 85 retained digests carry a `snapshot written:` value that is not a
timestamp, and four of those name no baseline at all.** A parser that treats them as
fresh misses a real lapse; one that treats them as a parse failure reports a false one.
They are enumerated in `parse_snapshot` with the rule for each.
"""
import argparse
import calendar
import json
import os
import re
import sys
import time

DIGEST_DIR = os.path.expanduser(
    os.environ.get("FLEET_DIGEST_DIR", "~/.claude/state/fleet-handback")
)
STATE_DIR = os.path.expanduser(
    os.environ.get("FLEET_DIGEST_STATE_DIR", "~/.claude/state/fleet-digest")
)
# Floored at 1: the re-fire step below divides by it, so a misconfigured 0 would raise
# `ZeroDivisionError` (exit 1, traceback) where the previous code merely reported stale —
# turning a tuning knob into a crash. A negative value would otherwise pin every lapse at
# step 1 and suppress the re-fire entirely.
LIMIT = max(1, int(os.environ.get("FLEET_DIGEST_LIMIT", "1080")))
TICK = int(os.environ.get("FLEET_DIGEST_TICK", "60"))

HEARTBEAT = "heartbeat"
TICKS = "ticks"
NOTIFIED = "notified.json"

EXIT_STALE = 10
EXIT_FAILED = 2

SNAPSHOT_RE = re.compile(r"snapshot written:\s*(.*)$", re.MULTILINE)
# PREFIX matches, not whole-string: the retained set's values carry suffixes and
# annotations — `2026-10-20T11:39:16Z - 23 sessions`, `2026-10-12T17:15 (see
# fleet-snapshot.py)`, `2026-10-12 (fleet-snapshot.py)`. An anchored-whole-value match
# rejected all three and carried a fresh round's baseline forward, which is the silent
# miss this watcher exists to prevent (caught by probing the live directory).
ISO_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z")
ISO_MIN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})\b")
ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\b")
BASELINE_KEPT = re.compile(r"baseline kept at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")


def _epoch(y, mo, d, h=0, mi=0, s=0):
    # calendar.timegm is UTC by definition; mktime + time.timezone would be an hour out
    # across a DST boundary, and a limit measured in seconds cannot absorb that.
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def parse_snapshot(value):
    """(epoch, quality) for one `snapshot written:` value; (None, reason) when unknown.

    The nine unparseable forms in the retained set, and the rule for each:

      `<iso full>`                                   -> parsed
      `<iso minutes>`  `2026-10-12T17:15`            -> parsed at minute precision
      `<iso day>`      `2026-10-12`                  -> unknown: coarser than LIMIT, so a
                                                        baseline at that precision cannot
                                                        distinguish healthy from lapsed
      `skipped (… baseline kept at <iso>)`           -> parsed: the value names its own
                                                        baseline
      `skipped (… baseline read ok)`                 -> unknown: no baseline named; the
                                                        caller carries the previous round's
      `skipped (roster join failed; …)`              -> unknown, carried forward
      `skipped (roster not supplied; round failed)`  -> unknown, carried forward
      `skipped (pass not run: …)`                    -> unknown, carried forward
      `skipped (not persisted by this agent run; …)` -> unknown, carried forward
      `(pending, set by fleet-snapshot.py)`          -> unknown, carried forward

    "Carried forward" is deliberately not "fresh": a round that persisted nothing has
    not proved the loop is alive, and treating it as fresh is exactly the silent miss
    this watcher exists to prevent.
    """
    text = value.strip()
    # The baseline-kept lookup runs FIRST, because that form's baseline lives inside the
    # parentheses and any generic trailing-annotation handling would throw it away.
    m = BASELINE_KEPT.search(text)
    if m:
        mm = ISO_FULL.match(m.group(1))
        if mm:
            return _epoch(*[int(x) for x in mm.groups()]), "parsed"
    m = ISO_FULL.match(text)
    if m:
        return _epoch(*[int(x) for x in m.groups()]), "parsed"
    m = ISO_MIN.match(text)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        return _epoch(y, mo, d, h, mi), "parsed"
    if ISO_DAY.match(text):
        return None, "day-precision"
    return None, "no-baseline"


def read_round(path):
    """(epoch, quality, raw) from a digest file's first `snapshot written:` line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError as exc:
        return None, "unreadable: %s" % exc, ""
    m = SNAPSHOT_RE.search(head)
    if not m:
        return None, "no-snapshot-line", ""
    raw = m.group(1).strip()
    epoch, quality = parse_snapshot(raw)
    return epoch, quality, raw


def newest_round():
    """(epoch, quality, path, raw) for the newest digest, or None when the dir is empty.

    Ordering key is the parsed snapshot timestamp where one exists and the file's mtime
    otherwise — never the filename, which routinely disagrees with its own contents.
    A newest file that names no baseline carries the newest PARSEABLE baseline forward,
    which is what keeps a run of `skipped` rounds from reading as either fresh or broken.
    """
    try:
        names = [n for n in os.listdir(DIGEST_DIR) if n.endswith(".digest")]
    except OSError as exc:
        raise RuntimeError("cannot read %s: %s" % (DIGEST_DIR, exc))
    if not names:
        raise RuntimeError("no .digest files under %s" % DIGEST_DIR)

    rows = []
    for name in names:
        path = os.path.join(DIGEST_DIR, name)
        epoch, quality, raw = read_round(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        rows.append((epoch if epoch is not None else mtime, name, path, epoch, quality, raw))

    rows.sort(key=lambda r: (r[0], r[1]))
    _, name, path, epoch, quality, raw = rows[-1]
    if epoch is not None:
        return epoch, quality, path, raw

    for _, name2, path2, epoch2, quality2, raw2 in reversed(rows):
        if epoch2 is not None:
            return epoch2, "carried-forward", path2, raw
    raise RuntimeError("no digest under %s names a usable baseline" % DIGEST_DIR)


def state_path(name):
    return os.path.join(STATE_DIR, name)


def write_atomic(path, text):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_ticks():
    try:
        with open(state_path(TICKS), encoding="utf-8") as fh:
            return [float(x) for x in fh.read().split()]
    except (OSError, ValueError):
        return []


def read_notified():
    try:
        with open(state_path(NOTIFIED), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_notified(payload):
    write_atomic(state_path(NOTIFIED), json.dumps(payload))


def clear_notified():
    try:
        os.unlink(state_path(NOTIFIED))
    except OSError:
        pass


def write_heartbeat(rc):
    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    write_atomic(state_path(HEARTBEAT), "%d %s rc=%d\n" % (now, iso, rc))


def resume_gap():
    """Seconds since this watcher's own previous tick, or None when there is no history.

    `None` covers both "first tick ever" and "previous tick missing", and both are
    resumes: a fresh install must not fire on the history it inherited, and a lost tick
    file is not evidence the watcher ran.
    """
    ticks = read_ticks()
    if not ticks:
        return None
    return time.time() - ticks[-1]


def record_tick():
    ticks = (read_ticks() + [time.time()])[-32:]
    write_atomic(state_path(TICKS), "".join("%.3f\n" % t for t in ticks))


def evaluate():
    """(state, detail) — never raises for a digest-content problem, only for IO."""
    epoch, quality, path, raw = newest_round()
    age = int(time.time() - epoch)
    name = os.path.basename(path)
    detail = "digest=%s age=%ds limit=%ds round=%s quality=%s" % (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
        age,
        LIMIT,
        name,
        quality,
    )
    if raw:
        detail += " raw=%r" % raw
    return ("STALE" if age > LIMIT else "OK"), detail, epoch


def cmd_check():
    gap = resume_gap()
    if gap is None or gap > 2 * TICK:
        record_tick()
        shown = "none" if gap is None else "%ds" % int(gap)
        write_heartbeat(0)
        print("UNOBSERVED gap=%s — this watcher did not run across the gap; baseline reset" % shown)
        return 0

    state, detail, epoch = evaluate()
    record_tick()
    was = read_notified()

    if state == "STALE":
        # Dedup on the baseline EPOCH, not on a substring of the rendered detail. The
        # rendered line is for the ledger and may change shape; keying the "already
        # reported" decision on it would make a cosmetic edit silently re-notify.
        # ⚠️ The epoch alone is not enough, and deduping on it alone was the defect
        # measured 2026-10-30: a lapse that never recovers keeps the SAME newest digest,
        # so the epoch never moves and every later tick printed `SUPPRESSED` while the
        # age climbed 1,374 s -> 23,064 s over 6h24m. The discriminator is therefore a
        # RISING age, recorded as the lapse interval it has reached: a flat age stays
        # quiet — the dedup's original job — and each further LIMIT of age re-fires once.
        age = int(time.time() - epoch)
        step = max(1, age // LIMIT)
        if was and was.get("epoch") == epoch and was.get("step", 1) >= step:
            write_heartbeat(0)
            print("SUPPRESSED %s reason=already-reported" % detail)
            return 0
        write_notified({"epoch": epoch,
                        "digest": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
                        "reported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "step": step})
        write_heartbeat(EXIT_STALE)
        print("STALE %s" % detail)
        return EXIT_STALE

    if was:
        clear_notified()
        write_heartbeat(0)
        print("RECOVERED %s" % detail)
        return 0

    write_heartbeat(0)
    print("OK %s" % detail)
    return 0


def cmd_state():
    try:
        state, detail, _ = evaluate()
    except RuntimeError as exc:
        print("MISSING\t%s" % exc)
        return 0
    print("%s\t%s" % (state, detail))
    return 0


def cmd_stale():
    limit = 2 * TICK
    path = state_path(HEARTBEAT)
    try:
        with open(path, encoding="utf-8") as fh:
            last = int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        print("MISSING %s" % path)
        return 1
    age = int(time.time()) - last
    if age > limit:
        print("STALE heartbeat age=%ds limit=%ds" % (age, limit))
        return 1
    print("OK heartbeat age=%ds limit=%ds" % (age, limit))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Model-free staleness check for the fleet digest round."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="one poll; exit 10 on a stale transition")
    group.add_argument("--state", action="store_true", help="one line of current state; always exit 0")
    group.add_argument("--stale", action="store_true", help="this watcher's own liveness")
    args = parser.parse_args(argv)

    if args.check:
        try:
            return cmd_check()
        except RuntimeError as exc:
            write_heartbeat(EXIT_FAILED)
            print("FAILED(rc=%d): %s" % (EXIT_FAILED, exc))
            return EXIT_FAILED
    if args.state:
        return cmd_state()
    return cmd_stale()


if __name__ == "__main__":
    sys.exit(main())
