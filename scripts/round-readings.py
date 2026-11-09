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

**A failure degrades per row, never to a whole-family blank.** The feed family is
`read` / `partial` / `unread`: `partial` keeps every positively-gated row `gated` and
marks only the rows that could own an unattributed gate `unread`. The document carries
`feed_counts`, counted from the rows themselves, so `gated + clear + unread` equals the
row count by construction, and a `probes` list naming every failed probe with its error
— a transient store fault and a genuinely gate-free fleet must be distinguishable
without re-running anything. Measured 2026-11-08 05:06:29Z: a single failed store read
rendered `gated=0 · clear=0 · unread=11` for 11 rows while the feed itself was
answering; a re-run 39 s later read `gated=1 · clear=10 · unread=0`.

**Reuse, never re-derive.** Id sets come from `session-ids.py`, liveness from
`session-liveness.py`, the registry and transcript age from `who-needs-me.py`, the
pane→session join from `gate-owner-filter.py`, and the tracked model from
`manager-predispatch.py`. Each of those owns its question; a second definition here
would drift from it, and the repo's own rule is that the two surfaces must agree.
"""
import argparse
import datetime
import glob
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

# How long a denial declaration may keep a row `stuck: denied`. The marker is a claim
# about a *live* block, and nothing expires it: `commands/worker-drive.md` asks the
# worker to delete it once the step succeeds, but that instruction is unenforced and
# was demonstrably not followed (session 6040cad5's marker outlived hours of a healthy
# worker, 2026-11-03). A ceiling is the backstop for a marker nobody removed.
DENIAL_TTL = 30 * 60

# The feed renders one row per gate as `  [<pane>]  <age>  <name>  <state> · …`, the
# pane column right-aligned to four: `[ 417]`, `[1234]`, `[ hst]` (headless, keyed by
# its `answer:` line) or `[   ?]` (neither). Any row marker counts as a gate; which
# ones resolve to a key is `gate-owner-filter.py`'s `feed_rows`, reused, never re-derived.
ROW_RE = re.compile(r"^\s*\[\s*(\d+|hst|\?)\s*\]")
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


def feed_panes(endpoint=None):
    """The feed read as one dict: `panes`, `owners`, `orphan_owners`, `state`, `error`.

    `--all` is part of the call, not a preference: the section caps at 20 rows and
    withholds the rest, so a capped read turns a gated row past the cap into a false
    `clear` — the defect this verdict exists to remove.

    ⚠️ **One retry, then the failure is NAMED — never rendered as a verdict about
    the rows.** `who-needs-me.py` globs every session's event log and reads the
    store and transcripts; under load it can exceed the 60 s bound and exit
    non-zero, and one such miss blanked the whole feed family for a round
    (measured 2026-11-08 05:06:29Z: `gated=0 · clear=0 · unread=11` for 11 rows,
    while a direct `who-needs-me.py --section needs-you` in the same minute
    returned `Needs you (25)`, exit 0; a re-run 39 s later read
    `gated=1 · clear=10 · unread=0`). A transient that clears in under a minute is
    exactly what a single retry absorbs; after it, the failure is reported as
    `error` and every row is honestly `unread` because no feed read succeeded.

    ⚠️ **A fallback render is retried too.** When `who-needs-me.py` answers exit 0
    from the event log (`STORE_FALLBACK_NOTE`), that read cannot certify a row
    clear — so one retry is worth its cost, and a store that recovered within the
    round then produces a complete read instead of a `partial` one. A second
    fallback is returned as `partial`, named.
    """
    cmd = [sys.executable, os.path.join(HERE, "who-needs-me.py"),
           "--section", "needs-you", "--all"]
    env = None
    if endpoint:
        # `--endpoint` is documented as the store URL the caller injects; the
        # subprocess reads it from the environment, so pass it through here rather
        # than leaving the flag a no-op.
        env = dict(os.environ, ATTENTION_STORE_URL=endpoint)
    fallback_note = _load("who-needs-me").STORE_FALLBACK_NOTE
    last = "no attempt ran"
    fallback = None
    for attempt in range(2):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                 env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if out.returncode != 0:
            tail = (out.stderr or out.stdout or "").strip().splitlines()
            last = f"exit {out.returncode}: {tail[-1] if tail else 'no output'}"
            continue
        if attempt == 0 and fallback_note in out.stdout:
            fallback = parse_feed(out.stdout)
            continue
        return parse_feed(out.stdout)
    if fallback is not None:
        return fallback
    return {
        "panes": set(),
        "owners": {},
        "orphan_owners": [],
        "state": "unread",
        "error": f"who-needs-me.py --section needs-you --all failed twice: {last}",
    }


def parse_feed(text):
    """The feed's rendered rows as `panes`, `owners`, `orphan_owners`, `state`, `error`.

    Keys are digit panes and `hst:<store item id>` keys, exactly as
    `gate-owner-filter.feed_rows` reads them. A pane-less row also carries the
    session that owns it (`owner_of`), so a gate is attributable without a second
    store read.

    A row marker that yields NEITHER a key nor an owner is a gate nobody can
    attribute. That sets `state: partial` — never `read`, because a `clear` from a
    read that saw an unattributable gate would let that gate's worker read clear.
    `partial` is still not a whole-family blank: the rows that ARE gated keep their
    verdict, and only the rows that could own the unattributed gate read `unread`.
    """
    lines = text.splitlines()
    filt = _load("gate-owner-filter")
    panes = {pane for pane, _ in filt.feed_rows(lines)}
    owners, orphan_owners = {}, []
    unkeyed = 0
    for index, line in enumerate(lines):
        m = ROW_RE.match(line)
        if not m:
            continue
        token = m.group(1)
        owner = filt.owner_of(lines, index)
        if token == "?":
            # A pane-less row with nothing to answer is still a gate; it just has
            # no key. Its owner line makes it attributable all the same.
            if owner:
                orphan_owners.append(owner)
            else:
                unkeyed += 1
            continue
        if token == "hst":
            key = filt.headless_key(lines, index)
            if key is None:
                if owner:
                    orphan_owners.append(owner)
                else:
                    unkeyed += 1
                continue
            if owner:
                owners[key] = owner
    state = "read" if unkeyed == 0 else "partial"
    error = None
    if unkeyed:
        error = (f"{unkeyed} feed row(s) name neither a key nor an owner — their "
                 "gates cannot be attributed to a tracked row")
    # `who-needs-me.py` falls back to the event log when the store does not answer,
    # and on that branch it renders `Needs you (0)` for a fleet that may hold open
    # store-only items. Reading that as a complete, gate-free feed is the fail-open
    # direction — the mirror of the fail-closed defect above: a fallback read cannot
    # certify any row `clear`, so the family is `partial` and the probe is named.
    if _load("who-needs-me").STORE_FALLBACK_NOTE in text:
        state = "partial"
        error = error or (
            "who-needs-me.py read the event log because the attention store was "
            "unreachable — a fallback read cannot certify a row clear")
    return {"panes": panes, "owners": owners, "orphan_owners": orphan_owners,
            "state": state, "error": error}


def feed_verdict(session_ids, gated_sessions, state):
    """`gated` | `clear` | `unread` — `clear` only from a read that actually ran.

    `gated` wins over every state: a row positively identified in the feed is
    gated whatever happened to the rest of the read. `partial` is a read that ran
    but could not attribute every gate it saw, so a row that is not positively
    gated cannot be certified clear — it may own the unattributed gate. That is
    what makes the failure per-row: the gated rows keep their verdict and only
    the rest fall to `unread`.
    """
    if any(s in gated_sessions for s in session_ids):
        return "gated"
    if state == "read":
        return "clear"
    return "unread"


def gated_sessions_from_panes(feed, attention_dir, store_endpoint=None):
    """`(gated_session_ids, state, error)` for the feed's rows.

    Digit panes join through the events log. `hst:<id>` keys join through the
    owner the row itself declares (`owner_of`); only a feed that carries no owner
    line (an older build) falls back to the attention store, and that fallback is
    the checked read, so a store failure is NAMED rather than rendered as a
    per-row verdict.

    A failed log read is `unread` for the whole family — no digit pane can be
    resolved — and it is named. An `hst:` key that resolves to no session leaves
    the read `partial`: the keys that did resolve keep their rows gated.
    """
    panes = set(feed.get("panes") or ())
    owners = dict(feed.get("owners") or {})
    out = set(feed.get("orphan_owners") or ())
    state = feed.get("state") or "read"
    # `error` here names only a failure THIS join hit. The feed read's own error
    # rides on `feed["error"]` and is reported by the caller, so echoing it here
    # would name the same failing probe twice.
    error = None
    if not panes:
        return out, state, error
    filt = _load("gate-owner-filter")
    digit = [p for p in panes if not filt.is_headless_key(p)]
    hst = [p for p in panes if filt.is_headless_key(p)]
    if digit:
        try:
            items = filt.log_items(attention_dir)
        except Exception as exc:
            return out, "unread", f"event-log read failed: {type(exc).__name__}: {exc}"
        for pane in digit:
            sid = filt.session_for_pane(pane, items)
            if sid:
                out.add(sid)
    unresolved = [key for key in hst if not owners.get(key)]
    for key in hst:
        sid = owners.get(key)
        if sid:
            out.add(sid)
    if unresolved:
        try:
            records, store_error = filt.headless_items_checked(unresolved, store_endpoint)
        except Exception as exc:
            records, store_error = [], f"attention store read failed: {type(exc).__name__}: {exc}"
        if store_error:
            return out, "partial", store_error
        found = {r["pane"]: r.get("session_id") for r in records}
        for key in unresolved:
            sid = found.get(key)
            if sid:
                out.add(sid)
            else:
                state = "partial"
                error = error or (f"{key} resolves to no session in the attention "
                                  "store")
    return out, state, error


# --------------------------------------------------------------------------- #
# stuck classification
# --------------------------------------------------------------------------- #


def denial_markers(session_ids, attention_dir):
    """Every readable denial declaration as `(sid, record)`, in id order.

    A classifier denial raises **no** `permission` gate: the attention event log has
    no `answer`/`decision` field at all and its only kinds are `idle`/`question`/
    `permission`, so nothing parks, no card exists and the feed reads `clear`. The
    worker therefore writes the fact down itself — `<sid>.denial.json`, the sibling of
    the `.tool.json` marker the hook already writes — before it messages its manager.
    Reading that file is the only durable way the manager can see the denial.

    **All** of them, not the first: a row carries every id the task ever ran under
    (`claude_session_id` plus each `metrics_sessions` id), so returning one and
    stopping would let a stale marker on the first id mask a live marker on a later
    one — a false `none` for a worker that is still blocked. The caller walks the list
    and takes the first that still holds.

    Each entry carries its session id, because whether a denial still blocks is
    decided against *that* session's transcript.
    """
    out = []
    for sid in session_ids:
        path = os.path.join(attention_dir, f"{sid}.denial.json")
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append((sid, rec))
    return out


def marker_ts(rec, attention_dir, sid):
    """The marker's own timestamp, falling back to its file mtime.

    The writer stamps `ts` (`commands/worker-drive.md:319`), but a marker written by
    an older build or by hand may not carry it. Falling back to the mtime keeps the
    TTL and the ordering check meaningful instead of silently disabling both.
    """
    ts = rec.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    try:
        return os.path.getmtime(os.path.join(attention_dir, f"{sid}.denial.json"))
    except OSError:
        return None


def _iso_epoch(value):
    """An ISO-8601 transcript timestamp as epoch seconds, or None if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def later_success(sid, tool, since_ts, projects_dir):
    """Did `tool` succeed in `sid` after `since_ts`? True / False / None.

    The transcript carries the two halves in separate records: a `tool_result` block
    names only `tool_use_id`, and the tool's *name* lives on the paired `tool_use`
    block. So the two are joined here — a result attributed to no known tool cannot
    clear anything.

    Success is `is_error` absent or false: the harness omits the key on success rather
    than writing `false`, so `if block.get("is_error"): continue` is the correct test.

    **None means unread, and unread is never a reason to clear.** A transcript that
    could not be opened, or that does not exist, leaves the marker in force — the
    same three-valued discipline the rest of this file follows.
    """
    # `sid` reaches both a glob pattern and a path join from task-file state, so a
    # value carrying a separator could address another session's transcript. The
    # answer is only a boolean and the state is operator-owned, but an exact join
    # costs nothing.
    if not sid or os.path.basename(sid) != sid:
        return None
    try:
        hits = glob.glob(os.path.join(projects_dir, "*", f"{glob.escape(sid)}.jsonl"))
    except OSError:
        return None
    if not hits:
        return None
    try:
        path = max(hits, key=os.path.getmtime)
        with open(path, encoding="utf-8") as fh:
            names = {}
            for line in fh:
                if '"tool_result"' not in line and '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        names[block.get("id")] = block.get("name")
                    elif block.get("type") == "tool_result":
                        if block.get("is_error"):
                            continue
                        if names.get(block.get("tool_use_id")) != tool:
                            continue
                        ts = _iso_epoch(rec.get("timestamp"))
                        if ts is not None and ts > since_ts:
                            return True
    except OSError:
        return None
    return False


def denial_still_blocks(sid, rec, attention_dir, now, projects_dir=None):
    """Whether the declared denial is still a live block.

    Two independent reasons it is not, both measured 2026-11-03 on session 6040cad5:

    * **the same tool later succeeded** — the worker got past the gate, so the
      declaration is about a block that has ended. The comparison is on the tool
      *name*: a success of a different tool says nothing about this gate, and clearing
      on one would un-stick a worker that is still blocked.
    * **the marker is older than `DENIAL_TTL`** — the worker never came back to remove
      it. The TTL is a ceiling on a declaration nothing else expires.

    Everything that cannot be established keeps the denial in force: a missing
    timestamp, a marker with no tool, a transcript that could not be read. That is the
    fail-safe direction — the cost of a stale `denied` is a nudge, the cost of a
    wrong `none` is a blocked worker nobody looks at.
    """
    ts = marker_ts(rec, attention_dir, sid)
    if ts is None:
        return True
    if now - ts > DENIAL_TTL:
        return False
    tool = rec.get("tool")
    if not tool:
        return True
    return later_success(sid, tool, ts, projects_dir or PROJECTS_DIR) is not True


def permission_channel_sid(session_ids, projects_dir, now=None):
    """The first session id whose permission channel is dead, or None.

    A row carries every id it ever ran under, so all of them are read — but only
    those whose transcript is still FRESH (`PERM_CHANNEL_STALE_S`). ⚠️ **Without that
    bound the carrier is a permanent false positive:** the channel window is anchored
    on the transcript's own last stamp, so an id from a session that died with its
    last 15 minutes full of aborts keeps matching forever, and pins a healthy live row
    to `permission-channel`. Measured on this change's first review, 2026-11-09.

    ⚠️ **The RULE is not restated here.** `manager-attention-watch.py` owns the
    window, the threshold, the staleness bound and the reason string; this function
    only supplies the wider read that rule needs and reports the id. A second copy
    would be a second place for the window to drift — and the drift would be silent,
    because too short a window reads as "no aborts" rather than as an error.
    """
    watch = _load("manager-attention-watch")
    now = time.time() if now is None else now
    for sid in session_ids or []:
        if not sid or os.path.basename(sid) != sid:
            continue
        try:
            hits = glob.glob(os.path.join(projects_dir, "*",
                                          glob.escape(sid) + "*.jsonl"))
        except OSError:
            continue
        if not hits:
            continue
        try:
            path = max(hits, key=os.path.getmtime)
            if now - os.path.getmtime(path) > watch.PERM_CHANNEL_STALE_S:
                continue
        except OSError:
            continue
        wide = watch.read_recent_tail(path, watch.PERM_CHANNEL_WINDOW_S)
        if wide is None:
            continue
        if watch.channel_aborts(wide) >= watch.PERM_CHANNEL_MIN:
            return sid
    return None


def stuck_verdict(row, session_ids, live, feed, interval, now, attention_dir=None,
                  projects_dir=None):
    """`(verdict, reason)` — denied | permission-channel | frozen | none | unread.

    Three carriers, all of which would otherwise render `progressing`:

    * **denied** — the worker declared a denial it could not get past (its
      `<sid>.denial.json` marker, written before it messages its manager). The feed
      cannot carry this class at all, so without this carrier a denied worker is
      invisible for as long as it keeps retrying.
    * **permission-channel** — the session's permission channel is aborting, read
      from its transcript by `manager-attention-watch.channel_aborts`. The registry
      reads such a worker `busy` or `waiting` and the feed calls it progressing,
      while every write it exists to make fails; measured 2026-10-28 on three
      sessions, one of them holding a live dev repair it could not run.
    * **frozen** — the task file has not moved for a whole interval while its owner
      is LIVE. That is shorter than the park cell's 2x threshold, and unlike it this
      is a stuck claim rather than a check-in.

    `none` is only returned when every carrier was actually examined.

    ⚠️ **`permission-channel` is read BEFORE the `feed == "gated"` short-circuit, and
    that ordering is the point.** A gated row is reported by the park cell — but a
    park on a session whose channel is dead is a park no answer can reach: the
    operator answers, the delivery aborts, and the row goes back to looking parked.
    The channel death is the more specific fact, exactly as `is_gated` in the watcher
    puts its `stuck` limb ahead of the registry half for the same reason.

    The `denied` carrier is a *live* claim, so it is read through
    `denial_still_blocks` rather than taken at face value — a marker whose tool has
    since succeeded, or one older than `DENIAL_TTL`, no longer holds. A stale marker
    falls through to the frozen carrier instead of short-circuiting to `none`, because
    a live worker with a frozen task file is still frozen.
    """
    if live != "LIVE":
        return "none", ""
    attention_dir = attention_dir or ATTENTION_DIR
    for sid, rec in denial_markers(session_ids, attention_dir):
        if denial_still_blocks(sid, rec, attention_dir, now, projects_dir):
            tool = rec.get("tool") or "a tool"
            return "denied", f"{tool} denied — {rec.get('reason') or 'no reason recorded'}"
    dead = permission_channel_sid(session_ids, projects_dir or PROJECTS_DIR, now)
    if dead:
        return "permission-channel", f"{dead[:8]} — permission channel aborting (Stream closed)"
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


# `read` < `partial` < `unread`: the worse of two probe states wins, so one failed
# probe is never washed out by a sibling that succeeded.
_STATE_RANK = {"read": 0, "partial": 1, "unread": 2}


def worst_state(*states):
    return max(states, key=lambda s: _STATE_RANK.get(s, 2))


def compute(subject, tracked_path, vault, interval, now=None,
            endpoint=None, registry_dir=None, attention_dir=None,
            sessions_dir=None, projects_dir=None, feed=None):
    """Build the readings document. Pure over its injected paths.

    `feed` injects the feed read (tests do); when None it is taken from
    `feed_panes`, which runs `who-needs-me.py`.
    """
    now = time.time() if now is None else now
    attention_dir = attention_dir or ATTENTION_DIR
    wnm = _load("who-needs-me")
    if projects_dir:
        wnm.PROJECTS_DIR = projects_dir
    predispatch = _load("manager-predispatch")

    members = read_tracked(tracked_path)
    rows, matched_members = (
        predispatch.resolve_tracked_by_member(vault, members) if vault else ([], []))
    # ⚠️ NO synthesised rows. A placeholder carrying `sessions: []` is not an observation:
    # `feed_verdict([], gated, "read")` answers `clear` and `external_wait([])` answers
    # `none`, so N placeholders render `clear=N · none=N` for a fleet that was never read.
    # Measured 2026-11-08 (Development Pipeline Throughput): the tracked set was 38
    # vault-relative page paths, which `resolve_tracked` matches against neither a task's
    # name nor its `goals:`, so every one of the 38 synthesised rows read `clear` while 543
    # real task rows — 387 of them carrying a session id — went unseen. An unresolved
    # member is routed to the unread vocabulary below instead, which is the answer the
    # document already had and could never reach.
    matched = set(matched_members)
    unresolved = [n for n in dict.fromkeys(members) if n not in matched]

    registry = wnm.registry_records(sessions_dir)
    feed = feed_panes(endpoint) if feed is None else feed
    gated, join_state, join_error = gated_sessions_from_panes(
        feed, attention_dir, endpoint)
    feed_state = worst_state(feed.get("state") or "unread", join_state)
    # The failing probes, named. A `clear` that came from a read nobody can point
    # at is indistinguishable from a genuinely gate-free fleet, and that is the
    # distinction the document must carry.
    probes = []
    if feed.get("error"):
        probes.append({"probe": "who-needs-me.py --section needs-you --all",
                       "error": feed["error"]})
    if join_error:
        probes.append({"probe": "gate-owner-filter.py feed join",
                       "error": join_error})

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
        sv, sreason = stuck_verdict(row, ids, live, fv, interval, now, attention_dir,
                                    projects_dir or PROJECTS_DIR)
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
            # The marker the families are read through: a resolved row's empty id set is a
            # real answer (no owner), an unresolved row's is not an observation at all.
            "resolved": True,
        }

    # A tracked name that resolved to no task file is NOT silently dropped: an absent
    # row reads exactly like a row that was never declared, and the reader's table
    # would then quietly shrink. It carries the unread vocabulary instead.
    for name in unresolved:
        if name in out_rows:  # a name that is both a task and a member keeps its row
            continue
        out_rows[name] = {
            "ids": [], "liveness": "UNKNOWN", "in_flight": "unread",
            "in_flight_limb": "no task file resolved", "feed": "unread",
            "external_wait": "unread", "task_mtime": None,
            "stuck": "unread", "stuck_reason": "no task file resolved for this name",
            "resolved": False,
        }

    # Counted from the rows themselves, not accumulated beside them, so the
    # document cannot carry a total that disagrees with its own table:
    # `gated + clear + unread == len(rows)` is structural, not asserted.
    feed_counts = {"gated": 0, "clear": 0, "unread": 0}
    for rec in out_rows.values():
        feed_counts[rec["feed"]] += 1

    # What the tracked set actually resolved to, as counts rather than an adjective.
    # `state` is the one word a caller may branch on, and two of its four values mean
    # NO family value in this document came from an observation: `unresolved` (members
    # were declared, none admitted a task) and `empty` (no members at all). A reader
    # that renders a table from either renders a quiet fleet out of nothing — the
    # 2026-11-08 defect. `partial` names, by name, the members that admitted nothing.
    if not members:
        resolution_state = "empty"
    elif not rows:
        resolution_state = "unresolved"
    elif unresolved:
        resolution_state = "partial"
    else:
        resolution_state = "resolved"

    return {
        "subject": subject,
        "recorded_at": now,
        "recorded_at_iso": datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        # The round this file belongs to, so a later reader can tell a fresh reading
        # from one the previous round left behind without opening the receipt.
        "round_started_at": _round_started_at(subject),
        # The park cell's threshold is 2x the cadence, NOT 1x -- the runbook's rule,
        # and what sweep-gate.py's park_threshold_seconds() derives. The frozen
        # threshold above is the 1x one; the two are different cells and reading this
        # field as the frozen threshold (or the reverse) is how the sweep reader ends
        # up reconciling two sources on every round.
        "park_threshold_seconds": 2 * interval,
        "feed_state": feed_state,
        # The feed family's own tally, taken from the rows above: it sums to the
        # row count by construction, so a reader can check the invariant without
        # re-counting.
        "feed_counts": feed_counts,
        # Did the tracked set resolve to anything? `resolved_rows` is the count of
        # TASK rows, never the member count — a goal member admits many rows.
        "resolution": {
            "state": resolution_state,
            "tracked": len(members),
            "matched_members": len(matched),
            "resolved_rows": len(rows),
            "unresolved": unresolved,
        },
        # Every probe that failed this round, with its own error. Empty on a clean
        # read; a transient store fault and a genuinely gate-free fleet are then
        # distinguishable without re-running anything.
        "probes": probes,
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
    # The round's own start is the authority when a receipt exists: readings taken
    # BEFORE the round opened belong to the previous round, and an age bound alone
    # cannot tell those apart from a fresh file on a slow tick.
    started = _round_started_at(subject)
    if started is not None and float(recorded) < started:
        return EXIT_REFUSED, (
            f"REFUSED: readings for {subject} predate this round's start "
            f"(recorded {int(float(recorded))}, round opened {int(started)}) — "
            "step 2 did not run this tick"
        )
    age = now - float(recorded)
    if age > interval:
        return EXIT_REFUSED, (
            f"REFUSED: readings for {subject} are {int(age)}s old (limit {interval}s) — "
            "step 2 did not run this tick"
        )
    return 0, f"OK: readings for {subject} recorded {doc.get('recorded_at_iso') or int(recorded)}"


def _round_started_at(subject):
    """The receipt's `round_started_at`, or None when there is no readable receipt."""
    try:
        with open(os.path.join(STATE_DIR, f"{slug(subject)}.json"), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    started = d.get("round_started_at") if isinstance(d, dict) else None
    return float(started) if isinstance(started, (int, float)) else None


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
