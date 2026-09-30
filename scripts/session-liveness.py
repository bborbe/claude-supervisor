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
import sys

LIVE, ABSENT, UNKNOWN, AMBIGUOUS = 0, 1, 2, 3

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


def read_registry(registry_dir=None):
    """`{sessionId: {pid, status, name, alive}}`, or `None` when the registry is unreadable.

    `None` and `{}` are different answers and must stay so: `{}` is "read it, no session is
    live", `None` is "could not read it". Collapsing them is how a permissions error becomes
    a confident all-clear.

    `glob` on a missing directory returns `[]` rather than raising, so the directory is
    checked explicitly — otherwise an absent registry would read as "no session is live" and
    a caller would resume onto a conversation it cannot see. Absence is not evidence of
    death; it is evidence the probe cannot run.

    `alive` is the pid check the runbook documents. `PermissionError` means the process
    exists but is not ours — that is life, not death.
    """
    d = registry_dir if registry_dir is not None else REGISTRY_DIR
    if not os.path.isdir(d):
        return None
    out = {}
    try:
        paths = glob.glob(os.path.join(d, "*.json"))
    except OSError:
        return None
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
        alive = False
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)
                alive = True
            except PermissionError:  # exists, but not ours -> alive
                alive = True
            except OSError:
                alive = False
        out[sid] = {
            "pid": pid,
            "status": rec.get("status", ""),
            "name": rec.get("name", ""),
            "alive": alive,
        }
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
    beating = sorted(
        h["session_id"] for h in (heartbeats or []) if (h.get("session_id") or "").lower().startswith(sid)
    )
    matches = sorted(set(registered) | set(beating))

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
    if any(h.get("session_id") == full for h in (heartbeats or [])):
        # A fresh stamp is decisive on its own — this is the half that answers for a session
        # the registry structurally cannot see.
        return LIVE, full
    # An entry whose pid is gone is a stale file, not a live session — a `kill -9` leaves the
    # record behind. The runbook's rule is "alive if ANY id holds an entry against a RUNNING
    # pid", so presence alone is not the verdict; presence against a live pid is.
    if not registry[full]["alive"]:
        return ABSENT, "%s is registered but pid %s is gone — stale record" % (
            full,
            registry[full]["pid"],
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
    args = parser.parse_args(argv)

    registry = read_registry(args.dir)
    heartbeats = read_heartbeats(args.heartbeat_dir, ttl=args.ttl)

    if args.check:
        verdict, payload = resolve(args.check, registry, heartbeats)
        if verdict == LIVE:
            beat = next((h for h in (heartbeats or []) if h["session_id"] == payload), None)
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

    rows = []
    for sid, rec in registry.items():
        if rec["alive"]:
            rows.append((sid, "pid %-7s %s" % (rec["pid"], rec["name"] or "(no name)")))
    for beat in heartbeats:
        rows.append((beat["session_id"], "heartbeat age %ss  pid %s" % (beat["age_seconds"], beat["pid"])))
    rows.sort(key=lambda r: r[1])
    for sid, desc in rows:
        print("%s  %s" % (sid, desc))
    if not rows:
        print("no live sessions in %s" % (args.dir or REGISTRY_DIR))
    return LIVE


if __name__ == "__main__":
    sys.exit(main())
