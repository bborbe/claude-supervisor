#!/usr/bin/env python3
"""`restart-worker.py` — restart ONE stale idle worker session, registry-guarded.

A manager's standing mandate (`65 Runbooks/Manager Session.md` § Gate triage class A)
covers killing and restarting a live WORKER whose loaded code is stale. This script is
the narrow, allowlistable way to exercise it: one session id in, one pid killed, the
same session resumed — never a broad `kill` rule.

Seven refusals, each exit 1 with a stable reason token as the first line of output:

  unknown-session-id  no registry entry carries that session id. The message says
                      whether the id was absent or the registry itself was unreadable
                      — the two are the same class (the id cannot be resolved) but
                      never the same sentence.
  ambiguous-pid       more than one registry pid claims that session id, or the entry
                      carries no integer pid. The pid must be unique or nothing is
                      killed.
  busy-target         the target is mid-turn (`status` is not `idle`). A mid-turn kill
                      loses the in-flight work.
  headless-target     the target is not `interactive`. A headless worker has no reliable
                      resume guard, so its liveness stays undetermined.
  manager-target      the target resolves as a manager. A manager is NEVER restarted.
  role-undetermined   no role signal could be read. Fail closed; never assume worker.
  stale-load-path     nothing would load differently: no load-path copy is newer than
                      the session's own start, so restarting it buys nothing.

Exit codes: 0 success, 1 refusal, 2 usage error.

The role predicate is NOT reimplemented here. It calls `fleet-board.py`'s own
`vault_index()`, `loop_slugs()`, `manager_subject()` and `colour_census()` through the
same importlib sibling-import the repo already uses, so this script and the fleet board
can never disagree about who is a manager. All four of `build_grouping()` Rule 1's
signals are honoured — the root name, `manager_subject()`'s two sources, and the orange
colour — because a predicate covering only the two page-based signals would classify the
root manager as a worker and restart it.

The resume recipe is a copy of `commands/open.md` Step 3.1's. A markdown command is not
executable, so the two are kept in lockstep by hand; that is the second place this
mechanism lives, and the reason `commands/worker-restart.md` is scoped to call THIS
script rather than reimplement kill+resume.
"""
import argparse
import datetime
import glob
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The registry. `SUPERVISOR_SESSIONS_DIR` first so a drill can point at a fixture tree;
# never a hardcoded home path in committed content.
SESSIONS_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSIONS_DIR") or "~/.claude/sessions"
)

# The plugin's load path — the copy that is actually executed. Deliberately NOT the
# marketplace clone, which updates independently of what a running session loaded.
# `SUPERVISOR_LOAD_PATH` first, for the same reason as the registry above: a drill or a
# test must be able to point the precondition-4 check at a tree it controls.
LOAD_PATH_ROOT = os.path.expanduser(
    os.environ.get("SUPERVISOR_LOAD_PATH")
    or "~/.claude/plugins/cache/claude-supervisor/supervisor"
)

PLUGIN = "supervisor@claude-supervisor"


def load_sibling(filename):
    """Import a sibling script by path — the repo's own convention for sharing code."""
    name = os.path.basename(filename).replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def registry_entries(directory=None):
    """Every registry record as `(path, rec)`. `None` when the directory is unreadable.

    `None` is a distinct answer from `[]`: an unreadable registry cannot prove the id
    is absent, and reporting an empty list would turn a failed probe into a confident
    "no such session".
    """
    d = directory or SESSIONS_DIR
    if not os.path.isdir(d):
        return None
    try:
        out = []
        for path in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                out.append((path, read_json(path)))
            except Exception:
                continue  # a half-written entry is skipped, not fatal
        return out
    except Exception:
        return None


def refuse(reason, detail):
    """Print the reason token, then the sentence. Returns exit code 1."""
    print(reason)
    print(f"❌ {reason}: {detail}")
    return 1


def find_target(entries, sid):
    """`(path, rec)`, or `('ambiguous', matches)`, or `(None, None)` when absent."""
    matches = [(p, r) for p, r in entries if r.get("sessionId") == sid]
    if not matches:
        return None, None
    if len(matches) > 1:
        return "ambiguous", matches
    return matches[0][0], matches[0][1]


def role_of(sid, name, fb, index, slugs, colours):
    """`'manager'`, `'worker'`, or `None` when no signal could be read.

    Mirrors `build_grouping()` Rule 1 exactly. `None` is the fail-closed answer and is
    deliberately distinct from `'worker'`: an unreadable index proves nothing.
    """
    if not name:
        return None
    # Signal 1 — the root. There is no `Fleet Manager` page, so no other signal catches
    # it, and missing it would restart the root manager.
    if name == fb.ROOT_NAME:
        return "manager"
    # Signals 2 + 3 — manager_subject()'s two sources. An unreadable index is not the
    # same answer as "not a manager".
    if index is None:
        return None
    if fb.manager_subject(name, index, slugs):
        return "manager"
    # Signal 4 — colour. An empty census is "not recorded", never "no managers"; it
    # costs the colour half of the rule and leaves the page half standing.
    if (colours.get(sid) or "").strip().lower() == fb.MANAGER_COLOUR:
        return "manager"
    return "worker"


def newest_load_path():
    """`(path, mtime)` for the newest load-path version dir, or `(None, None)`."""
    best, best_mtime = None, None
    for path in glob.glob(os.path.join(LOAD_PATH_ROOT, "*")):
        if not os.path.isdir(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if best_mtime is None or mtime > best_mtime:
            best, best_mtime = path, mtime
    return best, best_mtime


def parse_started(rec):
    """The session's start time as an epoch float, or `None` when unparseable."""
    raw = rec.get("startedAt") or rec.get("procStart")
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return float(raw)
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def resume_command(sid, title, chip):
    """The resume recipe, copied from `commands/open.md` Step 3.1.

    A markdown command is not executable, so this is a deliberate copy — see the module
    docstring. The `unset` list matters: a resumed session that inherits the parent's
    messaging socket believes it is the parent.
    """
    script = os.environ.get("CLAUDE_SCRIPT") or "claude"
    inner = (
        "unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN "
        "CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; "
        f'exec "{script}" --resume {sid} -n "{title}" "/color \'{chip}\'"'
    )
    return ["wezterm", "cli", "spawn", "--", "bash", "-lc", inner]


def main():
    ap = argparse.ArgumentParser(
        description="Restart one stale idle worker session, registry-guarded.",
    )
    ap.add_argument("session_id", help="the session id to restart")
    ap.add_argument("--dry-run", action="store_true", help="check everything, kill nothing")
    ap.add_argument("--sessions-dir", help=f"registry dir (default: {SESSIONS_DIR})")
    args = ap.parse_args()

    sid = args.session_id.strip()
    if not sid:
        ap.error("session id must not be empty")  # exits 2

    fb = load_sibling("fleet-board.py")

    entries = registry_entries(args.sessions_dir)
    if entries is None:
        return refuse(
            "unknown-session-id",
            f"the registry at {args.sessions_dir or SESSIONS_DIR} could not be read, "
            f"so {sid} cannot be resolved",
        )

    path, rec = find_target(entries, sid)
    if path == "ambiguous":
        pids = [os.path.basename(p) for p, _ in rec]
        return refuse(
            "ambiguous-pid",
            f"{len(rec)} registry entries claim session {sid} ({', '.join(pids)}); "
            "the pid must be unique or nothing is killed",
        )
    if path is None:
        return refuse("unknown-session-id", f"no registry entry carries session id {sid}")

    pid = rec.get("pid")
    if not isinstance(pid, int):
        return refuse("ambiguous-pid", f"registry entry {path} carries no integer pid")

    status = (rec.get("status") or "").strip().lower()
    if status != "idle":
        return refuse(
            "busy-target",
            f"session {sid} is {status or 'in an unknown state'}, not idle; "
            "a mid-turn kill loses the in-flight work",
        )

    kind = (rec.get("kind") or "").strip().lower()
    if kind != "interactive":
        return refuse(
            "headless-target",
            f"session {sid} is {kind or 'of unknown kind'}; a headless worker has no "
            "reliable resume guard, so liveness stays undetermined",
        )

    # Role — fail closed. An unreadable index or census is `role-undetermined`, never
    # `worker`.
    try:
        index = fb.vault_index()
    except Exception:
        index = None
    slugs = fb.loop_slugs()
    colours = fb.colour_census()
    name = (rec.get("name") or "").strip()

    role = role_of(sid, name, fb, index, slugs, colours)
    if role is None:
        return refuse(
            "role-undetermined",
            f"no role signal could be read for session {sid} (name {name!r}); "
            "refusing rather than assuming worker",
        )
    if role == "manager":
        return refuse(
            "manager-target",
            f"session {sid} ({name!r}) resolves as a manager; a manager is never restarted",
        )

    # Precondition 4 — something must actually load differently. Compare the newest
    # load-path copy against the session's own start; a load path no newer than the
    # session means the restart would reload identical code.
    load_path, load_mtime = newest_load_path()
    started = parse_started(rec)
    if load_path is None:
        return refuse("stale-load-path", f"no load-path copy found under {LOAD_PATH_ROOT}")
    if started is None:
        return refuse(
            "stale-load-path",
            f"session {sid} carries no parseable start time, so it cannot be shown that "
            "anything would load differently",
        )
    if load_mtime <= started:
        return refuse(
            "stale-load-path",
            f"load path {os.path.basename(load_path)} is not newer than session {sid}'s "
            "start; restarting would reload identical code",
        )

    print(
        f"🔄 restart-worker · session {sid[:8]} · pid {pid} · {name!r}"
        f"{' · dry-run' if args.dry_run else ''}"
    )
    print(f"   role: worker · status: {status} · load path: {os.path.basename(load_path)}")

    if args.dry_run:
        print("   ✅ would kill the pid above and resume the same session id")
        return 0

    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        print(f"   ℹ️ pid {pid} was already gone")
    except PermissionError:
        return refuse("unknown-session-id", f"not permitted to signal pid {pid}")

    title = name or sid[:8]
    chip = (colours.get(sid) or "pink").strip().lower() or "pink"
    try:
        spawned = subprocess.run(
            resume_command(sid, title, chip),
            capture_output=True, text=True, timeout=30,
        )
    except Exception as error:
        return refuse("unknown-session-id", f"resume failed: {error}")
    if spawned.returncode != 0:
        return refuse(
            "unknown-session-id",
            f"resume failed (exit {spawned.returncode}): {(spawned.stderr or '').strip()}",
        )

    pane = (spawned.stdout or "").strip().splitlines()
    print(
        f"   ✅ killed pid {pid}; resumed session {sid} in a new tab "
        f"(pane {pane[-1] if pane else '?'})"
    )
    print(
        f"   ↪ tell the resumed session it was restarted, and what changed: "
        f"{PLUGIN} @ {os.path.basename(load_path)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
