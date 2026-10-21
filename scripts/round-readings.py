#!/usr/bin/env python3
"""The round's readings, computed once instead of described in twelve bullets.

`commands/steps/round-readings.md` is the manager's step 2: it tells a round to probe
liveness, in-flight, the attention feed and each session's external wait, and to hand
all four to the sweep reader. It is **prose**, and prose is skippable — nothing in the
round's own output distinguishes a round that took the readings from one that did not.

Measured 2026-10-21 (BRO-21389 MDM via REST, manager round at 12:33): the round opened
its receipt and recorded **nothing** —

    {"topic": "BRO-21389 MDM via REST", "round_started_at": 1792578804.898, "fields": {}}

— so the sweep reader received none of the four verdicts, rendered the
`parked-on-unregistered-gate` cell UNVERIFIED, and bucketed a worker that had been
denied `gh pr merge` twice as `progressing` on liveness alone. The worker was not
working. It was blocked on a gate nothing had raised.

This script is that step made mechanical: it takes the caller's tracked set, computes
all four verdict families for every row, and writes ONE file. The reader then refuses
to render a table without it (`agents/goal-sweep-reader.md`), and the round's receipt
records it (`scripts/manager-receipt.py` step `readings`) — so a skipped step 2 becomes
a visible `skipped: readings` line instead of a plausible-looking table.

    --tracked <path>   the caller's tracked set, one task name per line — the file
                       `manager-predispatch.py --write-tracked` writes
    --subject <name>   the subject, for the output slug and the record
    --vault <path>     the vault root, for resolving a name to its task file
    --interval <sec>   the clamped tick interval; the frozen-file threshold is 1x this
    --out <path>       override the output path

Writes `<MANAGER_RECEIPT_STATE_DIR>/<slug>.readings` as JSON and prints its path.

**Every verdict is three-valued, and `unread` is never a negative.** A store that could
not be reached, a log that could not be parsed and a registry that could not be read
each resolve to the third value — never to `clear`, `none` or `quiet`. Folding a failed
read into a negative is the silent direction that makes a blocked worker read as a
working one, which is the defect this file exists to remove.

**Reuse, never re-derive.** Id sets come from `session-ids.py`, liveness from
`session-liveness.py`, the registry and transcript age from `who-needs-me.py`, the
pane→session join from `gate-owner-filter.py`, and the tracked model from
`manager-predispatch.py`. Each of those owns its question; a second definition here
would drift from it, and the repo's own rule is that the two surfaces must agree.
"""
import argparse
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_RECEIPT_STATE_DIR", "~/.claude/state/manager-receipts")
)
ATTENTION_DIR = os.path.expanduser(
    os.environ.get("ATTENTION_STATE_DIR") or "~/.claude/state/attention"
)
PROJECTS_DIR = os.path.expanduser(
    os.environ.get("PROJECTS_DIR") or "~/.claude/projects"
)

# Limb 2's own window, restated from `agents/goal-drive.md`: an open tool marker inside
# it is a worker mid-call; past it the stuck path fires instead.
TOOL_MARKER_WINDOW = 20 * 60

# The feed renders one row per gate as `  [<pane>]  <age>  <name>  <state> · …`.
PANE_RE = re.compile(r"^\s*\[(\d+)\]")
# The `⏰ Ends:` slot, taken from a `Stop` record's `detail`. Non-empty is required:
# the closer template emits the slot with a blank value when nothing is declared.
ENDS_RE = re.compile(r"⏰ Ends:\s*(\S.*)$", re.MULTILINE)

_MODS = {}


def _load(name):
    """Import a sibling script by path — the producers are hyphenated, not packages."""
    if name in _MODS:
        return _MODS[name]
    path = os.path.join(HERE, name + ".py")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MODS[name] = mod
    return mod


# --------------------------------------------------------------------------- #
# the four verdict families
# --------------------------------------------------------------------------- #


def _ends_slot(session_id, attention_dir):
    """The newest `Stop` record's `⏰ Ends:` value, or None.

    The log is append-only, so the tie-break is the rule: a session that waited on a
    machine once keeps that record forever, and a read that took *any* `⏰ Ends:`
    record rather than the newest would file the row `external` permanently.
    """
    path = os.path.join(attention_dir, f"{session_id}.events.jsonl")
    newest = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") != "Stop":
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, (int, float)):
                    continue
                newest = rec if newest is None or ts > newest["ts"] else newest
    except OSError:
        return None
    if newest is None:
        return None
    detail = newest.get("detail") or ""
    if isinstance(detail, list):
        detail = " ".join(str(x) for x in detail)
    m = ENDS_RE.search(str(detail))
    return m.group(1).strip() if m else None


def external_wait(session_ids, attention_dir):
    """`external` | `none` | `unread` — does a machine end this worker's wait?

    `unread` when no id at all could be examined, which is the honest answer for a row
    whose session log is absent rather than empty: an absent log is not a declaration
    of *nothing*, and reading it as `none` is the silent direction.
    """
    if not session_ids:
        return "none"
    readable = 0
    for sid in session_ids:
        path = os.path.join(attention_dir, f"{sid}.events.jsonl")
        if not os.path.exists(path):
            continue
        readable += 1
        if _ends_slot(sid, attention_dir):
            return "external"
    return "none" if readable else "unread"


def _registry_status(session_id, registry):
    if registry is None:
        return None
    rec = registry.get(session_id)
    if not rec:
        return None
    return rec.get("status")


def in_flight(session_ids, registry, attention_dir, now, transcript_age):
    """`(verdict, limb)` — the drive leg's three limbs, in its own order.

    Limb 1 is decisive on `shell` and `waiting` and falls through on `idle`/`busy`;
    limb 2 answers while a tool marker is open; limb 3 always answers. The carve-out
    is limb 1's: a `run_in_background` watcher outlives the turn, so a `shell` whose
    newest `Stop` declares an `⏰ Ends:` slot falls through rather than dropping.
    """
    unreadable = 0
    for sid in session_ids:
        status = _registry_status(sid, registry)
        if status == "shell":
            if _ends_slot(sid, attention_dir):
                break  # the carve-out: fall through to limb 2
            return "in-flight", "limb 1 (registry: shell)"
        if status == "waiting":
            return "quiet", "limb 1 (registry: waiting)"
        if status is None:
            unreadable += 1

    for sid in session_ids:
        marker = os.path.join(attention_dir, f"{sid}.tool.json")
        try:
            with open(marker, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if rec.get("state") != "open":
            continue
        ts = rec.get("ts")
        age = (now - float(ts)) if isinstance(ts, (int, float)) else TOOL_MARKER_WINDOW
        if age < TOOL_MARKER_WINDOW:
            tool = rec.get("tool") or "tool"
            return "in-flight", f"limb 2 (tool.json state=open {int(age // 60)} min, {tool})"
        # Open past the window is NOT a drop: that is the stuck path's shape.

    window = _load("who-needs-me").LIVE_WINDOW
    freshest = None
    for sid in session_ids:
        age = transcript_age(sid)
        if age >= float("inf"):
            continue
        if age < window:
            return "in-flight", f"limb 3 (transcript {int(age)}s)"
        freshest = age if freshest is None else min(freshest, age)
    if freshest is not None:
        return "quiet", f"limb 3 (transcript {int(freshest)}s)"

    # No id at all, or nothing readable: `unread`, never `quiet`. A failed read is not
    # a measurement, and the drive leg's own rule forbids reading absence as quiet.
    return "unread", "no readable limb"


# `session-liveness.check()` returns `(exit_code, message, row)` — an EXIT CODE, not a
# verdict word. Measured 2026-10-21: a first version compared its return against
# `"LIVE"`/`"ABSENT"` and every row silently read `UNKNOWN` against a store that was
# answering `ABSENT` correctly, which is the failure direction that hides a dead owner.
# `AMBIGUOUS` and `MALFORMED` are verdicts about the ARGUMENT, so neither may become
# `live` or `absent` — the mapping is `agents/goal-drive.md`'s own table.
LIVENESS_BY_EXIT = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "UNKNOWN", 4: "UNKNOWN"}


def liveness(session_ids, endpoint, registry_dir):
    """`LIVE` | `ABSENT` | `UNKNOWN` — alive if ANY id in the set returns LIVE."""
    if not session_ids:
        return "UNKNOWN"
    probe = _load("session-liveness")
    verdicts = []
    for sid in session_ids:
        try:
            code, _msg, _row = probe.check(sid, endpoint=endpoint, registry_dir=registry_dir)
        except Exception:
            code = 2  # an exception escaping the probe is UNKNOWN, never ABSENT
        verdicts.append(LIVENESS_BY_EXIT.get(code, "UNKNOWN"))
    if "LIVE" in verdicts:
        return "LIVE"
    if verdicts and all(v == "ABSENT" for v in verdicts):
        return "ABSENT"
    return "UNKNOWN"


def feed_panes(endpoint):
    """`(set_of_panes, state)` from the feed's `Needs you` section.

    `--all` is part of the call, not a preference: the section caps at 20 rows and
    withholds the rest, so a capped read turns a gated row past the cap into a false
    `clear` — the defect this verdict exists to remove.
    """
    cmd = [sys.executable, os.path.join(HERE, "who-needs-me.py"),
           "--section", "needs-you", "--all"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set(), "unread"
    if out.returncode != 0:
        return set(), "unread"
    panes = set()
    for line in out.stdout.splitlines():
        m = PANE_RE.match(line)
        if m:
            panes.add(m.group(1))
    return panes, "read"


def feed_verdict(session_ids, gated_sessions, state):
    """`gated` | `clear` | `unread` — `clear` only from a read that actually ran."""
    if state != "read":
        return "unread"
    if not session_ids:
        return "clear"
    return "gated" if any(s in gated_sessions for s in session_ids) else "clear"


def gated_sessions_from_panes(panes, attention_dir):
    """Map the feed's pane ids to session ids through the events log.

    The feed prints **pane ids only**, and `wezterm cli list` carries no session id,
    so the events log is the only join — the one `gate-owner-filter.py`'s
    `session_for_pane` already performs, reused here rather than re-derived.
    """
    if not panes:
        return set()
    filt = _load("gate-owner-filter")
    try:
        items = filt.log_items(attention_dir)
    except Exception:
        return set()
    out = set()
    for pane in panes:
        sid = filt.session_for_pane(pane, items)
        if sid:
            out.add(sid)
    return out


# --------------------------------------------------------------------------- #
# stuck classification
# --------------------------------------------------------------------------- #


def denial_marker(session_ids, attention_dir):
    """The worker's own denial declaration, or None.

    A classifier denial raises **no** `permission` gate: the attention event log has
    no `answer`/`decision` field at all and its only kinds are `idle`/`question`/
    `permission`, so nothing parks, no card exists and the feed reads `clear`. The
    worker therefore writes the fact down itself — `<sid>.denial.json`, the sibling of
    the `.tool.json` marker the hook already writes — before it messages its manager.
    Reading that file is the only durable way the manager can see the denial.
    """
    for sid in session_ids:
        path = os.path.join(attention_dir, f"{sid}.denial.json")
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            return rec
    return None


def stuck_verdict(row, session_ids, live, feed, interval, now, attention_dir=None):
    """`(verdict, reason)` — denied | frozen | none | unread.

    Two carriers, both of which today render `progressing`:

    * **denied** — the worker declared a denial it could not get past (its
      `<sid>.denial.json` marker, written before it messages its manager). The feed
      cannot carry this class at all, so without this carrier a denied worker is
      invisible for as long as it keeps retrying.
    * **frozen** — the task file has not moved for a whole interval while its owner
      is LIVE. That is shorter than the park cell's 2x threshold, and unlike it this
      is a stuck claim rather than a check-in.

    `none` is only returned when both carriers were actually examined.
    """
    if live != "LIVE":
        return "none", ""
    denial = denial_marker(session_ids, attention_dir or ATTENTION_DIR)
    if denial:
        tool = denial.get("tool") or "a tool"
        return "denied", f"{tool} denied — {denial.get('reason') or 'no reason recorded'}"
    if feed == "gated":
        # A genuinely gated worker is reported by the park cell, never double-reported.
        return "none", ""
    mtime = row.get("mtime")
    if mtime is None:
        return "unread", "no task-file mtime"
    age = now - float(mtime)
    if age >= interval:
        return "frozen", f"task file frozen {int(age // 60)}m (>= {int(interval // 60)}m) while live"
    return "none", ""


# --------------------------------------------------------------------------- #
# the readings
# --------------------------------------------------------------------------- #


def slug(topic):
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")


def readings_path(subject):
    return os.path.join(STATE_DIR, f"{slug(subject)}.readings")


def read_tracked(path):
    """The tracked set, one name per line. A missing file is an empty set, not a crash."""
    try:
        with open(path, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return []


def compute(subject, tracked_path, vault, interval, now=None,
            endpoint=None, registry_dir=None, attention_dir=None,
            sessions_dir=None, projects_dir=None):
    """Build the readings document. Pure over its injected paths."""
    now = time.time() if now is None else now
    attention_dir = attention_dir or ATTENTION_DIR
    wnm = _load("who-needs-me")
    if projects_dir:
        wnm.PROJECTS_DIR = projects_dir
    predispatch = _load("manager-predispatch")

    members = read_tracked(tracked_path)
    rows = predispatch.resolve_tracked(vault, members) if vault else []
    if not rows:
        rows = [{"name": n, "mtime": None, "session": None, "sessions": []} for n in members]

    registry = wnm.registry_records(sessions_dir)
    panes, feed_state = feed_panes(endpoint)
    gated = gated_sessions_from_panes(panes, attention_dir)

    out_rows = {}
    for row in rows:
        name = row.get("name") or "?"
        # `read_task` already carries the FULL id set under `sessions` — the
        # `claude_session_id` key plus every `metrics_sessions` id — so nothing is
        # extracted here. A row with no ids keeps an empty set, which is a real
        # answer (no owner) and not a failed read.
        ids = list(row.get("sessions") or [])
        live = liveness(ids, endpoint, registry_dir)
        fl, limb = in_flight(ids, registry, attention_dir, now, wnm.session_transcript_age)
        fv = feed_verdict(ids, gated, feed_state)
        ew = external_wait(ids, attention_dir)
        sv, sreason = stuck_verdict(row, ids, live, fv, interval, now, attention_dir)
        out_rows[name] = {
            "ids": ids,
            "liveness": live,
            "in_flight": fl,
            "in_flight_limb": limb,
            "feed": fv,
            "external_wait": ew,
            "task_mtime": row.get("mtime"),
            "stuck": sv,
            "stuck_reason": sreason,
        }

    # A tracked name that resolved to no task file is NOT silently dropped: an absent
    # row reads exactly like a row that was never declared, and the reader's table
    # would then quietly shrink. It carries the unread vocabulary instead.
    for name in members:
        if name in out_rows:
            continue
        out_rows[name] = {
            "ids": [], "liveness": "UNKNOWN", "in_flight": "unread",
            "in_flight_limb": "no task file resolved", "feed": "unread",
            "external_wait": "unread", "task_mtime": None,
            "stuck": "unread", "stuck_reason": "no task file resolved for this name",
        }

    return {
        "subject": subject,
        "recorded_at": now,
        "recorded_at_iso": datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "park_threshold_seconds": interval,
        "feed_state": feed_state,
        "rows": out_rows,
    }


def write(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


EXIT_REFUSED = 3


def verify(subject, path, interval, now=None):
    """`(exit_code, message)` — is there a readings file this tick can be read from?

    This is the sweep reader's refusal made mechanical. The reader must not degrade to
    `UNVERIFIED` when step 2 was skipped: an `UNVERIFIED` cell renders a table, and a
    table is what made the 12:17 round read as a complete sweep. A named refusal with
    no table is the honest answer, and it is the one `agents/goal-sweep-reader.md`
    states.
    """
    now = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError:
        return EXIT_REFUSED, f"REFUSED: no readings for {subject} — {path} does not exist"
    except ValueError as exc:
        return EXIT_REFUSED, f"REFUSED: readings for {subject} are unreadable — {exc}"
    recorded = doc.get("recorded_at") if isinstance(doc, dict) else None
    if not isinstance(recorded, (int, float)):
        return EXIT_REFUSED, f"REFUSED: readings for {subject} carry no recorded_at"
    age = now - float(recorded)
    if age > interval:
        return EXIT_REFUSED, (
            f"REFUSED: readings for {subject} are {int(age)}s old (limit {interval}s) — "
            "step 2 did not run this tick"
        )
    return 0, f"OK: readings for {subject} recorded {doc.get('recorded_at_iso') or int(recorded)}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compute the round's readings and write one file.")
    ap.add_argument("--tracked", help="the caller's tracked set, one name per line")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--verify", nargs="?", const="", help="check an existing readings file; prints the refusal")
    ap.add_argument("--vault", help="vault root, for resolving a name to its task file")
    ap.add_argument("--interval", type=int, default=900,
                    help="the clamped tick interval in seconds (frozen threshold is 1x)")
    ap.add_argument("--out", help="override the output path")
    ap.add_argument("--endpoint", help="attention-store base URL (tests point this at a fixture)")
    ap.add_argument("--registry-dir", help="override the identity registry path")
    a = ap.parse_args(argv)

    interval = max(int(a.interval), 300)  # the loop's own 5-minute floor
    if a.verify is not None:
        target = a.verify or a.out or readings_path(a.subject)
        code, message = verify(a.subject, target, interval)
        print(message)
        return code
    if not a.tracked:
        ap.error("--tracked is required unless --verify is given")
    doc = compute(a.subject, a.tracked, a.vault, interval, endpoint=a.endpoint,
                  registry_dir=a.registry_dir)
    path = write(a.out or readings_path(a.subject), doc)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
