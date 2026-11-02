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

Seven refusals, each exit 1 with a stable reason token as the first line of output:

  unknown-session-id  no registry entry carries that session id, or the registry is
                      unreadable. Closing needs a pid; there is none to close.
  ambiguous-pid       more than one registry pid claims that session id, or the entry
                      carries no integer pid. Closing one is a coin flip.
  held-session        the operator holds this session (`/supervisor:hold`). Only they
                      release it; closing is exactly the act the hold prevents.
  busy-target         the target is mid-turn (`status` is not `idle`). A close loses the
                      in-flight work outright — there is no resume to fall back on.
  headless-target     the target's `entrypoint` is not `cli` (an SDK worker registers
                      `sdk-cli`), so it has no pane to close.
  dirty-worktree      the target is the only live session in its worktree and that
                      worktree carries tracked changes. A close strands them: unlike a
                      restart, nothing comes back to finish the work.
  gate-held           the target's transcript tail carries an unanswered `👤 You:` gate.
                      Closing it discards a question the operator never answered.
  task-unresolved     no task file in the session's vault carries this session id as
                      `claude_session_id`. Absent evidence is not evidence of a finished
                      task; fail closed.
  task-not-terminal   the task is neither terminal (`completed`/`aborted`/`hold`) nor
                      deferred to a future `defer_date`.

Exit codes: 0 closed, 1 refusal or operational error, 2 usage error.

⚠️ **The close is a pane teardown, never a signal to the session's own conversation.**
`/vault-cli:sync-progress` and `/vault-cli:session-close` read the worker's conversation
and remain the worker's own act; this script only removes a pane whose work is already
finished. A session that still has something to say is refused by `task-not-terminal`.
"""
import argparse
import glob
import importlib.util
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


SESSIONS_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSIONS_DIR") or "~/.claude/sessions"
)

# A vault's task directory, newest naming first. `private-starcitizen` and
# `private-family` use `24 Tasks`; the numbered vaults use `25 Tasks`.
TASK_DIRS = ("25 Tasks", "24 Tasks")

# Where the vaults live. `SUPERVISOR_VAULTS_ROOT` first for the same reason the registry
# and the load path carry an override: a drill or a test must be able to point the task
# lookup at a tree it controls, and a committed script never hardcodes a home path.
VAULTS_ROOT = os.path.expanduser(
    os.environ.get("SUPERVISOR_VAULTS_ROOT") or "~/Documents/Obsidian"
)

# The terminal statuses. A deferred row is the other accepted end state: the operator has
# said "not now", which is a decision rather than an absence of one.
TERMINAL = {"completed", "aborted", "hold"}

# How long to wait for the pid and its registry entry to disappear after the pane is
# gone. Claude Code deletes the entry on exit (measured 2026-10-19: 34 live entries, zero
# stale), so this is normally a fraction of a second; the bound exists so a hung exit is
# reported rather than waited on forever.
EXIT_DEADLINE = 8.0


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


def task_dirs_for(cwd, vault_root=None):
    """Candidate task directories: the vault above `cwd`, then every known vault.

    The walk-up answers the common case — a worker whose cwd is its vault. The vault scan
    answers the repo-worktree case: a worker editing `claude-supervisor` has a repo cwd,
    and its task lives in whichever Obsidian vault filed it. Both are bounded and
    read-only; neither is a filesystem-wide search.
    """
    dirs = []
    if cwd:
        cur = os.path.abspath(cwd)
        while True:
            for name in TASK_DIRS:
                candidate = os.path.join(cur, name)
                if os.path.isdir(candidate) and candidate not in dirs:
                    dirs.append(candidate)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    root = vault_root or VAULTS_ROOT
    for vault in sorted(glob.glob(os.path.join(root, "*"))):
        for name in TASK_DIRS:
            candidate = os.path.join(vault, name)
            if os.path.isdir(candidate) and candidate not in dirs:
                dirs.append(candidate)
    return dirs


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
    """
    if not session_id:
        return None
    dirs = task_dirs_for(cwd, vault_root)
    if not dirs:
        return None
    try:
        found = subprocess.run(
            ["grep", "-rl", "--fixed-strings", session_id, *dirs],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    handed_off = None
    for path in sorted((found.stdout or "").splitlines()):
        fm = frontmatter(path)
        if (fm.get("claude_session_id") or "").strip() == session_id:
            return path, fm, False
        if handed_off is None and session_id in metrics_session_ids(path):
            handed_off = (path, fm, True)
    return handed_off


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


def task_verdict(task, today):
    """`(refusal, detail)` when the task does not permit a close, else `None`."""
    if task is None:
        return (
            "task-unresolved",
            "no task file carries this session id as claude_session_id; a close needs a "
            "finished task, and absent evidence is not evidence of one",
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
    return (
        "task-not-terminal",
        f"{os.path.basename(path)} is status {status or '(none)'!r} with "
        f"defer_date {defer or '(unset)'!r}; a close needs a terminal status "
        f"({'/'.join(sorted(TERMINAL))}) or a future defer_date",
    )


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
    path, rec = restart_worker.find_target(entries, sid)
    if path == "ambiguous":
        pids = [os.path.basename(p) for p, _ in rec]
        return refuse(
            "ambiguous-pid",
            f"{len(rec)} registry entries claim session {sid} ({', '.join(pids)}); "
            "the pid must be unique or nothing is closed",
        )
    if rec is None:
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
    # `sdk-cli`). An ABSENT entrypoint is not read as headless: it is an entry written before
    # the field existed, and refusing those would break closes for legitimate tab sessions.
    # Absence falls through to the pane join, which is fail-safe — it ends in an operational
    # `❌ error:` and never a close.
    entrypoint = (rec.get("entrypoint") or "").strip().lower()
    if entrypoint and entrypoint != "cli":
        return refuse(
            "headless-target",
            f"session {sid} runs entrypoint {entrypoint}; it has no pane to close",
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

    task = find_task(sid, cwd, VAULTS_ROOT)
    bad = task_verdict(task, defer_date.as_now())
    if bad is not None:
        return refuse(*bad)

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

    task_path, fm, _handed_off = task
    print(f"🧹 close-worker · session {sid[:8]} · pid {pid} · {rec.get('name')!r}")
    print(f"   task: {os.path.basename(task_path)} (status {fm.get('status')})")
    print(f"   worktree: {verdict}" + (f" · {others} other live session(s) share it" if verdict == "dirty" else ""))
    print(f"   classification: {branch} · pane {pane}")

    if args.dry_run:
        print("   ✅ would close the pane above")
        return 0

    killed = subprocess.run(
        ["wezterm", "cli", "kill-pane", "--pane-id", str(pane)],
        capture_output=True, text=True, timeout=10,
    )
    if killed.returncode != 0:
        print(
            f"❌ error: wezterm refused to close pane {pane} "
            f"(exit {killed.returncode}): {(killed.stderr or '').strip()}"
        )
        return 1

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
        f"   {'✅' if not alive and not entry_present else '⚠️'} closed pane {pane} · "
        f"pid {pid} {'dead' if not alive else 'STILL ALIVE'} · registry entry "
        f"{'removed' if not entry_present else 'STILL PRESENT'}"
    )
    return 0 if (not alive and not entry_present) else 1


if __name__ == "__main__":
    sys.exit(main())
