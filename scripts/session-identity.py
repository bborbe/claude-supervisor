#!/usr/bin/env python3
"""Who is this session? The session registry read — identity and names, never a liveness verdict.

The session registry `~/.claude/sessions/<pid>.json` is the plugin's **identity** store: it
answers which sessions exist, what each is called now, every name it has held, and which pid
holds its socket. It is read from `commands/` and `agents/` by a restated
`grep -l "<id>" ~/.claude/sessions/*.json`, and on 2026-09-26 a manager hand-rolled a SECOND
instrument over the same directory. The two disagreed about what an id argument means —
`grep -l` is substring matching, so a prefix is legal; the hand-rolled reader did
`reg.get(sid, [])`, an exact key lookup, so an 8-char prefix read as ABSENT. It published
`registry ABSENT` for two live sessions as *confirmed verdicts*. The drive leg re-derived from
disk, refused them, and spawned nothing; applied, the auto-resume gate would have read as
HOLDING and two resumes would have landed on two live conversations.

⚠️ **This module is the ONE reader of that directory, and it is not the liveness authority.**
The verdict "is this session id live?" moved to `session-liveness.py`, which reads the
attention store's `session-heartbeat` endpoint alone — a session that holds a socket and one
that holds none (a headless worker, a cluster pod) answer through the same channel there.
What stays here is the question the registry is actually good at: **identity** — the name a
session holds, the names it used to hold, the `cwd` to resume it into, and the pid its socket
sits on. `session-liveness.py --list --json` imports these functions to join that identity
onto the endpoint's live rows, and `restart-worker.py` reads the raw list shape to refuse a
resume onto the wrong claimant of two.

The measured facts the machinery below rests on are recorded on the functions themselves and
must not be lost: the per-pid `ps` cost curve, the pid-reuse window that bounds the start-time
cache TTL, and the UTC-vs-local zone split between a record's `procStart` and `ps -o lstart=`.
"""
import glob
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# `ps -o lstart=` and the registry's `procStart` carry the SAME format — and different zones.
_CTIME_FMT = "%a %b %d %H:%M:%S %Y"

REGISTRY_DIR = (
    os.environ.get("SUPERVISOR_SESSIONS_DIR")
    or os.environ.get("SESSIONS_DIR")
    or os.path.expanduser("~/.claude/sessions")
)


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
# names `("session-identity.py", "_ps_starts")`. A `ps` call moved into a helper would be a
# new site that enumeration does not carry.
def _env_float(name, default, minimum=0.0):
    """A tunable read from the environment, parsed tolerantly.

    ⚠️ **Tolerant because this module is imported by seven other scripts** —
    `approved-not-started.py`, `who-needs-me.py`, `manager-predispatch.py`, `fleet-board.py`,
    `cluster-heartbeat.py`, `worker-sessions.py` and `restart-worker.py` — so a `ValueError`
    raised here at import does not disable the probe, it disables every one of them. A bare
    `float(os.environ.get(...) or default)` turns a typo'd `SUPERVISOR_PS_TIMEOUT=5s` into a
    whole-plugin outage, which is the same tolerant-parse rule `_start_cache_path()` below
    already follows for its own variable.

    ⚠️ **And `float()` accepts `nan` and `inf`, which for the timeout is not a bound at all.**
    `min(nan, remaining)` is `nan`, `subprocess`'s deadline becomes `monotonic() + nan`, and
    its `_remaining_time(...) <= 0` test is then False forever — so the child is never killed
    and the very orphan this change removes comes back, reachable through a single typo. A
    value that is not finite, or not above `minimum`, falls back to the default rather than
    being trusted. Only the timeout is unsafe in this way; a non-finite budget or TTL degrades
    in the safe direction, but the guard is uniform because a bound that can be silently
    switched off is not a bound.
    """
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    if not math.isfinite(value) or value <= minimum:
        return default
    return value


_START_CACHE_DEFAULT = os.path.expanduser("~/.claude/state/session-liveness-starts.json")
# Seconds a cached start time is trusted. Bounded by the pid-reuse window — see above.
_START_CACHE_TTL = _env_float("SUPERVISOR_START_CACHE_TTL", 60)
# Seconds any single `ps` may run before it is killed. A single-pid `ps` measured 0.026 s, so
# this is a backstop against a wedged process, never a routine path.
_PS_TIMEOUT = _env_float("SUPERVISOR_PS_TIMEOUT", 5)
# Seconds the WHOLE probe may run. ⚠️ **A per-call bound is not a bound on the call.** The loop
# is strictly serial, so `_PS_TIMEOUT` alone leaves a worst case of `n x 5 s` for `n` uncached
# pids — worse than the single batched call this change replaced, in exactly the pathological
# case it exists to fix (a wedged `ps`). This caps the probe itself; the steady state is 0-3
# uncached pids at 0.026 s each, so it is never reached on the hot path.
_PS_BUDGET = _env_float("SUPERVISOR_PS_BUDGET", 20)

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
                except (TypeError, ValueError, OverflowError):
                    # ⚠️ **`OverflowError` is in this tuple because omitting it is not a missed
                    # entry — it is the wrong ANSWER.** `json.load` accepts a bare `Infinity`,
                    # so `{"123": [Infinity, 1.0]}` parses cleanly and `int(inf)` then raises
                    # `OverflowError` — a subclass of `ArithmeticError`, not of either of the
                    # other two. Uncaught, it escapes `_load_start_cache` → `_ps_starts` (which
                    # documents "Never raises") → `read_registry` → `main()`, and an uncaught
                    # exception exits **1**, which is this module's documented **ABSENT** code
                    # (`LIVE, ABSENT, UNKNOWN, AMBIGUOUS = 0, 1, 2, 3`). A corrupt cache file
                    # would therefore report a live session as ABSENT — the one answer that
                    # permits a resume onto a live conversation, and precisely the failure this
                    # file exists to prevent. `who-needs-me.py` carries the same tuple for the
                    # same reason; this loader had re-opened the gap it closed.
                    continue
    return _START_CACHE


def _save_start_cache(cache):
    """Persist the cache atomically. A write that fails is swallowed — see `_load_start_cache`.

    ⚠️ **No lock, deliberately.** `os.replace` on a temp file in the same directory means no
    reader ever sees a partial write, and the entries are immutable per pid, so the worst a
    concurrent writer can do is drop an entry another process just learned. That costs one
    re-read; it cannot produce a wrong start time, which is the only outcome worth locking
    against.

    ⚠️ **The writer's own view is a SNAPSHOT, frozen at first use.** `_START_CACHE` is loaded
    once per process and never re-read, so a caller that outlives a few seconds writes back the
    world as it looked when it started, clobbering whatever another process learned meanwhile.
    That is acceptable only because every importer of this module is a single-shot script
    (`approved-not-started.py`, `who-needs-me.py`, `manager-predispatch.py`, `fleet-board.py`,
    `cluster-heartbeat.py`, `worker-sessions.py`, `restart-worker.py`) — checked, not assumed.
    A long-lived importer would need the memo re-read before the merge.
    """
    path = _start_cache_path()
    tmp = "%s.tmp.%d" % (path, os.getpid())
    owned = False  # set once the `os.open` below succeeds — see the handler
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # ⚠️ **`os.open` with an explicit `0o600`, not a bare `open()`.** The repo's other
        # `~/.claude/state/` writers do the same (`manager-predispatch.py`), for the reason its
        # comment gives: the file is never world-readable, *not even for the instant between
        # the write and a later chmod*. A bare `open()` lands at the umask — typically 0644 —
        # and this file is read as an identity assertion, so a writable-by-others store is a
        # poisoning surface for the TTL window however low the odds.
        # ⚠️ **`O_EXCL`, because the temp name is derived from our own pid and is therefore
        # predictable.** Without it a pre-created path — or a symlink — in a writable parent is
        # followed and truncated rather than rejected, which matters precisely because this file
        # is read as an identity assertion. The `EEXIST` branch is not optional: the name is
        # predictable, so a crashed predecessor's leftovers would otherwise wedge every later
        # write permanently, and nothing else ever clears them.
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            os.unlink(tmp)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        owned = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in cache.items()}, fh, sort_keys=True)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        # ⚠️ **Widened past `OSError`, and the temp file is unlinked rather than abandoned.**
        # `_ps_starts` documents "Never raises" and calls this on its way out, so a `TypeError`
        # out of `json.dump` — an unserialisable value — escaping here would break that
        # contract from the one function whose entire job is to be skippable. And `tmp` is
        # named per process and never reused, so no other path will ever clean it: without the
        # unlink a single failed write is permanent litter in the state directory.
        # ⚠️ **Only a file WE created is unlinked.** The name is pid-derived and therefore
        # predictable, so between the `EEXIST` recovery's unlink and its retry another process
        # could take the name — and an unconditional unlink here would delete *theirs*. Gating
        # on `owned` keeps the cleanup aimed at our own leftovers. (Same-uid threat model: such
        # a process could write the cache directly, so this is hygiene rather than escalation.)
        if owned:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return


def _ps_starts(pids, refresh=False, deadline=None):
    """`{pid: start-epoch}` for the pids `ps` reported. Never raises, never guesses.

    ⚠️ **`deadline` is threaded in by a caller that makes TWO passes over one probe.** The
    identity pass in `read_registry` calls this once for the whole occupied set and again for
    the pids whose identity it declined to trust; without threading, each pass derives its own
    `_PS_BUDGET` and a single `read_registry` spends **twice** the bound the module note
    advertises — re-entering, on the mismatch path, the unbounded-fan-out shape this change
    exists to remove. One deadline, two passes.

    Cache-first: a pid whose start time is known and younger than `_START_CACHE_TTL` is
    returned from the cache and never re-read. A pid absent from the cache is read with **one
    `ps` call of its own** — see the module note above on why that is per-pid, not batched.

    ⚠️ **`refresh=True` bypasses the cache for every pid given, and the one caller that needs
    it is `read_registry`'s identity pass.** A cached value is whatever process *last held*
    that pid; inside the TTL a recycled pid therefore hands back the dead holder's start time,
    which does not match the live holder's record — and a mismatch is a NEGATIVE, so it would
    be published as `ABSENT` for a session that is alive. That is a regression the cache
    introduces and the pre-cache code did not have, since `ps` then always answered for the
    current holder. See `read_registry`: a mismatch is re-proved uncached before it is
    believed.

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
    # ⚠️ **`time.monotonic()`, not `time.time()`, for the deadline.** `subprocess` enforces its
    # own `timeout=` against the monotonic clock, so a wall-clock deadline would leave the two
    # halves of one bound measuring against different clocks: a backwards step inflates
    # `remaining` past the budget, a forwards one trips the break early. `now` stays wall-clock
    # because the cache epoch genuinely is a wall-clock fact.
    if deadline is None:
        deadline = time.monotonic() + _PS_BUDGET
    for pid in sorted(set(pids)):
        entry = cache.get(pid)
        if not refresh and entry is not None and 0 <= now - entry[1] < _START_CACHE_TTL:
            out[pid] = entry[0]
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # The probe's OWN bound, not the per-call one — see `_PS_BUDGET`. Stopping here
            # leaves the remaining pids unanswered, which `_pid_identity` reads as `None`, i.e.
            # UNKNOWN: the safe direction, and the same one a per-pid timeout already takes.
            # ⚠️ **Said out loud, because a silent degraded read is this file's own
            # prohibition** — `main()` warns on stderr for every other one, and the difference
            # between "probed and not held" and "gave up halfway" is not visible to a caller
            # from the return value alone. A fleet board that renders most sessions UNKNOWN
            # with no signal is indistinguishable from a healthy fleet.
            print(
                "UNKNOWN — probe budget (%.0fs) exhausted; %d pid(s) unanswered"
                % (_PS_BUDGET, sum(1 for p in set(pids) if p not in out)),
                file=sys.stderr,
            )
            break
        try:
            proc = subprocess.run(
                ["ps", "-o", "pid=,lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
                # Whichever runs out first — this call's own bound, or the probe's.
                timeout=min(_PS_TIMEOUT, remaining),
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
        # ⚠️ **Pruned on the next write, which any newly-learned pid forces within a TTL
        # window.** An entry past the TTL is re-read on its next use regardless, so keeping it
        # buys nothing — and it is only ever OVERWRITTEN when that re-read succeeds, which a
        # pid that has since exited never does. Left alone the file grows monotonically, one
        # dead pid per session the host has ever run. ⚠️ **The prune sits inside `learned`
        # deliberately:** in a fully-warm steady state no write happens, so no prune does
        # either — moving it out would buy a write on the hot path for no correctness gain.
        # ⚠️ **Pruned by "not currently servable", not by age.** `now - read_at >= TTL` misses a
        # FUTURE-dated `read_at` — clock skew, or a hand-edited file — and that is the one input
        # that would otherwise let the map grow without bound, since the read guard's `0 <=`
        # lower bound already refuses to serve such an entry.
        for dead in [k for k, v in cache.items() if not 0 <= now - v[1] < _START_CACHE_TTL]:
            cache.pop(dead, None)
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
        # ⚠️ **One deadline across BOTH passes** — see `_ps_starts`. Deriving a second one for
        # the re-prove below would let a single `read_registry` spend twice the bound the
        # module note advertises.
        deadline = time.monotonic() + _PS_BUDGET
        starts = _ps_starts([p for _, p, _ in occupied], deadline=deadline)
        # ⚠️ **A mismatch is re-proved UNCACHED before it is allowed to become a negative.**
        # Inside the TTL a cached entry is the start time of whatever process *last held* that
        # pid, so a pid the OS recycled within the window hands back the dead holder's start,
        # disagrees with the live holder's record, and would publish ABSENT for a session that
        # is alive — the one answer that permits a resume onto a live conversation, and a
        # regression this cache introduced rather than one the pre-cache code carried (there,
        # `ps` always answered for the current holder). Only the mismatch arm pays for the
        # re-read. ⚠️ **The entry is DROPPED first**, so a re-read that itself fails leaves the
        # pid unanswered — UNKNOWN — rather than silently reinstating the value we just
        # declined to trust.
        doubtful = [p for _, p, ps in occupied if _pid_identity(p, ps, starts) is False]
        if doubtful:
            for pid in doubtful:
                starts.pop(pid, None)
            starts.update(_ps_starts(doubtful, refresh=True, deadline=deadline))
        # ⚠️ **The hardening above is ASYMMETRIC, deliberately, and the asymmetry is the point.**
        # A cached value is "whatever process last held that pid" for a MATCH exactly as for a
        # mismatch, so a session that died and whose pid the OS recycled inside the TTL can
        # still read LIVE off its own stale holder's start — where the pre-cache code answered
        # ABSENT, since `ps` then always spoke for the current holder. That residual is accepted,
        # not overlooked: re-proving a match uncached would mean re-reading every cached pid, and
        # the cache would save nothing at all. It is bounded by the same measured reuse window as
        # the other polarity (~238 s against a 60 s TTL), and it errs toward **LIVE — the
        # direction that withholds** the resume permission, where the mismatch arm errs toward
        # ABSENT, which grants it.
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
