#!/usr/bin/env python3
"""Which live manager panes run without the exit recorder — so their death would leave no record.

**Why this exists.** `session-exit-record.py` records how a pane ended, but only for a pane
whose spawn installed the writer (`commands/open.md`, `server/tab-spawn.mjs`,
`scripts/restart-worker.py`). A manager opened by hand runs with no writer, and until this
script nothing read the store, so the absence was invisible until the manager died and left
nothing to attribute — measured 2026-11-01, when the fleet manager died at midday with no
record.

**The test is a spawn line whose pid is still alive, not "the pane has a log".** Two traps:

1. A fresh manager spawn writes `kind=spawn … session=-`: its session id does not exist until
   the tab registers one (`server/tab-spawn.mjs`), so requiring the line to name the session
   would flag every correctly-spawned manager (245 of 443 live spawn lines read `session=-`,
   measured 2026-11-02).
2. The store is keyed on the pane, and wezterm reuses pane ids. A record left by an earlier,
   dead pane would make a hand-opened manager on the reused id look recorded. A spawn line
   whose `pid` is dead is therefore no evidence for the pane's current occupant.

**Pane resolution is by title, and an unresolved pane is UNKNOWN, never clean.** The registry
carries no pane id. The pane title is Claude Code's session name behind a leading status glyph
and, for a long name, truncated — so the join is exact first, then a unique prefix above a
length floor, and anything else is counted as `unknown`. A check that cannot run must not read
as one that found nothing.

Only manager-role sessions are checked; the caller supplies them (`fleet-board.py` owns the
role rule). Workers are out of scope — the recorder is not rolled out to them.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))

#: A shorter shared prefix is a coincidence, not a match.
PREFIX_FLOOR = 20


#: ANSI escape sequences, control characters (C0/C1) and the invisible format
#: characters (zero-width, bidi overrides, BOM) removed before a name reaches the
#: operator's terminal. `fleet-board.py`'s `cell()` folds whitespace for its
#: TAB-separated `--mismatches` path; that is not enough here — a BEL or an SGR
#: sequence survives a whitespace fold and executes in the terminal it is printed to.
#: The format characters matter for a second reason: they render as nothing, so a name
#: made only of them stays truthy and would defeat `unknown_line`'s session-id fallback.
_CONTROL = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b."
    r"|[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


def printable(text):
    """`text` safe to print: no ANSI escape sequences, no control or format characters.

    Deliberately stricter than `fleet-board.py`'s `cell()`, which is
    `" ".join(str(text).split())` and strips none of these — it only folds whitespace,
    so a tab-only fixture would pass on the unmodified code and prove nothing.
    """
    return _CONTROL.sub("", text or "")


def normalize(text):
    """Casefolded name without leading glyphs (`✳`, `⚙`, spinner frames) or doubled spaces."""
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"^[^\w(]+", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def resolve_pane(name, panes):
    """The one pane id whose title names `name`, or `None`.

    `panes` is `{pane_id: title}`. Exact normalized equality wins; otherwise one side must
    be a prefix of the other (either may be the truncated one) for exactly one pane, with
    the shorter side at least `PREFIX_FLOOR` characters. Ambiguity resolves to `None`.
    """
    key = normalize(name)
    if not key:
        return None
    titles = {pid: normalize(t) for pid, t in panes.items()}
    exact = [pid for pid, t in titles.items() if t == key]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    hits = [
        pid for pid, t in titles.items()
        if t and min(len(t), len(key)) >= PREFIX_FLOOR and (t.startswith(key) or key.startswith(t))
    ]
    return hits[0] if len(hits) == 1 else None


def field(line, name):
    """One `name=value` field of a tab-separated record line, or `""`."""
    for part in line.split("\t"):
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return ""


def has_live_spawn(lines, alive):
    """Does any `kind=spawn … writer=trap` line name a pid `alive()` reports running?"""
    for line in lines:
        if field(line, "kind") != "spawn" or field(line, "writer") != "trap":
            continue
        pid = field(line, "pid")
        if pid.isdigit() and alive(int(pid)):
            return True
    return False


def coverage(managers, panes, records, alive):
    """`{"unrecorded": [...], "unknown": [...]}` for the given manager sessions.

    `managers` is `{session_id: name}`, `panes` `{pane_id: title}`, `records`
    `{pane_id: [record lines]}`. Each unrecorded entry carries `session_id`, `name`, `pane`
    and the rendered `line`. Pure — the caller reads the live stores.
    """
    unrecorded, unknown = [], []
    for sid in sorted(managers):
        name = managers[sid]
        pane = resolve_pane(name, panes)
        if pane is None:
            unknown.append({"session_id": sid, "name": name})
            continue
        if has_live_spawn(records.get(str(pane), []), alive):
            continue
        unrecorded.append({
            "session_id": sid,
            "name": name,
            "pane": str(pane),
            "line": (
                f"⚠️ UNRECORDED: {printable(name)} — pane {pane}, session {sid[:8]} runs without the exit "
                "recorder (no live `kind=spawn` line); its death would leave no record"
            ),
        })
    return {"unrecorded": unrecorded, "unknown": unknown}


def unknown_line(result):
    """The `unknown` count as an action line, or `None` when every pane resolved."""
    n = len(result["unknown"])
    if not n:
        return None
    names = ", ".join(printable(u["name"]).strip() or u["session_id"][:8] for u in result["unknown"])
    return f"⚠️ UNRECORDED unknown: {n} manager pane(s) unresolvable from `wezterm cli list` — {names}"


_RECORD = None


def _record_module():
    """`session-exit-record.py`, imported once — its `state_dir()` and `alive()` are the store's own."""
    global _RECORD
    if _RECORD is None:
        spec = importlib.util.spec_from_file_location(
            "session_exit_record", os.path.join(_HERE, "session-exit-record.py"))
        _RECORD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_RECORD)
    return _RECORD


def read_records(directory=None, panes=None):
    """`{pane_id: [lines]}` from the store. Absent store reads as `{}`.

    `panes` narrows the read to those pane ids — the ones that actually resolved to a
    manager. `None` reads the whole store, which is what a caller with no resolved set
    still gets; the board always has one, and scanning every file per render is the
    cost this argument removes (443 spawn lines measured 2026-11-02).

    A torn write by the trap can leave invalid UTF-8, so bytes are replaced rather than
    raised on — one damaged record must not blind the check to every other pane.
    """
    directory = directory or _record_module().state_dir()
    out = {}
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return out
    wanted = None if panes is None else {str(p) for p in panes}
    for name in names:
        if not (name.startswith("pane-") and name.endswith(".log")):
            continue
        pane = name[len("pane-"):-len(".log")]
        if wanted is not None and pane not in wanted:
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as handle:
                out[pane] = [ln.rstrip("\n") for ln in handle if ln.strip()]
        except OSError:
            continue
    return out


def all_unknown(managers):
    """Every manager as `unknown` — the answer when the check itself cannot run."""
    return {"unrecorded": [], "unknown": [{"session_id": s, "name": n} for s, n in sorted(managers.items())]}


def live_coverage(managers, pane_map):
    """`coverage()` over the live store. `pane_map` is `wezterm_panes()`'s answer or `None`.

    `None` (an unreadable `wezterm cli list`) makes every manager `unknown` — it cannot
    prove a pane either way.

    ⚠️ Known limit: `alive()` is `kill(pid, 0)`, so a dead spawn's pid recycled by an
    unrelated process reads as live and the pane as recorded — a false clean. It needs a
    pane id AND a pid reused together, which is rare; closing it would mean comparing the
    process start time with the spawn line's timestamp.
    """
    if pane_map is None:
        return all_unknown(managers)
    panes = {pid: (rec.get("title", "") if isinstance(rec, dict) else "") for pid, rec in pane_map.items()}
    # Resolve first, then read only the panes that resolved: `coverage()` looks up a
    # record by the pane it resolved, so a file for any other pane can never be
    # consulted. Resolving here costs one extra pass over the pane titles (tens of
    # panes against a handful of managers) and saves opening every file in the store.
    resolved = {resolve_pane(name, panes) for name in managers.values()}
    return coverage(managers, panes, read_records(panes={p for p in resolved if p is not None}),
                    _record_module().alive)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    board = importlib.util.spec_from_file_location("fleet_board", os.path.join(_HERE, "fleet-board.py"))
    fb = importlib.util.module_from_spec(board)
    board.loader.exec_module(fb)
    managers = fb.manager_sessions()
    if managers is None:
        print("exit-record-coverage: session registry unreadable", file=sys.stderr)
        return 1
    result = live_coverage(managers, fb.wnm.wezterm_panes())
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for row in result["unrecorded"]:
            print(row["line"])
        line = unknown_line(result)
        if line:
            print(line)
        print(f"checked {len(managers)} manager(s): {len(result['unrecorded'])} unrecorded, "
              f"{len(result['unknown'])} unknown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
