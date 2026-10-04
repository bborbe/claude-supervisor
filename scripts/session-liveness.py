#!/usr/bin/env python3
"""Is this session id live? One instrument, one answer, for the whole plugin.

The session registry `~/.claude/sessions/<pid>.json` is the liveness authority: an entry is
deleted when its session exits, so presence means live. It is read from `commands/` and
`agents/` by a restated `grep -l "<id>" ~/.claude/sessions/*.json`, and on 2026-09-26 a
manager hand-rolled a SECOND instrument over the same directory. The two disagreed about
what an id argument means — `grep -l` is substring matching, so a prefix is legal; the
hand-rolled reader did `reg.get(sid, [])`, an exact key lookup, so an 8-char prefix read as
ABSENT. It published `registry ABSENT` for two live sessions as *confirmed verdicts*. The
drive leg re-derived from disk, refused them, and spawned nothing; applied, the auto-resume
gate would have read as HOLDING and two resumes would have landed on two live conversations.

Measured 2026-09-27 on the live registry: the defect reproduces on **every** session, not the
two that were caught — 6 of 6 sampled, full id -> LIVE, that same session's own 8-char prefix
-> ABSENT. The near-miss rate is 100%, and nothing in the system detected it.

  --check <session-id>   LIVE (exit 0) / ABSENT (exit 1) / UNKNOWN (exit 2) / AMBIGUOUS (exit 3)
  --list                 one line per live session: `<session-id>  pid <n>  <name>`

⚠️ **An id argument is a PREFIX, and a prefix is legal.** Callers pass prefixes: the sweep
digest carries 8-char prefixes, `[ref]` in a roster is 6 chars, `sid8` is the display
convention, and the near-miss was one. A contract that rejects the input its own callers
produce is the defect, not the fix. A unique match resolves; an ambiguous one refuses rather
than guesses — the semantics `who-needs-me.py:pane_for()` already settled.

⚠️ **UNKNOWN is a third answer, not a flavour of ABSENT.** An unreadable or absent registry
means the probe could not run, and a caller that folds that into "not live" turns an I/O error
into permission to resume — which is exactly the double-writer this file exists to prevent.
Exit 2 is deliberately distinct from exit 1. Same rule as `live-workers.py`.

⚠️ **AMBIGUOUS is a fourth answer, and it is not a liveness verdict.** Two live sessions
sharing an 8-char prefix means the ARGUMENT did not identify one session; answering LIVE or
ABSENT either way would be a guess. Exit 3 names it and prints the candidates, so the caller
can pass the full id.

⚠️ **Why this file and not the two readers it replaces.** Neither existing registry reader
satisfied this contract. `who-needs-me.py:read_registry()` has the `None`-on-unreadable
three-state rule but returns names only, with no pid, so it cannot answer the runbook's
`ps -p <pid>` limb. `manager-predispatch.py:read_registry()` carries `pid` and `alive` but
returns `{}` when the registry directory cannot be read — "nothing is live", the dangerous
direction. One home has to hold both properties, so this reader does, and both of those
become callers of it.

⚠️ **TWO SOURCES, and the composition rule between them is asymmetric.** The registry answers
for every session that holds a socket — every interactive one — and is structurally blind to
a session with no process of its own: a headless worker is an in-process `query()` holding no
pid entry, and a cluster worker runs as a pod on another machine. Those are the heartbeat
store's half (`live-workers.py`, written by `server/heartbeat.mjs`; the store is also the
landing point for a cluster worker's registration). This file reads both and composes them
the way `server/liveness.mjs:checkLiveness()` does:

  * **a positive from either source is an answer.** A fresh heartbeat is decisive on its own,
    reported even when the registry could not be read — "could not tell" from one channel is
    not a refutation by the other.
  * **a negative needs BOTH sources readable.** With one unreadable, "no match" cannot rule
    out a match in the half that could not be read, so it is UNKNOWN, never ABSENT.

That asymmetry is the point. Only a readable-and-empty pair licenses ABSENT, which is the one
answer that permits a caller to resume onto the session.

⚠️ **A pid is not an identity, and `os.kill(pid, 0)` is not a liveness test.** It asks
whether SOME process holds the number — never whether that process is *this session's*. A
registry record that outlives its session (a `kill -9` leaves it behind) plus a pid the OS has
since recycled reads LIVE for a session that is gone, which is the residual
[[A Substring-Matched Liveness Probe Reports a Dead Session as Live]] left open. The record's
own `procStart` settles it: compared against the live holder's `ps -o lstart=`, a mismatch is
a different process wearing the same number. ⚠️ **The two are in different zones** —
`procStart` is UTC, `ps -o lstart=` is local — so they are compared as epochs, never as
strings; a string compare reports every live session as a mismatch, which is this rule
inverted. A record whose `procStart` is missing or unparseable while its pid IS occupied is
UNKNOWN: identity cannot be established, and "cannot tell" must not become death.

⚠️ **One instrument per store.** The heartbeat half is delegated to `live-workers.py` rather
than re-globbed here. Two readers over one directory is precisely the 2026-09-26 defect this
file's opening paragraph records, and it does not become acceptable by being written twice in
one repo instead of twice in one week.
"""
import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

LIVE, ABSENT, UNKNOWN, AMBIGUOUS = 0, 1, 2, 3

# `ps -o lstart=` and the registry's `procStart` carry the SAME format — and different zones.
_CTIME_FMT = "%a %b %d %H:%M:%S %Y"

REGISTRY_DIR = (
    os.environ.get("SUPERVISOR_SESSIONS_DIR")
    or os.environ.get("SESSIONS_DIR")
    or os.path.expanduser("~/.claude/sessions")
)

_LIVE_WORKERS = None


def _live_workers():
    """Import `live-workers.py` (hyphenated filename -> importlib) — the heartbeat reader.

    Lazy and cached: this module is itself imported by `who-needs-me.py` and
    `manager-predispatch.py`, and a registry-only caller must not pay for a store it never
    consults.
    """
    global _LIVE_WORKERS
    if _LIVE_WORKERS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live-workers.py")
        spec = importlib.util.spec_from_file_location("live_workers", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIVE_WORKERS = mod
    return _LIVE_WORKERS


def _record_start_epoch(proc_start):
    """The record's `procStart` as epoch seconds, or `None` when absent/unparseable.

    ⚠️ **`procStart` is UTC and `ps -o lstart=` is LOCAL, so the two are never
    string-comparable.** Measured 2026-10-01 on a CEST host: every one of six sampled live
    entries differed from its own process's `ps -o lstart=` by exactly +02:00 —
    `"Wed Sep 30 19:18:21 2026"` against `"Wed Sep 30 21:18:21 2026"`. A raw string compare
    therefore reports every LIVE session as a mismatch, which is this fix inverted: it turns
    the whole registry into false deaths.
    """
    if not isinstance(proc_start, str) or not proc_start.strip():
        return None
    try:
        # Tag it UTC rather than letting `.timestamp()` read the string in the host's zone.
        return int(datetime.strptime(proc_start.strip(), _CTIME_FMT).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


# --- the start-time probe, cached and time-bounded ------------------------------------------
#
# ⚠️ **Measured 2026-10-04, and it inverts the batching this probe used to be built around.**
# `ps -o pid=,lstart= -p <n>` costs **0.026 s at n=1 and 3.7–7.4 s at every n ≥ 2** (medians
# of 5 runs, 38 registry pids, host at load ~100): macOS `ps` takes a fast single-pid path
# for one `-p` argument and falls back to enumerating the WHOLE process table for two or
# more. The batched call was therefore the most expensive thing this function did, on every
# call, from every caller. Cache-first, the uncached set is normally 0–3 pids, so per-pid is
# both far cheaper and bounded.
#
# ⚠️ **The cache is PERSISTENT — a file, not a per-process dict — and that is what makes it
# work.** Every caller is a short-lived process (`session-liveness.py --check` from a manager
# loop, `who-needs-me.py`, `fleet-board.py`), so an in-process dict starts empty on every
# call and saves nothing at all. Measured 2026-10-04 before this change: ~3 probe processes
# per second, each making one `ps` that lived ~45 s under load, held **~134 concurrent `ps`
# children, 134 of them with ppid 1** — their python parent had been killed by a caller
# timeout — and pinned the load average at ~100.
#
# ⚠️ **A pid's start time is immutable, but its MEANING is not: pids are recycled.** A cached
# entry is trusted for `_START_CACHE_TTL` seconds and then re-read. That TTL is bounded by the
# pid-reuse window, which is measurable and was measured: pids allocate at **~420/s** on this
# host (2129 and 2091 across two 5 s windows) and the space is 99999 wide, so a given number
# cannot come round again for **~238 s**. 60 s leaves ~4x margin. ⚠️ **The failure this
# bounds is precisely the one the identity check exists to catch** — a stale record plus a
# recycled pid reading LIVE — so the margin is not decoration: at a churn rate above
# ~1667 pids/s the window closes and this TTL has to come down with it.
#
# ⚠️ **Every `ps` call stays inside `_ps_starts`, and that is load-bearing.**
# `transport-read-walk.py` derives transport sites from `subprocess` calls, and
# `transport-read-check.py` fails when the walk derives a site outside its `SITE_LIST`, which
# names `("session-liveness.py", "_ps_starts")`. A `ps` call moved into a helper would be a
# new site that enumeration does not carry.
_START_CACHE_DEFAULT = os.path.expanduser("~/.claude/state/session-liveness-starts.json")
# Seconds a cached start time is trusted. Bounded by the pid-reuse window — see above.
_START_CACHE_TTL = float(os.environ.get("SUPERVISOR_START_CACHE_TTL") or 60)
# Seconds any single `ps` may run before it is killed. A single-pid `ps` measured 0.026 s, so
# this is a backstop against a wedged process, never a routine path.
_PS_TIMEOUT = float(os.environ.get("SUPERVISOR_PS_TIMEOUT") or 5)

# `{pid: [start_epoch, read_epoch]}`, loaded once per process. `None` until first use.
_START_CACHE = None


def _start_cache_path():
    """Where the start-time cache lives, resolved PER CALL rather than at import.

    ⚠️ **Resolved per call so a test can redirect the store with one environment variable**,
    which is not a convenience — it is the same rule the heartbeat store already carries
    ("an isolated heartbeat store, and it is not optional"): a suite that reads the real
    store has a result that depends on the machine's mood rather than on the code. Five test
    modules load this one, and a pid-keyed cache is exactly the shape that goes flaky against
    a real store — a pid recycled between runs answers for the wrong process.
    """
    return os.environ.get("SUPERVISOR_START_CACHE") or _START_CACHE_DEFAULT


def _load_start_cache():
    """The persisted `{pid: [start, read_at]}` map; `{}` when absent or unreadable.

    ⚠️ **Unreadable is NOT an error here, and the contrast with the registry is the point.**
    `read_registry()` returns `None` for an unreadable directory because the registry *is*
    the answer — "could not tell" must never collapse into "not live". This cache only
    shortens the path to that answer: every entry it cannot supply is simply re-read from
    `ps`, so a missing or corrupt file costs latency and never correctness.
    """
    global _START_CACHE
    if _START_CACHE is None:
        _START_CACHE = {}
        try:
            with open(_start_cache_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return _START_CACHE
        if isinstance(data, dict):
            for key, val in data.items():
                if not isinstance(val, list) or len(val) != 2:
                    continue
                try:
                    _START_CACHE[int(key)] = [int(val[0]), float(val[1])]
                except (TypeError, ValueError):
                    continue
    return _START_CACHE


def _save_start_cache(cache):
    """Persist the cache atomically. A write that fails is swallowed — see `_load_start_cache`.

    ⚠️ **No lock, deliberately.** `os.replace` on a temp file in the same directory means no
    reader ever sees a partial write, and the entries are immutable per pid, so the worst a
    concurrent writer can do is drop an entry another process just learned. That costs one
    re-read; it cannot produce a wrong start time, which is the only outcome worth locking
    against.
    """
    path = _start_cache_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in cache.items()}, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        return


def _ps_starts(pids):
    """`{pid: start-epoch}` for the pids `ps` reported. Never raises, never guesses.

    Cache-first: a pid whose start time is known and younger than `_START_CACHE_TTL` is
    returned from the cache and never re-read. A pid absent from the cache is read with **one
    `ps` call of its own** — see the module note above on why that is per-pid, not batched.

    ⚠️ **The per-pid retry this function's caller used to hold is gone with the batching, and
    nothing was lost.** That retry existed because `ps` fails ALL-OR-NOTHING — measured
    2026-10-01, `ps -o pid=,lstart= -p 1,999999` exits 1, prints `process id too large`, and
    emits no rows at all, so one unreadable record blanked every session's identity at once.
    One pid per call contains a failure to the pid it happened to, by construction.

    ⚠️ **A pid whose read fails is simply ABSENT from the result, and that is deliberate.**
    `_pid_identity` answers `None` for it, and `None` is UNKNOWN — never death. Inventing a
    start time here would be the one direction that permits a caller to resume onto a live
    session, which is the answer this whole file exists to withhold.
    """
    if not pids:
        return {}
    now = time.time()
    cache = _load_start_cache()
    out, learned = {}, False
    for pid in sorted(set(pids)):
        entry = cache.get(pid)
        if entry is not None and 0 <= now - entry[1] < _START_CACHE_TTL:
            out[pid] = entry[0]
            continue
        try:
            proc = subprocess.run(
                ["ps", "-o", "pid=,lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
                timeout=_PS_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            # ⚠️ `subprocess.run`'s timeout kills the child AND waits for it, so the `ps` is
            # reaped here rather than reparented to launchd still running — which is exactly
            # how the ~134 orphans this change exists to remove were produced.
            continue
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            try:
                # `ps` prints in the host's LOCAL zone; a naive datetime's `.astimezone()`
                # reads it as local, which is exactly what is wanted on this side of the
                # comparison.
                start = int(
                    datetime.strptime(parts[1], _CTIME_FMT).astimezone(timezone.utc).timestamp()
                )
            except ValueError:
                continue
            cache[pid] = [start, now]
            out[pid] = start
            learned = True
            break
    if learned:
        _save_start_cache(cache)
    return out


def _pid_identity(pid, proc_start, live_starts):
    """`True`/`False` when the holder's identity can be decided, `None` when it cannot.

    `os.kill(pid, 0)` asks only whether SOME process holds the pid — never whether it is
    THIS session's. A record that outlives its session (a `kill -9` leaves it behind) plus a
    pid the OS has since recycled therefore reads LIVE for a session that is gone. The
    record's own `procStart` is the identity: compare it against the live holder's start
    time, and a mismatch is a different process wearing the same number.

    `None` is "cannot tell" and must never be read as death — ABSENT is the one answer that
    permits a caller to resume onto the session, so it needs evidence, not an absence of it.
    """
    recorded = _record_start_epoch(proc_start)
    if recorded is None:
        return None
    live = live_starts.get(pid)
    if live is None:
        return None
    return recorded == live


def read_registry(registry_dir=None):
    """`{sessionId: {pid, status, name, alive}}`, or `None` when the registry is unreadable.

    `None` and `{}` are different answers and must stay so: `{}` is "read it, no session is
    live", `None` is "could not read it". Collapsing them is how a permissions error becomes
    a confident all-clear.

    `glob` on a missing directory returns `[]` rather than raising, so the directory is
    checked explicitly — otherwise an absent registry would read as "no session is live" and
    a caller would resume onto a conversation it cannot see. Absence is not evidence of
    death; it is evidence the probe cannot run.

    ⚠️ **`alive` is a THREE-state — `True`, `False`, or `None` — not the bool it once was.**
    `os.kill(pid, 0)` proves only that the pid is OCCUPIED, never that the process holding it
    is this session. `True` means occupied **and** the record's `procStart` matches the live
    holder's start time; `False` means the pid is gone, or is held by a process that is not
    this record's; `None` means the pid IS occupied but the record carries no usable
    `procStart`, so identity cannot be established. `None` is "cannot tell", and a caller
    must read it as UNKNOWN — never as death.

    `PermissionError` means the process exists but is not ours — that is life, not death, so
    it still counts as occupied and the identity comparison runs on it.
    """
    d = registry_dir if registry_dir is not None else REGISTRY_DIR
    if not os.path.isdir(d):
        return None
    out = {}
    try:
        paths = glob.glob(os.path.join(d, "*.json"))
    except OSError:
        return None
    occupied = []  # (sid, pid, procStart) whose identity still has to be proven
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        sid = rec.get("sessionId")
        if not sid:
            continue
        pid = rec.get("pid")
        held = False
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)
                held = True
            except PermissionError:  # exists, but not ours -> occupied
                held = True
            except OSError:
                held = False
        if held:
            occupied.append((sid, pid, rec.get("procStart")))
        out[sid] = {
            "pid": pid,
            "status": rec.get("status", ""),
            "name": rec.get("name", ""),
            # `formerNames` and `cwd` are carried for `/supervisor:open`, which was reading
            # this registry itself — the second reader SC1 exists to collapse. It resolves a
            # topic's manager by matching the topic against the name the session holds NOW
            # **and** every name it has held: a long-lived manager gets renamed, and a
            # name-only match concludes there is none and spawns a SECOND manager onto a live
            # topic (observed 2026-09-18). It then spawns a resume into `cwd`, because the new
            # tab otherwise inherits wezterm's working directory and Claude Code stops on the
            # folder-trust prompt before registering a pid — so a resume that worked reads as
            # a no-op. Neither field is used by the liveness verdict; they are here so the one
            # reader can serve both questions and no caller has to open the directory again.
            "formerNames": [
                (f.get("name") if isinstance(f, dict) else f) or "" for f in (rec.get("formerNames") or [])
            ],
            "cwd": rec.get("cwd", ""),
            # `/supervisor:open` Step 4 checks this to prove a spawned session owns its name:
            # `nameSource: peer` means the `unset` prefix was ineffective and the session is
            # named after its spawner, which the title-match session-connect cannot resolve.
            "nameSource": rec.get("nameSource", ""),
            "alive": held,
        }
    # Identity pass, after every file is read so the probe sees the whole occupied set at
    # once and can answer each pid from the shared cache.
    if occupied:
        starts = _ps_starts([p for _, p, _ in occupied])
        for sid, pid, proc_start in occupied:
            out[sid]["alive"] = _pid_identity(pid, proc_start, starts)
    return out


def read_registry_entries(registry_dir=None):
    """Every registry record as `(path, rec)`, or `None` when the directory is unreadable.

    The **list** shape, alongside `read_registry()`'s dict — one file, one glob, one unreadable
    rule, two views. They exist because they answer different questions:

      * the dict answers "which sessions are there", and **collapses two files that claim one
        session id** — which is the right answer for a liveness verdict;
      * this answers "what is on disk", and **preserves that collision**.

    The collision is not hypothetical and must stay visible. `restart-worker.py` refuses when
    two entries claim one id, because resuming onto the wrong claimant of two is exactly the
    double-writer this plugin guards against — so a caller that needs to see the collision
    cannot be served by the dict, however convenient it is.
    """
    d = registry_dir if registry_dir is not None else REGISTRY_DIR
    if not os.path.isdir(d):
        return None
    try:
        paths = sorted(glob.glob(os.path.join(d, "*.json")))
    except OSError:
        return None
    out = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                out.append((path, json.load(fh)))
        except (OSError, ValueError):
            continue  # a half-written entry is skipped, not fatal
    return out


def read_heartbeats(heartbeat_dir=None, ttl=None, now=None):
    """Fresh stamps as `[{session_id, age_seconds, pid}]`, or `None` when unreadable.

    Delegates to `live-workers.py`. The verdict is the stamp's AGE against the TTL, never the
    file's existence: a store whose writer was `kill -9`'d keeps its files, so reading
    existence reports exactly the wrong answer for the case the store exists to catch.
    """
    lw = _live_workers()
    d = heartbeat_dir if heartbeat_dir is not None else lw.heartbeat_dir()
    return lw.read_live(d, ttl=lw.TTL_SECONDS if ttl is None else ttl, now=now)


def resolve(session_id, registry, heartbeats=()):
    """`(verdict, payload)` for one id argument against already-read sources.

    The argument is matched as a case-insensitive PREFIX against the UNION of both sources,
    which is what makes an 8-char `sid8` resolve to the same session its full UUID does —
    and what lets a cluster session, which holds no registry entry at all, resolve from its
    heartbeat. Exactly one match is a verdict; zero is ABSENT *only when both sources were
    readable*; more than one is AMBIGUOUS and carries the candidates, because a caller that
    guesses between two live sessions is the double-writer this file prevents.

    A matched registry entry is then read for its three-state `alive` (see `read_registry`):
    `True` is a live session, `False` is a pid that is gone or belongs to some other process,
    and `None` is "the pid is occupied but the record cannot prove the holder is this session"
    — UNKNOWN, never death.

    `heartbeats` defaults to `()`, i.e. "the store was read and holds nothing" — the honest
    reading for a caller that never consulted it, and the one that leaves a registry-only
    verdict unchanged. Pass `None` to mean "could not be read".
    """
    sid = (session_id or "").strip().lower()
    if not sid:
        return UNKNOWN, "no session id given"
    if registry is None and heartbeats is None:
        return UNKNOWN, "neither the session registry nor the heartbeat store is readable — cannot decide liveness"

    registered = sorted(k for k in (registry or {}) if k.lower().startswith(sid))
    # `state` is `live` for a fresh stamp and `unknown` for a cluster stamp whose store could
    # not be read — see `live-workers.py`. An entry predating the field has no `state`, and a
    # missing state must read as live rather than vanish: dropping it would silently empty the
    # store for every stamp written before this change.
    fresh = [h for h in (heartbeats or []) if (h.get("session_id") or "").lower().startswith(sid)]
    beating = sorted(h["session_id"] for h in fresh if h.get("state", "live") == "live")
    unresolved = sorted(h["session_id"] for h in fresh if h.get("state") == "unknown")
    matches = sorted(set(registered) | set(beating) | set(unresolved))

    if not matches:
        # A negative is licensed only by two readable sources. With either unreadable, the
        # match we did not find may be sitting in the half we could not read.
        if registry is None:
            return UNKNOWN, "session registry unreadable — cannot decide liveness"
        if heartbeats is None:
            return UNKNOWN, "heartbeat store unreadable — cannot decide liveness"
        return ABSENT, "no session id matches %s" % sid

    if len(matches) > 1:
        # 12 chars, not 8: the caller already passed a prefix long enough to collide, so
        # echoing 8 back hands them two identical strings and no way to tell them apart.
        return AMBIGUOUS, "%d live session ids match %s — pass a longer id: %s" % (
            len(matches),
            sid,
            ", ".join(m[:12] for m in matches),
        )

    full = matches[0]
    if full in unresolved:
        # The store says this cluster worker's stamp is stale while the cluster itself could
        # not be read. That is "cannot tell", never death — a network fault must not become
        # permission to resume onto a worker that may be alive behind it.
        return UNKNOWN, (
            "%s is a cluster worker and the cluster store could not be read — cannot decide liveness" % full
        )
    if any(h.get("session_id") == full and h.get("state", "live") == "live" for h in (heartbeats or [])):
        # A fresh stamp is decisive on its own — this is the half that answers for a session
        # the registry structurally cannot see.
        return LIVE, full
    entry = registry[full]
    if entry["alive"] is None:
        # The pid is occupied but the record carries no usable `procStart`, so we cannot say
        # whether the process holding it is this session. That is "cannot tell", and it must
        # NOT become ABSENT — ABSENT is the one answer that permits a caller to resume onto
        # the session, and the session may well be the one holding the pid.
        return UNKNOWN, (
            "%s is registered against pid %s, but the record carries no usable procStart "
            "— cannot tell whether that process is this session" % (full, entry["pid"])
        )
    # A pid that is gone, or that belongs to some OTHER process, is a stale file rather than a
    # live session — a `kill -9` leaves the record behind and the OS recycles the number. The
    # runbook's rule is "alive if ANY id holds an entry against a RUNNING pid", so presence
    # alone is not the verdict; presence against a pid that is demonstrably THIS session's is.
    if not entry["alive"]:
        # A stale record is a NEGATIVE, so it needs both sources readable — the same rule the
        # no-match branch applies. Returning ABSENT here while the heartbeat store is
        # unreadable would let a fresh stamp in the half we could not read be outvoted, and
        # ABSENT is the one answer that permits a caller to resume onto the session.
        if heartbeats is None:
            return UNKNOWN, (
                "%s is registered but pid %s is not this session, and the heartbeat store is "
                "unreadable — cannot decide liveness" % (full, entry["pid"])
            )
        return ABSENT, "%s is registered but pid %s is not this session — stale record" % (
            full,
            entry["pid"],
        )
    return LIVE, full


def main(argv=None):
    parser = argparse.ArgumentParser(description="Is this session id live? (registry + heartbeats)")
    parser.add_argument("--check", metavar="SESSION_ID", help="full id or an 8-char prefix")
    parser.add_argument("--list", action="store_true", help="print every live session")
    parser.add_argument("--dir", default=None, help="override the registry path")
    parser.add_argument(
        "--heartbeat-dir",
        default=None,
        help="override the heartbeat store; tests pass an isolated dir so a real headless "
        "worker on this machine cannot turn an ABSENT assertion into LIVE",
    )
    parser.add_argument("--ttl", type=int, default=None, help="heartbeat staleness bound in seconds")
    parser.add_argument(
        "--json",
        action="store_true",
        help="with --list, emit the records as JSON — the shape `/supervisor:open` reads to "
        "resolve a topic's manager, so it stops opening the registry itself",
    )
    args = parser.parse_args(argv)

    registry = read_registry(args.dir)
    heartbeats = read_heartbeats(args.heartbeat_dir, ttl=args.ttl)

    if args.check:
        verdict, payload = resolve(args.check, registry, heartbeats)
        if verdict == LIVE:
            beat = next(
                (h for h in (heartbeats or []) if h["session_id"] == payload and h.get("state", "live") == "live"),
                None,
            )
            if beat is not None:
                print("LIVE — %s  heartbeat age %ss  pid %s" % (payload, beat["age_seconds"], beat["pid"]))
            else:
                rec = registry[payload]
                print("LIVE — %s  pid %s  %s" % (payload, rec["pid"], rec["name"] or "(no name)"))
        elif verdict == ABSENT:
            print("ABSENT — %s" % payload, file=sys.stderr)
        elif verdict == AMBIGUOUS:
            print("AMBIGUOUS — %s" % payload, file=sys.stderr)
        else:
            print("UNKNOWN — %s" % payload, file=sys.stderr)
        return verdict

    # A list is only a list when BOTH halves were read. One unreadable source makes the output
    # partial, and a partial list presented as complete is the dangerous direction — it reads
    # as "this session is not live" for every session in the half that failed.
    if registry is None or heartbeats is None:
        missing = (args.dir or REGISTRY_DIR) if registry is None else (args.heartbeat_dir or "the heartbeat store")
        print("UNKNOWN — cannot read %s" % missing, file=sys.stderr)
        return UNKNOWN

    if args.json:
        # `alive` is normalised to a bool here, and the filter keeps everything but a PROVEN
        # negative. The dict's third state (`None` — "the pid is occupied but the record cannot
        # prove the holder is this session") is a verdict input for `resolve()`, not something a
        # `--list` consumer can act on; emitted raw it would read as `False` to any JSON caller
        # doing a truthiness test, and a consumer that acts on this list declares a task unowned
        # and spawns a duplicate onto a session that may be live.
        payload = [
            dict(rec, sessionId=sid, source="registry", state="live", alive=rec["alive"] is not False)
            for sid, rec in registry.items()
            if rec["alive"] is not False
        ]
        payload += [
            {
                "sessionId": beat["session_id"],
                "source": "heartbeat",
                "state": beat.get("state", "live"),
                "age_seconds": beat["age_seconds"],
                "pid": beat["pid"],
                "status": "",
                "name": "",
                "formerNames": [],
                "cwd": "",
                "nameSource": "",
                "alive": beat.get("state", "live") == "live",
            }
            for beat in heartbeats
        ]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return LIVE

    rows = []
    for sid, rec in registry.items():
        # Only a PROVEN negative is dropped. An unproven identity (`None`) stays listed, because
        # the alternative is a `--list` consumer concluding the session is gone — the direction
        # that permits a resume onto a conversation that may still be running.
        if rec["alive"] is not False:
            rows.append((sid, "pid %-7s %s" % (rec["pid"], rec["name"] or "(no name)")))
    for beat in heartbeats:
        if beat.get("state") == "unknown":
            rows.append((beat["session_id"], "heartbeat UNKNOWN (cluster unreachable)"))
        else:
            rows.append((beat["session_id"], "heartbeat age %ss  pid %s" % (beat["age_seconds"], beat["pid"])))
    rows.sort(key=lambda r: r[1])
    for sid, desc in rows:
        print("%s  %s" % (sid, desc))
    if not rows:
        print("no live sessions in %s" % (args.dir or REGISTRY_DIR))
    return LIVE


if __name__ == "__main__":
    sys.exit(main())
