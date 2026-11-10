#!/usr/bin/env python3
"""Card a wait that has not moved for 24 h — the lifecycle's `criterion challenge`.

`50 Knowledge Base/Session Lifecycle.md:43` and its edge case E5 (`:95`) say a session
parked on a machine wait is kept "while the `⏰ Ends:` observable is live", and that a
wait with **no observable change for 24 h** gets one card to the operator. Nothing
implemented the second half: a declared wait suppresses every other reader
(`scripts/declared-wait.py` answers a boolean, so `fleet-board.py` withholds `nudge` and
`manager-predispatch.py` returns `LIVENESS_PARKED`) and nothing expires it. A session
whose wake never fires therefore waits forever, silently — the exact state the lifecycle
page exists to prevent.

WHAT THE 24 h IS MEASURED ON, and why it needs no live probe. The declaration is already
on disk: the newest `Stop` record for a session carries `ts` (when it was rendered) and a
`detail` holding the `⏰ Ends:` text (what it waits on). A resumed session that re-parks
writes a newer record, so "the pair has not moved for 24 h" IS "the same record is still
the newest" — no probing, no clock arithmetic beyond one subtraction. That is also why
this reads `declared-wait.py`'s detail form instead of re-parsing the logs: the plugin
has one home for the `⏰ Ends:` read, and a second parser is the drift that reader's own
docstring forbids.

WHY THE STATE IS ITS OWN STORE. The dedup key ("this stall was already carded") must
survive rounds, and `drive-ledger.py` was the obvious home until its merge semantics
were checked: `merge` replaces a session's WHOLE entry via `canonical()`. A row that is
`waiting` this round and a `parked` candidate next round is written by two different
callers, and the second one's entry carries no wait fields — so the card dedup would be
silently dropped and the same stall re-carded every time the row changed class. One
writer, one file, no cross-caller overwrite. The store is keyed by session id and holds
exactly the three facts the next round compares.

WHAT IT DOES NOT DO. It never closes, resumes or judges a session — closing the process
is `[[A Parked Headless Worker Is Closed and Resumed When Its Wait Clears]]`, a separate
task. This script only surfaces the stall.

Exit codes: 0 ok · 2 refused (the wait store or the reader could not be read).

Subcommands: render
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

#: Exit code for a store or reader this script will not touch. Distinct from 1 (a
#: crash) so a caller can tell "refused, wrote nothing" from "broke mid-run".
REFUSED = 2

#: The 24 h the lifecycle page names. Overridable so a test can drive the boundary
#: without sleeping, and so a future page edit is one env var, not a code change.
DEFAULT_THRESHOLD_SECONDS = 24 * 60 * 60

HERE = os.path.dirname(os.path.abspath(__file__))
DECLARED_WAIT = os.path.join(HERE, "declared-wait.py")
ATTENTION_ASK = os.path.join(HERE, "attention-ask.py")
LIVENESS_SCRIPT = os.path.join(HERE, "session-liveness.py")

#: `~/.claude/state/fleet-drive/waits.json`, beside the drive ledger but never written
#: by it. The env override is what lets the tests exercise a tmp store.
STORE = os.environ.get("SUPERVISOR_WAIT_ESCALATE_STORE") or os.path.expanduser(
    "~/.claude/state/fleet-drive/waits.json"
)

#: The producer identity every card carries. `attention-ask.py` defaults the producer to
#: `$CLAUDE_CODE_SESSION_ID` and REFUSES when it is unset — and this script runs inside a
#: fleet round whose session id is the caller's, not this process's. Naming it explicitly
#: is what keeps a card postable from a context with no session (the `fleet-keeper.py`
#: rule, same reason).
PRODUCER_ID = "drive-escalate"
PRODUCER_KIND = "cron"

PROBE_TIMEOUT = 30


def _now(value=None):
    """Now as epoch seconds. `--now` takes an epoch int/float or an ISO-8601 string."""
    if value is None:
        return datetime.datetime.now().timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(
        timespec="seconds"
    )


def read_waits(script=None):
    """[(record, None)] or (None, reason) from `declared-wait.py --detail-json`.

    A refusal is never read as "no waits": an unreadable reader that returned [] would
    make a fleet of stalled waits look clean, which is the fail-open direction this
    whole leg exists to close.
    """
    out = subprocess.run(
        [sys.executable, str(script or DECLARED_WAIT), "--detail-json"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None, (out.stderr or "").strip() or "declared-wait.py refused"
    try:
        records = json.loads(out.stdout or "[]")
    except ValueError as exc:
        return None, "declared-wait.py emitted unparseable JSON: %s" % exc
    if not isinstance(records, list):
        return None, "declared-wait.py emitted %s, not a list" % type(records).__name__
    return [r for r in records if isinstance(r, dict)], None


def load_store(path=None):
    """The wait store as a dict keyed by session id. Absent is empty, corrupt refuses."""
    target = path or STORE
    if not os.path.exists(target):
        return {}, None
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        # ⚠️ NOT read as empty. An empty store re-cards every stall it had already
        # carded, and the operator's board fills with duplicates of the one problem the
        # card exists to raise once. Same rule `drive-ledger.py` states for its own file.
        return None, "%s exists but cannot be parsed (%s)" % (target, exc)
    if not isinstance(data, dict):
        return None, "%s top level is %s, not an object" % (
            target,
            type(data).__name__,
        )
    return data, None


def save_store(store, path=None):
    """pid-suffixed tmp then `os.replace`, so a concurrent round cannot read a torn file."""
    target = path or STORE
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp.%d" % (target, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(store, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def observable_key(record):
    """The dedup key: this declaration's `ts` paired with its `⏰ Ends:` text.

    Both halves, not the timestamp alone. `ts` is owned by `declared-wait.py`'s
    `wait_record()`: the start of the trailing run of `Stop` records naming the same
    observable (digits ignored), NOT the newest render. A session that re-parks on the
    SAME observable — including the hourly re-park the 1 h re-drive provokes — keeps
    the same `ts`, so the 24 h clock keeps running; a session that declares a different
    observable starts a new run and a new `ts`, and is cardable again. This script only
    compares; it never computes the clock.
    """
    ts = record.get("ts")
    stamp = "" if ts is None else repr(float(ts))
    return "%s|%s" % (stamp, record.get("ends") or "")


def live_sessions(script=None):
    """(set of live session ids, None) or (None, reason).

    ⚠️ **One call, not one per row.** `--check` per session is what
    `drive-retained.py` does and it is correct there — that leg probes a handful of
    retained rows. Here the population is every declaring session, measured 2026-11-10
    at **317**; probing each took **3 m 40 s** in one round, which is a third of the
    round's own budget spent on subprocess spawns. `--list` answers the same question in
    one call (79 rows, sub-second).

    ⚠️ **Membership is the liveness answer, and the header line is skipped by shape.**
    `--list` prints `rows=<n> registry_live=<n> degraded=<n>` first; its leading token
    carries `=`, which no session id does. A session the registry holds but the endpoint
    has no heartbeat for still appears, marked `DEGRADED` — so a non-member really is
    absent, rather than merely degraded.

    A failed `--list` returns None, never an empty set: an empty set would read as "the
    whole fleet exited" and silently suppress every card this leg exists to raise.
    """
    try:
        out = subprocess.run(
            [sys.executable, str(script or LIVENESS_SCRIPT), "--list"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT * 4,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "session-liveness.py --list failed: %s" % exc
    if out.returncode != 0:
        return None, (out.stderr or "").strip() or "session-liveness.py --list refused"
    ids = set()
    for line in (out.stdout or "").splitlines():
        token = line.split()[0] if line.split() else ""
        if token and "=" not in token and len(token) >= 8:
            ids.add(token)
    return ids, None


def is_alive(sid, live):
    """Whether `sid` is in the live set, matching a full id or its 8-char prefix."""
    if not sid or live is None:
        return None
    if sid in live:
        return True
    prefix = sid[:8]
    return any(other.startswith(prefix) for other in live)


def disposition(record, store, now, threshold=DEFAULT_THRESHOLD_SECONDS, alive=True):
    """One declaring session's escalation verdict. Pure — this is the test seam.

    Returns a dict carrying the rendered evidence and the action to take. The actions:
      escalate   — live, stale past the threshold, and this exact pair is not carded yet
      carded     — live and stale, but this pair was already carded (once-per-stall)
      fresh      — the declaration is younger than the threshold
      gone       — the session is not live; nobody is waiting, so nothing is carded
      unreadable — no usable `ts`, or liveness could not be established
    """
    sid = record.get("session_id") or ""
    key = observable_key(record)
    prior = store.get(sid) or {}
    row = {
        "session_id": sid,
        "wait_observable": record.get("ends") or "",
        "wait_carded": prior.get("wait_carded") or "",
        # The key this round computed, carried on the row so `cmd_render` stores the
        # SAME string it compared against. Rebuilding it from the rendered `wait_since`
        # was the first draft and it never matched: `wait_since` is an ISO string and
        # `observable_key()` uses the raw float, so every round re-carded.
        "key": key,
        "action": "unreadable",
        "wait_since": "",
        "age_seconds": None,
        "card": "",
    }
    ts = record.get("ts")
    if ts is None:
        # No usable timestamp: say so, never guess one. A fabricated 0 would read as a
        # wait declared in 1970 and card immediately on no evidence.
        return row
    age = now - float(ts)
    row["wait_since"] = _iso(float(ts))
    row["age_seconds"] = age
    if age < threshold:
        row["action"] = "fresh"
        return row
    if alive is False:
        row["action"] = "gone"
        return row
    if alive is None:
        # Cannot prove the session dead OR alive. Carding on a failed probe is the
        # false-positive direction; staying silent is the safe one, and the next round
        # re-probes.
        return row
    if row["wait_carded"] == key:
        row["action"] = "carded"
        return row
    row["action"] = "escalate"
    return row


def card_payload(row, threshold=DEFAULT_THRESHOLD_SECONDS):
    """The question the operator reads, and the options that answer it.

    ⚠️ **A criterion challenge, not a status report.** The lifecycle page's own words:
    the card asks whether the declared wait is still worth waiting on. Both options are
    actions the operator can take; neither is "do nothing", which is what leaving the
    card alone already means.
    """
    hours = int(threshold // 3600)
    return (
        "Wait unchanged for %d h: session %s declares `⏰ Ends: %s` (since %s). "
        "The observable has not moved, so this wait may never resolve."
        % (hours, row["session_id"][:8], row["wait_observable"] or "(none)", row["wait_since"])
    )


def card_options():
    return (
        "keep waiting — the observable is still live",
        "re-scope — this wait is not going to resolve",
    )


def post_card(row, threshold=DEFAULT_THRESHOLD_SECONDS, ask_script=None):
    """Post one attention card. Returns its item id, or "" when the post failed.

    The dedup key is derived from the observable pair, so a retried round posts the same
    card rather than a second one even if the store write was lost.
    """
    argv = [
        sys.executable,
        str(ask_script or ATTENTION_ASK),
        "post",
        "--producer-id",
        PRODUCER_ID,
        "--producer-kind",
        PRODUCER_KIND,
        "--dedup-key",
        "wait-escalation:%s:%s" % (row["session_id"][:8], row["key"]),
        "--payload",
        card_payload(row, threshold=threshold),
    ]
    for option in card_options():
        argv += ["--option", option]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (out.stdout or "").splitlines():
        if line.startswith("ITEM_ID:"):
            return line.split(":", 1)[1].strip()
    return ""


def render_block(rows, threshold=DEFAULT_THRESHOLD_SECONDS):
    """The `ESCALATED` block: one line per stale wait, however long the list.

    Rendered here rather than by the agent for the reason `drive-retained.py` records:
    prose that forbids collapsing a block loses to an agent under load, and a rendered
    block cannot be collapsed without deleting lines a reader can count.
    """
    stale = [r for r in rows if r["action"] in ("escalate", "carded")]
    lines = [
        "STALE WAITS — waits whose observable has not moved for %d h"
        % int(threshold // 3600)
    ]
    for row in stale:
        lines.append(
            "  [%s] · waiting since %s · %s"
            % (row["session_id"][:8], row["wait_since"] or "?", row["wait_observable"] or "(none)")
        )
        if row["action"] == "escalate":
            lines.append("      card: %s" % (row["card"] or "post failed"))
        else:
            lines.append("      card: already carded %s" % row["wait_carded"])
    if not stale:
        lines.append("  (none)")
    return "\n".join(lines)


def cmd_render(args):
    threshold = args.threshold_seconds
    records, reason = read_waits(args.declared_wait_script)
    if reason:
        print("drive-escalate: %s" % reason, file=sys.stderr)
        return REFUSED
    store, reason = load_store(args.store)
    if reason:
        print("drive-escalate: %s -- refusing to read it as an empty store" % reason,
              file=sys.stderr)
        return REFUSED

    now = _now(args.now)
    live, live_reason = live_sessions(args.liveness_script)
    if live_reason:
        # ⚠️ Not fatal, and deliberately not fatal. A liveness outage must not stop the
        # round; it means every row's liveness is unknown, so nothing is carded and the
        # reason is reported. Failing hard here would take down the whole drive leg for
        # a probe that only narrows the population.
        print("drive-escalate: %s — no row carded this round" % live_reason, file=sys.stderr)
    rows = [
        disposition(
            rec, store, now, threshold, alive=is_alive(rec.get("session_id") or "", live)
        )
        for rec in records
    ]

    changed = False
    for row in rows:
        if row["action"] == "unreadable":
            # No usable timestamp, so there is nothing to record. Leaving any prior
            # entry untouched is deliberate: a momentarily torn log must not wipe the
            # dedup key and re-card a stall that was already carded.
            continue
        prior_carded = row["wait_carded"]
        posted = ""
        if row["action"] == "escalate":
            if args.no_post:
                row["card"] = "not posted (--no-post)"
            else:
                posted = post_card(row, threshold=threshold,
                                   ask_script=args.attention_ask_script) or ""
                row["card"] = posted
        # ⚠️ **The carded marker is set by a REAL post only, never by a rendered one.**
        # Keying it off `row["card"]` was the first draft and `--no-post` sets that field
        # to a non-empty "not posted" string — so a dry run recorded every stall as
        # carded and the next real round suppressed the card it exists to send. A dry
        # run must leave the store's dedup state exactly as it found it.
        if posted:
            new_carded = row["key"]
        elif prior_carded == row["key"]:
            new_carded = prior_carded
        else:
            # The observable moved: clear the marker so a later stall on the new
            # observable can card again (SC2).
            new_carded = ""
        entry = {
            "wait_since": row["wait_since"],
            "wait_observable": row["wait_observable"],
            "wait_carded": new_carded,
        }
        if store.get(row["session_id"]) != entry:
            store[row["session_id"]] = entry
            changed = True

    if changed:
        save_store(store, args.store)

    print(render_block(rows, threshold))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"round": args.round, "rows": rows}, handle, indent=2, sort_keys=True)
            handle.write("\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="drive-escalate.py")
    sub = parser.add_subparsers(dest="command")
    render = sub.add_parser(
        "render", help="card every wait whose observable has not moved for 24 h"
    )
    render.add_argument("--threshold-seconds", type=int,
                        default=int(os.environ.get(
                            "SUPERVISOR_WAIT_ESCALATE_SECONDS",
                            DEFAULT_THRESHOLD_SECONDS)))
    render.add_argument("--now", default=None,
                        help="override the clock (epoch seconds or ISO-8601)")
    render.add_argument("--store", default=None, help="wait store path (default: the live one)")
    render.add_argument("--round", default=None, help="round tag, for the payload")
    render.add_argument("--json-out", default=None, help="also write the rows here")
    render.add_argument("--no-post", action="store_true",
                        help="detect and render, post nothing")
    render.add_argument("--declared-wait-script", default=None)
    render.add_argument("--liveness-script", default=None)
    render.add_argument("--attention-ask-script", default=None)
    args = parser.parse_args(argv)
    if args.command == "render":
        return cmd_render(args)
    parser.print_help(sys.stderr)
    return REFUSED


if __name__ == "__main__":
    sys.exit(main())
