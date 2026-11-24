#!/usr/bin/env python3
"""`close-worker.py` — close ONE finished session's pane, registry-guarded.

The manager-side twin of `restart-worker.py`. That script kills a stale pid and resumes
the same session; this one kills a *finished* session's pane and resumes nothing. It
exists because the fleet manager could open and restart a session but not close one, so
every finished tab waited for the operator (`commands/steps/rules.md`).

⚠️ **This is not a second kill mechanism.** The registry read, the hold store and the
`refuse()` contract are imported from `restart-worker.py`; the worktree probe, the
occupancy count and the cause-of-death classifier are imported from
`restart-precheck.py`; the pane→session join is imported from `jump.py`. What this file
adds is only what none of them answers: *is the target's task finished, and where is its
pane.*

Eight refusals, each exit 1 with a stable reason token as the first line of output:

  unknown-session-id  no registry entry carries that session id, or the registry is
                      unreadable. Closing needs a pid; there is none to close.
  ambiguous-pid       more than one registry pid claims that session id, or the entry
                      carries no integer pid. Closing one is a coin flip.
  held-session        the operator holds this session (`/supervisor:hold`). Only they
                      release it; closing is exactly the act the hold prevents.
  busy-target         the target is mid-turn. For a tab session that means `status` is not
                      `idle`. For a pane-less one (`sdk-cli`) it also covers an open tool
                      marker in `~/.claude/state/attention/<sid>.tool.json` younger than 20
                      minutes: such a session parked on the permission channel keeps
                      `status: idle` and writes no transcript closer, so the marker is the
                      only record left. A close loses the in-flight work outright — there is
                      no resume to fall back on. ⚠️ The marker is not reliably cleared at tool
                      end, so read it as "used a tool recently": it delays a pane-less close
                      by up to 20 minutes rather than blocking it.
  dirty-worktree      the target is the only live session in its worktree and that
                      worktree carries tracked changes. A close strands them: unlike a
                      restart, nothing comes back to finish the work.
  gate-held           the target's transcript tail carries an unanswered `👤 You:` gate.
                      Closing it discards a question the operator never answered.
                      A pane-less target whose gate no channel can answer classifies
                      `gate-unanswerable` instead (see `restart-precheck.py`) and is
                      closed — holding it would keep the slot until the process dies.
  task-unresolved     no task file in the session's vault carries this session id as
                      `claude_session_id` — and no goal or topic page does either (see
                      the goal/topic anchor below) — while the absence is NOT a proven
                      one: the session's own vault contributed no `tasks_dir`, or the
                      search covered nothing, so the anchor may live where this process
                      cannot read. Absent evidence is not evidence of a finished task;
                      fail closed. A *proven* absence is a different fact and closes —
                      see `unanchored-closed` below.
  task-not-terminal   any task the session holds as `claude_session_id` is neither
                      terminal (`completed`/`aborted`/`hold`) nor deferred to a future
                      `defer_date` — one live task refuses the close even when another
                      task bound to the same session has finished. Or, for a goal/topic
                      anchor, the page fails the goal/topic terminal-state rule below.

One close outcome, printed as the first line of output before the `🧹` header:

  unanchored-closed   every configured vault's `tasks_dir` was searched and no task, goal
                      or topic page carries this session id. Nothing owns the session and
                      nothing can resume it for, so the guards above are the whole
                      authorisation — busy, gate, hold and worktree have all already
                      passed. This is the token `reap-idle-headless.py` and
                      `close-idle-tab.py` emit for the same shape; until now this verb was
                      the one close path that refused it, leaving a finished idle session
                      with no sanctioned close at all.

Goal/topic anchor. When no task resolves the session, a goal page (`24 Goals` /
`23 Goals`) or topic page (`23 Topics`) is resolved by the same join: `claude_session_id`,
else the session's `metrics_sessions` entry. Goal/topic terminal-state rule: the session
may be closed when (1) it has handed the page over — its id is in `metrics_sessions` while
`claude_session_id` names another session; or (2) the page's `status` is `completed` or
`aborted`; or (3) the page carries a future `defer_date`. It may NOT be closed while it is
still the page's `claude_session_id` on a non-terminal page (`in_progress`, `next`,
`backlog`, `hold`, …) with no future `defer_date`. The task rule does not transfer: a goal
or topic stays `in_progress` across many sessions, so gating on "goal complete" would make
every goal manager unclosable.

Exit codes: 0 closed, 1 refusal or operational error, 2 usage error.

⚠️ **A pane is where the close is delivered, never what authorises it.** A tab session
(`entrypoint: cli`) is closed by killing its pane; a headless one (`entrypoint: sdk-cli`)
has no pane and is closed by SIGTERM on its registry pid. Both run the same guards above,
and both wait `EXIT_DEADLINE` for the pid and its registry entry to disappear. The old
`headless-target` refusal is gone: it made "no pane" read as "not closeable", which left
the operator as the only actor who could close a finished headless worker.

⚠️ **A headless worker that ended its turn is parked, not finished.** SIGTERM is not a
substitute for the task gate — `task-not-terminal` still refuses one whose task is live,
because an exited headless worker waits for a continuation and closing it discards that
(`docs/fleet-surface.md` § A headless worker exits at turn end — that is not "finished").

⚠️ **The close is a teardown, never a signal to the session's own conversation.**
`/vault-cli:sync-progress` and `/vault-cli:session-close` read the worker's conversation
and remain the worker's own act; this script only removes a surface whose work is already
finished. A session that still has something to say is refused by `task-not-terminal`.
"""
import argparse
import glob
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


SESSIONS_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSIONS_DIR") or "~/.claude/sessions"
)

# A vault's task directory is whatever its `vault-cli` config declares (`tasks_dir`);
# there is no built-in list. The goal and topic directories below are still fixed names,
# because those lookups pass an explicit tuple rather than reading the config.

# A vault's goal and topic directories — where a goal or topic manager's anchor lives.
# `private-personal` / `seibert-*` use `24 Goals`, the `24 Tasks` vaults use `23 Goals`;
# topics live in `23 Topics` wherever a vault has them.
GOAL_DIRS = ("24 Goals", "23 Goals")
TOPIC_DIRS = ("23 Topics",)

# A goal or topic page's own end states. Narrower than TERMINAL on purpose: `hold` on a
# goal is a parked page whose holder is still its manager, not a finished one.
PAGE_TERMINAL = {"completed", "aborted"}

# Where the vaults live. `SUPERVISOR_VAULTS_ROOT` first for the same reason the registry
# and the load path carry an override: a drill or a test must be able to point the task
# lookup at a tree it controls, and a committed script never hardcodes a home path.
VAULTS_ROOT = os.path.expanduser(
    os.environ.get("SUPERVISOR_VAULTS_ROOT") or "~/Documents/Obsidian"
)

#: `vault-cli config list --output json`, read once per process: {realpath(vault): tasks_dir}.
#: A `tasks_dir` of `None` means the vault declares no task directory and contributes none.
_VAULT_CONFIG = None


def _vault_config():
    """`{realpath(vault path): tasks_dir}` from the vault config.

    Read once per process: `find_task` and `find_pages` call the directory helpers several
    times per run, and shelling out per call would re-create the slow-lookup failure the
    `find_task` docstring records. The source is `vault-cli config list --output json`, or
    `SUPERVISOR_VAULT_CONFIG` (a JSON file with the same shape) when set — the override a
    drill or a test needs, for the same reason the registry and the vault root carry one.
    A failed or unreadable config yields `{}` — every vault then contributes nothing,
    which is the fail-closed direction.
    """
    global _VAULT_CONFIG
    if _VAULT_CONFIG is not None:
        return _VAULT_CONFIG
    config = {}
    override = os.environ.get("SUPERVISOR_VAULT_CONFIG")
    try:
        if override:
            with open(override, encoding="utf-8") as handle:
                entries = json.load(handle)
        else:
            proc = subprocess.run(
                ["vault-cli", "config", "list", "--output", "json"],
                capture_output=True, text=True, timeout=10,
            )
            entries = json.loads(proc.stdout or "[]") if proc.returncode == 0 else []
        for entry in entries:
            path = entry.get("path")
            if path:
                config[os.path.realpath(path)] = entry.get("tasks_dir")
    except Exception:
        config = {}
    _VAULT_CONFIG = config
    return config

# The terminal statuses. A deferred row is the other accepted end state: the operator has
# said "not now", which is a decision rather than an absence of one.
TERMINAL = {"completed", "aborted", "hold"}

# How long to wait for the pid and its registry entry to disappear after the pane is
# gone. Claude Code deletes the entry on exit (measured 2026-10-19: 34 live entries, zero
# stale), so this is normally a fraction of a second; the bound exists so a hung exit is
# reported rather than waited on forever.
EXIT_DEADLINE = 8.0

#: The attention store's in-flight tool markers, `<session-id>.tool.json`. Same override the
#: other readers honour, so a drill or a test can point at a tree it controls.
ATTENTION_DIR = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser(
    "~/.claude/state/attention"
)

#: How recent an open tool marker must be to mean "mid-call right now". The same window
#: `round-readings.py` applies: past it, an open marker is the stuck path's shape — a session
#: that is not working — and treating it as in-flight would refuse every close forever.
TOOL_MARKER_WINDOW = 20 * 60


def in_flight_tool(session_id):
    """`(label, age_seconds)` when an open tool marker says the session is mid-call, else None.

    This is the guard a transcript cannot give. A worker parked on the supervisor's permission
    channel writes no `👤 You:` line, so `gate-held` — which reads the transcript tail — sees
    nothing and reports `mid-work`, and the registry `status` does not move either.

    ⚠️ **The marker is written at tool start and is NOT reliably cleared at tool end** —
    measured 2026-11-02: the store held markers open for 10, 43 and 1,033 days. So an open
    marker is read as *"used a tool within the window"*, not as *"inside a call right now"*.
    The effect is a bounded delay, never a permanent block: a close attempted inside the
    window refuses as `busy-target` and succeeds on a later sweep. That is the safe
    direction, and it is the same reading `round-readings.py` applies — past the window an
    open marker is the *stuck* path's shape, and refusing on it forever would refuse every
    close.
    """
    path = os.path.join(ATTENTION_DIR, f"{session_id}.tool.json")
    try:
        with open(path, encoding="utf-8") as handle:
            rec = json.load(handle)
    except (OSError, ValueError):
        return None
    if rec.get("state") != "open":
        return None
    ts = rec.get("ts")
    age = (time.time() - float(ts)) if isinstance(ts, (int, float)) else TOOL_MARKER_WINDOW
    if age >= TOOL_MARKER_WINDOW:
        return None
    return (rec.get("tool") or rec.get("detail") or "tool"), age


def load_sibling(filename):
    """Import a sibling script by path — the repo's own convention for sharing code."""
    name = os.path.basename(filename).replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: `defer_date` is an instant, not a date — see `defer-date.py`.
defer_date = load_sibling("defer-date.py")


def frontmatter(path):
    """The YAML-ish frontmatter as a flat dict, or `{}` when there is none.

    Deliberately not a YAML parser: the vault's frontmatter is written by one tool
    (`vault-cli`), the keys this script needs are flat scalars, and a hand-rolled reader
    cannot fail on a construct the writer never emits.
    """
    out = {}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return out
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[match.group(1)] = value
    return out


def metrics_session_ids(path):
    """Session ids under `metrics_sessions` — every session that ever worked the task.

    The block is a list of maps (`session_id` + `started_at`), which the flat reader above
    cannot see: it keeps only top-level keys. The ids are read here so a session the task
    was handed off from can still be resolved to it.
    """
    ids = []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return ids
    if not lines or lines[0].strip() != "---":
        return ids
    in_block = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip():
            continue
        if not line[0].isspace():
            in_block = line.partition(":")[0].strip() == "metrics_sessions"
            continue
        if in_block:
            key, sep, value = line.strip().lstrip("- ").partition(":")
            if sep and key.strip() == "session_id":
                ids.append(value.strip().strip("'\""))
    return ids


def local_task_dirs(cwd, names=None):
    """Candidate directories at or above `cwd` — the fast half of `task_dirs_for`.

    Split out so a caller can answer the common case (a worker whose cwd is its vault)
    without paying for the whole-vault scan. With `names` given (the goal/topic lookup)
    each ancestor is probed for those directory names; with `names=None` (the task lookup)
    each ancestor that is a configured vault contributes its declared `tasks_dir`.
    """
    dirs = []
    if not cwd:
        return dirs
    config = None if names is not None else _vault_config()
    cur = os.path.realpath(cwd)
    while True:
        if names is not None:
            for name in names:
                candidate = os.path.join(cur, name)
                if os.path.isdir(candidate) and candidate not in dirs:
                    dirs.append(candidate)
        else:
            tasks_dir = config.get(os.path.realpath(cur))
            if tasks_dir:
                candidate = os.path.join(cur, tasks_dir)
                if os.path.isdir(candidate) and candidate not in dirs:
                    dirs.append(candidate)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return dirs


def task_dirs_for(cwd, vault_root=None, names=None):
    """Candidate directories: the vault above `cwd`, then every configured vault.

    The walk-up answers the common case — a worker whose cwd is its vault. The vault scan
    answers the repo-worktree case: a worker editing `claude-supervisor` has a repo cwd,
    and its task lives in whichever vault filed it. Both are bounded and read-only;
    neither is a filesystem-wide search.

    With `names` given (the goal/topic lookup) every directory under `vault_root` is
    probed for those names. With `names=None` (the task lookup) each configured vault
    contributes its declared `tasks_dir`, wherever it lives — not only under `vault_root`
    — so a vault outside `~/Documents/Obsidian` is searched too.
    """
    dirs = local_task_dirs(cwd, names)
    if names is not None:
        root = vault_root or VAULTS_ROOT
        for vault in sorted(glob.glob(os.path.join(root, "*"))):
            for name in names:
                candidate = os.path.join(vault, name)
                if os.path.isdir(candidate) and candidate not in dirs:
                    dirs.append(candidate)
        return dirs
    for vault_path, tasks_dir in sorted(_vault_config().items()):
        if not tasks_dir:
            continue
        candidate = os.path.join(vault_path, tasks_dir)
        if os.path.isdir(candidate) and candidate not in dirs:
            dirs.append(candidate)
    return dirs


#: Task-folder names probed for a vault that `vault-cli` config does not list — the same
#: tuple `fleet-sessions.py:108` uses. An absence proof must cover every vault the fleet
#: sweep searches, not only the configured ones.
FLEET_PROBE_DIRS = ("25 Tasks", "24 Tasks", "24 Goals", "23 Goals", "tasks")


def fleet_task_dirs(cwd, vault_root=None):
    """Every task directory the FLEET resolves anchors in — the union, never a subset.

    `task_dirs_for(cwd)` alone answers from `vault-cli` config, so a vault missing from
    that config contributes nothing and its sessions look unanchored. The sweep that
    decides what may be closed probes every vault under the Obsidian root, falling back to
    `FLEET_PROBE_DIRS` for unconfigured ones, so an absence proof taken over the narrower
    configured set would kill a live anchored tab. Union both: the root-wide probe and the
    configured scan, which also reaches a configured vault living outside the root.
    """
    dirs = list(task_dirs_for(cwd, vault_root, FLEET_PROBE_DIRS))
    for d in task_dirs_for(cwd, vault_root):
        if d not in dirs:
            dirs.append(d)
    return dirs


def _vault_for_cwd(cwd):
    """The configured vault whose path is `cwd` or one of its ancestors, else `None`."""
    if not cwd:
        return None
    config = _vault_config()
    cur = os.path.realpath(cwd)
    while True:
        if cur in config:
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def resolution_refusal(session_id, cwd, vault_root=None):
    """`(refusal, detail)` for a session that resolved to no task — naming the cause.

    Two causes share the `task-unresolved` token, and a reader must be able to tell them
    apart: the id appears in no page among the directories searched, or the session's own
    vault is not among those directories (its `tasks_dir` is unset, so it contributed
    nothing). The first sends the reader to the page; the second sends them to the config.
    """
    dirs = task_dirs_for(cwd, vault_root)
    own_vault = _vault_for_cwd(cwd)
    if own_vault and not any(
        d == own_vault or d.startswith(own_vault + os.sep) for d in dirs
    ):
        return (
            "task-unresolved",
            f"searched {', '.join(dirs)}; {own_vault} not among them",
        )
    return ("task-unresolved", f"no page carries {session_id}")


def unanchored_verdict(session_id, cwd, vault_root=None):
    """`(token, detail)` for a session no page carries — `task-unresolved` or `unanchored-closed`.

    `resolution_refusal` names *why* nothing matched; this decides whether the absence is
    evidence. Two facts look alike and are not:

    - the search was **incomplete** — the session's own vault contributed no `tasks_dir`,
      or no directory was searched at all — so the anchor may live where this process
      cannot read. That is `task-unresolved`, and it keeps refusing.
    - the search was **complete** — every configured vault's `tasks_dir` was walked and
      none carries the id. Nothing owns the session and nothing can resume it for, so a
      finished one may be closed.

    ⚠️ **The absent-search guard is load-bearing, not defensive.** Without it an empty
    `vault-cli` config would read every session as unanchored and close the whole fleet in
    one tick.

    ⚠️ **`fleet_task_dirs`, not `task_dirs_for`.** The proof must be as wide as the
    population it quantifies over: a vault missing from `vault-cli` config contributes no
    `tasks_dir`, so an absence proven over the configured set alone would declare a
    session in an unconfigured vault unanchored — the unreadable-vs-absent collapse this
    module forbids everywhere else.

    Shared with `close-idle-tab.py`, and the same predicate `reap-idle-headless.py`
    applies inline.
    """
    _refusal, detail = resolution_refusal(session_id, cwd, vault_root)
    if "not among them" in detail or not fleet_task_dirs(cwd, vault_root):
        return ("task-unresolved", detail)
    return (
        "unanchored-closed",
        f"no page among every configured vault carries {session_id}; nothing owns it, "
        "so nothing can resume it for",
    )


def find_pages(session_id, cwd, vault_root=None):
    """`(held, handed, error)` — every goal or topic page that records this session.

    `held` are pages whose `claude_session_id` is this session, `handed` pages that list it
    only under `metrics_sessions` (a recorded handover); each entry is
    `(kind, path, frontmatter, handed_off)`. `error` is a string when a lookup failed, and
    the caller must refuse on it: a failed grep is not evidence that no live page exists.

    All pages are collected rather than the first, because pages stay stamped after they
    end — one session can hold a completed goal and a live topic at once, and the live one
    must decide. A goal or topic manager holds a page, not a task, and `vault-cli goal
    work-on` / `topic work-on` record it the same way they record a task
    (`claude_session_id` plus a `metrics_sessions` entry — `vault-cli`
    `pkg/ops/goal_workon.go` `persistGoalSessionID`, `pkg/ops/topic_workon.go`
    `persistTopicSessionID`), so the join is the one `find_task` uses, over the goal and
    topic directories instead of the task ones.
    """
    held, handed = [], []
    if not session_id:
        return held, handed, None
    for kind, names in (("goal", GOAL_DIRS), ("topic", TOPIC_DIRS)):
        dirs = task_dirs_for(cwd, vault_root, names)
        if not dirs:
            continue
        try:
            found = subprocess.run(
                ["grep", "-rl", "--fixed-strings", session_id, *dirs],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:
            return held, handed, f"{kind} lookup failed ({exc})"
        # `grep` exits 1 on "no match", which is an answer; 2 is an error, which is not.
        if found.returncode not in (0, 1):
            return held, handed, f"{kind} lookup failed (grep exit {found.returncode})"
        for path in sorted((found.stdout or "").splitlines()):
            fm = frontmatter(path)
            if (fm.get("claude_session_id") or "").strip() == session_id:
                held.append((kind, path, fm, False))
            elif session_id in metrics_session_ids(path):
                handed.append((kind, path, fm, True))
    return held, handed, None


def page_verdict(page, today):
    """`(refusal, detail)` when a goal or topic anchor does not permit a close, else `None`.

    Goal/topic terminal-state rule. The session may be closed when (1) it has handed the
    page over — its id is in `metrics_sessions` while `claude_session_id` names another
    session; or (2) the page's `status` is `completed` or `aborted`; or (3) the page carries
    a future `defer_date`. It may NOT be closed while it is still the page's
    `claude_session_id` on a non-terminal page (`in_progress`, `next`, `backlog`, `hold`, …)
    with no future `defer_date`.

    The task rule does not transfer. A task's terminal status proves one session's work is
    finished; a goal or topic stays `in_progress` across many sessions, and its boxes and
    phase describe the page's whole lifetime. Gating on "goal complete" would make every
    goal manager unclosable — so the provable fact is that the session no longer holds the
    page, or that the page itself has ended or is parked.
    """
    kind, path, fm, handed_off = page
    if handed_off:
        return None
    status = (fm.get("status") or "").strip()
    if status in PAGE_TERMINAL:
        return None
    defer = fm.get("defer_date")
    if is_future(defer, today):
        return None
    return (
        "task-not-terminal",
        f"{kind} {path} is status {status or '(none)'!r} with defer_date "
        f"{defer or '(unset)'!r} and this session still holds it as claude_session_id; "
        f"a {kind} outlives its sessions, so a close needs a handover, a page that is "
        f"{'/'.join(sorted(PAGE_TERMINAL))}, or a future defer_date",
    )


def find_task(session_id, cwd, vault_root=None):
    """`(path, frontmatter, handed_off)` for the task this session may close against, or `None`.

    `claude_session_id` is the join: `vault-cli` writes it on entry and the session-connect
    step appends the metrics entry beside it. When that id has moved to another session the
    task the session was handed off from still counts — the id is then in its
    `metrics_sessions` while `claude_session_id` names the successor — and the third element
    says so, because a handed-off session has already put its work on the record.

    A session id appearing anywhere else in a task body is not a claim of ownership, so the
    candidates come from a substring grep (one process over a bounded directory set) and the
    match is then confirmed on `claude_session_id`, or on `metrics_sessions`.

    ⚠️ `grep -l` exiting 1 means "no match", which is the honest answer here and not an
    error — an empty stdout is read as "no task claims this session".

    ⚠️ **Two passes, cwd-derived directories first.** `task_dirs_for` lists the vault above
    the cwd before every other known vault, and the common case — a worker whose cwd IS its
    vault — is answered by those few directories in well under a second. A single grep over
    all of them takes longer than the timeout on a loaded host, and the `except` below then
    reads that as "no task claims this session": a false `task-unresolved` for a session
    that has an anchor, which is the unreadable-vs-absent collapse this repo forbids
    everywhere else. Measured 2026-11-09 — 29 directories, 15.0 s timeout exceeded, and the
    same session flipping between `would close` and `task-unresolved` across runs minutes
    apart.
    """
    if not session_id:
        return None
    dirs = task_dirs_for(cwd, vault_root)
    if not dirs:
        return None
    local = local_task_dirs(cwd)
    rest = [d for d in dirs if d not in local]
    handed_off = None
    for batch in (local, rest):
        if not batch:
            continue
        own, handoff, _error = _grep_task(session_id, batch)
        if own is not None:
            return own
        if handed_off is None and handoff is not None:
            handed_off = handoff
    return handed_off


def find_task_checked(session_id, cwd, vault_root=None):
    """`(task, error)` — `find_task`, with a failed SEARCH reported instead of collapsed.

    ⚠️ **`find_task` returns `None` for two different facts**: no page claims the session,
    and the grep that would have found one timed out or exited ≥ 2. A caller that treats
    `None` as proof of an absent anchor must use this function instead — closing on an
    unproven absence kills a live anchored session, the unreadable-vs-absent collapse this
    module forbids everywhere else. `find_task` keeps its old signature for the callers
    whose next step is a refusal, where collapsing to `None` fails closed.

    ⚠️ **The search covers `fleet_task_dirs`, not `task_dirs_for`.** A vault missing from
    `vault-cli` config contributes no `tasks_dir`, so an absence proven over the configured
    set alone would declare a session in an unconfigured vault unanchored and close a live
    anchored tab — an absence proof must be as wide as the population it quantifies over.
    """
    if not session_id:
        return None, None
    dirs = fleet_task_dirs(cwd, vault_root)
    if not dirs:
        return None, None
    local = local_task_dirs(cwd)
    rest = [d for d in dirs if d not in local]
    handed_off = None
    error = None
    for batch in (local, rest):
        if not batch:
            continue
        own, handoff, err = _grep_task(session_id, batch)
        if err:
            error = err
        if own is not None:
            return own, None
        if handed_off is None and handoff is not None:
            handed_off = handoff
    return handed_off, error


def _session_grep(session_id, dirs, timeout=15):
    """`(paths, error)` — every task file in `dirs` that mentions `session_id`.

    The one grep pass both task matchers share. `paths` carries whatever the search
    printed, `error` carries why it may be incomplete, and the caller weighs the two: a
    match is evidence even when the search also failed, while an empty result from a
    failed search is not.

    ⚠️ **ripgrep when it exists, and the difference is not cosmetic.** `grep -rl` over the
    11 375-file `private-personal/25 Tasks` measured **19.6 s** on 2026-11-12 — past this
    call's 15 s timeout — and a timeout here is indistinguishable from "no task claims this
    session". That is the false `task-unresolved` the `find_task` docstring records, and for
    the resume guard it is worse: `task is None` reads as "no anchor, allow the resume", so
    the terminal-task guard went silently inert for a task that was plainly there.
    `rg -l -F` over the same directory: **0.7 s**. The fallback keeps a host without rg
    working, with grep's known cost.

    ⚠️ **Parse the stdout BEFORE judging the exit code.** `grep -rl` can exit ≥ 2 and still
    have printed every match it found before the error — discarding that output would lose a
    task that is plainly there, which is the failure the exit-code check is meant to
    prevent, arrived at from the other side.
    """
    rg = shutil.which("rg")
    argv = (["rg", "-l", "-F", session_id, *dirs] if rg
            else ["grep", "-rl", "--fixed-strings", session_id, *dirs])
    try:
        found = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return [], f"task lookup failed ({exc})"
    paths = sorted((found.stdout or "").splitlines())
    # `grep` exits 1 on "no match", which is an answer; 2 is an error, which is not.
    if found.returncode not in (0, 1):
        return paths, f"task lookup failed (exit {found.returncode})"
    return paths, None


def _grep_task(session_id, dirs, timeout=15):
    """`(own, handed_off, error)` for one grep pass over `dirs`; each of the first two may
    be `None`.

    Split out of `find_task` so the two passes share one matcher and the ownership rule
    (`claude_session_id` beats a `metrics_sessions` membership) stays in a single place.

    ⚠️ **`error` is not decoration.** A timeout or a grep exit ≥ 2 returns no matches, which
    is indistinguishable from "no task claims this session" unless the failure is carried
    out separately — and a caller that reads the absence as proof closes a live anchored
    session.
    """
    paths, error = _session_grep(session_id, dirs, timeout)
    handed_off = None
    for path in paths:
        fm = frontmatter(path)
        if (fm.get("claude_session_id") or "").strip() == session_id:
            return (path, fm, False), None, None
        if handed_off is None and session_id in metrics_session_ids(path):
            handed_off = (path, fm, True)
    if handed_off is not None:
        return None, handed_off, None
    # No match AND a failed search is the one combination that must not read as absence.
    if error:
        return None, None, error
    return None, None, None


def _grep_tasks(session_id, dirs, timeout=15):
    """`(held, handed, error)` — every task in `dirs` that records this session.

    The collect-all sibling of `_grep_task`. `_grep_task` returns on the FIRST
    `claude_session_id` match, which is right for a caller that needs one anchor and wrong
    for a close: one session can be bound to two tasks at once — a finished one and a live
    one — and a first-candidate return then closes on the finished one while the live one
    is still being worked. Measured 2026-11-09 on session `f8544288`, whose reap closed it
    off a `completed` row while a second task sat at `in_progress`/`execution`.

    `held` are tasks whose `claude_session_id` is this session, `handed` tasks that list it
    only under `metrics_sessions` (a recorded handover); each entry is
    `(path, frontmatter, handed_off)`. `error` is carried out unchanged from
    `_session_grep`, for the caller to weigh against the set it collected.
    """
    paths, error = _session_grep(session_id, dirs, timeout)
    held, handed = [], []
    for path in paths:
        fm = frontmatter(path)
        if (fm.get("claude_session_id") or "").strip() == session_id:
            held.append((path, fm, False))
        elif session_id in metrics_session_ids(path):
            handed.append((path, fm, True))
    return held, handed, error


def find_tasks(session_id, cwd, vault_root=None):
    """`(held, handed, error)` — every TASK that records this session.

    The collect-all sibling of `find_task`, and **close-only**. `find_task` and
    `find_task_checked` keep their first-candidate contract: `restart-worker.py`,
    `reap-idle-headless.py`, `close-idle-tab.py`, `park-nudge.py` and `task-standdown.py`
    import them, so changing `_grep_task`'s return semantics in place would move the
    resume guard and the reap decision with it.

    ⚠️ **`task_dirs_for`, not `fleet_task_dirs`.** The fleet set is the *anchor* population
    — it includes `24 Goals` and `23 Goals` (`FLEET_PROBE_DIRS`) because an absence proof
    must cover every page a session can be anchored by. Collecting from it here would read
    a goal page as a task and take the task branch for a session that holds only a goal,
    bypassing `find_pages`/`page_verdict` and their different terminal rule. Goals and
    topics keep their own path below; this function is tasks only.

    Both passes are scanned — unlike `find_task`, which stops at the first batch that
    answers — because a close needs the whole bound set, not one anchor. The cwd-derived
    directories still go first, so the most relevant task is collected even when the wider
    scan fails.

    ⚠️ **A hit suppresses the error**, the rule `find_task_checked` already applies: a set
    that was found is evidence, and only an EMPTY set from a failed search is the
    unreadable-vs-absent collapse this module forbids. Refusing on any partial failure
    would make the verb unusable on a host without `ripgrep`, where the wider pass costs the
    measured 19.6 s and times out on every close — a refusal that names no live task at all.
    """
    held, handed = [], []
    if not session_id:
        return held, handed, None
    dirs = task_dirs_for(cwd, vault_root)
    if not dirs:
        return held, handed, None
    local = local_task_dirs(cwd)
    rest = [d for d in dirs if d not in local]
    error = None
    seen = set()
    for batch in (local, rest):
        if not batch:
            continue
        batch_held, batch_handed, err = _grep_tasks(session_id, batch)
        if err and error is None:
            error = err
        for entry in batch_held + batch_handed:
            if entry[0] not in seen:
                seen.add(entry[0])
                (held if not entry[2] else handed).append(entry)
    if held or handed:
        error = None
    return held, handed, error


def is_future(value, today):
    """True when `value` names an instant after `today`.

    `vault-cli` writes `2026-05-26`, the RFC3339 form an unquoted date scalar is
    normalised to, and a full timestamp with a time of day; `defer-date.py` reads all
    three, so a defer later TODAY counts as future and permits the close. An
    unparseable value is *not* a deferral — it is a value this script cannot vouch for,
    so the task is judged on its status alone and the reason goes to stderr.
    """
    parsed, reason = defer_date.parse_defer_date(value)
    if reason:
        print(f"close-worker: {reason} — not counted as a future defer", file=sys.stderr)
        return False
    if parsed is None:
        return False
    return parsed > defer_date.as_now(today)


def _defer_expired(value, today):
    """True when `value` parses to an instant strictly before the start of `today`.

    Strictly before, not `<=`. A defer of exactly local midnight today is still *today*,
    and that is the same boundary the vault-side metric probe uses (`defer_date < today`).
    Reusing `defer_state`'s `due` here would name that boundary case expired while the
    probe does not count it — two instruments disagreeing again, which is the defect this
    change exists to remove.

    Silent on an unparseable value on purpose: `is_future` has already reported it to
    stderr on the call immediately above, and a second report reads as two defects.
    """
    parsed, _ = defer_date.parse_defer_date(value)
    return parsed is not None and parsed < defer_date.as_now(today)


def task_verdict(task, today):
    """`(refusal, detail)` when the task does not permit a close, else `None`."""
    if task is None:
        return (
            "task-unresolved",
            "no task, goal or topic file carries this session id as claude_session_id; a "
            "close needs a finished anchor, and absent evidence is not evidence of one",
        )
    path, fm, handed_off = task
    if handed_off:
        # The session no longer owns this task: its work is on the record under
        # `metrics_sessions` and a successor is working it. Terminality is the
        # successor's business, not this session's.
        return None
    status = (fm.get("status") or "").strip()
    if status in TERMINAL:
        return None
    defer = fm.get("defer_date")
    if is_future(defer, today):
        return None
    if _defer_expired(defer, today):
        # The park lapsed. Without this branch the detail reads identically to a task that
        # was never deferred at all — both end in "a future defer_date" — so a reader
        # cannot tell "you deferred it and the date passed" from "you never set one".
        return (
            "task-not-terminal",
            f"{os.path.basename(path)} is status {status or '(none)'!r} with an EXPIRED "
            f"defer_date {defer!r} (earlier than today, {today.isoformat()}); the task is "
            f"neither deferred nor running. A close needs a terminal status "
            f"({'/'.join(sorted(TERMINAL))}), a re-defer to a future date, or a hold with a reason",
        )
    return (
        "task-not-terminal",
        f"{os.path.basename(path)} is status {status or '(none)'!r} with "
        f"defer_date {defer or '(unset)'!r}; a close needs a terminal status "
        f"({'/'.join(sorted(TERMINAL))}) or a future defer_date",
    )


def held_task_gate(tasks, today):
    """The first refusal across every task the session holds, or `None` when all permit.

    A session can be bound to more than one task — a finished one and a live one — so the
    close is gated on the whole set rather than on one resolved candidate. The first
    refusal is returned, and because a live task is the only kind that produces one, the
    message names the live task rather than a terminal sibling.
    """
    for task in tasks:
        verdict = task_verdict(task, today)
        if verdict is not None:
            return verdict
    return None


def pane_for_session(jump, session_id):
    """`(pane_id, pmap)` for this session, `(None, pmap)` when it has no identifiable pane.

    The join is `jump.py`'s, not a second copy of it: registry pid → tty (`ps`) and
    wezterm pane → tty is the only mapping the fleet has, and a second one would drift
    silently — both would keep returning a pane id.
    """
    pmap = jump.wezterm_panes()
    if pmap is None:
        return None, None
    by_pane = jump.pane_sessions(pmap)
    if by_pane is None:
        return None, pmap
    matches = [pane for pane, sid in by_pane.items() if sid == session_id]
    return (matches[0] if len(matches) == 1 else None), pmap


def tab_kill(jump, session_id, pane, runner=None):
    """Re-resolve the pane, then kill it. `0` on a close, `1` on a refusal.

    ⚠️ **Re-resolve immediately before the kill.** `pane` comes from a `wezterm cli list`
    read taken inside `pane_for_session`, and `kill-pane` is a separate `wezterm`
    invocation — it re-resolves the id in a new process. A pane id is a lease, not an
    identifier: it is freed when its pane closes and reassigned after a WezTerm mux
    restart, so an id that was correct for that read can belong to a different session by
    the time the kill runs. The window is short — the guards run *before* the resolution
    (see the note above the `pane = None` line), so it is the resolution itself plus a few
    prints, not the guards — but it is not zero. `kill-pane` refuses a *dead* pane; it
    does not refuse a *live* pane that now belongs to someone else. Re-resolving closes
    the window: a different id, or none, means the lease expired and nothing may be
    signalled.

    `runner` is a seam for the test only; production passes `subprocess.run`.
    """
    fresh_pane, fresh_pmap = pane_for_session(jump, session_id)
    if fresh_pane != pane:
        why = (
            "wezterm cli list unavailable" if fresh_pmap is None
            else "no single live pane maps to it" if fresh_pane is None
            else "a different pane holds it"
        )
        print(
            f"❌ error: the pane for {session_id} changed before the kill "
            f"(pane {pane} → {fresh_pane}, {why}); nothing was signalled"
        )
        return 1
    killed = (runner or subprocess.run)(
        ["wezterm", "cli", "kill-pane", "--pane-id", str(pane)],
        capture_output=True, text=True, timeout=10,
    )
    if killed.returncode != 0:
        print(
            f"❌ error: wezterm refused to close pane {pane} "
            f"(exit {killed.returncode}): {(killed.stderr or '').strip()}"
        )
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Close one finished session's pane — registry-guarded.",
    )
    parser.add_argument("session_id")
    parser.add_argument("--dry-run", action="store_true", help="check everything, close nothing")
    parser.add_argument("--sessions-dir", default=None)
    args = parser.parse_args()

    sid = (args.session_id or "").strip()
    if not sid:
        print("❌ usage: close-worker.py <session-id>", file=sys.stderr)
        return 2

    restart_worker = load_sibling("restart-worker.py")
    precheck = load_sibling("restart-precheck.py")
    wnm = load_sibling("who-needs-me.py")
    jump = load_sibling("jump.py")
    refuse = restart_worker.refuse

    entries = restart_worker.registry_entries(args.sessions_dir or SESSIONS_DIR)
    if entries is None:
        return refuse(
            "unknown-session-id",
            f"the registry at {args.sessions_dir or SESSIONS_DIR} could not be read, so "
            f"{sid} cannot be resolved",
        )
    path, rec, note = restart_worker.resolve_owned_target(entries, sid)
    if note:
        # stderr: this verb's first stdout line is a machine-parsed token.
        print(f"note: {note}", file=sys.stderr)
    if path == "ambiguous":
        pids = [os.path.basename(p) for p, _ in rec]
        return refuse(
            "ambiguous-pid",
            f"{len(rec)} registry entries claim session {sid} ({', '.join(pids)}) and none "
            "owns it — no live pid, or several unrelated live pids; nothing is closed",
        )
    if rec is None:
        # ⚠️ **A live session with no registry entry is unaddressable, not unknown** — but
        # this verb DESTROYS, so the classified answer is a refusal that names what it
        # found, never a close verdict for a session it cannot prove it owns. Measured
        # 2026-11-19 (session 616a52dd): the heartbeat read LIVE while this verb refused
        # `unknown-session-id`, so a caller could neither close nor understand why.
        age = wnm.session_transcript_age(sid)
        if age is not None and age != float("inf") and age <= wnm.LIVE_WINDOW:
            print("unregistered-live")
            print(
                "❌ unregistered-live: no registry entry carries session id %s, but its "
                "transcript was written %ds ago — a live session this verb cannot close. "
                "⚠️ **No sanctioned verb reaches it yet:** `restart-worker` refuses "
                "`unknown-session-id` for the same session, so this needs an operator "
                "decision, not a restart." % (sid, int(age))
            )
            return 1
        return refuse("unknown-session-id", f"no registry entry carries session id {sid}")

    # A hold outranks every other consideration: `busy` says the moment is wrong, a hold
    # says the session is not yours to touch at all.
    held = restart_worker.held_reason(sid)
    if held is not None:
        return refuse(
            "held-session",
            f"session {sid} carries an operator hold ({held}); only the operator releases "
            "it -- see the /supervisor:hold skill",
        )

    pid = rec.get("pid")
    if not isinstance(pid, int):
        return refuse("ambiguous-pid", f"registry entry {path} carries no integer pid")

    status = (rec.get("status") or "").strip().lower()
    if status != "idle":
        return refuse(
            "busy-target",
            f"session {sid} is {status or 'in an unknown state'}, not idle; a close loses "
            "the in-flight work with no resume to fall back on",
        )

    # `entrypoint`, not `kind`. `kind` is `interactive` for EVERY registry entry — a tab
    # session and an SDK worker spawned with `interactive: false` both write it — so keying on
    # it made this guard unreachable. `entrypoint` separates them: a tab registers `cli`, an
    # SDK worker `sdk-cli` (measured 2026-10-20 across the live fleet: 24 × `cli`, 1 ×
    # `sdk-cli`; 2026-11-02: 28 × `cli`, 1 × `sdk-cli`).
    #
    # ⚠️ **A pane-less entrypoint is not a refusal.** Every other guard here is about the
    # session's *state*; this one was about where its surface lives, and a `sdk-cli` session
    # has a registry pid to signal even though it has no pane. Refusing it left the operator
    # as the only actor who could close one — eleven hand-typed `kill`s on 2026-11-02. So the
    # old `headless-target` refusal is gone: the same guards run, and the pane kill is
    # replaced by SIGTERM on the registry pid. The task-terminal gate below still carries the
    # weight, because a headless worker that ends its turn is a *parked* process, not a
    # finished one (`docs/fleet-surface.md` § A headless worker exits at turn end).
    #
    # ⚠️ **`sdk-cli` exactly, not "anything but `cli`".** It is the only pane-less value
    # measured in the fleet, and widening the signal to an unmeasured value would aim SIGTERM
    # at a pid on a guess. Any other value — and an absent one — falls through to the pane
    # join, which is fail-safe: it ends in an operational `❌ error:` and never a close.
    entrypoint = (rec.get("entrypoint") or "").strip().lower()
    headless = entrypoint == "sdk-cli"

    # `busy-target` again, from the other source, and **only for the pane-less population**.
    # A tab session's registry `status` moves while it works, so `busy-target` above already
    # covers it and a second check would only add delay. A pane-less one parked on the
    # supervisor's permission channel does not move its `status` and writes no `👤 You:` line,
    # so neither guard above sees it — the tool marker is the only record left.
    if headless:
        in_flight = in_flight_tool(sid)
        if in_flight is not None:
            label, age = in_flight
            return refuse(
                "busy-target",
                f"session {sid} used a tool {int(age // 60)} min ago ({label}); a close loses "
                "in-flight work with no resume to fall back on",
            )

    cwd = (rec.get("cwd") or "").strip()
    verdict, tracked, _untracked = precheck.worktree_probe(cwd)
    others = precheck.other_occupants(entries, cwd, sid)
    if verdict == "dirty" and others == 0:
        return refuse(
            "dirty-worktree",
            f"session {sid} is the only live session in {cwd} and that worktree has "
            f"{len(tracked)} tracked change(s); a close strands them with nothing coming "
            "back to finish the work",
        )

    branch, closer = precheck.classify(rec, wnm)
    if branch == "gate-held":
        return refuse(
            "gate-held",
            f"session {sid} holds an unanswered operator gate ({closer}); closing it "
            "discards a question the operator never answered",
        )

    held_tasks, handed_tasks, task_error = find_tasks(sid, cwd, VAULTS_ROOT)
    task = held_tasks[0] if held_tasks else (handed_tasks[0] if handed_tasks else None)
    pages = []
    # Set when the session resolved to no page at all and the search that proved it was
    # complete — the one state that closes without an anchor (`unanchored_verdict`).
    unanchored = False
    if held_tasks:
        # Every held task must pass. A session can be bound to a finished task and a live
        # one at once, so a first-candidate verdict closes on whichever sorts first — the
        # defect this gate exists to remove.
        bad = held_task_gate(held_tasks, defer_date.as_now())
    else:
        # No held task. A task matched only through `metrics_sessions` (handed off) is
        # weaker evidence than a page the session still holds, so the pages are read in
        # both cases, and every held page must pass.
        held, handed, error = find_pages(sid, cwd, VAULTS_ROOT)
        bad = None
        if error:
            bad = (
                "task-unresolved",
                f"{error}; a failed lookup is not evidence that no live goal or topic "
                "page holds this session",
            )
        else:
            for candidate in held:
                bad = page_verdict(candidate, defer_date.as_now())
                if bad is not None:
                    break
            if bad is None:
                if held:
                    pages = held
                elif task is None and handed:
                    pages = handed[:1]
                elif task_error:
                    # ⚠️ The task search failed AND nothing else resolves the session — no
                    # held task, no held page, no handed page. Only here is the failure the
                    # reason to refuse: a page that does resolve it decides, as it did
                    # before, so a goal-only session is not refused over a task-dir error.
                    bad = (
                        "task-unresolved",
                        f"{task_error}; a failed lookup is not evidence that no live "
                        "task holds this session",
                    )
                else:
                    if task is not None:
                        bad = task_verdict(task, defer_date.as_now())
                    else:
                        # No task, no held page, no handed page, and the search did not
                        # fail. `unanchored_verdict` decides whether that absence is
                        # evidence: a proven one closes, an incomplete search refuses.
                        token, detail = unanchored_verdict(sid, cwd, VAULTS_ROOT)
                        if token == "unanchored-closed":
                            unanchored = True
                        else:
                            bad = (token, detail)
    if bad is not None:
        # ⚠️ Every refusal here names the session id, because the round joins a close line to
        # the refusal that follows it on that value. `task-unresolved` already carries it
        # ("no page carries <id>"); `task-not-terminal` and the page verdicts named only a
        # path, which no close line can be joined to. Measured 2026-11-11 on 0.287.5: a
        # refusal round produced four lines, two of them unjoinable.
        detail = bad[1] if sid in bad[1] else f"session {sid} · {bad[1]}"
        return refuse(bad[0], detail)

    # A headless worker has no pane to join, so the join is the one step this branch skips.
    # Every guard above has already run — the pane is where the close is *delivered*, never
    # what authorises it.
    pane = None
    if not headless:
        pane, pmap = pane_for_session(jump, sid)
        if pane is None:
            # An operational failure, not one of the target refusals: the session resolved and
            # every guard passed, the pane simply could not be identified. Reported under an
            # `error:` prefix so it can never be read as a refusal token.
            print(
                f"❌ error: session {sid} resolved but no single live pane maps to it "
                f"({'wezterm cli list unavailable' if pmap is None else 'no tty join'}) — "
                "this is an operational failure, not a target refusal"
            )
            return 1

    where = f"pane {pane}" if pane is not None else f"pid {pid} (entrypoint {entrypoint}, no pane)"
    # The success line names the pid separately, so its target must not repeat it.
    closed_target = f"pane {pane}" if pane is not None else f"headless session ({entrypoint})"
    # ⚠️ The FULL session id, not an 8-char prefix. The round prints this line as its close
    # record, and the refusal that follows a close (`restart-worker.py`'s `task-terminal`) names
    # the same full id — the two are joined on it, so a truncated one here cannot be joined and
    # the refusal reads as an unrelated event. Measured 2026-11-11 on session 2e540d65.
    # ⚠️ The close token is the caller's contract, so it is printed BEFORE the `🧹` header —
    # the same reason every refusal token is. `reap-idle-headless.py` prints it here too.
    if unanchored:
        print("unanchored-closed")
    print(f"🧹 close-worker · session {sid} · pid {pid} · {rec.get('name')!r}")
    if pages:
        for kind, page_path, fm, page_handed_off in pages:
            how = f"handed off to {fm.get('claude_session_id') or '(none)'}" if page_handed_off else "held"
            print(f"   {kind}: {page_path} (status {fm.get('status')}, {how}, session {sid})")
    elif task is not None:
        task_path, fm, _handed_off = task
        print(f"   task: {os.path.basename(task_path)} (status {fm.get('status')})")
    else:
        print(f"   anchor: none — no task, goal or topic page carries session {sid}")
    print(f"   worktree: {verdict}" + (f" · {others} other live session(s) share it" if verdict == "dirty" else ""))
    print(f"   classification: {branch} · {where}")

    if args.dry_run:
        print(f"   ✅ would close {where}")
        return 0

    if headless:
        # ⚠️ **Re-read the entry immediately before the signal.** `pid` was resolved before
        # the guards, which take tens of seconds — a `git status`, a transcript read and a
        # `grep` over every vault's task dirs. A session that exits in that window can have
        # its pid recycled onto an unrelated process, and SIGTERM would land there. One file
        # read narrows the window to the signal itself. The tab path below resolves its pane
        # *after* the guards, so its window is narrower — but `kill-pane` is a separate
        # wezterm call that re-resolves the id, so it re-resolves too and refuses a change.
        try:
            with open(path, encoding="utf-8") as handle:
                fresh = json.load(handle)
        except (OSError, ValueError):
            fresh = {}
        if fresh.get("pid") != pid or fresh.get("sessionId") != sid:
            print(
                f"❌ error: the registry entry for {sid} changed during the guards "
                f"(pid {pid} → {fresh.get('pid')}); nothing was signalled"
            )
            return 1
        if precheck.pid_is_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                # It exited between the check above and the signal. The wait below still
                # proves it gone and the stale-entry cleanup still applies — the same outcome
                # a delivered SIGTERM produces, so this is not an error.
                pass
            except PermissionError as error:
                print(f"❌ error: cannot signal pid {pid}: {error}")
                return 1
        # else: it exited during the guards. Fall through — the wait proves it gone and the
        # stale-entry cleanup applies, which is the same outcome a delivered signal produces.
    else:
        rc = tab_kill(jump, sid, pane)
        if rc != 0:
            return rc

    # The registry entry is Claude Code's, not ours, and it deletes it on exit. Wait for
    # that rather than racing it; only a provably dead pid whose entry lingers is removed
    # here, because a liveness record for a dead pid is stale by construction.
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
            print(f"   ⚠️ registry entry {path} survived pid {pid} and could not be removed: {error}")

    print(
        f"   {'✅' if not alive and not entry_present else '⚠️'} closed {closed_target} · "
        f"pid {pid} {'dead' if not alive else 'STILL ALIVE'} · registry entry "
        f"{'removed' if not entry_present else 'STILL PRESENT'}"
    )
    return 0 if (not alive and not entry_present) else 1


if __name__ == "__main__":
    sys.exit(main())
