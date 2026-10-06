#!/usr/bin/env python3
"""List Claude Code sessions that need the operator, joined to WezTerm panes.

Reads the attention store at `$ATTENTION_STORE_URL` (default `localhost:18080`)
when it answers, and falls back to the hook-written files when it does not, so a
stopped store blinds no manager. Which source answered is printed once.
  GET /api/1.0/attention  the store — authoritative for WHICH items are open; it
                      resolves the producer's liveness server-side and drops dead
                      askers as a side effect of the read
  <sid>.events.jsonl  append-only event log — one `open` line per item, one `close`
                      line when it clears; folded by `load_events()` into current
                      state. Also the join source for the event-time fields the
                      store deliberately does not carry (pane, cwd, host, transcript)
  <sid>.needs.json    the older one-file-per-session snapshot
  <sid>.tool.json     session inside a tool call (stale => probably stuck)
Sections:
  Needs you       — permission prompt or open question (oldest first)
  Probably stuck  — inside one tool call longer than --stuck-min (default 20)
  Reapable        — close gate whose anchored task is already finished
  Idle            — turn ended, waiting for a prompt (counted, never listed)
--jump PANE activates that WezTerm pane.

The feed answers "was a gate raised", never "is a gate open" — it is hook-written
and goes stale until the session's next tool call. Read the pane before reporting
any gate as open.

A row is rendered only when its session is still live AND its pane still exists.
Liveness is read from the session registry `~/.claude/sessions/<pid>.json`, per
`vault-cli/docs/session-liveness.md`; the same source is read by the attention
store's `pkg/session-liveness-checker.go`. Pane existence alone is not liveness —
panes are renumbered and reused, and a session killed without `SessionEnd` leaves
its item open on a pane that outlives it, which is how orphans reached this feed.
"""
import argparse, glob, importlib.util, json, math, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime

STATE = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser("~/.claude/state/attention")
OBSIDIAN = os.path.expanduser("~/Documents/Obsidian")
# The session registry: one `<pid>.json` per live session, deleted on exit, so its
# presence is the authoritative live-vs-exited probe. Overridable so the reader can
# be pointed at a fixture, and so a caller can mirror the store's own override.
SESSIONS_DIR = os.environ.get("SESSIONS_DIR") or os.path.expanduser("~/.claude/sessions")
PROJECTS_DIR = os.environ.get("PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")
# Matched to vault-cli's per-session flock, per `session-liveness.md`. A transcript
# written inside this window counts as fresh.
LIVE_WINDOW = 5 * 60

# How long a posted-closer record stays authoritative. The echo it suppresses
# always lands at the END of the same turn as its post, so the window only has to
# outlast a turn. Unbounded, the record lives as long as the session id, so an
# IDENTICAL closer text re-raised on a genuinely later turn would be suppressed
# forever — "identity, not recency" is the right call for a *different* ask, but
# unbounded text equality is a weaker identity than it looks. Past the window the
# record is ignored and the closer is judged on its own merits.
def _ttl_from_env():
    """The posted-closer window, falling back to the default on a bad override.

    Guarded because this is a documented tunable, so an operator WILL set it, and
    an unguarded `float()` at module scope raises ValueError at import — killing
    the reader outright rather than costing it the window. The hook's
    `_ttl_from_env` carries the full rationale; the two halves must agree on the
    window, so the shape is mirrored here deliberately.
    """
    try:
        ttl = float(os.environ.get("ATTENTION_POSTED_CLOSER_TTL") or 6 * 3600)
    except ValueError:
        return 6 * 3600
    # `inf` and `nan` parse as floats but make every comparison false, so the
    # record would never expire — the same unbounded case a typo would have
    # caused, reached by a value that looks numeric. Zero and negatives would
    # silently disable suppression instead. Only a finite positive window is a
    # window; anything else falls back to the default rather than to no bound.
    if not math.isfinite(ttl) or ttl <= 0:
        return 6 * 3600
    return ttl


POSTED_CLOSER_TTL = _ttl_from_env()

# The attention store. Tried first; the event log is the fallback, so a stopped
# store degrades the feed rather than emptying it.
STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
# Short on purpose. The store is local, and this runs inside a manager's sweep:
# a hung store must cost a fallback, never a stalled manager. 3s is generous for
# a localhost socket and still far below any sweep's patience.
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))

# The watcher's working dir. It owns the enrichment records the store has no room
# for — `session_name`, `mode`, `owner`, `topic`, `task` — keyed by producer_id.
# The store carries exactly the schema's fourteen fields, so the name a manager
# reads off a row is joined from here, not fetched from the store.
WATCHER_DIR = os.environ.get("ATTENTION_WATCHER_DIR") or os.path.expanduser(
    "~/.claude/state/attention-watcher"
)
ENRICHED = os.path.join(WATCHER_DIR, "enriched")

# The store's `answer_mechanism` enum is {message, permission, ack}; this
# reader's `kind` vocabulary is {permission, question}. `ack` maps to nothing —
# an acknowledgement is not a gate and must not be rendered as one. The mapping
# is the watcher's own (`MECHANISM`), inverted.
KIND_FROM_MECHANISM = {"permission": "permission", "message": "question"}

# Rows rendered under `Needs you` before the list is cut. The manager reads a
# summary, not a wall — the point is to help the operator choose, and a reader
# that prints twenty-five undifferentiated rows has failed to triage rather than
# failed to report. `--all` lifts the cap.
CAP = 20

# Which source answered the last `needs_source()` call: None for the store, or
# the one-line note printed when the fallback carried the read. A silent
# fallback would read as a working store and hide the outage.
SOURCE_NOTE = None

# session_id -> enrichment record. One file per session, many items per session.
_ENRICHED_CACHE = {}

# sessions_dir -> {session_id: name}, or None when that dir cannot be read. The
# registry is read once per run and asked two questions of the same read — which
# sessions are live, and what each is called now.
_REGISTRY_CACHE = {}
# The same glob pass's `status` field, kept beside the names so `busy_session_ids()`
# reads the registry zero extra times. Two reads of this directory could disagree
# about which entries exist, which is why `read_registry()` shares one pass.
_REGISTRY_STATUS_CACHE = {}
# The same pass's FULL records (`pid`, `alive`, …), kept for `registry_records()`.
# `read_registry()` narrows each entry to its name because that is all the ownership
# check needs; the plausibility check needs the pid and the liveness verdict, and
# re-reading the directory for them would be the second instrument this module's
# header warns against — one glob, one read, three views.
_REGISTRY_RECORDS_CACHE = {}

# Closer verbs describing a parked wait rather than an open gate. `later (on
# <trigger>):` names the event that resumes the work; until it fires there is
# nothing for the operator to answer. Measured 2026-09-19: two such closers sat at
# the top of `Needs you` for over five hours, reported as neglect.
PARKED_VERBS = ("later (on ",)

# A close gate whose anchored task is finished is the operator's call but not an
# open gate — it is reapable, and is listed separately.
#
# Two closer forms are sanctioned and both must count. `vault-cli:sync-progress`
# Phase 6 emits `approve: /vault-cli:session-close` verbatim; the operator's global
# ⚪ DONE rule prescribes the `pick` form instead. Matching only the first silently
# ignored every session that followed the second (measured 2026-09-19, pane 17).
CLOSE_GATE = "approve: /vault-cli:session-close"
_CLOSE_TARGET = "/vault-cli:session-close"
# The first (recommended) disposition of a `pick` menu, up to the `· 2.` separator.
_PICK_FIRST = re.compile(r"^pick\s*[—–-]\s*1\.\s*(.*?)(?:\s*·\s*2\.|$)")

# `&nbsp;` is six literal characters, not whitespace, so strip() does not remove it.
_ENTITY = re.compile(r"&(?:nbsp|#160|#xa0);", re.I)
_PANE_REF = re.compile(r"\bpane\s*#?\s*(\d+)\b", re.I)
_TASK_LINK = re.compile(r"📌 Task: \[[^\]]*\]\(obsidian://open\?vault=[^&]+&file=([^)]+)\)")


def normalize_closer(text):
    """Reduce a closer verb to a comparable form.

    Panes have emitted `👤 You: &nbsp;&nbsp;&nbsp;&nbsp;nothing`. The entity is
    text, so `startswith("nothing")` missed it and a parked session was
    reclassified as an open question. Strip entities and collapse whitespace so
    the comparison sees the verb rather than its padding.
    """
    text = _ENTITY.sub(" ", text or "")
    return " ".join(text.split()).strip()


def is_parked_verb(detail):
    """True when a closer names the event that resumes the work.

    `later (on <trigger>):` is a parked wait: until that event fires there is
    nothing for the operator to answer, so it is not an open gate regardless of
    age. Measured 2026-09-19: two such closers sat at the top of `Needs you` for
    over five hours, reported as neglect.

    Checked on both paths on purpose. A parked closer reaches the feed two ways —
    re-derived from an `idle` record's transcript, or written straight in by the
    hook as `kind: "question"`. Both live instances measured 2026-09-19 arrived the
    second way, so a reclassify-only check would have missed every real one.
    """
    return normalize_closer(detail).startswith(PARKED_VERBS)


class StoreUnreachable(Exception):
    """The store did not answer. Retryable, and the event log covers it."""


def _parse_ts(value):
    """An RFC3339 timestamp as epoch seconds, or now when unparseable.

    `created_at` is `libtime.DateTime`, which marshals RFC3339Nano. Falling back
    to now is deliberate: a row with an unreadable timestamp must still render,
    and an age reading slightly young beats a row that vanishes.
    """
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def store_items():
    """Open items from the attention store, or raise StoreUnreachable.

    The store is authoritative for WHICH items are open: it resolves the
    producer's liveness server-side and drops dead askers as a side effect of
    the read, so a dead producer costs this reader nothing. It is not
    authoritative for the event-time fields — `pane`, `cwd`, `host` and
    `transcript` belong to the event, not the session, so the store has no room
    for them and the hook wrote them on its own log line instead.
    """
    try:
        with urllib.request.urlopen(
            STORE + "/api/1.0/attention", timeout=STORE_TIMEOUT
        ) as resp:
            return json.loads(resp.read().decode("utf-8") or "[]")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise StoreUnreachable(str(e)) from e


def enrichment(session_id):
    """The watcher's enrichment record for a session, or {}.

    This is where a row's `session_name` comes from. The store cannot carry it,
    so an absent record means an absent name — rendered absent, never guessed
    from the pane title or the session id.

    Cached per session: one file per session, but many items per session, and
    the caller runs inside a manager's sweep.
    """
    if session_id in _ENRICHED_CACHE:
        return _ENRICHED_CACHE[session_id]
    try:
        with open(os.path.join(ENRICHED, session_id + ".json"), encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        rec = {}
    _ENRICHED_CACHE[session_id] = rec
    return rec


def normalize_store_item(item, events):
    """One store item as the record shape every classifier below already reads.

    The store is a THIRD producer of one shape, not a second code path:
    `answered()`, `is_open_gate()`, `is_reapable()` and `name_of()` stay
    untouched, so the store and the event log cannot drift apart in
    classification. Returns None for an item this reader has no kind for — an
    `ack` is an acknowledgement, not a gate, and rendering it as one would
    invent a block.
    """
    kind = KIND_FROM_MECHANISM.get(item.get("answer_mechanism") or "")
    if kind is None:
        return None
    # Join on `dedup_key`: the watcher sets it to the LOG's item_id, which is
    # what makes the event-time fields recoverable at all.
    ev = events.get(item.get("dedup_key") or "") or {}
    rec = {
        # The LOG's item_id, and the join key every classifier above already reads.
        # It is deliberately NOT the store's: `events` is keyed by the log's value,
        # so repointing this would silently break the event-time join.
        "item_id": item.get("dedup_key") or item.get("item_id"),
        # The STORE's own id, kept separately because it is the key that answers the
        # item. `attention-answer.py answer ITEM_ID` POSTs
        # /api/1.0/attention/{ITEM_ID}/answer, which resolves only against the store's
        # id — measured 2026-09-25, the two differ (`0f701f0b…` vs `f064a3c6…`), and a
        # handover built from `item_id` would be unresolvable by construction.
        "store_item_id": item.get("item_id") or "",
        "session_id": item.get("producer_id") or "",
        "kind": kind,
        "detail": item.get("payload") or "",
        "state": "open" if item.get("state") == "open" else "answered",
        "ts": _parse_ts(item.get("created_at")),
        "source": "store",
    }
    # Absent stays absent: only fields the event actually carried are copied, so
    # a missing pane is missing rather than defaulted to something plausible.
    for key in ("pane", "cwd", "host", "transcript", "event", "options", "tool_name"):
        if ev.get(key) is not None:
            rec[key] = ev[key]
    # The name the session actually holds, joined from the watcher's enrichment
    # record — the store has no room for it. Absent stays absent.
    enriched = enrichment(rec["session_id"])
    for key in ("session_name", "mode", "owner", "topic", "task"):
        if enriched.get(key):
            rec[key] = enriched[key]
    return rec


def needs_source():
    """Open items from the store, or from the event log when it is unreachable.

    The store is tried first and wins when it answers. The log is the fallback,
    and which one carried the read is recorded in SOURCE_NOTE for the caller to
    print: a fallback that announced nothing would be indistinguishable from a
    healthy store, which is the failure this exists to prevent.
    """
    global SOURCE_NOTE
    try:
        items = store_items()
    except StoreUnreachable:
        SOURCE_NOTE = "store unreachable — reading event log"
        # Stamp every row this fallback produced, so the ROW says where it came from.
        # The note above is printed once, before any row, and every row then renders
        # through the same `row()` a live read uses — so a manager could not tell a
        # replayed gate from a live one and acted on gates answered hours ago.
        # Measured 2026-09-27: a 7 s store read against this reader's 3 s timeout
        # rendered 18 rows where the live store held 2, and no row said which read
        # produced it. `row()` renders this stamp.
        replayed = load_events()
        for rec in replayed:
            rec["replayed"] = True
        return replayed
    SOURCE_NOTE = None
    events = {r.get("item_id"): r for r in load_events()}
    out = []
    for item in items:
        rec = normalize_store_item(item, events)
        if rec is not None:
            # ⚠️ Mark the row as STORE-VOUCHED. The store resolves the producer's
            # liveness server-side and drops dead askers as a side effect of this very
            # read (`store_items()`), so a row that came back from it has ALREADY had
            # its liveness decided — by the component that owns that question, with a
            # better signal than this reader has. `quiet_session_ids()` reads this flag
            # and declines to re-decide it. The flag exists because the reader's own
            # re-judgment was wrong for exactly the class this feed protects: a session
            # blocked on a gate writes nothing, so its transcript ages past
            # `LIVE_WINDOW`, so the reader called it `provably finished` and dropped
            # the row the operator was being waited on. Measured 2026-09-28 on pane
            # 2555: row rendered 09:46:48, gone 09:51:48, back 10:01:49 — while the
            # store held its open item throughout.
            rec["store_vouched"] = True
            out.append(rec)
    # The store carries only the kinds the watcher pushes — `permission` and
    # `question`. `idle` is not one of them, and it is not dead weight: the
    # reader PROMOTES an idle record to a real gate by reading its transcript
    # (`reclassify_idle`), so a store-only read silently drops every gate that
    # arrives that way. Measured 2026-09-21: the log held 16 open `idle` items
    # the store never received. Take them from the log, where they still are.
    seen = {r.get("item_id") for r in out}
    for ev in events.values():
        if ev.get("kind") == "idle" and ev.get("item_id") not in seen:
            out.append(ev)
    return out


def load(suffix):
    """Attention records, minus this session's own.

    A session is never its own attention item: its gate is already in front of the operator
    in its own pane. Listing it makes a manager surface its own question as a peer's, and
    under the ask-here-relay-back rule it would then relay the answer into its own pane.
    Measured 2026-09-17: the fleet-loop session appeared in its own blocked list (tab 0).

    Reads the store first and the hook files second, deliberately. The store is
    authoritative for which items are open, and the hook-written formats are the
    fallback that keeps a stopped store from emptying the feed. Two hook formats
    coexist while the fleet rolls over and both are still read: the append-only
    event log (`<sid>.events.jsonl`, one `open` line per item and one `close`
    line when it clears) and the older `.needs.json` snapshot — a session that
    has not yet restarted still has the latter, and dropping that branch before
    the last one ages out would silently lose it. The `.needs.json` branch is
    removed only once no session writes it.
    """
    me = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    out = []
    covered = set()
    if suffix == "needs":
        for rec in needs_source():
            if me and rec.get("session_id") == me:
                continue
            covered.add(rec.get("session_id"))
            out.append(rec)
    for p in glob.glob(os.path.join(STATE, f"*.{suffix}.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if me and d.get("session_id") == me:
            continue
        # A legacy snapshot for a session the primary source already covers is a
        # stale duplicate, not a second item: `.needs.json` holds one record per
        # session, so merging it re-adds a row the store (or the event log) has
        # already resolved — and the store's whole point is that it resolved it.
        # Sessions the primary source does NOT cover are still merged, which is
        # what keeps a not-yet-rolled-over session from vanishing.
        if d.get("session_id") in covered:
            continue
        out.append(d)
    return out


def load_events():
    """Fold each session's event log into its currently-open items.

    The log is append-only and never rewritten: `open` lines carry the item,
    `close` lines carry only the `item_id` they clear. An item is open exactly
    when its `item_id` has an `open` line and no matching `close`.

    `state` is reconstructed here rather than read from the record, so the whole
    downstream classification (`answered()`, `is_open_gate()`) keeps working
    unchanged against the new format — the record shape stays what every other
    function already expects.

    A malformed line is skipped, not fatal: the hook appends while this reads, so
    a torn final line is expected rather than exceptional.
    """
    out = []
    for p in glob.glob(os.path.join(STATE, "*.events.jsonl")):
        opened, closed = {}, set()
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    iid = r.get("item_id")
                    if not iid:
                        continue
                    if r.get("type") == "open":
                        opened[iid] = r
                    else:
                        closed.add(iid)
        except Exception:
            continue
        for iid, rec in opened.items():
            # `state` mirrors the old on-disk marker so `answered()` is unchanged.
            out.append(dict(rec, state="answered" if iid in closed else "open"))
    return out


def wezterm_panes():
    """The live panes as `pane id -> the wezterm record`, or `None` if the query failed.

    `None` is a distinct answer from `{}` — a failed `wezterm cli list` cannot prove
    a pane is gone any more than it can prove one is live, so no caller may read it
    as "no panes exist". Same convention as `read_registry()` below, same reason.
    """
    try:
        r = subprocess.run(["wezterm", "cli", "list", "--format", "json"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        return {str(p["pane_id"]): p for p in json.loads(r.stdout)}
    except Exception:
        return None


def live_pane_ids():
    """Confirmed pane ids, or `None` when the query failed — see `wezterm_panes()`."""
    p = wezterm_panes()
    return None if p is None else set(p)


_LIVENESS = None


def _session_liveness():
    """Import session-liveness.py (hyphenated filename -> importlib) — the one registry reader."""
    global _LIVENESS
    if _LIVENESS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-liveness.py")
        spec = importlib.util.spec_from_file_location("session_liveness", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIVENESS = mod
    return _LIVENESS


_FLEET_COLOURS = None


def _fleet_colours():
    """Import fleet-colours.py (hyphenated filename -> importlib) — the one pid->tty reader.

    `pid_ttys()` there is the repo's only resolver from a registry pid to its tty, and
    it already carries the two rules this check needs: `None` on a failed `ps`, and
    `??` dropped rather than returned as a tty. A second `ps` parser here would be a
    second instrument over one transport — the drift `read_registry()` above exists to
    prevent, one transport over.
    """
    global _FLEET_COLOURS
    if _FLEET_COLOURS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet-colours.py")
        spec = importlib.util.spec_from_file_location("fleet_colours", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _FLEET_COLOURS = mod
    return _FLEET_COLOURS


def read_registry(sessions_dir=None):
    """The session registry as `session id -> the name it holds now`; `None` if unreadable.

    One read answers both questions the feed asks of this directory, so the two
    cannot disagree about which entries exist:

      * `live_session_ids()` takes the keys — an entry is deleted when its session
        exits, so presence means live.
      * the ownership check takes the values — the name the session holds *now*.

    The values are why this is a map rather than a set. `name` is rewritten on
    rename, while the watcher's enrichment record holds only the name as of the
    event, so the registry is the only source that survives a rename. Measured
    2026-09-21: a live session read `Sample Task` here against `Renamed Sample Task` in
    its enrichment record, the old name sitting in the registry's own
    `formerNames` — comparing a pane title against the snapshot marked that row
    unroutable while its pane was genuinely correct (1 false positive in 27).

    `None` is a distinct answer from `{}` — an unreadable registry cannot prove
    anything, so neither caller may read it as "nothing is there".
    """
    d = sessions_dir if sessions_dir is not None else SESSIONS_DIR
    if d in _REGISTRY_CACHE:
        return _REGISTRY_CACHE[d]
    # One reader for the whole plugin: `session-liveness.py` owns the glob, the
    # `None`-on-unreadable rule and the pid check. Two instruments over one registry is what
    # let an 8-char prefix read as `ABSENT` on 2026-09-26 while its session was live, and
    # this reader was one of the copies.
    #
    # ⚠️ `alive` is deliberately NOT applied here. This feed's rule is **presence** — an
    # entry is deleted when its session exits — and that rule is out of this change's scope
    # (see the task's Out of Scope). The shared reader carries both signals so each caller
    # takes the one it owns; `live_session_ids()` is `set(registry)` either way, so applying
    # the pid check here would silently sweep items for a session whose entry outlived it.
    records = _session_liveness().read_registry(d)
    if records is None:
        out, status = None, None
    else:
        out = {sid: rec.get("name") or "" for sid, rec in records.items()}
        status = {sid: rec.get("status") or "" for sid, rec in records.items()}
    _REGISTRY_CACHE[d] = out
    _REGISTRY_STATUS_CACHE[d] = status
    _REGISTRY_RECORDS_CACHE[d] = records
    return out


def registry_records(sessions_dir=None):
    """The full `session-liveness` records, or `None` when the registry is unreadable.

    The unnarrowed view of the same single read `read_registry()` performs, for
    callers that need a field the name/status maps drop — here `pid` (for the
    tty lookup) and `alive` (the three-state liveness verdict).

    ⚠️ `None` on an unreadable registry, and `None` again when the read never ran.
    Both are "cannot tell", never "nothing is there": the plausibility check fails
    OPEN on either, because refusing a feed on an unreadable registry would make a
    probe failure indistinguishable from the defect it looks for.
    """
    d = sessions_dir if sessions_dir is not None else SESSIONS_DIR
    if d not in _REGISTRY_RECORDS_CACHE:
        read_registry(d)  # one read populates all three caches
    return _REGISTRY_RECORDS_CACHE.get(d)


def live_session_ids(sessions_dir=None):
    """Session ids currently registered as live, or `None` if that cannot be told.

    A pane id is a lease, not an identifier — WezTerm renumbers and reuses them, so
    `str(rec["pane"]) in wezterm_panes()` answers "is some pane wearing this id", never "is
    the session that raised this item still running". A session killed without
    emitting `SessionEnd` (OOM, a killed worker, a crash) leaves its item open
    forever and its pane still alive, so pane-existence alone renders an item the
    operator can never clear. Measured 2026-09-21 against the live store: 4 such
    orphans were rendered, every one of them on a pane that still existed.

    The authority for liveness is the session registry `~/.claude/sessions/<pid>.json`
    — an entry is deleted when its session exits, so presence means live. This is the
    same source the attention store reads in
    `attention-controller/pkg/session-liveness-checker.go`; that file and
    `vault-cli/docs/session-liveness.md` are the rule, and this is its reader, not a
    second definition of it.

    `None` is a distinct answer from `set()`, and the distinction is the whole point:
    an unreadable registry cannot prove a session is dead, so reporting `set()` would
    sweep every legitimate item the moment the registry is absent. The caller keeps
    everything on `None` — the store's own rule at `session-liveness-checker.go:59-77`,
    where a failed read returns live rather than gone.
    """
    registry = read_registry(sessions_dir)
    return None if registry is None else set(registry)


def session_transcript_age(sid):
    """Seconds since the session's transcript was last written; `inf` if absent.

    The second signal of the liveness rule. Unlike `fleet-sessions.py`'s
    `last_message_ts()`, which prefers the last transcript line's embedded
    `timestamp`, this reads the transcript file's own mtime — so `touch` moves this
    reading and not that one. The two answer different questions on purpose, and the
    idle check in `agents/manager-drive.md` reads this one. An absent transcript
    returns `inf` so it reads as stale, which is the honest answer: nothing has been
    written for a session with no transcript.

    Memoized: `quiet_session_ids()` asks per record, and several records share a
    session, so without this the same file is stat'ed once per item.
    """
    if sid in _AGE_CACHE:
        return _AGE_CACHE[sid]
    age = float("inf")
    try:
        hits = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{sid}.jsonl"))
        if hits:
            age = time.time() - max(os.path.getmtime(h) for h in hits)
    except Exception:
        age = float("inf")
    _AGE_CACHE[sid] = age
    return age


_AGE_CACHE = {}


def quiet_session_ids(records, live_ids):
    """Session ids that are provably finished — the `quiet` verdict of the rule.

    `quiet` is **both** signals, never one of them:

      * not in the registry (the session holds no `<pid>.json` entry), AND
      * its transcript is stale — last written more than `LIVE_WINDOW` ago.

    ⚠️ **Registry absence alone is not death, and reading it as death drops live
    work.** A headless worker is an in-process SDK `query()` inside the supervisor
    server: it holds no socket, so Claude Code never writes it a registry entry at
    all. Measured 2026-09-21: of the 21 headless workers the ledger called `running`,
    **0** appeared in the registry, while its 23 entries were interactive sessions
    only. So "absent from the registry" describes *every* headless worker, live or
    not, and a filter keyed on registry absence alone silently drops every gate a
    headless worker raises. The transcript is what separates the two cases — a live
    worker keeps writing it, a finished one stops. This is the documented decision
    table in `vault-cli/docs/session-liveness.md`, whose `stale + no process` row is
    exactly this predicate.

    `live_ids=None` means the registry could not be read; that cannot prove anything
    dead, so nothing is quiet.

    ⚠️ **A STORE-VOUCHED session is never quiet, and that is the whole correction.**
    `store_items()` resolves the producer's liveness **server-side** and drops dead
    askers as a side effect of the read, so a row that came back from a healthy store
    read has **already had its liveness decided** — by the component that owns that
    question. Re-deciding it here is not a second opinion; it is a **worse** one, and
    it is wrong for precisely the class this feed exists to protect: a session blocked
    on a gate writes nothing, so its transcript ages past `LIVE_WINDOW`, so the reader
    called it `provably finished` and **dropped the row the operator was being waited
    on** — the ask disappearing *because* nobody answered it. Measured 2026-09-28 on
    pane 2555: the row rendered at 09:46:48, `+LIVE_WINDOW` put its transcript stale
    at ~09:51, the row was gone at 09:51:48, and it returned at 10:01:49 once the
    session wrote again — while the store held its open item throughout. That is
    a recorded note § *A Liveness Signal the Watched
    Thing Must Maintain Fails for the Class That Is Blocked*. A larger `LIVE_WINDOW`
    only moves the cliff; re-judging a vouched row is the defect.

    ⚠️ **The clause must NOT be widened to "a session holding an open gate is never
    quiet".** That was tried first and the repo's own tests disprove it:
    `test_dead_session_with_live_pane_is_not_rendered` asserts that a session which is
    absent from the registry with a stale transcript and an open gate record must **not**
    render — and that record *is* an open gate by the text-only rule. **Blocked-live
    and dead are identical under registry-absence plus transcript-staleness**, so no
    predicate built on those two signals can separate them; only the store's own
    verdict can, which is why the flag above carries it. Do not re-propose the wider
    clause — it fails the same two tests.

    ⚠️ **The unvouched path keeps the old rule unchanged**, which is what keeps the
    orphan protection intact: rows from the log fallback (`replayed`) and the
    log-sourced `idle` promotions were never liveness-filtered by anything, so for
    them registry-absence plus staleness is still the best signal available.
    """
    if live_ids is None:
        return set()
    vouched = {
        rec.get("session_id")
        for rec in records
        if rec.get("session_id") and rec.get("store_vouched")
    }
    quiet = set()
    for rec in records:
        sid = rec.get("session_id")
        if not sid or sid in live_ids:
            continue
        if sid in vouched:
            continue
        if session_transcript_age(sid) > LIVE_WINDOW:
            quiet.add(sid)
    return quiet


def busy_session_ids(sessions_dir=None):
    """Session ids the registry marks `busy`; `set()` when that cannot be told.

    `busy` is the working state. A session parked on a prompt reads `waiting`, and one
    that has ended its turn reads `idle` -- so `busy` cannot be a session waiting on the
    operator, which is what makes it usable as a supersession signal. Measured
    2026-09-25 over the live registry: `idle` 15, `shell` 9, `busy` 4, `waiting` 1.

    Reads the same single glob pass as `read_registry()`, so the two can never disagree
    about which entries exist.

    `set()` on an unreadable registry, matching `quiet_session_ids()`: a failed read
    proves nothing, so the caller drops nothing.
    """
    d = sessions_dir if sessions_dir is not None else SESSIONS_DIR
    read_registry(d)
    status = _REGISTRY_STATUS_CACHE.get(d)
    if status is None:
        return set()
    return {sid for sid, value in status.items() if value == "busy"}


def turn_after_closer(rec):
    """True when the transcript holds a user or tool turn after its last closer line.

    A **position** read, not a contains read. Every real turn has tool calls in it, so
    "the transcript mentions a tool call" would supersede every panel; what matters is
    whether one lands *after* the closer. The closer is written by the `Stop` hook at
    the end of a turn, so anything after it is a new turn and the closer is stale.

    A later closer resets the reading: a session that took a turn and then parked again
    holds a fresh closer, and that panel is live.

    `False` when the transcript is unreadable -- the same rule as the registry reads: an
    absent probe proves nothing, so the row is kept rather than dropped.
    """
    sid = rec.get("session_id")
    path = rec.get("transcript")
    if not path and sid:
        path = next(iter(glob.glob(os.path.expanduser(
            f"~/.claude/projects/*/{sid}.jsonl"))), None)
    if not path or not os.path.exists(path):
        return False
    closer_at = -1
    superseded = False
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 200_000))
            for i, line in enumerate(f.read().decode("utf-8", "replace").splitlines()):
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                kind = j.get("type")
                if kind == "assistant":
                    blocks = j.get("message", {}).get("content") or []
                    if any(isinstance(c, dict) and c.get("type") == "text"
                           and "👤 You:" in (c.get("text") or "") for c in blocks):
                        closer_at, superseded = i, False
                    elif closer_at >= 0 and any(isinstance(c, dict)
                                                and c.get("type") == "tool_use"
                                                for c in blocks):
                        superseded = True
                elif kind == "user" and closer_at >= 0:
                    superseded = True
    except Exception:
        return False
    return superseded


def resumed_session_ids(records):
    """Sessions whose transcript shows a turn after their closer -- one read per session.

    Deduped deliberately: `records` carries several rows per session, and the tail read
    is a 200KB seek, so reading per record would repeat it for no new information.
    """
    seen = {}
    for rec in records:
        sid = rec.get("session_id")
        if sid and sid not in seen:
            seen[sid] = turn_after_closer(rec)
    return {sid for sid, resumed in seen.items() if resumed}


def is_superseded(rec, busy_ids=frozenset(), resumed_ids=frozenset()):
    """A rendered closer whose session has moved on -- nothing waits on the operator.

    Two independent signals, either sufficient:

      * the registry says the session is `busy` -- it is inside a turn; or
      * its transcript shows a user or tool turn after the closer.

    Each half covers what the other misses. The registry alone cannot speak for a
    headless worker (an in-process SDK `query()` holds no registry entry at all --
    measured 2026-09-21: 0 of 21 in the registry), which the transcript half covers;
    the transcript alone cannot speak for a session whose file is unreadable, which
    the registry covers.

    Deliberately **not** a parser. A session that is genuinely blocked emits the
    identical closer string, so no text rule can separate the two -- the signal has to
    come from session/turn state, which is why this reads neither `detail` nor the
    closer text.

    `False` for a record with no session id: there is nothing to look up, so it is kept.
    """
    sid = rec.get("session_id")
    if not sid:
        return False
    return sid in busy_ids or sid in resumed_ids


def is_live(rec, pmap, quiet):
    """A row is rendered only when its session is not provably finished AND its pane
    still exists.

    Both conditions are necessary. The pane check alone is what leaked the orphans;
    the session check alone would keep a row the operator cannot jump to, and the
    jump line is this feed's payload. `quiet` is a set of ids, and `None` (an
    unreadable registry) reads as empty — drop nothing. `pmap` follows the same
    convention: `None` is an unreadable `wezterm cli list`, which cannot prove a
    pane is gone, so the row is kept. Only a *readable* map that lacks the pane
    drops it — `{}` is a real "no panes exist" answer and still drops everything.
    """
    if pmap is not None and str(rec.get("pane")) not in pmap:
        return False
    return not quiet or rec.get("session_id") not in quiet


_TEXT_CACHE = {}


def last_assistant_text(rec):
    """The transcript's last assistant text block, memoized per session.

    Memoized because classification now asks for it more than once per record —
    `is_reapable()` resolves the live closer from it while `task_status_from_closer()`
    resolves the anchored task from it — and the read is a 200KB tail seek. The
    script is one-shot, so a per-run cache cannot go stale within a run.
    """
    key = rec.get("session_id")
    if key is not None and key in _TEXT_CACHE:
        return _TEXT_CACHE[key]
    text = _read_last_assistant_text(rec)
    if key is not None:
        _TEXT_CACHE[key] = text
    return text


def _read_last_assistant_text(rec):
    path = rec.get("transcript") or next(iter(glob.glob(os.path.expanduser(
        f"~/.claude/projects/*/{rec['session_id']}.jsonl"))), None)
    if not path or not os.path.exists(path):
        return ""
    last = ""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 200_000))
            for line in f.read().decode("utf-8", "replace").splitlines():
                if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                for c in j.get("message", {}).get("content", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        last = c["text"]
    except Exception:
        pass
    return last


def closer_from_transcript(rec):
    """The last `👤 You:` closer line in the transcript, normalized; "" when absent."""
    lines = [l.strip() for l in last_assistant_text(rec).splitlines() if l.strip()]
    you = [l for l in lines if l.startswith("👤 You:")]
    return normalize_closer(you[-1].split(":", 1)[1]) if you else ""


def current_closer(rec):
    """The session's live closer text — re-derived from the transcript, not cached.

    `detail` is written once by the hook and never refreshed, so a hook-written
    `question` record goes stale the moment its session clears one gate and raises
    another. `reclassify_idle()` already re-derives from the transcript, but it
    returns early on `kind != "idle"`, so every hook-written gate kept the old text.
    Measured 2026-09-19: panes 338 and 254 carried a cleared `you run:` push gate in
    `detail` while their live closer was `approve: /vault-cli:session-close`.

    Falls back to `detail` when no transcript is readable, so a record whose session
    file is gone is classified on what the hook recorded rather than dropped.
    """
    return closer_from_transcript(rec) or (rec.get("detail") or "")


def posted_closer(session_id):
    """The closer this session already posted a card for, or "".

    A session that posts a card and then ends its turn with the same ask on its
    `👤 You:` line puts ONE ask on the board twice. The hook's `Stop` branch now
    declines to mint the echo and writes an `idle` row instead — but this reader
    promotes an `idle` row back to `question` from the transcript, so without
    this check it re-derives the very echo the hook suppressed and the row
    returns. Measured 2026-10-03: the store carried one item while the feed still
    rendered the echo.

    Written by `attention-ask.py`'s `record_posted_closer`. Absent or unreadable,
    this returns "" and the reader behaves exactly as it did before — the fix
    degrades to a no-op, never to a wrong suppression.

    Bounded by `POSTED_CLOSER_TTL` against the record's own `ts`, so the identity
    expires with the turn that produced it rather than lasting as long as the
    session id. A record past the window is ignored, not deleted.
    """
    if not session_id:
        return ""
    try:
        with open(
            os.path.join(STATE, f"{session_id}.posted.json"), encoding="utf-8"
        ) as f:
            rec = json.load(f)
    except Exception:
        return ""
    # Valid JSON is not necessarily an OBJECT. A list, string, number or null
    # parses cleanly and then raises `AttributeError` on `.get` — which is not a
    # TypeError, so the guard below would not catch it and a single bad file
    # would take the whole feed down. Same fail-open direction as the rest: an
    # unreadable record means the echo returns, never a wrong suppression.
    if not isinstance(rec, dict):
        return ""
    # A record with no usable `ts` is treated as expired rather than as
    # unbounded: failing open here means the echo returns, which is the safe
    # direction — the alternative suppresses a closer on an unreadable clock.
    # `OverflowError` is in the tuple because a bare integer literal of unbounded
    # size decodes to a Python `int`, and `float()` of one beyond the float range
    # raises it — neither a `TypeError` nor a `ValueError`, so it would escape
    # this function entirely and take the render down with it, the exact class of
    # failure this guard exists to prevent. `math.isfinite` below cannot help:
    # the raise happens before `ts` exists. The tuple is now total for JSON —
    # `float()` on a decoded str/int/float/bool/list/dict/None raises only these
    # three.
    try:
        ts = float(rec.get("ts") or 0)
    except (TypeError, ValueError, OverflowError):
        return ""
    # A future-dated `ts` makes the delta negative, so it would never exceed the
    # TTL and the record would stay authoritative indefinitely — the unbounded
    # case the docstring above says it prevents. A clock-skewed or buggy poster
    # is enough to trigger it, so anything meaningfully ahead of now expires too.
    # Mirrors the hook's guard; the two halves must agree on the window. The
    # counterpart is NOT in this repository — it is `hooks/attention-log.py` in
    # `bborbe/claude` (its `Stop` branch), so a reader here has no local file to
    # follow and the agreement cannot be verified from this worktree.
    #
    # `NaN` reaches the same unbounded case by another route, so it is rejected
    # here rather than left to the comparisons: `json.load` accepts a bare `NaN`
    # by default (`allow_nan=True` on decode), and `nan > x` and `x - nan > y`
    # are BOTH False — a non-finite `ts` would pass every clause below and keep
    # the record authoritative forever. That is the identical reasoning applied
    # to the TTL override above ("inf and nan parse as floats but make every
    # comparison false"), and it has to hold for the record's own clock too.
    # `isfinite` covers `inf` and `-inf` with it.
    now = time.time()
    if not math.isfinite(ts) or ts > now + 60 or now - ts > POSTED_CLOSER_TTL:
        return ""
    # A record is a local hint written by another process, so its shape is not
    # guaranteed: `normalize_closer` calls `.sub()` on the value and raises on a
    # non-string, which would take the whole feed down for one bad file.
    closer = rec.get("closer")
    if not isinstance(closer, str):
        return ""
    return normalize_closer(closer)


def reclassify_idle(rec):
    """Stop hook may predate the closer-panel rule; re-derive from the transcript.

    Handles the two text-only false-positive classes. A closer that is absent,
    `nothing`, or a `later (on <trigger>):` parked wait is left as `idle` — it is
    genuinely idle, and stays out of the gate list.
    """
    if rec.get("kind") != "idle":
        return rec
    ask = closer_from_transcript(rec)
    if not ask or ask.startswith("nothing") or is_parked_verb(ask):
        return rec
    # Identity, not recency: a closer carrying a DIFFERENT ask than the one this
    # session posted must still reach the board, so the comparison is on the text
    # the poster recorded rather than on "this session has a card".
    if ask == posted_closer(rec.get("session_id")):
        return rec
    return dict(rec, kind="question", detail=ask)


def answered(rec):
    """True once the hook marked the record answered — the operator has acted."""
    return rec.get("state") == "answered"


def read_status(path):
    """The `status:` value from a vault task's frontmatter, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(4000)
    except Exception:
        return None
    if not head.startswith("---"):
        return None
    parts = head.split("---", 2)
    if len(parts) < 3:
        return None
    m = re.search(r"^status:\s*(\S+)", parts[1], re.M)
    return m.group(1).strip().strip("\"'") if m else None


def task_status_from_closer(rec):
    """`status:` of the vault task this session anchored, read from its closer.

    The closer names its own task (`📌 Task: [..](obsidian://open?vault=..&file=..)`),
    so no vault scan is needed. Returns None when there is no anchor or the file is
    not on disk — which keeps the record an open gate rather than suppressing it.
    """
    m = _TASK_LINK.search(last_assistant_text(rec))
    if not m:
        return None
    rel = urllib.parse.unquote(m.group(1))
    for vault in sorted(glob.glob(os.path.join(OBSIDIAN, "*"))):
        path = os.path.join(vault, rel + ".md")
        if os.path.exists(path):
            return read_status(path)
    return None


def is_close_gate_closer(text):
    """A closer whose decision is closing this session.

    Both sanctioned forms count — the `approve:` form and the `pick` form.

    Rejected on purpose, one for each direction of error:
      * a `later (on <trigger>):` line that merely mentions session-close — that is
        a deferral, not a close gate. `is_parked_verb()` is checked first, so
        widening a prefix test into a containment test cannot turn a parked wait
        into a close gate. This is the trap: a false-negative fix becoming a false
        positive.
      * a `pick` menu whose first/recommended disposition is not session-close —
        the operator is being offered other work, so it is not a close gate. The
        fleet emits these routinely (`pick — 1. keep SC3 as designed; …`).
    """
    text = normalize_closer(text)
    if not text or is_parked_verb(text):
        return False
    if text.startswith(CLOSE_GATE):
        return True
    m = _PICK_FIRST.match(text)
    return bool(m) and _CLOSE_TARGET in m.group(1)


def is_reapable(rec, task_status):
    """A close gate whose anchored task already reads `status: completed`.

    The close is genuinely the operator's call, so it stays visible — but the work
    is done and nothing is blocked, so it is not an open gate. Listed separately
    rather than counted under `Needs you`.

    Reads the *live* closer rather than the record's cached `detail`, and accepts
    both sanctioned close forms — the two defects measured 2026-09-19, which left
    `Reapable (0)` while three finished sessions sat in `Needs you` in the same run.

    Fails safe: no resolver, or an unresolvable task, leaves it an open gate.
    """
    if not is_close_gate_closer(current_closer(rec)):
        return False
    if task_status is None:
        return False
    return task_status(rec) == "completed"


def names_peer_gate(rec, open_panes):
    """True when this closer restates another pane's gate rather than raising its own.

    A session relaying "the approval in pane 285 is yours alone" is not a second
    decision — pane 285 already carries it, and the operator's action belongs
    there. Suppressed only when the named pane is *itself* an open gate in this
    run, so a closer that merely mentions a pane id (a ticket reference, a log
    line) is left alone and a real gate is never silently eaten.
    """
    if not open_panes:
        return False
    mine = str(rec.get("pane"))
    for named in _PANE_REF.findall(rec.get("detail") or ""):
        if named != mine and named in open_panes:
            return True
    return False


def is_rendered_panel(rec):
    """A closer panel the `Stop` handler rendered — not a parked gate.

    `kind` cannot tell them apart: the `Stop` panel path and the `Notification`
    elicitation path both write `kind="question"` with an identical `cleared_by`
    (`{"event": "UserPromptSubmit"}`), and the two `idle` paths collide the same
    way. The writer is recorded in `event`: `Stop` means the session ended its
    turn and rendered a line describing what waiting looks like; anything else
    is a real parked tool call.

    A record with no `event` predates the marker and keeps the old behaviour —
    counted as a gate — so nothing is silently reclassified by this change.
    """
    return rec.get("event") == "Stop"


def is_open_gate(rec, open_panes=frozenset(), task_status=None, include_panels=False):
    """A gate the operator has not answered yet.

    The record carries `state` ("open" until its answering event fires, then "answered")
    and `cleared_by`, naming that event. The old rule was file-existence — the hook
    deleted the record on ANY later event, so a background task completing erased an open
    gate and the pane vanished from the feed with nothing left to classify. Reproduced
    2026-09-18 on a throwaway pane: Stop wrote the gate, PostToolUse removed it, and the
    feed could not tell that apart from the operator having answered.

    `open_panes` and `task_status` carry the context the context-dependent classes
    need; both default to absent, which degrades to the text-only rule.

    `include_panels` is the one knob that separates a *block* from a *soft signal*.
    A rendered closer panel is not waiting on the operator, so it must not be
    counted as a block — but it must still be listed, or `/who-needs-me` silently
    starts hiding panes. Callers pass True to enumerate the panel group through
    this same predicate, so the two surfaces cannot disagree about which rows are
    panels.
    """
    if answered(rec) or rec.get("kind") not in ("permission", "question"):
        return False
    if is_parked_verb(rec.get("detail")):
        return False
    if is_reapable(rec, task_status):
        return False
    if names_peer_gate(rec, open_panes):
        return False
    if is_rendered_panel(rec) and not include_panels:
        return False
    return True


def rendered_panels(records, open_panes=frozenset(), task_status=None,
                    busy_ids=frozenset(), resumed_ids=frozenset()):
    """The Rendered-panels list: closers still waiting on the operator, oldest first.

    One home for the composition, so `main()`'s render and any test asking "is this row
    listed" cannot drift apart -- the same reason `is_open_gate(include_panels=True)` is
    already shared between the block count and this list rather than written twice.

    A superseded row leaves the list. The sibling rule that forbids hiding panes
    ("panel rows stay visible ... a panel disappearing from the render is a failure of
    this criterion, not a pass") protects a **live** closer panel; a superseded row is
    no longer a panel row, so removing it is a reclassification, not a hidden pane. The
    `busy`/`resumed` sets are the only input that can remove one, and both are empty
    when their probe fails -- so a blind reader lists more, never fewer.
    """
    return sorted([r for r in records
                   if is_rendered_panel(r) and not answered(r)
                   and not is_superseded(r, busy_ids, resumed_ids)
                   and is_open_gate(r, open_panes=open_panes, task_status=task_status,
                                    include_panels=True)],
                  key=lambda r: r["ts"])


def age(ts):
    m = int((time.time() - ts) // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def strip_status_glyph(text):
    """A name without Claude Code's leading status glyph.

    Claude Code prefixes the pane title with a status glyph (✳ ◐ ◑ ◒ ◓ ⠿ …) and a
    session's own name may carry one too (`⚙ …`), so the strip is applied to *both*
    sides of the ownership comparison. Extracted from `name_of()` so the display path
    and the ownership check cannot drift on what "the name" is.
    """
    text = (text or "").strip()
    while text and not (text[0].isalnum() or text[0] in "/~._-"):
        text = text[1:].lstrip()
    return text


def current_session_name(rec, registry=None):
    """The session's name as it stands NOW, or "" when it cannot be told.

    The registry wins while the session is registered: it rewrites `name` on rename,
    while the watcher's enrichment record holds only the name as of the event.
    Measured 2026-09-21 — a live session read `Sample Task` in the registry against
    `Renamed Sample Task` in its enrichment record, the old name sitting in the
    registry's own `formerNames`. Reading the snapshot as current shows a stale name
    as resolved, which is the same defect as a recycled pane wearing another
    session's route.

    A session the registry does not carry is not therefore nameless: a headless worker
    is an in-process SDK query holding no entry at all, so the enrichment record is
    the fallback — and it is what keeps a headless worker's inherited pane detectable,
    since the worker's own name is what the spawner's pane title disagrees with.
    """
    if registry:
        name = registry.get(rec.get("session_id") or "")
        if name:
            return name
    return rec.get("session_name") or ""


def pane_owner(rec, pmap, registry=None):
    """The registry session whose name the recorded pane's title matches, or "".

    Ownership by elimination, and the complement of `is_routable()`: that proves a pane
    is *this* session's by matching its title to this session's name, and is silent
    when the session has no name to compare. This asks the other question — whose pane
    is it — which is the one that survives a nameless session.

    It exists because a nameless row used to borrow the pane's title as its identity,
    and a headless worker inherits its spawner's `WEZTERM_PANE`. Measured 2026-09-25:
    session `50193c14` (headless) carried pane 0, whose title is `◐ Fleet Manager`, so
    the fallback rendered the SPAWNER's name as the worker's row identity.

    Only a registry match counts. A title matching no registered session proves nothing,
    and treating that as a borrow would strip identity from every row whose pane belongs
    to a session the registry cannot speak for — the same false-positive shape
    `is_routable()`'s docstring records for renames.
    """
    if not registry:
        return ""
    title = strip_status_glyph(pmap.get(str(rec.get("pane")), {}).get("title"))
    if not title:
        return ""
    for sid, name in registry.items():
        if sid != rec.get("session_id") and strip_status_glyph(name) == title:
            return sid
    return ""


def resolve_missing_panes(records, pmap, registry=None):
    """Fill in a pane for rows the event log could not locate one for.

    ⚠️ **Why a row has no pane at all.** A store row takes its pane from the hook's
    own log, joined on `dedup_key` (`normalize_store_item`). Two live producers write
    no line under that key, so the row carries no pane and `is_live()`'s pane clause
    drops it before any classifier sees it — the operator is never told the session is
    parked, so it waits indefinitely. Measured 2026-10-01 on pane 479 (session
    `29d4464c`): the store item open, the modal footer on screen, and the feed
    rendering the pane `absent`.

    The two producers are `attention-push.py` (a `session:`-marked declaration, not a
    hook event, so it has no log line by construction) and any session that
    **inherits** `WEZTERM_PANE` from its spawner — a headless worker does this by
    construction, and since `owned_pane()` began refusing an unprovable pane such a
    session records an empty pane by design.

    **The join is the one the board already uses, and this reader already has it.**
    `attention-controller` resolved the same class with a second source beside the log:
    name the item's session from its registry entry, then match that name against the
    live panes' glyph-stripped titles. `is_routable()` proves a *recorded* pane by
    exactly that comparison, so this reaches the same predicate from the other side
    rather than inventing one.

    ⚠️ **It only fills a gap; a recorded pane always wins.** The logged pane is the
    event-time fact, and re-resolving it would swap a fact for a name-guess — the
    confident-wrong-direction defect `is_routable()` refuses. The controller draws the
    same line ("the logged path is unchanged and still wins wherever a line exists").

    ⚠️ **An unresolvable session yields no pane, and that is the safety property.**
    `pmap` `None` (unreadable) and `registry` `None` (unreadable) both mean the
    question was never answerable, so no claim is made; a session the registry cannot
    name, or whose name matches no live pane title, likewise claims nothing. Handing
    an unnamed session the first pane on screen would mark every pane as its own and
    stamp a confident route onto a row this reader knows nothing about — § Silence 7's
    failure, which is why the join is keyed on the registry's name and not on
    proximity.

    Mutates each record in place and returns the list, so callers keep the shape they
    already iterate.
    """
    if not pmap or not registry:
        return records
    for rec in records:
        if rec.get("pane"):
            continue
        sid = rec.get("session_id")
        name = registry.get(sid) if sid else None
        if not name:
            continue
        wanted = strip_status_glyph(name)
        for pane_id, pane in pmap.items():
            if strip_status_glyph(pane.get("title")) == wanted:
                rec["pane"] = pane_id
                break
    return records


def short_id(sid):
    """A session id as a row's identity, for a row that has no name to render."""
    return f"session {(sid or '?')[:8]}"


def name_of(rec, pmap, registry=None):
    """The session's name, preferring the name it actually holds.

    A store row carries `session_name` from the watcher's enrichment record, but that
    is a snapshot taken at event time and a rename never reaches it — so the registry's
    current name is preferred and the snapshot is the fallback for a session the
    registry cannot speak for.

    Rows with neither fall back to the pane title ONLY when that pane is not provably
    another session's. Borrowing it otherwise renders a different session's name as this
    row's identity — the mis-attribution measured 2026-09-25 — so a borrowed pane's
    title is refused and the row carries its own session id instead. A pane whose owner
    cannot be established keeps the old fallback: an absence is not a verdict, and
    stripping identity from an unprovable row would be a regression dressed as a fix.
    """
    name = current_session_name(rec, registry)
    if name:
        return name
    if pane_owner(rec, pmap, registry):
        return short_id(rec.get("session_id"))
    p = pmap.get(str(rec.get("pane")))
    if p:
        return strip_status_glyph(p.get("title")) or os.path.basename(rec.get("cwd", ""))
    return os.path.basename(rec.get("cwd", "")) + " (pane gone)"


def is_routable(rec, pmap, registry=None):
    """True when this row's recorded pane is proven to be *this* session's pane.

    A pane written at event time is not proof it is still this session's: pane ids are
    recycled across tab moves and WezTerm restarts, so a stale lookup returns *another*
    session's pane — a wrong answer wearing the appearance of a resolved one, which
    § Silence 7 of the attention-item schema calls strictly worse than a blank.

    Existence is necessary but not sufficient, and it was all this check used to ask:
    `str(pane) in pmap` rejects a pane that is *gone*, never one that exists and
    belongs to a different session. Measured 2026-09-21 — a headless worker inherits
    its spawner's `WEZTERM_PANE`, so its item carries the spawner's pane id, which
    exists, and the row rendered a confident jump to the wrong tab.

    Ownership is proven by the name: the pane's title against the session's current
    name, both glyph-stripped. Measured 2026-09-21 against the 27 rendered rows whose
    session had a name — the registry-first comparison matched 27, while reading the
    enrichment snapshot matched 26 and marked a renamed session unroutable while its
    pane was genuinely correct.

    A *mismatch* is required, not merely an absence. A session with no name to compare
    (neither a registry entry nor an enrichment record) has unprovable ownership, and
    reporting that as unroutable would strip the jump from every such row; the row is
    kept and `name_of()` still serves it from the pane title.
    """
    pane = rec.get("pane")
    if not pane or str(pane) not in pmap:
        return False
    # Provably ANOTHER session's pane, refused before the name test below — which
    # cannot see this case at all. That test needs a name to compare, so a nameless
    # session reaches `return True` by design ("an absence is not a verdict") and
    # keeps a jump to a pane it does not own. Measured 2026-09-25 by exercising the
    # nameless branch directly: the row rendered `activate-pane --pane-id 0` — the
    # spawner's tab — which is the confident-wrong-direction defect this reader exists
    # to prevent, and it survived the name-mismatch fix precisely because that fix
    # only fires when a name is present.
    if pane_owner(rec, pmap, registry):
        return False
    name = current_session_name(rec, registry)
    if not name:
        return True
    title = strip_status_glyph(pmap.get(str(pane), {}).get("title"))
    if not title:
        return True
    return title == strip_status_glyph(name)


def provenance_of(rec):
    """The row's provenance line — host, cwd and tool, each absent-marked when absent.

    These are the *event's* fields: the store carries none of them, so they arrive
    from the hook's own log line and are missing for any item whose producer wrote
    no log. An absent value renders as `—`, never as a blank — a missing host and a
    host that is genuinely empty are different claims, and a blank reads as the
    second. Never defaulted to something plausible: the whole point of this line is
    that the operator can walk to the thing that needs them.
    """
    host = rec.get("host") or "—"
    cwd = rec.get("cwd") or "—"
    tool = rec.get("tool_name") or "—"
    return f"{host}:{cwd} · {tool}"


# The reader and the answering arm ship in the same plugin, so the handover resolves the
# script exactly as a command does: `CLAUDE_PLUGIN_ROOT` when set, the marketplace clone
# otherwise. Measured 2026-09-25 — the variable is EMPTY in a command's Bash, so the
# fallback is the branch that actually fires.
ANSWER_SCRIPT = ("${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/"
                 "claude-supervisor}/scripts/attention-answer.py")


def answer_handover(rec):
    """The command that answers this item, or "" when there is nothing to answer.

    Rendered only where there is no pane this session provably owns. There is no jump
    to hand over, and stopping at `unroutable` told the operator the row could not be
    reached without ever saying how to clear it — the gate is real, open, and
    answerable, so the row must name the way (measured 2026-09-25, session `50193c14`).

    The id is the STORE's, never the row's `item_id` — see `normalize_store_item`; the
    row's value is the log's join key and does not resolve against the answer endpoint.
    A `permission` item needs an explicit verdict and neither default is safe, so both
    are named and neither is chosen, matching the refusal `attention-answer.py` enforces.
    """
    item = rec.get("store_item_id")
    if not item or rec.get("kind") not in ("permission", "question"):
        return ""
    cmd = f"answer: python3 {ANSWER_SCRIPT} answer {item}"
    return cmd + " --decision allow|deny" if rec.get("kind") == "permission" else cmd


def row(rec, pmap, what, registry=None):
    pane = rec.get("pane")
    if pane and is_routable(rec, pmap, registry):
        jump = f"wezterm cli activate-pane --pane-id {pane}"
    else:
        if pane:
            # Present but unvalidatable. Never rendered as a route: the id may now
            # belong to a different session entirely.
            jump = f"unroutable — pane {pane} does not resolve to this session"
        else:
            jump = "unroutable — no pane recorded for this item"
        # This branch is where the dead end used to be: no jump to hand over, and the
        # gate is still real. Name the way out of it — and ONLY here, so a routable row
        # is untouched and keeps the single jump line it has always rendered.
        answer = answer_handover(rec)
        if answer:
            jump = f"{jump}\n         {answer}"
    # A replayed row says so ON THE ROW, in the scan position a manager reads first.
    # The marker is EMPTY when the store answered, so a live read renders
    # byte-identically to what it always did — the no-regression requirement is that
    # a responsive store's output is unchanged, and a marker that was always present
    # would fail it. `replayed` is set by `needs_source()` on the fallback branch.
    mark = "⟳replay  " if rec.get("replayed") else ""
    return (f"  [{pane or '?':>4}] {age(rec['ts']):>6}  {mark}"
            f"{name_of(rec, pmap, registry)[:50]:<50}  {what[:60]}\n"
            f"         {provenance_of(rec)}\n"
            f"         {jump}")


def capped(rows, show_all):
    """The rows to render, and how many were withheld.

    Split out from main() so the cap is testable without driving the render, and
    so the omitted count is computed from the same slice that is printed — the
    two cannot disagree.
    """
    if show_all or len(rows) <= CAP:
        return rows, 0
    return rows[:CAP], len(rows) - CAP


def pane_for(session_id):
    """Print the pane of one live session, or exit non-zero.

    Liveness comes from the **session registry**, never from the recorded pane. An
    entry is deleted when its session exits, so presence means live — the authority
    `session-liveness.md` names. Asking instead whether the *recorded* pane still
    exists answers a different question, and gets it wrong in the direction that
    costs the operator a jump: a pane id is a lease, WezTerm renumbers and reuses
    them, so a live session whose item predates a renumber carries a pane that is
    gone. Measured 2026-09-24: `Sample Task` was live in the registry on pane 1391
    while all eight of its attention records carried pane 85, and the old check
    reported the session **dead** — a refusal that hid a session the operator could
    have jumped to, which is the exact failure this path exists to prevent.

    Resolution order, and the reason for it:

      1. the recorded pane, when it still exists — it is the pane the sweep itself
         resolved, so it is preferred whenever it is still good;
      2. otherwise the registry's **current** name against the WezTerm titles.

    Step 2 is a name match, which is only sound because of *which* name: it is read
    from the registry at call time, so `/rename` is tracked rather than broken. A
    name carried in from anywhere else — a task title, an enrichment snapshot, the
    caller's prompt — is the stale join that goes wrong silently, and is not used.

    The caller passes the 8-char prefix the sweep digest carries, so a unique prefix
    resolves and an ambiguous one refuses rather than guesses. Two panes sharing a
    title refuse too. Every failure names what it saw: the caller renders
    `no pane — <reason>`, so a vague reason leaves the operator with a dead end.
    """
    sid = (session_id or "").strip().lower()
    if not sid:
        sys.stderr.write("pane-for: no session id given\n")
        return 1

    registry = read_registry()
    if registry is None:
        sys.stderr.write("pane-for: session registry unreadable — cannot decide liveness\n")
        return 1
    matches = sorted(str(i) for i in registry if str(i).lower().startswith(sid))
    if not matches:
        sys.stderr.write("pane-for: no live session id matches %s\n" % sid)
        return 1
    if len(matches) > 1:
        sys.stderr.write(
            "pane-for: %d live session ids match %s — pass the full id\n" % (len(matches), sid)
        )
        return 1
    full = matches[0]
    name = strip_status_glyph(registry.get(full))

    pmap = wezterm_panes()
    if pmap is None:
        sys.stderr.write("pane-for: WezTerm pane list unreadable\n")
        return 1

    # A session holds several records at once — the store returns one item per open
    # item, and `load` folds the event log per item — so take the first that carries
    # a pane rather than the first record outright, which may have none while a
    # sibling does.
    records = load("needs")
    recorded = next(
        (str(r["pane"]) for r in records
         if r.get("session_id") == full and r.get("pane")),
        None,
    )
    if recorded and recorded in pmap:
        # Existence is not ownership. A pane id is recycled across tab moves and
        # WezTerm restarts, so an existing pane can be *another* session's — the
        # wrong answer wearing the appearance of a resolved one, which is strictly
        # worse than a blank. `is_routable` asks for the title as well, for exactly
        # this reason; the same rule applies here, or this path hands over a
        # confident link to the wrong tab.
        title = strip_status_glyph(pmap[recorded].get("title"))
        if not name or not title or title == name:
            print(recorded)
            return 0

    if not name:
        sys.stderr.write(
            "pane-for: session %s is live but carries no name, and its recorded pane %s is gone\n"
            % (full[:8], recorded or "(none)")
        )
        return 1
    hits = sorted(pid for pid, p in pmap.items() if strip_status_glyph(p.get("title")) == name)
    if len(hits) == 1:
        print(hits[0])
        return 0
    if not hits:
        sys.stderr.write(
            "pane-for: session %s is live but no pane resolves — recorded pane %s is gone and no "
            "WezTerm pane is titled %r\n" % (full[:8], recorded or "(none)", name)
        )
        return 1
    sys.stderr.write(
        "pane-for: session %s is live but %d WezTerm panes are titled %r — ambiguous\n"
        % (full[:8], len(hits), name)
    )
    return 1


# The five sections this feed renders, in render order. `--section` names them;
# a manager's attention watcher arms on `needs-you` alone.
SECTIONS = ("needs-you", "rendered-panels", "stuck", "reapable", "idle")


def section_filter(names):
    """Predicate over section names; no names selects every section.

    Extracted from `main()` so the scoping is testable without driving the whole
    render (which needs a live WezTerm and session registry). `--section` exists
    because the consumer, `gate-owner-filter.py`'s `panes_from_feed`, matches
    `[<pane>]` on ANY line regardless of the section header above it — so a
    whole-feed arm fires on every `Rendered panels` closer, a line this feed's own
    header labels "not a parked gate".
    """
    want = set(names) if names else None

    def wanted(name):
        return want is None or name in want

    return wanted


# The one token `gate-owner-filter.py` keys on. It is written to STDOUT, not stderr,
# because the documented arm is a PIPE:
#
#   who-needs-me.py --section needs-you | gate-owner-filter.py --feed --self <sid>
#
# and a pipe carries stdout only. The refusal was stderr-only, so the filter read an
# empty feed, reported `gates: 0`, and exited 0 — a confident clear queue certified by
# the success code, for a transport that never answered. Measured 2026-10-06 against
# v0.106.0: with the mux socket unreachable the pipeline printed `gates: 0  emit: 0 …`
# and exited 0 (1 under `set -o pipefail`, from this script), while the refusal text
# reached only the terminal.
#
# ⚠️ A MARKER, not a second copy of the message. The filter must be able to tell a
# REFUSAL from a genuinely empty feed, because an empty feed is a real answer that must
# keep exiting 0: the two were once collapsed and a `Monitor` armed on this pipeline
# died with `script failed (exit 2)` on every quiet tick (measured 2026-10-05). The
# marker is the third state that makes the two distinguishable downstream.
REFUSAL_MARKER = "WHO-NEEDS-ME-REFUSED"


def refuse(reason):
    """Refuse the read on BOTH streams, then exit non-zero. Never returns.

    stdout carries the machine-readable marker, so the refusal survives the pipe;
    stderr carries the same text for a human reading the terminal. `docs/pane-reads.md`
    § The three responses puts this site in its third row — *"already exits non-zero on
    failure → refuse non-zero, naming the transport"* — and the missing half was never
    the exit code, it was that nothing reached the consumer.
    """
    sys.stdout.write(f"{REFUSAL_MARKER}: {reason}\n")
    sys.stdout.flush()
    sys.stderr.write(f"who-needs-me: {reason}\n")
    sys.stderr.flush()
    sys.exit(1)


def pane_table_implausible(pmap, registry):
    """True when a READABLE socket's pane table cannot be this machine's.

    The caller's `pmap is None` guard tests the socket's READABILITY; this tests the
    pane table's PLAUSIBILITY, and the two fail independently. Pointed at a
    different-but-live socket — a stray `wezterm-mux-server` whose pane table held ONE
    bare default pane — the readability guard passes and the feed returns a confident
    "Needs you (0)" against 26 live claude processes. Measured 2026-10-06 ~08:10.

    Rule: take the live registered sessions (from the same single `session-liveness`
    read the rest of the feed uses), resolve each one's tty through
    `fleet-colours.py:pid_ttys()`, and ask how many of those ttys the pane table
    carries as `tty_name`. Refuse when at least one is missing AND fewer than half are
    accounted for.

    ⚠️ **The denominator is the sessions whose tty is RESOLVABLE, not every live
    registered session.** A headless session reports `??` and can never appear in any
    pane table, so counting it as unaccounted-for would fire this check on fleet
    composition rather than on transport health — the FALSE REFUSAL `docs/pane-reads.md`
    names as this defect class's mirror, and the one a naive "always warn" fix produces.
    Dropping them keeps the broken-socket case firing (3 tab sessions, 0 matched) while
    a healthy headless-heavy fleet stays quiet. Measured 2026-10-06 on this machine:
    28 live registered sessions, 26 matched, 2 unmatched — well clear of the threshold.

    Fails OPEN — returns False — on every input that cannot be read: an unreadable
    registry, a failed `ps`, or a pane table carrying no `tty_name` at all. Each is
    "cannot tell", and refusing a feed on a failed probe would make the probe's own
    failure indistinguishable from the defect it looks for.
    """
    if registry is None:
        return False
    # Cheapest test first: a pane table carrying no `tty_name` anywhere is not the
    # shape `wezterm cli list --format json` emits, so the comparison cannot be made
    # and the `ps` below would be spent to learn nothing.
    pane_ttys = {p.get("tty_name") for p in pmap.values() if p.get("tty_name")}
    if not pane_ttys:
        return False
    live = [rec for rec in registry.values() if rec.get("alive") is True]
    if not live:
        return False
    ttys = _fleet_colours().pid_ttys()
    if ttys is None:
        return False
    resolvable = matched = 0
    for rec in live:
        tty = ttys.get(rec.get("pid"))
        if not tty:
            continue
        resolvable += 1
        if tty in pane_ttys:
            matched += 1
    if not resolvable:
        return False
    return resolvable - matched >= 1 and matched < resolvable / 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stuck-min", type=int, default=20)
    ap.add_argument(
        "--all",
        action="store_true",
        help=f"list every open item instead of the first {CAP}",
    )
    ap.add_argument(
        "--section",
        action="append",
        choices=SECTIONS,
        help="render only this section (repeatable); the default renders all five. "
        "A manager's attention watcher arms on `--section needs-you`: the other four "
        "are not gates, and an unscoped feed makes that watcher fire on every peer's "
        "rendered closer.",
    )
    ap.add_argument("--jump", metavar="PANE", help="activate this WezTerm pane and exit")
    ap.add_argument(
        "--pane-for",
        metavar="SESSION_ID",
        help="print the pane of this session (full id or unique 8-char prefix) and exit; "
        "non-zero when no live session resolves",
    )
    a = ap.parse_args()
    if a.jump:
        sys.exit(subprocess.call(["wezterm", "cli", "activate-pane", "--pane-id", a.jump]))
    if a.pane_for:
        sys.exit(pane_for(a.pane_for))

    # `wezterm_panes()` directly, not `panes()`: the latter folds a failed query into
    # `{}`, and every record below is filtered through `is_live()`, whose test is
    # membership in this map. A failed `wezterm cli list` therefore drops *every*
    # record and this command prints `Needs you (0)` / `Nothing needs you.` and exits
    # 0 — a confident clear fleet, certified by the success code, for a transport it
    # never reached. Measured 2026-09-25: with the mux socket unreachable the feed
    # lost 4 rendered panels and 16 idle rows and still exited 0 with an empty
    # stderr. The `is None` test is deliberate, not `not pmap`: a reachable WezTerm
    # with no panes is a real empty answer and must keep rendering, which is the
    # same distinction `wezterm_panes()` documents.
    pmap = wezterm_panes()
    if pmap is None:
        refuse(
            "WezTerm pane list unreadable — the mux socket is not answering, so no "
            "record can be proven live. Refusing to print a feed that would read as "
            "an empty queue."
        )
    # One registry read serves both questions: which sessions are live (the `quiet`
    # pass) and what each is called now (the ownership check). Reading it twice would
    # let the two disagree about which entries exist.
    registry = read_registry()
    # The socket answered — but a live socket is not proof it is THIS machine's. The
    # readability guard above cannot see the difference: a stray mux server's one-pane
    # table passes it while every real record is filtered out one line below. Checked
    # here, before the store read, so a refusal costs one `ps` rather than a full feed
    # build. `registry_records()` is the same read `read_registry()` just performed —
    # no second glob, no second instrument.
    if pane_table_implausible(pmap, registry_records()):
        refuse(
            "WezTerm pane table does not account for the live registered sessions — "
            "the socket answered, but its pane table is not this machine's, so no "
            "record can be proven live. Refusing to print a feed that would read as "
            "an empty queue."
        )
    live_ids = None if registry is None else set(registry)
    # Read the store once: load("needs") folds every event log, so a second call for
    # the quiet pass would double that cost.
    records = load("needs")
    # Fill a pane for rows the event log could not locate one for, BEFORE the liveness
    # filter — `is_live()` drops a row whose pane is absent, so a store item with no log
    # line (an `attention-push.py` declaration, or a session that inherits its spawner's
    # `WEZTERM_PANE`) would otherwise vanish from the feed entirely. The resolution only
    # fills a gap: a pane the log recorded always wins.
    resolve_missing_panes(records, pmap, registry)
    quiet = quiet_session_ids(records, live_ids)
    # The registry half of supersession rides the same read as `quiet` — one glob pass,
    # no second look at the directory.
    busy = busy_session_ids()
    live = lambda r: is_live(r, pmap, quiet)
    needs = [reclassify_idle(r) for r in records if live(r)]
    # The transcript half is deferred until after the liveness filter, and that ordering
    # is load-bearing: the read is a 200KB tail seek per session, and `records` holds
    # every session the store has ever seen. A superseded row must be live to be listed
    # at all, so a dead session's tail can never change the answer — reading it would be
    # pure cost in a manager's sweep.
    resumed = resumed_session_ids(needs)
    tools = [r for r in load("tool") if live(r)]

    # Two passes: the pane set that carries a gate is computed without peer-dedup,
    # so a restating pane is dropped only when the pane it names is itself a gate.
    open_panes = {str(r.get("pane")) for r in needs
                  if is_open_gate(r, task_status=task_status_from_closer)}
    blocked = sorted([r for r in needs
                      if is_open_gate(r, open_panes=open_panes, task_status=task_status_from_closer)],
                     key=lambda r: r["ts"])
    # Rendered closer panels: the same predicate with `include_panels=True`, so the
    # block count and the panel list can never disagree about which rows are panels.
    # They stay VISIBLE — the soft signal is what this command is for — and are
    # simply not counted as blocks.
    panels = rendered_panels(needs, open_panes, task_status_from_closer, busy, resumed)
    reapable = sorted([r for r in needs if not answered(r)
                       and is_reapable(r, task_status_from_closer)], key=lambda r: r["ts"])
    stuck = sorted([r for r in tools if time.time() - r["ts"] > a.stuck_min * 60], key=lambda r: r["ts"])
    idle = sorted([r for r in needs if r["kind"] == "idle" and not answered(r)], key=lambda r: r["ts"])

    # Say which source answered, once, before any row. A silent fallback would
    # be indistinguishable from a healthy store — which is the whole failure
    # this note exists to prevent.
    # `--section` scopes the render to the named sections; with no flag all five
    # render, byte-for-byte as before. The scoping exists because the consumer,
    # `gate-owner-filter.py`'s `panes_from_feed`, matches `[<pane>]` on ANY line
    # regardless of the section header above it — so a whole-feed arm fires on every
    # `Rendered panels` closer, a line this feed's own header labels "not a parked
    # gate". Measured 2026-10-01 on the Manager Layer arm: 8 wakes in ~55 minutes,
    # 1 actionable, each wake a full manager turn.
    wanted = section_filter(a.section)

    if SOURCE_NOTE:
        print(SOURCE_NOTE)
    if wanted("needs-you"):
        print(f"Needs you ({len(blocked)})")
        # Capped: the manager reads a summary. A reader that prints thirty
        # undifferentiated rows has failed to triage, not failed to report — and the
        # count of what it omitted is what keeps the cap honest rather than silent.
        shown, withheld = capped(blocked, a.all)
        for r in shown:
            print(row(r, pmap, f"{r['kind']}: {r['detail']}", registry))
        if withheld:
            print(f"{withheld} more — pass --all")
    if wanted("rendered-panels"):
        print(f"\nRendered panels ({len(panels)})  — a closer line, not a parked gate")
        for r in panels:
            print(row(r, pmap, r["detail"], registry))
    if wanted("stuck"):
        print(f"\nProbably stuck > {a.stuck_min}m ({len(stuck)})")
        for r in stuck:
            print(row(r, pmap, r["detail"], registry))
    if wanted("reapable"):
        print(f"\nReapable ({len(reapable)})  — finished work on a close gate, yours to close")
        for r in reapable:
            print(row(r, pmap, r["detail"], registry))
    if wanted("idle"):
        print(f"\nIdle, turn ended: {len(idle)}")
    if wanted("needs-you") and not blocked and not stuck:
        print("\nNothing needs you.")


if __name__ == "__main__":
    main()
