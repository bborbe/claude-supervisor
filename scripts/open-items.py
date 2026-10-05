#!/usr/bin/env python3
"""Per-session ledger of operator asks (manager loops: fleet-loop / manager-loop).

Storage: ~/.claude/state/open-items/<session-id>.json — never hand-written, same
write discipline as fleet-snapshot.py (atomic tmp+rename, timestamps stamped here).

Kinds:
  asked-of-me   an operator instruction; resolves when its task reads status: completed, or
                when the operator withdraws it
  asked-of-you  a question put to the operator; resolves on their explicit answer — so `answer`
                closes it outright. On the other two kinds `answer` records a note and leaves
                the entry `open`: they resolve on their task, and only `asked-of-you` is a kind
                the operator can resolve by replying. `withdraw` is REFUSED here — see below.
  pushed        a task filed/spawned on the operator's behalf; resolves on status: completed,
                or when the operator withdraws it

The three close paths are distinct claims and must not be collapsed into one another:
`answer` (the operator replied to a question), `close --evidence` (the entry's own resolution
condition was met, verified on disk), and `withdraw` (the operator withdrew the ask, so it
should not be outstanding — filed in error, superseded, or never real). A
withdrawal records `operator withdrew it: <reason>` and is REFUSED on `asked-of-you`, whose
only close path is `answer` — a withdrawal there would forge the operator attribution that
kind exists to protect. Without this verb an entry naming no task could never reach a terminal
state: `asked-of-me` and `pushed` resolve on a task file, and a manager tick summary filed as
one of them has no task behind it, so no close condition could ever fire.

A tick summary cannot become a `pushed` entry in the first place — `add` REFUSES a `pushed`
text matching TICK_SUMMARY_RE. That kind is for a task the manager filed, so its text records
that task, never the tick log that spawned it. Measured 2026-10-05: six 2026-09-29 manager-loop
tick summaries sat open in one ledger with no resolution path, indistinguishable in the render
from an ask genuinely still outstanding.

An OPEN `asked-of-me` / `pushed` entry naming no task now renders `⚠️ NO TASK`: `target_state`
returned `none` for it and `MARKERS` had no key for `none`, so it printed exactly like a
healthy entry — the render half of the same defect. The marker is deliberately scoped to those
two kinds, because an `asked-of-you` legitimately names no task and flagging it would be a
false positive.

An `asked-of-you` whose ask is HELD IN A WORKER'S PANE cannot be a ledger entry, and `add`
REFUSES one. The operator releases such a gate with their own keystroke in that pane, and a
relay never releases a gate — so this session never receives the words `answer` needs, and
the entry could never close: it would sit open forever, indistinguishable from a question
genuinely still outstanding. Surface the gate and hand over the pane instead. The same fact
is ordinary provenance on `asked-of-me` / `pushed`, which close on their task file, so
`--held-in-pane` is accepted and stored there.

An `asked-of-you` also records WHERE its gate was raised — the raising session and the pane
it was raised in (`$WEZTERM_PANE`) — captured automatically at `add` time, never via a flag.
This is provenance, not a close path: it is what lets a later reader tell a gate whose pane
is gone from one still outstanding. A session-liveness check cannot make that distinction —
the ledger's own session stays LIVE long after one of its panes is gone, and every entry in
a ledger carries that same session — so the PANE is the discriminator, and it is stored
here. `classify` reads the field and reports `gone` / `live` / `unknown` per entry; it
renders nothing itself and adds no close path.

Subcommands: add | set | answer | note | withdraw | close | list | classify

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
import re
import subprocess
import sys
import uuid

KINDS = ("asked-of-me", "asked-of-you", "pushed")
STATES = ("open", "closed")
ROOT = os.path.expanduser("~/.claude/state/open-items")

# A manager-loop tick summary filed as a `pushed` entry. Such a log is the manager's own
# record, not a task filed on the operator's behalf, and it names no task — so `pushed`'s
# close condition (its task file reading `status: completed`) can never fire and the entry
# can never reach a terminal state. Scoped to `pushed` on purpose: that kind's text is the
# manager's own writing, whereas `asked-of-me` carries the operator's words verbatim and a
# refusal there could block a genuine instruction that happens to quote a tick. Measured
# 2026-10-05 against a live 72-entry ledger: seven entries match, all `pushed` — the six
# open ones this guard exists for, plus one already closed that had named a task.
#
# The pattern stays `^`-anchored and the CALLER normalises its input (`tick_summary_text`),
# rather than the pattern growing a `^\s*(?:[-*+>]\s*)?`: the shape above is the one measured
# against real entries, and a pattern that loosens to accommodate input would make that
# measurement describe something else. Normalising first closes the whitespace AND the
# list/quote-marker bypasses without touching the measured shape.
TICK_SUMMARY_RE = re.compile(r"^Manager-loop tick \d+ \(\d{4}-\d{2}-\d{2}")

# A LEADING run of list markers, stripped by `tick_summary_text` before the pattern above is
# applied. The set is the forms a sweep's own output actually renders: bullets (`-`, `*`, `+`,
# and the en/em dashes a paste can carry), quotes (`>`), checkboxes (`[ ]` / `[x]` / `[X]`),
# and ordered items (`1.` / `1)`). It is a RUN, so `- [ ] …` — a checkbox inside a bullet,
# which is how a task-list line renders — reduces in one pass rather than needing a second.
LIST_MARKER_RE = re.compile(r"^(?:\s|[-*+>–—]|\[[ xX]\]|\d+[.)])+")

# Rendered on an OPEN entry's summary line. `unknown` gets its own wording rather
# than sharing UNRESOLVABLE's: a search that could not run and a search that found
# nothing are different facts, and only one of them is a problem with the entry.
MARKERS = {
    "unresolvable": " · ⚠️ UNRESOLVABLE",
    "unknown": " · ⚠️ UNCHECKED (no task dirs)",
    # `none` is what `target_state` returns for an entry naming no task at all. Scoped by
    # KIND at the render site (`marker_for`), never here: an `asked-of-you` legitimately
    # names no task because it resolves on the operator's answer, so a bare key would flag
    # every question the ledger holds. Only `asked-of-me` / `pushed` must name one.
    "none": " · ⚠️ NO TASK",
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


def current_pane():
    """The pane the calling process runs in (`$WEZTERM_PANE`), or None outside WezTerm.

    Read from the environment rather than resolved through the session registry: the
    registry carries no pane field (measured 2026-10-05 — its keys are `cwd`, `name`,
    `pid`, `procStart`, `sessionId`, … and no pane), and a pane id is a lease WezTerm
    renumbers and reuses, so a resolution through titles is a join that goes stale
    silently. `$WEZTERM_PANE` is the pane `add` ran in, which is exactly the question
    `classify` later asks of it.

    ⚠️ A headless worker INHERITS its spawner's `WEZTERM_PANE` (who-needs-me.py:1161), so
    the recorded pane is the spawner's there. Acceptable because the field is provenance
    and never a resolution: on `asked-of-you` — the only kind that records it —
    `--held-in-pane` is refused, so the value is always the pane the asking session runs in.
    """
    pane = os.environ.get("WEZTERM_PANE")
    return str(pane) if pane else None


def live_panes():
    """The set of live WezTerm pane ids (strings), or None when the query failed.

    `None`, never `{}` — the convention `jump.py:46` and `who-needs-me.py` already use: a
    failed `wezterm cli list` cannot prove a pane is gone any more than it can prove one is
    live, so no caller may read it as "no panes exist". `{}` is reserved for a WezTerm that
    answered with zero panes — a real empty answer that must keep working.
    """
    try:
        proc = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        return {str(p["pane_id"]) for p in json.loads(proc.stdout)}
    except (OSError, ValueError, subprocess.SubprocessError, KeyError, TypeError):
        return None


def classify_origin(item, panes):
    """`gone` | `live` | `unknown` — does the entry's recorded origin pane still exist?

    Three answers, not two, and the third must never collapse into `gone`: `unknown` means
    either that the entry records no origin (a legacy entry written before the field
    existed) or that the pane probe could not run. Reading "could not tell" as "gone" is
    the same false-negative shape the whole ledger exists to remove — a dead pane and an
    unprobed one must not render alike.
    """
    pane = item.get("origin_pane")
    if not pane:
        return "unknown"
    if panes is None:
        return "unknown"
    return "live" if str(pane) in panes else "gone"


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


def marker_for(item, state):
    """The marker to render on an entry's summary line, or "".

    Only an OPEN entry can be flagged: a closed one is terminal, its close condition gates
    nothing, and flagging it would make the very entries this check explains look broken
    after they were closed correctly.

    An `asked-of-you` is carved out by KIND, covering EVERY state rather than only `none`,
    and that is why this is a function rather than a dict lookup. That kind resolves on the
    operator's answer and makes no file claim, so no task-state marker is a true statement
    about it: `none` would fire on every question the ledger holds, and `unresolvable` would
    fire on a question whose task is context rather than a resolution path — and `set`, the
    verb the UNRESOLVABLE rule tells a manager to repair with, refuses this kind. Covering
    only `none` leaves the second case live: an `asked-of-you` added with `--task` naming no
    file renders `⚠️ UNRESOLVABLE` with no verb able to clear it. `asked-of-me` and `pushed`
    both resolve on a task file, so an entry of either kind naming none is exactly the entry
    whose close condition can never fire.
    """
    if item["state"] != "open":
        return ""
    if item["kind"] == "asked-of-you":
        return ""
    return MARKERS.get(state, "")


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
    item.setdefault("held_in_pane", None)
    # The origin record (where an asked-of-you's gate was raised) is forward-only: entries
    # written before it existed carry no key, and healing them to an explicit None keeps the
    # schema uniform so `classify` reads one shape rather than testing for absence. It is
    # NOT back-filled — the origin is not recoverable from an entry's data, which is the
    # whole reason the field had to be added at the write site.
    item.setdefault("origin_pane", None)
    item.setdefault("origin_session", None)
    # The withdrawal stamp is forward-only, like the origin record: entries closed before the
    # verb existed carry no key, and healing them to an explicit None keeps the schema uniform
    # so a reader tells a withdrawal from an evidence-close by one field rather than by
    # testing for absence. NOT back-filled — a past close cannot be reclassified from its data.
    item.setdefault("withdrawn_at", None)
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


def tick_summary_text(text):
    """`text` normalised for the tick-summary guard: leading whitespace and list markers off.

    The marker strip is why this is not just `lstrip()`. A tick summary is copied out of a
    sweep's own output, where it arrives as a bullet (`- Manager-loop tick 9 (…)`), a quoted
    block (`> Manager-loop tick 9 (…)`), a checkbox line (`- [ ] Manager-loop tick 9 (…)`)
    or an ordered item (`1. Manager-loop tick 9 (…)`), so an `^`-anchored pattern with only
    the whitespace stripped would refuse the unprefixed form and admit every form the text
    actually comes in. Stripping is confined to LEADING markers and is a run, so a checkbox
    behind a bullet (`- [ ] …`) reduces in one pass.

    Only the REMAINDER has to match the tick shape, so a `pushed` text that merely begins
    with punctuation or an ordinal is unaffected: `- Added the retry guard` and
    `1) Fix the thing` both reduce to something the pattern does not match, and are accepted.
    """
    return LIST_MARKER_RE.sub("", text)


def cmd_add(args):
    sid = session_id(args)
    if args.kind == "asked-of-you" and args.held_in_pane:
        # REFUSE, and write nothing at all — not even the heal `load` would do. This is
        # the one `add` that is refused rather than warned about, because the entry is
        # provably unclosable rather than merely unverified: `answer` is this kind's only
        # close path, `answer` needs this session to RECEIVE the operator's words, and a
        # gate in a worker's pane is released by the operator's own keystroke there — the
        # manager is forbidden from receiving it ("a relay never releases a gate"). Warn-
        # and-record, the shape used for an unresolvable --task, would leave an entry that
        # can never close: indistinguishable from a question genuinely still outstanding,
        # which is the misreading this ledger exists to prevent.
        sys.exit(
            "error: an `asked-of-you` held in pane %s is not a ledger entry — nothing "
            "written.\n"
            "  Its only close path is `answer`, which needs THIS session to receive the\n"
            "  operator's words. A gate in a worker's pane is released by the operator's\n"
            "  own keystroke there, and a relay never releases a gate — so this session\n"
            "  never receives them, and the entry would sit open forever.\n"
            "  Surface the gate and hand over the pane instead, saying a direct go is\n"
            "  needed — the jump link, never a relay:\n"
            "    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/jump-link.py %s\n"
            "  Omit --held-in-pane for a question this session CAN receive the answer to:\n"
            "  a relayable non-gate question, or a headless worker's gate answered over\n"
            "  the supervisor's permission channel. Those close normally."
            % (args.held_in_pane, args.held_in_pane)
        )
    if args.kind == "pushed" and TICK_SUMMARY_RE.match(tick_summary_text(args.text)):
        # REFUSE, and write nothing at all — the same discipline as the held_in_pane
        # refusal above, and for the same reason: the entry could never close. `pushed`
        # resolves on its task file reading `status: completed`, a tick summary names no
        # task, so no close condition could ever fire. It would sit open forever,
        # indistinguishable in the render from an ask genuinely still outstanding — which
        # is exactly what six of them did (measured 2026-10-05).
        sys.exit(
            "error: a manager-loop tick summary is not a `pushed` entry — nothing "
            "written.\n"
            "  `pushed` is for a task this manager filed or spawned on the operator's\n"
            "  behalf, and it resolves when that task reads `status: completed`. A tick\n"
            "  summary names no task, so that condition can never fire and the entry\n"
            "  would sit open forever — indistinguishable from an ask still outstanding.\n"
            "  The tick's record belongs in the sweep output, not in the ledger. If the\n"
            "  tick DID file a task, add an entry whose text is that task:\n"
            "    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py add --kind pushed \\\n"
            "      --text \"<the task>\" --task \"<the task>\"\n"
            "  An operator instruction that happens to quote a tick is `asked-of-me`,\n"
            "  which this guard deliberately does not touch."
        )
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
        # Provenance: which pane the ask lives in, when it is not this session's. On
        # `asked-of-me` / `pushed` this is a fact a later reader needs — the instruction
        # was given to a worker, not here. On `asked-of-you` it can never be set; see the
        # refusal above.
        "held_in_pane": args.held_in_pane,
        # The origin record: where THIS ask was raised. Captured automatically at write
        # time — never a flag — and only on `asked-of-you`, the one kind whose close path
        # (`answer`) needs this session to receive the operator's words. It is what lets a
        # later reader tell a gate whose pane is gone from one still outstanding; the
        # session alone cannot, because every entry in a ledger carries this same session.
        # `origin_session` is the raising session, `origin_pane` the pane it ran in
        # (`$WEZTERM_PANE`, None outside WezTerm). `classify` is the reader.
        "origin_pane": current_pane() if args.kind == "asked-of-you" else None,
        "origin_session": sid if args.kind == "asked-of-you" else None,
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
    warn_unresolvable_task(args.task, item["task_path"], dirs)
    return 0


def warn_unresolvable_task(task, path, dirs):
    """Warn on stderr that a named task backs no file, or could not be checked at all.

    WARN, never refuse: the ledger exists to record an instruction BEFORE its task exists —
    the stretch between being said and becoming a task — so an unresolvable `--task` is often
    correct at write time. Refusing would delete the ledger's reason to exist; saying nothing
    would let the entry look resolved until someone audits it by hand, which is the defect.

    The two branches are not one message with a suffix: a search that could not run and a
    search that found nothing are different facts, and only one of them is a problem with
    the entry. `add` and `set` share this so the wording cannot drift between them.
    """
    if not task or path:
        return
    print(
        (
            "⚠️  --task %r resolves to no file in any configured vault — the entry "
            "will render as UNRESOLVABLE until a task by that title exists."
            if dirs
            else "⚠️  --task %r was NOT checked — no vault task dir was searchable "
            "(vault-cli missing, or its config unreadable). The entry will render "
            "as UNCHECKED."
        )
        % task,
        file=sys.stderr,
    )


def cmd_set(args):
    """Name a task on an entry that already exists.

    The act step's missing half, and the reason the `⚠️ NO TASK` marker can be acted on at
    all. `add` was the only writer of `task`, so a sweep that found an open entry with no
    task behind it — the act rule's own trigger — could file a task but could not name it
    on the entry: the entry stayed unresolvable forever and the rule stayed prose nothing
    could follow. Naming it here is what makes the entry's close condition checkable.

    It cannot be used to fake a resolution: `list` re-resolves the target on every read, so
    a task that backs no file renders `⚠️ UNRESOLVABLE` regardless of what was written here.

    REFUSED on an `asked-of-you` and on a closed entry. Both refusals write nothing: the
    first because that kind makes no file claim, so naming a task would move its
    `target_state` off the `none` that `marker_for`'s kind carve-out keys on and render
    `⚠️ UNRESOLVABLE` on a question the answer resolves; the second because a closed entry's
    task is history, not a resolution path.
    """
    sid = session_id(args)
    data = load(sid)
    item = find(data, args.id)
    if item["state"] == "closed":
        # REFUSE. A closed entry is terminal and its task is history; re-pointing it would
        # change what `list --state closed` reports about a decision already taken.
        sys.exit(
            "error: %s is already closed — nothing written.\n"
            "  A closed entry is terminal, so its task is history rather than a resolution\n"
            "  path: re-pointing it would rewrite the record of a decision already taken."
            % item["id"]
        )
    if item["kind"] == "asked-of-you":
        # REFUSE. This kind resolves on the operator's ANSWER and makes no file claim, so a
        # task named on it is not a resolution path — and naming one would move its
        # `target_state` off `none`, which is the value `marker_for` keys its kind carve-out
        # on. An unresolvable name would then render `⚠️ UNRESOLVABLE` on a question whose
        # real resolution is the answer: the false positive that carve-out exists to prevent,
        # reachable through a verb that did not exist when it was written.
        sys.exit(
            "error: an `asked-of-you` cannot take a task — nothing written.\n"
            "  This kind resolves on the operator's ANSWER and makes no file claim, so a\n"
            "  task named on it is not a resolution path, and naming one would render\n"
            "  `⚠️ UNRESOLVABLE` on a question the answer resolves.\n"
            "  Record the link with `note` instead:  note --id %s --text \"<the link>\""
            % item["id"]
        )
    if not args.task.strip():
        # REFUSE. `resolve_task("")` returns None, so this writes `task: ""` with no path and
        # STRIPS a previously valid resolution path — the same empty-value hole `--reason` and
        # `--resolves-on` are guarded against on this verb, and the third and last flag that
        # carried one.
        sys.exit(
            "error: set --task needs a non-empty value — nothing written.\n"
            "  An empty task names nothing, so the entry would carry no resolution path."
        )
    if args.resolves_on is not None and not args.resolves_on.strip():
        # REFUSE. An empty close condition is one nothing can check — the same defect the
        # `⚠️ NO TASK` marker exists to make visible. Omit the flag to leave it unchanged.
        sys.exit(
            "error: set --resolves-on needs a non-empty value — nothing written.\n"
            "  An empty close condition is one nothing can check. Omit the flag to leave\n"
            "  the entry's existing condition unchanged."
        )
    dirs = task_dirs_for(args)
    previous = item.get("task")
    item["task"] = args.task
    item["task_path"] = resolve_task(args.task, dirs)
    if args.resolves_on:
        item["resolves_on"] = args.resolves_on
    save(data)
    print("set task on %s [%s]: %s" % (item["id"], item["kind"], args.task))
    if previous and previous != args.task:
        # WARN, never refuse: re-pointing is the legitimate way to correct a wrong title, and
        # a wrong one cannot fake a resolution because `list` re-resolves on every read. But a
        # silent re-point rewrites a decision leaving no trace, so name what was replaced —
        # the same discipline `warn_unresolvable_task` applies to a name backing no file.
        print(
            "⚠️  replaced task %r with %r on %s — the prior title is not recorded"
            % (previous, args.task, item["id"]),
            file=sys.stderr,
        )
    warn_unresolvable_task(args.task, item["task_path"], dirs)
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


def cmd_withdraw(args):
    """Close an entry on the OPERATOR's withdrawal.

    The third close path, and deliberately not a synonym for `close --evidence`: `close`
    asserts the entry's OWN resolution condition was met and verified on disk, while this
    asserts the entry should never have existed. Both end at `state: closed`, but a later
    reader tells them apart by `withdrawn_at` — and a reader who cannot tell them apart
    cannot tell a resolved ask from one that was never real.

    It exists because an entry can otherwise be permanently open: `asked-of-me` / `pushed`
    resolve on a task file, and a manager tick summary filed as one of them has no task, so
    no close condition could ever fire. `skills/open-items/SKILL.md` already promised this
    path for `asked-of-me` ("or the operator withdraws it") with no verb behind it.

    REFUSED on an `asked-of-you`, and on an entry that is ALREADY closed. Both write nothing.
    The second is not symmetry for its own sake: without it the unconditional write below
    replaces an evidence-close's `closed_evidence` with a withdrawal claim, so a resolved ask
    is retrospectively recorded as one that was never real — the exact indistinguishability
    this docstring says the verb exists to prevent.
    """
    sid = session_id(args)
    data = load(sid)
    item = find(data, args.id)
    if item["kind"] == "asked-of-you":
        # REFUSE, and write nothing. `answer` is this kind's only close path — the skill's
        # kind table says so outright — because the operator answering a question is the one
        # fact that resolves it. A withdrawal here would stamp an operator act onto a
        # question they may never have seen: the forgery `answer`'s own docstring warns
        # about, reached through a second verb instead of the first.
        sys.exit(
            "error: an `asked-of-you` cannot be withdrawn — nothing written.\n"
            "  Its only close path is `answer`, because the operator replying to a\n"
            "  question is the fact that resolves it; a withdrawal would record an\n"
            "  operator act on a question they may never have seen.\n"
            "  If the operator answered it:  answer --id %s --answer \"<their words>\"\n"
            "  If it was filed in error:     close  --id %s --evidence \"<on-disk fact>\""
            % (args.id, args.id)
        )
    if item["state"] == "closed":
        # REFUSE, and write nothing. Without this guard the unconditional write below turns
        # an entry already closed by `close --evidence` or `answer` into one recorded as
        # never real — replacing its on-disk evidence with a withdrawal claim and stamping
        # `withdrawn_at`. That is precisely the indistinguishability this verb's docstring
        # says it exists to prevent, defeated by the verb itself. `find()` matches on an id
        # PREFIX, so a stale `--id` landing on an already-CLOSED entry is easy — and the
        # skill's act rule has a manager acting on rows from a render taken before a close.
        # (A prefix landing on a different OPEN entry is the same exposure `close` and
        # `answer` already carry; this guard does not reach that case and does not claim to.)
        sys.exit(
            "error: %s is already closed — nothing written.\n"
            "  Its close record is on disk: %s\n"
            "  Overwriting it would record a resolved ask as one that was never real, and\n"
            "  `withdrawn_at` is the only field that tells the two apart.\n"
            "  A close you believe is wrong is corrected by a new entry, not by re-closing\n"
            "  this one." % (item["id"], item.get("closed_evidence") or "(none recorded)")
        )
    if not args.reason.strip():
        # REFUSE. This verb's whole rationale is that the OPERATOR spoke, and the record it
        # writes is `operator withdrew it: <reason>` — so a blank reason records a withdrawal
        # carrying zero operator words: the attribution forgery the docstring warns about,
        # reached by omission rather than by intent. `cmd_close` guards the same field the
        # same way, for the same reason.
        sys.exit(
            "error: withdraw needs a non-empty --reason — nothing written.\n"
            "  The recorded evidence is 'operator withdrew it: <reason>', so a blank reason\n"
            "  records a withdrawal the operator never voiced."
        )
    stamp = now()
    item["state"] = "closed"
    item["closed_at"] = stamp
    item["closed_evidence"] = "operator withdrew it: %s" % args.reason
    item["withdrawn_at"] = stamp
    save(data)
    print(
        "withdrew %s [%s] — operator withdrew it: %s"
        % (item["id"], item["kind"], args.reason)
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
        # `marker_for` also carries the kind scoping `none` needs; see its docstring.
        marker = marker_for(item, state)
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
        if item.get("held_in_pane"):
            detail.append("pane: %s" % item["held_in_pane"])
        if item.get("answer"):
            detail.append("answer: %s" % item["answer"])
        if item.get("note"):
            detail.append("note: %s" % item["note"])
        if detail:
            print("  (%s) %s" % (item["id"], " — ".join(detail)))
        else:
            print("  (%s)" % item["id"])
    return 0


def cmd_classify(args):
    """Report, per entry, whether its recorded origin pane still exists.

    The READ half of the origin record: `add` stores where an `asked-of-you` was raised,
    and this is the code path that reads it back to distinguish a gate whose pane is gone
    from one still outstanding. It renders nothing on any surface the ledger's consumers
    already read — `list`'s summary line is untouched and no marker is added to it — so the
    classification is available without changing what a manager sweep prints. A verdict is
    `gone` / `live` / `unknown`; `unknown` covers both an entry with no recorded origin
    (legacy) and a probe that could not run.
    """
    sid = session_id(args)
    data = load(sid)
    panes = live_panes()
    items = data["items"]
    if args.state != "all":
        items = [i for i in items if i["state"] == args.state]
    elif not args.include_closed:
        items = [i for i in items if i["state"] != "closed"]
    if args.format == "json":
        rendered = [dict(i, origin_verdict=classify_origin(i, panes)) for i in items]
        print(
            json.dumps(
                {"session_id": sid, "panes_readable": panes is not None, "items": rendered},
                indent=2,
            )
        )
        return 0
    if not items:
        print("(none)")
        return 0
    for item in items:
        verdict = classify_origin(item, panes)
        print(
            "- %s · %s · %s · origin %s · %s"
            % (
                item["id"],
                item["kind"],
                item["state"],
                item.get("origin_pane") or "(none)",
                verdict,
            )
        )
    if panes is None:
        # A probe that could not run must say so, not leave the reader to infer it from a
        # column of `unknown` — the same rule `list` follows for an unsearchable task dir.
        print(
            "⚠️  `wezterm cli list` unreadable — pane existence unknown; every verdict is "
            "`unknown`.",
            file=sys.stderr,
        )
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
    p_add.add_argument(
        "--held-in-pane",
        dest="held_in_pane",
        help="the pane this ask is held in, when it lives in a worker's pane rather than "
        "in this session. REFUSED on --kind asked-of-you: a gate the operator releases by "
        "their own keystroke there never reaches this session, so the entry could never "
        "close — hand over the pane instead. Accepted as provenance on the other kinds.",
    )
    p_add.set_defaults(func=cmd_add)

    p_set = sub.add_parser(
        "set",
        help="name a task on an existing entry — the act step's missing half, so a "
        "task-less entry can stop rendering `⚠️ NO TASK` and become closable",
    )
    p_set.add_argument("--id", required=True)
    p_set.add_argument("--task", required=True, help="the vault task covering this entry")
    p_set.add_argument(
        "--resolves-on", dest="resolves_on", help="what closes this entry (rarely needed)"
    )
    p_set.set_defaults(func=cmd_set)

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

    p_withdraw = sub.add_parser(
        "withdraw",
        help="close an entry on the OPERATOR's withdrawal — the "
        "third close path, distinct from `answer` and `close --evidence`. REFUSED on "
        "--kind asked-of-you, whose only close path is `answer`",
    )
    p_withdraw.add_argument("--id", required=True)
    p_withdraw.add_argument(
        "--reason",
        required=True,
        help="why the entry is withdrawn, quoting the operator where they spoke. Recorded "
        "as 'operator withdrew it: <reason>' — a withdrawal is an OPERATOR act, so never "
        "pass a manager's own tidying-up here; an entry whose own condition was met is a "
        "`close --evidence`, and one that was answered is an `answer`.",
    )
    p_withdraw.set_defaults(func=cmd_withdraw)

    p_close = sub.add_parser("close", help="close an entry on evidence")
    p_close.add_argument("--id", required=True)
    p_close.add_argument("--evidence", required=True, help="the on-disk fact that closes it")
    p_close.set_defaults(func=cmd_close)

    p_list = sub.add_parser("list", help="render the ledger")
    p_list.add_argument("--state", default="all", choices=("all",) + STATES)
    p_list.add_argument("--include-closed", action="store_true")
    p_list.add_argument("--format", default="text", choices=("text", "json"))
    p_list.set_defaults(func=cmd_list)

    p_classify = sub.add_parser(
        "classify",
        help="report whether each entry's recorded origin pane still exists "
        "(gone / live / unknown) — reads the origin field; renders no marker",
    )
    p_classify.add_argument("--state", default="all", choices=("all",) + STATES)
    p_classify.add_argument("--include-closed", action="store_true")
    p_classify.add_argument("--format", default="text", choices=("text", "json"))
    p_classify.set_defaults(func=cmd_classify)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
