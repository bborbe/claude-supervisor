#!/usr/bin/env python3
"""Per-session ledger of operator asks (manager loops: fleet-loop / manager-loop).

Storage: ~/.claude/state/open-items/<session-id>.json — never hand-written, same
write discipline as fleet-snapshot.py (atomic tmp+rename, timestamps stamped here).

Kinds:
  asked-of-me   an operator instruction; resolves when its task reads status: completed
  asked-of-you  a question put to the operator; resolves on their explicit answer — so `answer`
                closes it outright. On the other two kinds `answer` records a note and leaves
                the entry `open`: they resolve on their task, and only `asked-of-you` is a kind
                the operator can resolve by replying.
  pushed        a task filed/spawned on the operator's behalf; resolves on status: completed

Subcommands: add | answer | note | close | list

`--task` is resolved to a vault file on both write and read. `add` stores the path it
found (and warns when it finds none); `list` re-resolves and marks any OPEN entry whose
task target backs no file as `⚠️ UNRESOLVABLE`. An entry claiming `resolves on: task file
status: completed` while naming a task that does not exist is a close condition that can
never fire — indistinguishable, until this check existed, from an entry that is simply
still open. Task dirs come from vault-cli's config, so this ships vault-agnostic;
`--tasks-dir` overrides for a caller that already knows its vault.

`answer` is for something the OPERATOR said; `note` is for anything else you want to attach
(evidence, a measurement, progress). Reach for `note` by default.

Misusing `answer` on an `asked-of-you` is worse than an early close: it writes
`closed_evidence: "operator answered in session: <text>"`, forging an attribution to the
operator that a later reader cannot tell from a genuine answer. The entry then looks resolved
by the one party who never saw it.
Session id defaults to $CLAUDE_CODE_SESSION_ID (falling back to the legacy
$CLAUDE_SESSION_ID); --session overrides.
"""
import argparse
import datetime
import glob
import json
import os
import subprocess
import sys
import uuid

KINDS = ("asked-of-me", "asked-of-you", "pushed")
STATES = ("open", "closed")
ROOT = os.path.expanduser("~/.claude/state/open-items")

# Rendered on an OPEN entry's summary line. `unknown` gets its own wording rather
# than sharing UNRESOLVABLE's: a search that could not run and a search that found
# nothing are different facts, and only one of them is a problem with the entry.
MARKERS = {
    "unresolvable": " · ⚠️ UNRESOLVABLE",
    "unknown": " · ⚠️ UNCHECKED (no task dirs)",
}
DETAIL_SUFFIX = {"ok": "", "unknown": " (not searched)"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def age(created_at):
    started = parse_ts(created_at)
    if started is None:
        return "?"
    delta = datetime.datetime.now(datetime.timezone.utc) - started
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def session_id(args):
    sid = (
        args.session
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not sid:
        sys.exit(
            "error: no session id. Pass --session <your-session-id>.\n"
            "note: Claude Code exports the session id as CLAUDE_CODE_SESSION_ID, so this\n"
            "      default fires for any Bash a session runs. CLAUDE_SESSION_ID is kept as a\n"
            "      fallback for callers written against the old name. Both are empty in a\n"
            "      spawned child, which is deliberately stripped of them — there, pass --session\n"
            "      explicitly from the session's own transcript path\n"
            "      ~/.claude/projects/<project>/<session-id>.jsonl. Never guess it from the\n"
            "      newest file in ~/.claude/state/context/: a wrong id silently splits the\n"
            "      ledger in two and both halves look healthy."
        )
    return sid


def path_for(sid):
    return os.path.join(ROOT, "%s.json" % sid)


_TASK_DIRS = None


def filename_candidates(title):
    """Filename stems a vault task title may live under.

    A title carrying a path (``~/.claude/commands/open.md``) cannot be a filename
    verbatim: `/` is the one byte a POSIX filename cannot hold, so the vault writes
    `.` in its place and the two strings stop matching. A resolver that re-derives
    the file from the human title therefore has to apply the same substitution, or it
    reports "no such file" for a task sitting right there — a report indistinguishable
    from the task never having existed.

    This guards a shape, not a repair of an observed one: no live ledger entry carries
    such a title in its `task` field (the one that does, `efabd66e`, already stores the
    sanitised form and resolves).
    """
    yield title
    if "/" not in title:
        return
    # The substitution is not a plain one-for-one. The observed pair is
    # `~/.claude/commands/open.md` -> `~.claude.commands.open.md`: the `/.` collapsed
    # to a single `.` rather than doubling to `..`. The exact rule is inferred from
    # that one measured filename, so both readings are emitted — a resolver only has
    # to FIND the file, and a candidate that misses costs one stat().
    collapsed = title.replace("/.", ".").replace("/", ".")
    yield collapsed
    plain = title.replace("/", ".")
    if plain != collapsed:
        yield plain


def configured_task_dirs():
    """Every configured vault's task directory, from vault-cli's own config.

    Read from the operator's config rather than hardcoded: this script ships in a
    plugin that must not depend on any particular vault (docs/fleet-surface.md).
    vault-cli's config is the single source of truth for where each vault keeps its
    tasks, so a vault folder rename needs no edit here — the same reasoning
    fleet-sessions.py's vault_dirs_from_cli() follows.

    Returns [] on any failure. The caller must then report a task-carrying entry as
    UNRESOLVABLE rather than as fine: "could not check" and "checked, resolves"
    rendering the same way is the exact defect this file exists to fix.
    """
    global _TASK_DIRS
    if _TASK_DIRS is not None:
        return _TASK_DIRS
    dirs = []
    try:
        proc = subprocess.run(
            ["vault-cli", "--output", "json", "config", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            for vault in json.loads(proc.stdout):
                path = os.path.expanduser(vault.get("path") or "")
                sub = vault.get("tasks_dir")
                if path and sub:
                    dirs.append(os.path.join(path, sub))
    except (OSError, ValueError, subprocess.SubprocessError):
        dirs = []
    _TASK_DIRS = dirs
    return dirs


def resolve_task(title, dirs):
    """Absolute path of the vault file backing `title`, or None."""
    if not title:
        return None
    for name in filename_candidates(title):
        for directory in dirs:
            path = os.path.join(directory, name + ".md")
            if os.path.exists(path):
                return path
    return None


def target_state(item, dirs):
    """(state, path) for an entry's task target: none | ok | unresolvable | unknown.

    `none` means the entry names no task at all, so it makes no file claim to
    falsify — an `asked-of-you` resolves on the operator's answer and legitimately
    has none. `unresolvable` means the entry DOES claim a task and no file backs it,
    so its `resolves on: task file status: completed` can never fire.

    No live entry is in that state today. The audit that filed one as unreachable had
    searched a single vault for a task living in a sibling — which is why `dirs`
    defaults to every configured vault.

    `unknown` is the case that must NOT collapse into `unresolvable`: no task
    directory was searchable at all (vault-cli absent, its config unreadable, and no
    `--tasks-dir` given). Calling that `unresolvable` would assert "no such task" from
    a search that never ran — the same positive-claim-from-a-failed-lookup shape this
    check exists to remove, merely inverted, and it would flag every entry on a host
    without vault-cli. Unknown is reported as unknown.
    """
    title = item.get("task")
    if not title:
        return "none", None
    stored = item.get("task_path")
    if stored and os.path.exists(stored):
        return "ok", stored
    if not dirs:
        return "unknown", None
    path = resolve_task(title, dirs)
    if path:
        return "ok", path
    return "unresolvable", None


def load(sid):
    path = path_for(sid)
    if not os.path.exists(path):
        return {"session_id": sid, "updated_at": None, "items": []}
    with open(path) as f:
        data = json.load(f)
    # The FILENAME is the authority for whose ledger this is, never the stored field: a
    # copied or hand-edited file carrying a foreign session_id would otherwise make every
    # write land in that other session's ledger.
    data["session_id"] = sid
    data.setdefault("items", [])
    healed = [migrate(item) for item in data["items"]]
    if any(healed) and os.path.exists(path):
        # Persist the normalisation instead of re-deriving it on every read: anything that
        # reads the JSON directly rather than through this script would otherwise still see
        # the broken shape, and a heal that only ever reaches the render is not a heal of
        # the store.
        save(data)
    return data


def migrate(item):
    """Heal entries written before `answered` was removed as a state.

    The writer fix was forward-only, so a ledger annotated under the old code kept a stored
    `state: answered` that no longer means anything — and on a pushed/asked-of-me entry it
    still read as though the operator had replied. Normalising on read is what actually
    clears those, in every session, without anyone re-annotating.

    Returns True when the entry changed, so `load` can persist the normalisation rather
    than re-deriving it on every read.
    """
    before = dict(item)
    item.setdefault("note", None)
    item.setdefault("noted_at", None)
    if item["kind"] != "asked-of-you":
        # Only an asked-of-you may carry `answer` / `answered_at` — on any other kind they
        # assert an operator reply that never happened. Two broken shapes exist and BOTH are
        # healed here, keyed on the fields rather than on the state: the original
        # `state: answered`, and the narrower one the first fix produced — `state: open` with
        # `answered_at` still stamped, because that fix set the state but left the stamp
        # unconditional. Keying on `state` alone silently skips the second.
        if item.get("answer") is not None or item.get("answered_at") is not None:
            item["note"] = item.get("note") or item.get("answer")
            item["noted_at"] = item.get("noted_at") or item.get("answered_at")
            item["answer"] = None
            item["answered_at"] = None
        if item.get("state") == "answered":
            item["state"] = "open"
        return item != before
    if item.get("state") == "answered":
        item["state"] = "closed"
        item["closed_at"] = item.get("closed_at") or item.get("answered_at")
        item["closed_evidence"] = item.get("closed_evidence") or (
            "operator answered in session: %s" % item.get("answer")
        )
    return item != before


def forked_from(sid, data):
    """Detect a ledger that was copied to a new session id instead of started fresh.

    `/branch` copies the parent's state files to the child's session id, so the child
    inherits the parent's entries and the two then diverge silently: an entry closed in
    one stays open in the other, and neither can tell which is stale. That is the very
    failure this ledger exists to prevent, one layer down. Measured 2026-09-18: two
    ledgers sharing 6 entry ids, the parent already 4 entries ahead.

    Two signals, because neither alone is enough. `origin_session_id` is authoritative but
    only exists on ledgers written after this change; the shared-id scan is what catches
    the ones already forked. Both only ever WARN — a fork is the operator's to resolve,
    and a script that silently picked a winner would be the same silent-divergence bug
    wearing a different hat.
    """
    warnings = []
    origin = data.get("origin_session_id")
    if origin and origin != sid:
        warnings.append(
            "this ledger was created by session %s, not %s — it looks copied (/branch?)"
            % (origin[:8], sid[:8])
        )
    # OPEN entries only, on both sides. A closed entry cannot diverge, so counting closed ones
    # makes a reconciled fork warn forever — and the warning would keep claiming the two "will
    # diverge" after one side has already been resolved. Measured 2026-09-19: a fork reconciled
    # with `close --id … --evidence "reconciled: owned by session <A>"` left A open 2 · B open 0
    # and still printed the warning on every read.
    mine = {i["id"] for i in data.get("items", []) if i.get("state") == "open"}
    if mine:
        for other in glob.glob(os.path.join(ROOT, "*.json")):
            other_sid = os.path.basename(other)[:-5]
            if other_sid == sid:
                continue
            try:
                with open(other) as f:
                    shared = mine & {
                        i["id"]
                        for i in json.load(f).get("items", [])
                        if i.get("state") == "open"
                    }
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if shared:
                ids = sorted(shared)
                shown = ", ".join(ids[:6]) + (" …" if len(ids) > 6 else "")
                warnings.append(
                    "%d entr%s also live in session %s's ledger — the two have forked and "
                    "will diverge.\n"
                    "    shared ids: %s\n"
                    "    to reconcile, decide which side owns each entry and close the copy on "
                    "the other:\n"
                    "      close --id <id> --evidence \"reconciled: owned by session <owner-id>\"\n"
                    "    this script never picks a winner — a silent pick is the same "
                    "divergence bug wearing a different hat."
                    % (
                        len(shared),
                        "y" if len(shared) == 1 else "ies",
                        other_sid[:8],
                        shown,
                    )
                )
    return warnings


def save(data):
    data["updated_at"] = now()
    data.setdefault("origin_session_id", data["session_id"])
    path = path_for(data["session_id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def find(data, item_id):
    for item in data["items"]:
        if item["id"] == item_id or item["id"].startswith(item_id):
            return item
    sys.exit("error: no entry %r in session %s" % (item_id, data["session_id"]))


def task_dirs_for(args):
    """Task dirs to resolve against: --tasks-dir when given, else every vault.

    The flag exists for callers that already know their vault (and for tests, which
    must not depend on the machine's vault-cli config); the default reads every
    configured vault so the manager call sites need no change.
    """
    explicit = [os.path.expanduser(d) for d in (args.tasks_dir or [])]
    dirs = explicit or configured_task_dirs()
    # A directory that does not exist is not searchable. Leaving a dead path in the
    # list would make it non-empty, so every entry would read UNRESOLVABLE — the same
    # "no such task" asserted from a search that could not have found one. Filtering
    # here is what lets target_state's empty-list check catch a typo'd --tasks-dir and
    # a moved vault, not just a missing vault-cli.
    return [d for d in dirs if os.path.isdir(d)]


def cmd_add(args):
    sid = session_id(args)
    data = load(sid)
    dirs = task_dirs_for(args)
    item = {
        "id": uuid.uuid4().hex[:8],
        "kind": args.kind,
        "text": args.text,
        "created_at": now(),
        "resolves_on": args.resolves_on
        or ("the operator's explicit answer" if args.kind == "asked-of-you" else None),
        "task": args.task,
        # Resolved once, here: `list` then does a file-existence test instead of a
        # repeated search, and a title the vault sanitised on disk is matched at the
        # moment the operator can still be told the target is missing.
        "task_path": resolve_task(args.task, dirs),
        "state": "open",
        "answer": None,
        "answered_at": None,
        "note": None,
        "noted_at": None,
        "closed_at": None,
        "closed_evidence": None,
    }
    data["items"].append(item)
    save(data)
    print("added %s [%s] %s" % (item["id"], item["kind"], item["text"]))
    if args.task and not item["task_path"]:
        # WARN, never refuse: the ledger exists to record an instruction BEFORE its
        # task exists — the stretch between being said and becoming a task — so an
        # unresolvable --task is often correct at add time. Refusing would delete the
        # ledger's reason to exist; saying nothing would let the entry look resolved
        # until someone audits it by hand, which is the defect itself.
        print(
            (
                "⚠️  --task %r resolves to no file in any configured vault — the entry "
                "is recorded and will render as UNRESOLVABLE until a task by that "
                "title exists."
                if dirs
                else "⚠️  --task %r was NOT checked — no vault task dir was searchable "
                "(vault-cli missing, or its config unreadable). The entry is recorded "
                "and will render as UNCHECKED."
            )
            % args.task,
            file=sys.stderr,
        )
    return 0


def cmd_answer(args):
    sid = session_id(args)
    data = load(sid)
    item = find(data, args.id)
    stamp = now()
    if item["kind"] == "asked-of-you":
        # The operator's explicit answer IS the resolution for a question put to them
        # (design rule 1); there is no further evidence to wait for.
        item["answer"] = args.answer
        item["answered_at"] = stamp
        item["state"] = "closed"
        item["closed_at"] = stamp
        item["closed_evidence"] = "operator answered in session: %s" % args.answer
        print("answered + closed %s: %s" % (item["id"], args.answer))
    else:
        # Not an answer from the operator: a `pushed` / `asked-of-me` entry resolves on its
        # task, so this is an annotation and the entry STAYS `open`. Rendering it `answered`
        # would read as "the operator replied" next to entries where they genuinely have not.
        # `answered_at` is deliberately NOT stamped and `answer` is not set: both assert an
        # operator reply, which is exactly what this branch is not.
        item["note"] = args.answer
        item["noted_at"] = stamp
        item["state"] = "open"
        print(
            "noted on %s: %s (still open — %s resolves on its task reading status: completed)"
            % (item["id"], args.answer, item["kind"])
        )
    save(data)
    return 0


def cmd_note(args):
    """Attach evidence/progress to an entry without asserting an operator reply.

    This exists because `answer` means two things — it CLOSES an asked-of-you and only
    annotates the other kinds — and an overloaded verb produced exactly the mistake it
    invites: a session with real measurements to attach judged that no such verb existed
    and put them in a compact checkpoint instead, which is then consumed. `note` is the
    unambiguous verb: it never closes anything, on any kind.
    """
    sid = session_id(args)
    data = load(sid)
    item = find(data, args.id)
    item["note"] = args.text
    item["noted_at"] = now()
    save(data)
    print(
        "noted on %s: %s (still %s — nothing about a note resolves an entry)"
        % (item["id"], args.text, item["state"])
    )
    return 0


def cmd_close(args):
    sid = session_id(args)
    data = load(sid)
    item = find(data, args.id)
    if not args.evidence:
        sys.exit("error: close needs --evidence (rule 6: entries close on evidence only)")
    item["state"] = "closed"
    item["closed_at"] = now()
    item["closed_evidence"] = args.evidence
    save(data)
    print("closed %s — evidence: %s" % (item["id"], args.evidence))
    return 0


def cmd_list(args):
    sid = session_id(args)
    data = load(sid)
    dirs = task_dirs_for(args)
    for warning in forked_from(sid, data):
        print("⚠️  FORKED LEDGER: %s" % warning, file=sys.stderr)
    items = data["items"]
    if args.state != "all":
        items = [i for i in items if i["state"] == args.state]
    elif not args.include_closed:
        items = [i for i in items if i["state"] != "closed"]
    if args.format == "json":
        rendered = []
        for item in items:
            state, path = target_state(item, dirs)
            row = dict(item)
            # A three-valued field, not a boolean: a vacuously-true `task_resolved`
            # on an entry that names no task is the same false-green shape this
            # script is fixing. "none" is a fact, not a pass.
            row["task_state"] = state
            row["task_path"] = path
            rendered.append(row)
        print(json.dumps({"session_id": sid, "items": rendered}, indent=2))
        return 0
    if not items:
        print("(none open)")
        return 0
    for item in items:
        state, path = target_state(item, dirs)
        # Only an OPEN entry can be flagged. A closed one is terminal — its close
        # condition no longer gates anything, so an unresolvable target on it is
        # history, not a problem, and marking it would make the entries this fix
        # exists to explain look broken *after* they were correctly closed.
        marker = MARKERS.get(state, "") if item["state"] == "open" else ""
        # The marker rides the summary line, not the detail line: a manager renders
        # these one per line under "📋 Open with the operator", and the detail line is
        # exactly what a reader skimming that list does not see.
        line = "- %s · %s · %s · %s%s" % (
            item["kind"],
            item["text"],
            item["state"],
            age(item["created_at"]),
            marker,
        )
        print(line)
        detail = []
        if item.get("task"):
            detail.append(
                "task: %s%s"
                % (item["task"], DETAIL_SUFFIX.get(state, " (no file)"))
            )
        if item.get("resolves_on"):
            detail.append("resolves on: %s" % item["resolves_on"])
        if item.get("answer"):
            detail.append("answer: %s" % item["answer"])
        if item.get("note"):
            detail.append("note: %s" % item["note"])
        if detail:
            print("  (%s) %s" % (item["id"], " — ".join(detail)))
        else:
            print("  (%s)" % item["id"])
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--session",
        help="session id (default $CLAUDE_CODE_SESSION_ID, falling back to $CLAUDE_SESSION_ID)",
    )
    parser.add_argument(
        "--tasks-dir",
        action="append",
        default=None,
        help="vault task dir to resolve --task against (repeatable; default: every vault "
        "in vault-cli's config)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="record a new open item")
    p_add.add_argument("--kind", required=True, choices=KINDS)
    p_add.add_argument("--text", required=True, help="what was asked, verbatim where possible")
    p_add.add_argument("--task", help="vault task this resolves through")
    p_add.add_argument("--resolves-on", dest="resolves_on", help="what closes this entry")
    p_add.set_defaults(func=cmd_add)

    p_answer = sub.add_parser(
        "answer",
        help="record the OPERATOR's answer — closes an asked-of-you outright; on the other "
        "kinds it only annotates, so prefer `note` when nobody actually replied",
    )
    p_answer.add_argument("--id", required=True)
    p_answer.add_argument(
        "--answer",
        required=True,
        help="the operator's words, verbatim. On an asked-of-you this CLOSES the entry AND "
        "records 'operator answered in session: <text>' as its evidence — an attribution a "
        "later reader cannot distinguish from a real answer. Never pass a manager's own "
        "note here; use `note`.",
    )
    p_answer.set_defaults(func=cmd_answer)

    p_note = sub.add_parser(
        "note", help="attach evidence/progress — never closes, never asserts an operator reply"
    )
    p_note.add_argument("--id", required=True)
    p_note.add_argument("--text", required=True)
    p_note.set_defaults(func=cmd_note)

    p_close = sub.add_parser("close", help="close an entry on evidence")
    p_close.add_argument("--id", required=True)
    p_close.add_argument("--evidence", required=True, help="the on-disk fact that closes it")
    p_close.set_defaults(func=cmd_close)

    p_list = sub.add_parser("list", help="render the ledger")
    p_list.add_argument("--state", default="all", choices=("all",) + STATES)
    p_list.add_argument("--include-closed", action="store_true")
    p_list.add_argument("--format", default="text", choices=("text", "json"))
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
