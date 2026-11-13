#!/usr/bin/env python3
"""`reap-idle-headless.py` — close the PROCESS of an idle headless worker, resumable later.

The fleet's chosen end-of-life for a parked headless worker: a worker idle for at least
`IDLE_FLOOR` is closed — **process only, transcript and task anchor untouched** — and the
manager later resumes the same session id with `--resume <sid>` when the recorded wait
clears. The reap-and-resume loop is the fix; this script is only its close half. It does
not decide *when* to run (the trigger) and it does not resume anything.

Why a separate verb rather than `close-worker.py --force`: `close-worker.py` documents that
SIGTERM is not a substitute for its task gate, and a force flag there would relax that
refusal for every caller. This verb has a different contract — the task need not be
finished, only parked — so it names its own gate instead of weakening that one. A
terminal-task worker is **refused** here and stays with `close-worker.py`.

⚠️ **It closes the process, never the session.** The transcript
(`~/.claude/projects/<escaped-cwd>/<sid>.jsonl`) and the task's `claude_session_id` must
survive: the resume consumer needs both. The success path asserts the transcript still
exists before reporting, so a change that turned this into a session delete fails loudly.

`--all` enumerates its own candidates instead of taking them on the command line: every
registry record that is `entrypoint: sdk-cli` and `status: idle`. That is exactly the
population the `not-headless` and `busy-target` guards would refuse anyway, so an
unfiltered sweep would print one refusal per tab worker per tick and bury the refusals
that matter. It decides *which* sessions to consider, never *when* to run — the caller
still owns the clock.

Eleven refusals, each exit 1 with a stable token as the first line of output:

  unknown-session-id    no registry entry carries that session id.
  ambiguous-pid         more than one entry claims the id, none carries an integer pid, or
                        the pid was recycled between the guards and the signal.
  already-gone          the pid is not alive; nothing to reap (a stale entry is removed).
  not-headless          `entrypoint` is not `sdk-cli` — a tab worker's pane would be left
                        open behind a dead session; that close is `close-worker.py`'s.
  busy-target           registry `status` is not `idle` — the worker is mid-turn.
  not-idle-long-enough  idle less than `IDLE_FLOOR` (15 min) per `statusUpdatedAt`.
  wrapper-busy          the wrapper record is unreadable, or reads a non-idle state, a
                        `pending_turns` that is not 0, or a `question` parked for less
                        than the bound. A question parked PAST the bound no longer
                        refuses — the park clock governs it (see `question_age_seconds`).
  gate-held             the session has an open attention card; closing it orphans a
                        question a person is about to answer.
  watcher-alive         a surviving child of the worker is not one of its MCP/stdio
                        helpers — a backgrounded command would die with the process.
  task-terminal         the task is terminal (`completed`/`aborted`/`hold`) or deferred to
                        the future: that is `close-worker.py`'s population, not this one.
  task-unresolved       the id appears in no page among the searched task dirs, and the
                        session's own configured vault contributed none of them because it
                        declares no `tasks_dir`. That is a config defect, not an unanchored
                        session — the anchor may live where this process cannot read — so it
                        keeps refusing. The other cause behind this token (an id in no page
                        among every configured vault) is now a close; see `unanchored-closed`.

One close outcome, exit 0, printed as the first line of output before the `🧹` header:

  unanchored-closed     the id appears in no task page among every configured vault's
                        `tasks_dir`, and at least one such directory was searched, so
                        nothing owns the session and nothing can resume it for. The guard
                        chain above has already proven it headless, idle past `IDLE_FLOOR`,
                        card-free, wrapper-idle and pid-identity-verified; the process is
                        closed and the transcript kept, same as any other reap. An empty
                        directory list (no vault declares a `tasks_dir`) keeps refusing —
                        nothing was searched, which is not the same as nothing being there.

⚠️ **The watcher read has a stated hole.** It inspects children of the claude pid; a
watcher detached with `setsid`/`nohup` leaves that tree and escapes it. The read guards the
common case, it is not a proof.

Exit codes: 0 reaped, 1 refusal or operational error, 2 usage error.
"""
import argparse
import datetime
import glob
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_sibling(filename):
    """Import a sibling script by path — the repo's own convention for sharing code."""
    name = os.path.basename(filename).replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


close_worker = load_sibling("close-worker.py")
precheck = load_sibling("restart-precheck.py")

SESSIONS_DIR = close_worker.SESSIONS_DIR

#: How long a worker must have been idle before its process may be closed. The operator's
#: ruling, 2026-11-07: idle >= 15 minutes -> close the process, keep the transcript, resume
#: on the recorded event. A scheduling policy, not an inference about abandonment.
IDLE_FLOOR = 15 * 60

#: "Not supplied" for an injected reading whose real value may legitimately be `None`.
_UNSET = object()

#: How long to wait for the pid and its registry entry to disappear after SIGTERM.
EXIT_DEADLINE = 8.0

WRAPPER_URL = os.environ.get("SUPERVISOR_WRAPPER_URL") or "http://localhost:18090"
#: The attention store — **the repo's own name for it**, not a second one. `session-liveness.py`,
#: `attention-ask.py`, `restart-precheck.py` and `server/config.mjs` all resolve it from
#: `ATTENTION_STORE_URL` (the server additionally honours `SUPERVISOR_ATTENTION_STORE`), and
#: the rule in `server/config.mjs` is explicit: an operator who moves the store must not have
#: to discover a second name for the same address. A private name here would leave the
#: `gate-held` guard reading the default port after the store moved — refusing every reap, or
#: worse, reading a stale store with no card and reaping a worker whose card lives elsewhere.
ATTENTION_URL = (
    os.environ.get("ATTENTION_STORE_URL")
    or os.environ.get("SUPERVISOR_ATTENTION_STORE")
    or "http://localhost:18080"
).rstrip("/")
#: The ledger is served by the same store — one address, one name.
LEDGER_URL = ATTENTION_URL

#: Where Claude Code writes transcripts, `<projects>/<escaped-cwd>/<sid>.jsonl`.
PROJECTS_DIR = os.environ.get("SUPERVISOR_PROJECTS_DIR") or os.path.expanduser(
    "~/.claude/projects"
)

#: The helper shapes `is_helper` recognises. Measured 2026-11-07: every idle worker has
#: children and every child seen was one of these — so "has a child" is not a watcher
#: signal, and this filter is what makes the read mean something. The match is anchored on
#: the executable (see `is_helper`), never on a bare substring.
SUPERVISOR_MCP_DIR_SUFFIX = "/server"


def refuse(token, detail):
    """Print the token on its own first line, then the human report.

    The caller branches on that first line, so a refusal that printed its report first
    would read as success — the same contract `close-worker.py` keeps.
    """
    print(token)
    print(f"❌ {token}: {detail}")
    return 1


def registry_entries(session_id, sessions_dir=None):
    """Every registry record claiming `session_id`."""
    out = []
    for path in sorted(glob.glob(os.path.join(sessions_dir or SESSIONS_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                rec = json.load(handle)
        except (OSError, ValueError):
            continue
        if rec.get("sessionId") == session_id:
            out.append((path, rec))
    return out


def candidate_session_ids(sessions_dir=None):
    """Every registry `sessionId` this verb can ever act on: headless, and currently idle.

    Filtered here rather than left to the guards. `not-headless` and `busy-target` would
    refuse every other record, so a sweep over the whole registry would emit one refusal
    per tab worker and per mid-turn worker on every tick — noise that buries the refusals
    that carry information. The other nine guards still run per candidate; this filter
    removes only the two that can never pass for the excluded records.

    A record with no `sessionId` is skipped: nothing downstream could join it back.
    """
    ids = []
    for path in sorted(glob.glob(os.path.join(sessions_dir or SESSIONS_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                rec = json.load(handle)
        except (OSError, ValueError):
            continue
        session_id = rec.get("sessionId")
        if not session_id:
            continue
        if rec.get("entrypoint") != "sdk-cli":
            continue
        if rec.get("status") != "idle":
            continue
        if session_id not in ids:
            ids.append(session_id)
    return ids


def idle_seconds(rec, now=None):
    """Seconds since the registry last moved this session to `idle`, or None.

    `statusUpdatedAt` is the moment the status became `idle`; `updatedAt` is the fallback
    for a record written before that field existed. Both are epoch milliseconds.
    """
    stamp = rec.get("statusUpdatedAt") or rec.get("updatedAt")
    if not isinstance(stamp, (int, float)):
        return None
    return max(0.0, (now if now is not None else time.time()) - stamp / 1000.0)


def question_age_seconds(question, now=None):
    """Seconds since the wrapper registered this park, or None when it cannot be aged.

    The clock is `question.requested_at`, the wrapper's own registration time
    (`claude-wrapper` `pkg/session.go:178`). ⚠️ **Never `last_event_age_seconds`** — that
    field measures stream silence, and the wrapper's own comment says "State is never
    derived from this value"; a child busy inside a long tool call is silent too.

    `None` means "cannot be aged", and the caller fails CLOSED on it: an unageable park is
    kept, never reaped on a guess.
    """
    if not isinstance(question, dict):
        return None
    raw = question.get("requested_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (now if now is not None else time.time()) - stamp.timestamp())


def is_helper(args):
    """True when a child's command line is one of the session's MCP / stdio helpers.

    ⚠️ **Biased toward "not a helper"**, because the two mistakes are not symmetric: a
    helper read as a watcher costs one refusal, while a watcher read as a helper reaps a
    process that was still working. So the match is anchored on the executable, not on a
    bare substring — a backgrounded `bash -c 'uvx foo'` is a watcher, and the old
    substring form called it a helper.
    """
    parts = args.split()
    if not parts:
        return False
    if "--mcp-stdio" in args:
        return True
    exe = os.path.basename(parts[0])
    # The supervisor's own MCP server: `bun run --cwd <claude-supervisor>/server …`.
    # ⚠️ The `/server` suffix alone is not enough — a worker's own `bun run --cwd
    # <repo>/server build` ends with `/server` too, and reading it as a helper would reap a
    # worker mid-build. The supervisor's checkout is named for it, so require that.
    if exe == "bun" and "run" in parts and "--cwd" in parts:
        index = parts.index("--cwd")
        cwd = parts[index + 1] if index + 1 < len(parts) else ""
        trimmed = cwd.rstrip("/")
        return trimmed.endswith(SUPERVISOR_MCP_DIR_SUFFIX) and "claude-supervisor" in trimmed
    if exe not in {"uv", "uvx", "npm", "npx", "bun", "node"}:
        return False
    # A known MCP server PACKAGE, not the bare `mcp-` substring: `node scripts/mcp-watch.js`
    # is a backgrounded watcher and must read as one, while `uvx mcp-obsidian` is a server.
    # The discriminator is the shape of the token — a package name carries no script
    # extension — so a watcher written as `mcp-<name>.js` cannot hide behind the filter.
    if "@playwright/mcp" in args or "@modelcontextprotocol/" in args:
        return True
    for token in parts:
        name = os.path.basename(token)
        if name.startswith(("mcp-", "mcp-server-")) and not name.endswith(
            (".js", ".mjs", ".cjs", ".py", ".ts", ".sh")
        ):
            return True
    return False


def watchers(pid):
    """Child pids of `pid` that are not MCP/stdio helpers — possible background work.

    ⚠️ **Three-state, like every transport read in this repo:** `None` means the process
    tree could not be read, `[]` means it was read and holds no non-helper child, and a
    list means that work is still running. Collapsing `None` into `[]` would reap a worker
    whose watcher merely could not be observed.
    """
    try:
        listed = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    out = []
    for child in listed:
        try:
            args = subprocess.run(
                ["ps", "-o", "args=", "-p", child],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if args and not is_helper(args):
            out.append(child)
    return out


def process_start(pid):
    """The pid's start time as `ps` reports it, or None when it cannot be read.

    Compared before and after the guards: the guards take seconds (a registry read, HTTP
    calls, a process-tree walk), and a pid that exited in that window can be recycled onto
    an unrelated process. A changed start time is proof the pid is not the one we vetted.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return " ".join(out.split()) or None


def fetch_json(url, timeout=3):
    """The parsed JSON at `url`, or None when it cannot be read."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None


def wrapper_handle(session_id, ledger_url=None):
    """The key the wrapper's routes use for this session.

    ⚠️ **Usually the Claude id, but not always.** Measured 2026-11-08: 22 of 22 live
    `sdk-cli` sessions answered `/api/1.0/sessions/<claude-id>` with a matching `id` —
    none of them wrapper-backed. A session spawned through `spawn_agent` carries a
    distinct `wrapper_session_id` in the ledger (`server/ledger.mjs:179-185`), and
    `server/supervisor.mjs:2983-2985` says every wrapper route keys off that handle.
    Those are exactly the workers this verb exists to reap, so resolving it matters.
    """
    row = fetch_json(
        f"{ledger_url or LEDGER_URL}/api/1.0/session-ledger/{session_id}"
    )
    if isinstance(row, dict):
        handle = row.get("wrapper_session_id")
        if isinstance(handle, str) and handle:
            return handle
    return session_id


def wrapper_record(session_id, wrapper_url=None, ledger_url=None):
    handle = wrapper_handle(session_id, ledger_url)
    return fetch_json(f"{wrapper_url or WRAPPER_URL}/api/1.0/sessions/{handle}")


def open_cards(session_id, attention_url=None):
    """`(cards, readable)` — the session's open attention cards, and whether the store answered.

    ⚠️ **An unrecognised payload is unreadable, not empty.** `server/open-cards.mjs` returns
    `null` when the body is neither a list nor an object carrying `items`/`data`; collapsing
    that into `[]` would let a store answering `{"error": …}` satisfy the `gate-held` guard
    and close a session whose card was never read.
    """
    data = fetch_json(f"{attention_url or ATTENTION_URL}/api/1.0/attention")
    if data is None:
        return None, False
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items")
        if items is None:
            items = data.get("data")
    else:
        return None, False
    if not isinstance(items, list):
        return None, False
    cards = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("producer_id") == session_id
        and item.get("state") == "open"
    ]
    return cards, True


def transcript_path(cwd, session_id, projects_dir=None):
    """`~/.claude/projects/<escaped-cwd>/<sid>.jsonl` — the artifact a resume reads.

    ⚠️ **Both `/` and `.` munge to `-`.** Claude Code escapes the cwd that way, so a
    checkout under `github.com` lands in `-repos-github-com-…`; escaping only `/` derived
    a path that never exists, and the success assertion then reported `transcript MISSING`
    for a reap that worked. The same formula is in `manager-menu-guard.py`.
    """
    escaped = (cwd or "").replace("/", "-").replace(".", "-")
    return os.path.join(projects_dir or PROJECTS_DIR, escaped, f"{session_id}.jsonl")


def resolve_transcript(cwd, session_id, projects_dir=None):
    """The transcript this session actually wrote, or None.

    The cwd-derived path is preferred, with a `*/<sid>.jsonl` glob as the fallback: a
    vault reached through a symlink produces two project directories, and the derived one
    can be the copy that does not exist.
    """
    derived = transcript_path(cwd, session_id, projects_dir)
    if os.path.isfile(derived):
        return derived
    hits = glob.glob(
        os.path.join(projects_dir or PROJECTS_DIR, "*", f"{glob.escape(session_id)}.jsonl")
    )
    return hits[0] if hits else None


def _stale_busy(rec, wrapper, in_flight):
    """True when a non-`idle` registry `status` is a stale claim, not a live turn.

    ⚠️ `status` is written by the harness and survives a turn that ended without an update,
    so a session can sit at `busy` with nothing in flight. The registry field alone is not a
    mid-turn signal in either direction — `close-worker.py` records the mirror case, where a
    pane-less worker parked on the permission channel "does not move its `status`".

    Quiescence needs both readings to say "nothing pending": an idle wrapper record with no
    pending turn and no parked question — the authoritative reading this guard already
    trusts further down — and no open tool marker inside `TOOL_MARKER_WINDOW`, the only
    record a pane-less session leaves mid-call and one this script does not otherwise read.
    A missing or unreadable wrapper fails closed to the old refusal.
    """
    if not isinstance(wrapper, dict):
        return False
    if wrapper.get("state") != "idle":
        return False
    if wrapper.get("pending_turns") != 0:
        return False
    if wrapper.get("question"):
        return False
    if in_flight is _UNSET:
        in_flight = close_worker.in_flight_tool(rec.get("sessionId") or "")
    return in_flight is None


def guard(rec, now=None, wrapper=None, cards=None, cards_readable=True, children=None, in_flight=_UNSET):
    """`(token, detail)` for the first failing guard, or None when the close may proceed.

    Pure apart from the injected readings, so the whole predicate is testable without a
    live worker. Order matters: identity, liveness, cheap state reads, then the watcher.
    """
    pid = rec.get("pid")
    if not isinstance(pid, int):
        return ("ambiguous-pid", "the registry entry carries no integer pid")
    if not precheck.pid_is_alive(pid):
        return ("already-gone", f"pid {pid} is not alive; nothing to reap")
    # ⚠️ Headless only. A tab worker's session id handed to this verb would be SIGTERMed as
    # a process, leaving its pane open behind a dead session; `sdk-cli` is the discriminator
    # the repo already uses (`commands/close.md`, `close-worker.py`), and a tab close is that
    # script's pane path, not this one.
    entrypoint = (rec.get("entrypoint") or "").strip()
    if entrypoint != "sdk-cli":
        return (
            "not-headless",
            f"entrypoint is {entrypoint or '(none)'!r}, not 'sdk-cli'; use close-worker.py",
        )
    status = (rec.get("status") or "").strip()
    if status != "idle" and not _stale_busy(rec, wrapper, in_flight):
        return ("busy-target", f"registry status is {status or '(none)'!r}, not 'idle'")
    # ⚠️ A parked wrapper session is aged by its PARK clock, not the registry's idle clock.
    # `statusUpdatedAt` says when the session last went idle, which can be hours before the
    # question was asked; using it would close a session minutes into a fresh park. The
    # park clock is resolved FIRST, and the registry clock below is skipped entirely for a
    # parked question — the two clocks are not alternatives, the park clock replaces it.
    question = wrapper.get("question") if isinstance(wrapper, dict) else None
    if question:
        park_age = question_age_seconds(question, now)
        if park_age is None:
            return (
                "wrapper-busy",
                "a question is parked on the wrapper record and its requested_at cannot be aged",
            )
        if park_age < IDLE_FLOOR:
            return (
                "wrapper-busy",
                f"a question has been parked for {int(park_age)}s < {IDLE_FLOOR}s floor",
            )
        # Past the bound: the park clock has governed. The registry idle clock is bypassed.
    else:
        idle = idle_seconds(rec, now)
        if idle is None:
            return ("not-idle-long-enough", "the entry carries no statusUpdatedAt/updatedAt")
        if idle < IDLE_FLOOR:
            return ("not-idle-long-enough", f"idle {int(idle)}s < {IDLE_FLOOR}s floor")
    if wrapper is None:
        return ("wrapper-busy", "the wrapper record could not be read")
    if wrapper.get("state") != "idle":
        return ("wrapper-busy", f"wrapper state {wrapper.get('state')!r}, not 'idle'")
    if wrapper.get("pending_turns") != 0:
        return (
            "wrapper-busy",
            f"wrapper pending_turns {wrapper.get('pending_turns')!r}, expected 0",
        )
    # ⚠️ The `question` refusal that used to sit here is gone: the park clock above decided
    # it. `gate-held` is untouched and still refuses at every age — closing a card-holding
    # session loses a question a person is about to answer (operator decision
    # `d09d23f5bdd857734714dbdc6a1d5f07`).
    if not cards_readable:
        return ("gate-held", "the attention store could not be read; failing closed")
    if cards:
        return (
            "gate-held",
            f"{len(cards)} open attention card(s): {cards[0].get('item_id')}",
        )
    if children is None:
        children = watchers(pid)
    if children is None:
        return (
            "watcher-alive",
            "the process tree could not be read, so a background watcher cannot be ruled out",
        )
    if children:
        return (
            "watcher-alive",
            f"non-helper child(ren) still running: {' '.join(children)}",
        )
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Close the process of an idle headless worker, keeping its transcript."
    )
    parser.add_argument("session_ids", nargs="*", help="session id(s) to reap")
    parser.add_argument(
        "--all",
        action="store_true",
        help="enumerate every headless idle session from the registry, in addition to any ids given",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="check every guard, close nothing"
    )
    parser.add_argument(
        "--sessions-dir", default=None, help="override the session registry directory"
    )
    args = parser.parse_args(argv)

    session_ids = list(args.session_ids)
    if args.all:
        for session_id in candidate_session_ids(args.sessions_dir):
            if session_id not in session_ids:
                session_ids.append(session_id)
        if not session_ids:
            # An empty fleet is not a usage error: --all found nothing to consider.
            print("nothing to do: no headless idle session in the registry")
            return 0
    elif not session_ids:
        parser.error("pass at least one session id, or --all")

    today = close_worker.defer_date.as_now()
    exit_code = 0

    for session_id in session_ids:
        entries = registry_entries(session_id, args.sessions_dir)
        if not entries:
            exit_code = refuse(
                "unknown-session-id", f"no registry entry carries {session_id}"
            )
            continue
        if len(entries) > 1:
            exit_code = refuse(
                "ambiguous-pid",
                f"{len(entries)} registry entries claim {session_id}: "
                f"{', '.join(os.path.basename(p) for p, _ in entries)}",
            )
            continue

        path, rec = entries[0]
        pid = rec.get("pid")
        # ⚠️ Sampled BEFORE the guard chain, not after it. The window this guard exists for
        # is the whole span between reading the entry and sending the signal — the guards
        # take seconds (HTTP reads, a process-tree walk), which is exactly the window a pid
        # can be recycled in. Sampling afterwards would cover only the microseconds between
        # that sample and the kill, and would look like a guard while covering nothing.
        started_at = process_start(pid) if isinstance(pid, int) else None
        wrapper = wrapper_record(session_id)
        cards, cards_readable = open_cards(session_id)
        verdict = guard(rec, wrapper=wrapper, cards=cards, cards_readable=cards_readable)

        unanchored = False
        if verdict is None:
            # The task gate is inverted here on purpose: `task-not-terminal` is this verb's
            # population, and a resolved *terminal* task belongs to close-worker.py.
            task = close_worker.find_task(session_id, rec.get("cwd"))
            bad = close_worker.task_verdict(task, today)
            if bad is None and task is not None:
                # `task_verdict` returns None for three different states — terminal, handed
                # off, and deferred to the future — and only the first is "already terminal".
                # Naming the wrong one sends the operator to close-worker.py on a false
                # premise, so say which it is.
                path_, fm, handed_off = task
                status = (fm.get("status") or "").strip()
                defer = fm.get("defer_date")
                if handed_off:
                    why = "has been handed off to a successor session"
                elif status in close_worker.TERMINAL:
                    why = f"is already terminal (status {status})"
                else:
                    why = f"is deferred to {defer}"
                verdict = (
                    "task-terminal",
                    f"{os.path.basename(path_)} {why}; use close-worker.py",
                )
            elif bad is not None and bad[0] == "task-unresolved":
                # Two causes share this token and only one is this verb's to close.
                # `vault-not-searched` is a config defect: the session's own vault declares
                # no `tasks_dir`, so its anchor may live in a directory this process cannot
                # read — closing it would be the defect wearing the close. The other cause,
                # an id in no page among every configured vault, is an unanchored finished
                # session: nothing owns it, so nothing can resume it for, and the guard chain
                # above has already proven it headless, idle past `IDLE_FLOOR`, card-free and
                # wrapper-idle.
                #
                # ⚠️ An EMPTY dir list is a third cause, and it must keep refusing: no
                # configured vault contributed a `tasks_dir`, so nothing was searched and
                # "no page carries it" is an absent search, not an absent anchor. Without
                # this guard an empty `vault-cli` config would read every session as
                # unanchored and close the whole fleet in one tick.
                _refusal, detail = close_worker.resolution_refusal(
                    session_id, rec.get("cwd")
                )
                searched = close_worker.task_dirs_for(rec.get("cwd"))
                if "not among them" in detail or not searched:
                    verdict = ("task-unresolved", bad[1])
                else:
                    unanchored = True
            elif bad is not None and bad[0] != "task-not-terminal":
                # Defensive: any future token keeps failing closed.
                verdict = ("task-unresolved", bad[1])

        if verdict is not None:
            # `already-gone` promises the stale entry is removed, so remove it: the file is
            # known to claim this session id and its pid is provably dead, and a liveness
            # record for a dead pid is stale by construction. Without this the entry
            # re-refuses on every run and the docstring is a lie. The refusal prints FIRST
            # — the token is the caller's contract, so nothing may precede it.
            exit_code = refuse(verdict[0], verdict[1])
            if verdict[0] == "already-gone":
                try:
                    os.unlink(path)
                    print(f"   ℹ️ removed the stale registry entry for dead pid {pid}")
                except OSError as error:
                    print(f"   ⚠️ stale registry entry {path} could not be removed: {error}")
            continue

        transcript = transcript_path(rec.get("cwd"), session_id)

        # --- the pre-signal re-read ----------------------------------------------------
        # Everything above was read once; the guards take seconds (two HTTP reads and a
        # process-tree walk), and the state they vetted can move in that window. The
        # readings are therefore re-taken immediately before the signal, and the whole
        # block runs BEFORE the success-looking header is printed: the refusal token is
        # the caller's contract, so a token printed under a `🧹` header reads as a
        # successful reap to anything branching on the first output line.
        try:
            with open(path, encoding="utf-8") as handle:
                fresh = json.load(handle)
        except (OSError, ValueError):
            fresh = {}
        if fresh.get("pid") != pid or fresh.get("sessionId") != session_id:
            exit_code = refuse(
                "ambiguous-pid",
                f"the registry entry changed during the guards (pid {pid} → "
                f"{fresh.get('pid')}); nothing was signalled",
            )
            continue
        # The wrapper record is the authoritative "nothing pending" reading, and for a
        # wrapper-backed worker it is the only one: a continuation that arrived during the
        # guards would otherwise be killed with the process.
        #
        # ⚠️ Read BEFORE the registry idle clock, and aged by the PARK clock when a question
        # is parked — the same rule `guard()` applies. Without this the re-read silently
        # undoes the decision: `idle_seconds` says when the session last went idle, which
        # can be minutes into a park whose question was asked hours ago, and the question
        # refusal would fire at every age.
        fresh_wrapper = wrapper_record(session_id)
        # A turn accepted during the guards flips the entry out of `idle` and re-stamps
        # `statusUpdatedAt`, so the guard chain's reading is stale exactly when it matters.
        # ⚠️ A `busy` that was already stale before the guards is still stale now: the same
        # `_stale_busy` reading decides it, against the re-read wrapper and a re-read tool
        # marker, so this second gate cannot silently undo the first one's decision.
        fresh_status = (fresh.get("status") or "").strip()
        if fresh_status != "idle" and not _stale_busy(fresh, fresh_wrapper, _UNSET):
            exit_code = refuse(
                "busy-target",
                f"registry status became {fresh_status or '(none)'!r} during the guards; "
                "nothing was signalled",
            )
            continue
        if fresh_wrapper is None or fresh_wrapper.get("state") != "idle":
            exit_code = refuse(
                "wrapper-busy",
                "the wrapper record became unreadable or non-idle during the guards; "
                "nothing was signalled",
            )
            continue
        fresh_question = fresh_wrapper.get("question")
        if fresh_question:
            fresh_park = question_age_seconds(fresh_question)
            if fresh_park is None:
                exit_code = refuse(
                    "wrapper-busy",
                    "the question parked during the guards cannot be aged; nothing was signalled",
                )
                continue
            if fresh_park < IDLE_FLOOR:
                exit_code = refuse(
                    "wrapper-busy",
                    f"the question has been parked {int(fresh_park)}s < {IDLE_FLOOR}s floor; "
                    "nothing was signalled",
                )
                continue
        else:
            fresh_idle = idle_seconds(fresh)
            if fresh_idle is None or fresh_idle < IDLE_FLOOR:
                exit_code = refuse(
                    "not-idle-long-enough",
                    f"idle dropped to {int(fresh_idle or 0)}s during the guards; "
                    "nothing was signalled",
                )
                continue
        if fresh_wrapper.get("pending_turns") != 0:
            exit_code = refuse(
                "wrapper-busy",
                "a turn arrived during the guards; nothing was signalled",
            )
            continue
        # The same recycle, seen from the process side rather than the registry side.
        # ⚠️ Fails CLOSED: if the pre-guard start time could not be read, the pid's identity
        # was never established, and a signal sent on an unverified identity is the defect
        # this guard exists to prevent.
        if started_at is None:
            exit_code = refuse(
                "ambiguous-pid",
                f"the start time of pid {pid} could not be read before the guards, so its "
                "identity was never established; nothing was signalled",
            )
            continue
        if process_start(pid) != started_at:
            exit_code = refuse(
                "ambiguous-pid",
                f"pid {pid} was recycled during the guards (start time changed); "
                "nothing was signalled",
            )
            continue

        if unanchored:
            # The close token is the caller's contract (`idle-reaper-tick.py` maps it to its
            # own outcome), so it is printed before the `🧹` header for the same reason every
            # refusal token is: a token printed under the header reads as a plain reap.
            print("unanchored-closed")
        print(
            f"🧹 reap-idle-headless · session {session_id[:8]} · pid {pid} · "
            f"{rec.get('name')!r}"
        )
        print(f"   idle {int(idle_seconds(rec) or 0)}s · transcript {transcript}")
        if args.dry_run:
            print(f"   ✅ would close pid {pid} (transcript kept)")
            continue

        if precheck.pid_is_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                print(f"❌ error: cannot signal pid {pid}: {error}")
                exit_code = 1
                continue

        deadline = time.time() + EXIT_DEADLINE
        while time.time() < deadline and precheck.pid_is_alive(pid):
            time.sleep(0.2)
        while time.time() < deadline and os.path.exists(path):
            time.sleep(0.2)

        alive = precheck.pid_is_alive(pid)
        entry_present = os.path.exists(path)
        if not alive and entry_present:
            try:
                os.unlink(path)
                entry_present = False
                print(f"   ℹ️ removed the stale registry entry for dead pid {pid}")
            except OSError as error:
                print(f"   ⚠️ registry entry {path} survived pid {pid}: {error}")

        # The whole point of this verb over close-worker: the resume consumer needs both.
        kept = resolve_transcript(rec.get("cwd"), session_id)
        transcript_kept = kept is not None
        ok = not alive and not entry_present and transcript_kept
        print(
            f"   {'✅' if ok else '⚠️'} reaped pid {pid} · "
            f"process {'dead' if not alive else 'STILL ALIVE'} · registry entry "
            f"{'removed' if not entry_present else 'STILL PRESENT'} · transcript "
            f"{'kept' if transcript_kept else 'MISSING'}"
        )
        if not ok:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
