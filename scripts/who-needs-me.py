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
import argparse, glob, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
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
        return load_events()
    SOURCE_NOTE = None
    events = {r.get("item_id"): r for r in load_events()}
    out = []
    for item in items:
        rec = normalize_store_item(item, events)
        if rec is not None:
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


def panes():
    """The live panes; `{}` when the query failed, for callers that only filter.

    ⚠️ `{}` here is a *conflation*: it is what a failed `wezterm cli list` returns
    and also what a reachable WezTerm holding no panes returns. A caller that must
    tell those apart — anything deciding liveness, since `is_live()` drops every
    record on an empty map — has to call `wezterm_panes()` and test `is None`
    instead. `main()` does. `pane_for()` and `fleet-board.py` still do not, and
    read a broken transport as "no panes" (tracked as a follow-up, not fixed here).
    """
    return wezterm_panes() or {}


def live_pane_ids():
    """Confirmed pane ids, or `None` when the query failed — see `wezterm_panes()`."""
    p = wezterm_panes()
    return None if p is None else set(p)


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
    2026-09-21: a live session read `MDM Bugs` here against `Octopus MDM Bugs` in
    its enrichment record, the old name sitting in the registry's own
    `formerNames` — comparing a pane title against the snapshot marked that row
    unroutable while its pane was genuinely correct (1 false positive in 27).

    `None` is a distinct answer from `{}` — an unreadable registry cannot prove
    anything, so neither caller may read it as "nothing is there".
    """
    d = sessions_dir if sessions_dir is not None else SESSIONS_DIR
    if d in _REGISTRY_CACHE:
        return _REGISTRY_CACHE[d]
    # `glob` on a missing directory returns `[]` rather than raising, so an absent
    # registry would otherwise read as "no session is live" and sweep the whole feed.
    # Absence is not evidence of death — it is evidence the probe cannot run, which is
    # `None`. Checked explicitly because the silent-empty shape is indistinguishable
    # from a real empty registry downstream.
    out = None
    if os.path.isdir(d):
        try:
            out = {}
            for path in glob.glob(os.path.join(d, "*.json")):
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
                sid = rec.get("sessionId")
                if sid:
                    out[sid] = rec.get("name") or ""
        except Exception:
            out = None
    _REGISTRY_CACHE[d] = out
    return out


def live_session_ids(sessions_dir=None):
    """Session ids currently registered as live, or `None` if that cannot be told.

    A pane id is a lease, not an identifier — WezTerm renumbers and reuses them, so
    `str(rec["pane"]) in panes()` answers "is some pane wearing this id", never "is
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
    """
    if live_ids is None:
        return set()
    quiet = set()
    for rec in records:
        sid = rec.get("session_id")
        if not sid or sid in live_ids:
            continue
        if session_transcript_age(sid) > LIVE_WINDOW:
            quiet.add(sid)
    return quiet


def is_live(rec, pmap, quiet):
    """A row is rendered only when its session is not provably finished AND its pane
    still exists.

    Both conditions are necessary. The pane check alone is what leaked the orphans;
    the session check alone would keep a row the operator cannot jump to, and the
    jump line is this feed's payload. `quiet` is a set of ids, and `None` (an
    unreadable registry) reads as empty — drop nothing.
    """
    if str(rec.get("pane")) not in pmap:
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
    Measured 2026-09-21 — a live session read `MDM Bugs` in the registry against
    `Octopus MDM Bugs` in its enrichment record, the old name sitting in the
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
    § Silence 7 of [[Attention Item Schema]] calls strictly worse than a blank.

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
    return (f"  [{pane or '?':>4}] {age(rec['ts']):>6}  "
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
    gone. Measured 2026-09-24: `MDM Bugs` was live in the registry on pane 1391
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

    pmap = panes()
    if not pmap:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stuck-min", type=int, default=20)
    ap.add_argument(
        "--all",
        action="store_true",
        help=f"list every open item instead of the first {CAP}",
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
        sys.stderr.write(
            "who-needs-me: WezTerm pane list unreadable — the mux socket is not "
            "answering, so no record can be proven live. Refusing to print a feed "
            "that would read as an empty queue.\n"
        )
        sys.exit(1)
    # One registry read serves both questions: which sessions are live (the `quiet`
    # pass) and what each is called now (the ownership check). Reading it twice would
    # let the two disagree about which entries exist.
    registry = read_registry()
    live_ids = None if registry is None else set(registry)
    # Read the store once: load("needs") folds every event log, so a second call for
    # the quiet pass would double that cost.
    records = load("needs")
    quiet = quiet_session_ids(records, live_ids)
    live = lambda r: is_live(r, pmap, quiet)
    needs = [reclassify_idle(r) for r in records if live(r)]
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
    panels = sorted([r for r in needs
                     if is_rendered_panel(r) and not answered(r)
                     and is_open_gate(r, open_panes=open_panes,
                                      task_status=task_status_from_closer, include_panels=True)],
                    key=lambda r: r["ts"])
    reapable = sorted([r for r in needs if not answered(r)
                       and is_reapable(r, task_status_from_closer)], key=lambda r: r["ts"])
    stuck = sorted([r for r in tools if time.time() - r["ts"] > a.stuck_min * 60], key=lambda r: r["ts"])
    idle = sorted([r for r in needs if r["kind"] == "idle" and not answered(r)], key=lambda r: r["ts"])

    # Say which source answered, once, before any row. A silent fallback would
    # be indistinguishable from a healthy store — which is the whole failure
    # this note exists to prevent.
    if SOURCE_NOTE:
        print(SOURCE_NOTE)
    print(f"Needs you ({len(blocked)})")
    # Capped: the manager reads a summary. A reader that prints thirty
    # undifferentiated rows has failed to triage, not failed to report — and the
    # count of what it omitted is what keeps the cap honest rather than silent.
    shown, withheld = capped(blocked, a.all)
    for r in shown:
        print(row(r, pmap, f"{r['kind']}: {r['detail']}", registry))
    if withheld:
        print(f"{withheld} more — pass --all")
    print(f"\nRendered panels ({len(panels)})  — a closer line, not a parked gate")
    for r in panels:
        print(row(r, pmap, r["detail"], registry))
    print(f"\nProbably stuck > {a.stuck_min}m ({len(stuck)})")
    for r in stuck:
        print(row(r, pmap, r["detail"], registry))
    print(f"\nReapable ({len(reapable)})  — finished work on a close gate, yours to close")
    for r in reapable:
        print(row(r, pmap, r["detail"], registry))
    print(f"\nIdle, turn ended: {len(idle)}")
    if not blocked and not stuck:
        print("\nNothing needs you.")


if __name__ == "__main__":
    main()
