#!/usr/bin/env python3
"""Per-session ledger of operator asks (manager loops: fleet-manager / worker-manager).

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

`answer` is for something the OPERATOR said; `note` is for anything else you want to attach
(evidence, a measurement, progress). Reach for `note` by default — `answer` closes an
asked-of-you outright, and a verb that resolves one kind while annotating two others is easy
to reach for by mistake.
Session id defaults to $CLAUDE_SESSION_ID; --session overrides.
"""
import argparse
import datetime
import glob
import json
import os
import sys
import uuid

KINDS = ("asked-of-me", "asked-of-you", "pushed")
STATES = ("open", "closed")
ROOT = os.path.expanduser("~/.claude/state/open-items")


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
    sid = args.session or os.environ.get("CLAUDE_SESSION_ID")
    if not sid:
        sys.exit(
            "error: no session id. Pass --session <your-session-id>.\n"
            "note: CLAUDE_SESSION_ID is NOT exported into the shell by Claude Code — a manager\n"
            "      must pass its own id explicitly (read it from /status or the session's own\n"
            "      transcript path ~/.claude/projects/<project>/<session-id>.jsonl). Never guess\n"
            "      it from the newest file in ~/.claude/state/context/: a wrong id silently\n"
            "      splits the ledger in two and both halves look healthy."
        )
    return sid


def path_for(sid):
    return os.path.join(ROOT, "%s.json" % sid)


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
    mine = {i["id"] for i in data.get("items", [])}
    if mine:
        for other in glob.glob(os.path.join(ROOT, "*.json")):
            other_sid = os.path.basename(other)[:-5]
            if other_sid == sid:
                continue
            try:
                with open(other) as f:
                    shared = mine & {i["id"] for i in json.load(f).get("items", [])}
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if shared:
                warnings.append(
                    "%d entr%s also live in session %s's ledger — the two have forked and "
                    "will diverge" % (len(shared), "y" if len(shared) == 1 else "ies", other_sid[:8])
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


def cmd_add(args):
    sid = session_id(args)
    data = load(sid)
    item = {
        "id": uuid.uuid4().hex[:8],
        "kind": args.kind,
        "text": args.text,
        "created_at": now(),
        "resolves_on": args.resolves_on
        or ("the operator's explicit answer" if args.kind == "asked-of-you" else None),
        "task": args.task,
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
    for warning in forked_from(sid, data):
        print("⚠️  FORKED LEDGER: %s" % warning, file=sys.stderr)
    items = data["items"]
    if args.state != "all":
        items = [i for i in items if i["state"] == args.state]
    elif not args.include_closed:
        items = [i for i in items if i["state"] != "closed"]
    if args.format == "json":
        print(json.dumps({"session_id": sid, "items": items}, indent=2))
        return 0
    if not items:
        print("(none open)")
        return 0
    for item in items:
        line = "- %s · %s · %s · %s" % (
            item["kind"],
            item["text"],
            item["state"],
            age(item["created_at"]),
        )
        print(line)
        detail = []
        if item.get("task"):
            detail.append("task: %s" % item["task"])
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
    parser.add_argument("--session", help="session id (default $CLAUDE_SESSION_ID)")
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
    p_answer.add_argument("--answer", required=True)
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
