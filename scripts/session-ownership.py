#!/usr/bin/env python3
"""The ownership readers shared by `gate-owner-filter.py` and `adopt-orphans.py`.

Three questions, one answer each, so the two callers cannot drift:

  * `load_ledger_checked` — the spawn records, with "could not read" kept distinct from
    "read, nobody recorded" (`None` vs `{}`). ⚠️ **That distinction is this module's
    reason to exist as much as the sharing is**: the filter fails OPEN on `None` (an
    unreadable ledger resolves every spawner as a manager, so every gate is kept) while
    `adopt-orphans.py` must REFUSE. Collapsing the two would turn a dead controller into
    "no spawns ever" for the adopter — a silent ownership move on no evidence.
  * `load_claims` — which manager has adopted a gated session.
  * `is_manager` — whether a spawner was never itself spawned, or was spawned AS a manager.

Extracted from `gate-owner-filter.py` so `adopt-orphans.py` can import the three without
importing the whole filter, which would otherwise keep the filter undeletable. The filter
imports them back and re-exports them unchanged.
"""

import importlib.util
import json
import os

# Same env override the writer (`ownership-claim.py`) honours, deliberately: a
# writer pointed at one file while every reader reads another is a SILENT
# no-op -- the claim is recorded, `list` reports it held, and no watcher ever
# consults it. The two resolutions must not drift.
CLAIMS = os.environ.get("SUPERVISOR_OWNERSHIP_CLAIMS") or os.path.expanduser(
    "~/.claude/state/ownership-claims.json"
)

_SPAWN_LEDGER = None


def _spawn_ledger():
    """Import `spawn-ledger.py` (hyphenated filename -> importlib) — the ONE ledger reader.

    ⚠️ **The ledger moved to the attention store's endpoint on 2026-10-14, and this module
    DELEGATES rather than reading the store itself.** `spawn-ledger.py` was extracted so
    `worker-sessions.py` and `session-liveness.py` could share one reader; this module
    must not grow a second endpoint client, which would recreate exactly the drift the
    extraction removed. What it does NOT adopt is that module's `None`-on-unreadable
    answer — see `load_ledger_checked` for why the two callers need different ones.
    """
    global _SPAWN_LEDGER
    if _SPAWN_LEDGER is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spawn-ledger.py")
        spec = importlib.util.spec_from_file_location("spawn_ledger", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SPAWN_LEDGER = mod
    return _SPAWN_LEDGER


def load_ledger_checked(endpoint=None):
    """Spawn records keyed by session id, or None when the store could not be read.

    ⚠️ **`{}` and `None` are different answers and the two callers need different ones.**
    `{}` is "read it, nobody is recorded"; `None` is "could not read it" — an unreachable
    controller, a non-200, or a body that is not a row array. `load_ledger` COLLAPSES them,
    because the filter must fail OPEN (an unreadable ledger resolves every spawner as a
    manager, and keeping every gate is the safe direction there). `adopt-orphans.py` must
    NOT collapse them: it refuses on an unreadable store, because adopting on the strength
    of spawn edges it could not read would hand one manager's workers to another.
    """
    return _spawn_ledger().read_ledger(endpoint)


def load_claims(path=CLAIMS):
    """Open ownership claims, keyed by gated session id, or {} when unreadable.

    An unreadable store is NOT an empty one, and here the two happen to agree:
    both fail open. That is deliberate -- a claim store that cannot be read must
    never silence a manager's watcher, and the cost of a missed drop is one
    wasted wake against a fleet-wide silence.

    Only entries carrying a `manager` are returned; a malformed entry is skipped
    rather than read as a claim held by nobody. Read directly rather than by
    shelling out to `ownership-claim.py`, so the caller keeps its single-process
    shape and stays runnable when the script is absent.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    claims = data.get("claims")
    if not isinstance(claims, dict):
        return {}
    out = {}
    for session_id, entry in claims.items():
        if isinstance(entry, dict) and entry.get("manager"):
            out[str(session_id)] = str(entry["manager"])
    return out


def is_manager(session_id, ledger):
    """A spawner is a manager when it was never itself spawned, or was spawned AS one.

    The spawn edge alone cannot answer this. `/supervisor:open` starts a topic
    manager in a plain wezterm tab, which writes no ledger record, so absence is the
    common case -- but `spawn_agent(role="manager")` is a documented call that DOES
    write one, and reading any record as proof of "worker" made such a manager
    indistinguishable from the workers it spawns. Measured 2026-10-06: the CDB in
    Weldall Manager (`1d379d23`, spawned by the Fleet Manager) had a record, so every
    peer resolved its workers `worker-spawner` and re-escalated gates it had already
    handled -- the same gates escalated twice.

    ONLY an explicit `manager` counts, and a record with no `role` reads as a
    worker. That default is NOT "no pre-field manager has a record" -- the very
    manager this fixes had one. It is that a pre-field record cannot be told
    apart: the field is absent on every worker AND every manager spawned before
    it existed alike. Defaulting the other way would flip every pre-field record
    to manager, and workers outnumber managers by orders of magnitude, so it
    would drop the whole fleet's gates -- the fleet-wide silence every other hop
    here fails open to avoid. The residue is a known gap, not a claim that no
    such manager exists: a pre-change `spawn_agent(role="manager")` manager still
    reads as a worker until it is re-spawned or its record gains the field.
    """
    if not session_id:
        return False
    record = ledger.get(session_id)
    if record is None:
        return True
    return record.get("role") == "manager"


HEADLESS_PREFIX = "hst:"

def is_human(session_id, ledger):
    """The gated session is the OPERATOR'S own workspace, not a delegate.

    `role: human` is what `spawn_agent` writes for a row opened into the Direct window
    (`commands/open.md` § Step 0.6), so its gate is answered in that tab and no manager ever
    acts on it. Measured 2026-10-27 on Fleet Manager `ac24db61`: pane 2165
    (`Check Prometheus Alerts - 2026-10-27`, `role: human`) fired the feed three times across
    three consecutive 30-minute windows — see the module header § ROLE.

    ⚠️ **ONLY an explicit `human` counts, and the default is the OPPOSITE of `is_manager`'s.**
    A record with no `role`, `role: null`, or no record at all is NOT human. The two defaults
    point opposite ways because the failure directions do: `is_manager` defaults to worker so
    a pre-field record cannot drop the whole fleet's gates, and this defaults to not-human so
    an unrecorded session cannot be silently excluded — that same fleet-wide silence, reached
    from the other side. `role` is a positive assertion here, never an absence.
    """
    if not session_id:
        return False
    record = ledger.get(session_id) or {}
    return record.get("role") == "human"


def feed_rows(stream):
    """(pane, replayed) pairs in a `who-needs-me.py` render, first-appearance order.

    Reads the `[<pane>]` row marker rather than any `--pane-id` flag: the flag
    also appears on the activate command of a row whose pane may already be
    gone, so the marker is the row's own identity.

    ⚠️ **The marker is right-aligned inside its brackets**, so the digits are
    padded: a live render carries `[ 417]`, `[  90]`, `[   0]`, never `[417]`.
    Matching `\\[(\\d+)\\]` therefore matched **nothing** on a real feed and the
    caller exited `pass --pane, --feed, or both` against a feed holding eight
    panes — a scoping filter that renders as a filter which found nothing to
    pass. Measured 2026-10-01 against the live feed (8 pane rows, exit 2); the
    fixtures in `tests/test_gate_owner_filter.py` used unpadded ids and so never
    caught it. Tolerate whitespace on both sides.

    ⚠️ **`replayed` is read off the same line, and it is a guard rather than
    noise.** `who-needs-me.py` stamps `⟳replay` on every row it returns from its
    event-log fallback — the branch it takes when the attention store is
    unreachable — precisely so a manager does not act on a gate answered hours
    ago. This function used to return the pane id alone and discard the rest of
    the line, so the filter emitted `864` for a row the feed had rendered
    `5h49m  ⟳replay  ⚙ …` and the watcher woke on an already-answered gate.
    Measured 2026-10-03 (Manager Layer, ticks 89-90): **10 firings in ~70
    minutes, 2 real.** The mark is carried, never re-derived: it is a property
    of the read that produced the row, and `--pane` callers have no line to
    read it from.

    ⚠️ **A pane-less headless row is a gate too, keyed on its store item id.**
    `who-needs-me.py` renders a headless worker's answerable gate `[ hst]` — a
    headless worker owns no pane by construction — and the digit-only match used
    to skip it, so the filter read `gates: 0` over a feed carrying the gate and
    the armed watcher never woke. Measured 2026-11-03 01:06: 11 of 13 live gates
    were `[ hst]`. The row's own identity is the store item id its `answer:`
    continuation line names (`hst` is rendered ONLY when that line exists), so
    the key is `hst:<store item id>` — never a pane, which this row cannot have.
    `headless_items` resolves the session behind that key from the store.
    """
    import re

    lines = list(stream)
    out, marks = [], {}
    for index, line in enumerate(lines):
        match = re.match(r"\s*\[\s*(\d+)\s*\]", line)
        if match:
            pane = match.group(1)
        elif re.match(r"\s*\[\s*hst\s*\]", line):
            pane = headless_key(lines, index)
            if pane is None:
                continue
        else:
            continue
        if pane not in marks:
            marks[pane] = REPLAY_MARK in line
            out.append(pane)
    return [(pane, marks[pane]) for pane in out]


def is_headless_key(pane):
    return str(pane).startswith(HEADLESS_PREFIX)


# What one store read says about one `hst:<id>` key. Three values, because a two-valued
# read ("a record, or none") collapsed two different facts into one.

REPLAY_MARK = "⟳replay"

def headless_key(lines, index):
    """`hst:<store item id>` for the `[ hst]` row at `lines[index]`, or None.

    Reads the row's continuation lines only — up to the next `[...]` row marker —
    so an `answer:` line can never be borrowed from a neighbouring row.
    """
    import re

    for line in lines[index + 1:]:
        if re.match(r"\s*\[", line):
            return None
        match = re.search(r"attention-answer\.py answer (\S+)", line)
        if match:
            return HEADLESS_PREFIX + match.group(1)
    return None


def owner_of(lines, index):
    """The session id a pane-less row declares on its `owner:` line, or None.

    A row with no pane cannot be joined to its session through the event log —
    `session_for_pane` has no pane to match. `who-needs-me.py` knows the owner (it
    read it from the store, or from the log on the fallback branch), so it renders
    it on the row's continuation block; this reads it back.

    The scan stops at the next `[...]` row marker, so an `owner:` line can never be
    borrowed from a neighbouring row — the same guard `headless_key` applies.

    ⚠️ **The alternative is a SECOND attention-store read.** `headless_items`
    re-reads the store to recover exactly this mapping, and a transient 3 s
    timeout there blanked every row's feed verdict for a whole manager round
    (measured 2026-11-08 05:06:29Z: `gated=0 · clear=0 · unread=11` for 11 rows,
    while the feed itself was answering). Carrying the owner on the row removes
    that read and the failure with it.
    """
    import re

    for line in lines[index + 1:]:
        if re.match(r"\s*\[", line):
            return None
        match = re.search(r"^\s*owner:\s*(\S+)\s*$", line)
        if match:
            return match.group(1)
    return None
