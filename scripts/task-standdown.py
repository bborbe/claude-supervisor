#!/usr/bin/env python3
"""`task-standdown.py` — the task tier's deliberate-stop marker, and its only clear verb.

Why this exists
---------------
`/supervisor:stop` has always meant "stand this subject down" for a **manager**: it writes
`<branch>--<base>.stopped` into the liveness state dir, the keepalive reads `STOPPED` as
not-a-lapse, and only `--arm --resume` clears it. A **task** had no such tier. A worker
that ran `/supervisor:stop` either wrote nothing (`commands/stop.md` § `<constraints>`:
*"Write no page, and no state file except the stop marker"*) or — worse — fell through
step 2b's `(none recorded)` fallback onto its task's **goal** and stood down a goal
manager's loop it did not own.

The operator's ruling is the distinction the fleet must draw everywhere:

    "restart crashed session is fine … but session i want to stop"   and   "worker should work too"

So: **crashed → restart, stopped → never reopen**, uniformly.

Why frontmatter, and not the state file
---------------------------------------
The manager marker is a machine-local file keyed by a slug. It cannot carry a task, for
three reasons and the first is decisive:

  * **A task stop must not touch `sweep-gate/*.stopped` at all.** A worker stopping itself
    must leave every manager's stand-down state byte-identical. A task-tier file in that
    directory would be indistinguishable from a subject marker to every reader that globs
    it.
  * **Task titles collide across vaults.** `~/.claude/state` is per-user, not per-vault, so
    `<base_slug>` would let two same-titled tasks in different vaults share one marker —
    the exact reason `/supervisor:stop`'s own docs make `--vault` mandatory.
  * **The row is the record.** Both readers that must honour a stop (`open-queue.py`'s
    `guards()`, `worker-keepalive.py`'s park check) already hold the task's frontmatter and
    read nothing else. A page field is one read they already do.

`stopped: true` is a NEW key, and `status` is deliberately not reused: `hold`/`blocked`
already mean "blocked, wait of unknown length", and reclassifying a deliberate stop as a
blocker would make the two indistinguishable in every surface that reads `status`.

Written through `vault-cli task set`, never by editing the file — the same rule and the
same reason as `park-event.py`'s `park_event` key: the vault owns its own frontmatter, and
an unattended writer that bypasses the CLI is how a page ends up with a key no reader
expects. `park_event` also proves the CLI accepts an arbitrary key.

Usage
-----
    task-standdown.py --session <session-id> [--cwd <dir>]      stamp the anchored task
    task-standdown.py --clear --subject "<task>" [--vault <n>]  clear it (operator verb)

Output contract — one line on stdout, `<verdict>` TAB `<path-or-empty>`, where `<verdict>`:

  stopped      the anchored task was stamped `stopped: true` + `stop_date`
  cleared      both keys were removed
  no-task      no task page carries this session id as `claude_session_id`
  handed-off   the id appears only in `metrics_sessions` — the session gave the task away,
               so it is not this session's to stop. ⚠️ NOT treated as `no-task`: a
               handed-off session that stamped the row would stop a task another session
               now owns.
  cannot-tell  the read failed (unreadable vault config, unresolvable name). ⚠️ Never
               collapsed into `no-task`: "no task claims me" and "I could not look" license
               opposite acts, and only one of them is a fact.
  ambiguous    the name matched more than one task; the candidates are on stderr

Exit codes: 0 for `stopped`/`cleared`, 1 for every refusal, 2 for usage.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

STOPPED = "stopped"
STOP_DATE = "stop_date"


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def vaults():
    """`[(name, realpath)]` for every configured vault, or `None` when unreadable.

    `None` is the third state. An empty list would read as "no vaults configured", which
    would turn every session into `no-task` — a false answer about a task that exists.
    """
    try:
        proc = subprocess.run(
            ["vault-cli", "--output", "json", "config", "list"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(payload, list):
        return None
    out = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        path = os.path.expanduser(entry.get("path") or "")
        if name and path:
            out.append((name, os.path.realpath(path)))
    return out or None


def vault_name_for(path, known):
    """The configured vault containing `path`, by longest matching root, or `None`."""
    real = os.path.realpath(path)
    best = None
    for name, root in known:
        if real == root or real.startswith(root + os.sep):
            if best is None or len(root) > len(best[1]):
                best = (name, root)
    return best[0] if best else None


def task_name(path):
    """The task's name — its filename stem.

    ⚠️ **`vault-cli task set` does not accept a path.** It joins its argument under the
    vault's `tasks_dir`, so an absolute path resolves to `<tasks_dir>/<absolute path>` and
    fails with *"find task file: … file not found"* (measured 2026-11-10). The stem plus
    `--vault` is unambiguous, and the vault name is always resolved here — a bare stem is
    the one form that collides across vaults.
    """
    return os.path.basename(path)[:-3] if path.endswith(".md") else os.path.basename(path)


def set_field(task, key, value, vault):
    """One `vault-cli task set`. Returns `None` on success, a reason string on failure."""
    cmd = ["vault-cli", "task", "set", task, key, value]
    if vault:
        cmd += ["--vault", vault]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return detail[-1] if detail else "exit %d" % done.returncode
    return None


def remove_field(task, key, vault):
    """Clear one field. `vault-cli task clear` is the repo's verb for this."""
    cmd = ["vault-cli", "task", "clear", task, key]
    if vault:
        cmd += ["--vault", vault]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return detail[-1] if detail else "exit %d" % done.returncode
    return None


def now_iso():
    """RFC3339 with the local offset — the shape `vault-cli` writes for a datetime."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def anchored_task(session_id, cwd, vault_root):
    """`(path, handed_off, reason)` for the task this session is anchored to.

    Delegates to `close-worker.py`'s `find_task` rather than re-deriving the join: that
    function is the repo's single `claude_session_id` reader, and a second copy is how the
    stop and the close would come to disagree about which task a session owns.
    """
    if not session_id:
        return "", False, "no session id given"
    close_worker = _load("close_worker", "close-worker.py")
    found = close_worker.find_task(session_id, cwd, vault_root)
    if found is None:
        return "", False, "no task claims this session"
    path, _fm, handed_off = found
    return path, handed_off, ""


def stamp(args):
    """Write the marker, resolving the task by session id or by name.

    ⚠️ **Both entry points reach this function.** The bare form passes `--session` (the
    caller's own anchor); the by-name form passes `--subject`, because
    `/supervisor:stop "<task>"` writes the `task--<base>.stopped` state marker and
    **neither `open-queue.py` nor `worker-keepalive.py` reads that file** — without this
    second write a task stopped by name was still reopened and resumed, which is the
    half-merge the by-name PR's own SC3 deferred to this change.
    """
    if args.subject.strip():
        path, ambiguous, reason = by_name(args.subject)
        if ambiguous:
            print("ambiguous\t")
            return 1
        if not path:
            print("no-task\t")
            print(reason or "no task named %r" % args.subject, file=sys.stderr)
            return 1
        handed_off = False
    else:
        path, handed_off, reason = anchored_task(
            args.session, args.cwd or os.getcwd(), args.vault_path
        )
        if not path:
            print("no-task\t")
            print(reason or "no task claims this session", file=sys.stderr)
            return 1
        if handed_off:
            print("handed-off\t%s" % path)
            print(
                "this session handed the task off; its current owner holds it as "
                "claude_session_id — refusing to stop a task this session no longer owns",
                file=sys.stderr,
            )
            return 1

    known = vaults()
    if known is None:
        print("cannot-tell\t%s" % path)
        print("vault-cli config list is unreadable", file=sys.stderr)
        return 1
    vault = args.vault or vault_name_for(path, known)
    name = task_name(path)

    reason = set_field(name, STOPPED, "true", vault)
    if reason:
        print("cannot-tell\t%s" % path)
        print("could not write %s: %s" % (STOPPED, reason), file=sys.stderr)
        return 1
    # The date is informational, so a failure here is reported and does not undo the stop:
    # `stopped` is the operative key and every reader gates on it alone.
    date_reason = set_field(name, STOP_DATE, now_iso(), vault)
    if date_reason:
        print("stopped\t%s" % path)
        print(
            "⚠️ %s was not written (%s); %s stands and is what the readers honour"
            % (STOP_DATE, date_reason, STOPPED),
            file=sys.stderr,
        )
        return 0
    print("stopped\t%s" % path)
    return 0


def by_name(subject):
    """`(path, ambiguous, reason)` for a task resolved by NAME.

    Delegates to `resolve-task-file.py`, the repo's own "never guesses" resolver: zero
    hits or more than one inside the tier reached resolves nothing, and the colliding
    paths go to stderr. Shared by the by-name stamp and the `--clear` verb, so the two
    cannot disagree about which page a name means.
    """
    resolve = _load("resolve_task_file", "resolve-task-file.py")
    payload, degraded = resolve.vault_dirs()
    if degraded:
        return "", [], degraded
    path, ambiguous = resolve.resolve(subject, payload)
    if ambiguous:
        for candidate in ambiguous:
            print("AMBIGUOUS %s\t%s" % (subject, candidate), file=sys.stderr)
        return "", ambiguous, "ambiguous name"
    if not path:
        return "", [], "no task named %r" % subject
    return path, [], ""


def clear(args):
    """Clear the marker by NAME — the operator's verb, resolved by the repo's own rule."""
    known = vaults()
    if known is None:
        print("cannot-tell\t")
        print("vault-cli config list is unreadable", file=sys.stderr)
        return 1
    path, ambiguous, reason = by_name(args.subject)
    if ambiguous:
        print("ambiguous\t")
        return 1
    if not path:
        print("cannot-tell\t" if "unreadable" in reason or "DEGRADED" in reason else "no-task\t")
        print(reason, file=sys.stderr)
        return 1
    vault = args.vault or vault_name_for(path, known)
    name = task_name(path)

    for key in (STOPPED, STOP_DATE):
        reason = remove_field(name, key, vault)
        # A key that is already absent is the state we want, so a refusal here is only a
        # failure when the field is still present afterwards — checked by re-reading.
        if reason:
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                # ⚠️ Guarded: an unreadable page after a failed clear is `cannot-tell`,
                # never a traceback out of a verb whose whole contract is three verdicts.
                print("cannot-tell\t%s" % path)
                print("could not clear %s: %s; page unreadable (%s)" % (key, reason, exc),
                      file=sys.stderr)
                return 1
            if ("\n%s:" % key) in text:
                print("cannot-tell\t%s" % path)
                print("could not clear %s: %s" % (key, reason), file=sys.stderr)
                return 1
    print("cleared\t%s" % path)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stamp or clear the task-tier deliberate-stop marker"
    )
    parser.add_argument("--session", default="", help="the caller's session id (stamp)")
    parser.add_argument("--clear", action="store_true", help="clear the marker (operator verb)")
    parser.add_argument("--subject", default="",
                        help="task name — stamps it, or names the row to clear with --clear")
    parser.add_argument("--vault", default="", help="vault name (overrides path detection)")
    parser.add_argument("--vault-path", default=None,
                        help="vault root for the task lookup (default: derive from cwd)")
    parser.add_argument("--cwd", default=None, help="cwd for the task lookup")
    args = parser.parse_args(argv)

    if args.clear:
        if not args.subject.strip():
            parser.error("--clear needs --subject")
        return clear(args)
    if not args.session.strip() and not args.subject.strip():
        parser.error("one of --session, --subject or --clear is required")
    return stamp(args)


if __name__ == "__main__":
    sys.exit(main())
