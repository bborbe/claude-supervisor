#!/usr/bin/env python3
"""manager-menu-guard — `PreToolUse` on `AskUserQuestion`: a manager may not offer its own work as a menu.

WHAT IT DENIES, AND WHY A DENY RATHER THAN A WARNING. A manager session that
presents a **manager-owned action** as an `AskUserQuestion` option is asking the
operator to choose work the manager is supposed to execute itself. The rule is
already written down — `commands/manager-loop.md` § Manager contract (the
always-hold invariant) and `commands/steps/manager-drive.md` § Drive (the
per-round form) — and the rule alone did not hold: measured **2026-10-15**, the
Sentry Agent manager reverted to a menu within **~2 hours** of being told by
`SendMessage` that it should push such work forward, and a menu blocks its pane
until the operator presses Esc. An advisory signal is what a manager ignores, so
this is a gate that blocks the call (exit 2) and names the action to execute.

⚠️ **The deny is the whole point, and it is why this hook exists beside the prose.**
A warning here would be a third statement of a rule two files already carry.

THE VERB LIST IS DERIVED, NOT INVENTED, AND MAY NOT WIDEN. It is the vocabulary of
the two rule homes, verbatim: *"Opening or resuming an approved row, readying a
row, filing a task, repairing a page box — those are the manager's own work."*
Grounding, read rather than assumed: `agents/manager-drive.md` carries the report
vocabulary `To open` (×10) and `To resume` (×8); `commands/steps/manager-repair.md`
is a real step for `repairable` rows; `commands/manager-loop.md:92` has the manager
itself running `/vault-cli:create-task`.

⚠️ **Adding to the list is an operator decision, not a manager one** — the Manager
Session runbook § Gate triage class A is a **closed list** and the rule says so
(*"adding to it is an operator decision, and this rule may not widen it"*). So
`MANAGER_VERBS` is a single named constant and the only place a verb is written.

WHAT COUNTS AS NAMING AN ACTION — the predicate, and the defect that shaped it. An
option denies only when it **names a manager-owned action**: a manager-owned verb in
the LEADING position, followed by a manager-owned object (`row` / `task` / `page` /
`box` / `goal`, or one of the vault's own topic and goal page titles) within a short
window. ⚠️ **A bare word-boundary match on the label was the first predicate and it
was measured WRONG on 2026-10-16**, denying two legitimate operator-only menus
against the live `Sentry Agent Manager` session — `["keep the session open", "leave
it unchanged"]` (denied on `open`) and `["Keep working", "resume tomorrow"]` (denied
on `resume`). Both halves of the predicate are load-bearing and each alone is a
defect: leading-only denies `"Resume tomorrow"`, object-only denies `"keep the
session open"`. The full rationale is at the predicate's own comment block.

⚠️ **Named residual, in the fail-open direction:** an option naming a bare task title
with no object noun (`"Open A Docker Spawn's Create Reply …"`) is ALLOWED, because
reading the `25 Tasks` titles here would cost 51,285 files / 47.7 ms before every
`AskUserQuestion`. Also allowed: a manager-owned action that names no object at all
(`"Open"` alone) — indistinguishable from `"Resume tomorrow"` without the object test,
and a miss is the safe direction.

SCOPING — `fleet-board.py`'s RULE 1, COMPUTED LOCALLY. A session is a manager when
its name resolves to a `23 Topics` / `24 Goals` page, **or** its colour is orange.
⚠️ **`fleet-board.py` is never called.** It walks every vault and every task file,
and `server/manager-sessions.mjs` records its measured wall time as **14–73 s per
call (7.1 s quiet)**; this hook runs before *every* `AskUserQuestion`, so paying
that would stall the tool it polices. The whole local computation measures
**~26 ms** (registry scan 1.3 ms · vault page names 6.9 ms · transcript glob
15.5 ms · 256 KB tail read ~2 ms), and a full hook invocation — process start
included — **23 ms median over 10 runs** on the live fleet. Two orders of
magnitude off the board's cost.

⚠️ **`gate-owner-filter.py`'s `is_manager(session_id, ledger)` is a DIFFERENT rule**
(*"a session with no ledger record reads AS a manager"*) and is deliberately not
imported, mirrored or blended: it answers a spawn-ownership question, and a session
with no ledger record is the common case here, so blending it would classify most
of the fleet as a manager and deny the menus of workers — the exact scope this
hook must keep working.

⚠️ **BOTH ARMS ARE LOAD-BEARING, measured 2026-10-16 on the live fleet.** Of the
four live manager sessions, **two resolve by name** (`Sentry Agent Manager` →
topic `Sentry Agent`; `Fleet Manager` → `ROOT_NAME`) and **two resolve only by
colour** (`Single Source of Session State Manager`, `⚙ A2A Worker Controller
Manager` — neither names a topic or goal page). A name-only guard would miss half
the fleet's managers, so the colour arm is implemented rather than noted as a gap.

⚠️ **The colour is read from the session's OWN transcript, bounded, because the
registry does not carry one.** Neither `~/.claude/sessions/<pid>.json` nor
`wezterm cli list` has a colour key (`fleet-colours.py`'s own header says so): it
is persisted only as the last `{"type":"agent-color","agentColor":…}` record in
the transcript. Transcripts reach **500 MB** (median 3 MB, measured over 5,753
files), so a full read is not affordable here — the read is a **backwards chunk
scan from the tail** (256 KB windows, 8 MB cap) that returns the last record it
finds. Measured on all 18 live sessions: every one resolved inside the first
256 KB window, including the 500 MB transcript. A scan that finds nothing returns
`None` and the session is simply not a manager by colour — fail-open, never a guess.

⚠️ **Not reproduced from rule 1: its source-1 arm** (`manager_subject`'s
`loop_slugs` lookup against `~/.claude/state/sweep-gate/`). It fires only for a
name whose *slug* matches a subject slug, which a `<Subject> Manager` name never
does (`sentry-agent-manager` ≠ `sentry-agent`), so it can reach only the same
sessions the name arm already reaches — and it would add a stale on-disk record as
a second manager signal. Omitted deliberately; the colour arm is what actually
covers the names the page arm misses.

FAIL-OPEN, ALWAYS. Any parse error, a missing `session_id`, a registry entry that
cannot be read, or a vault that yields no topic and no goal page at all → **exit 0
(allow)**. A gate that failed closed on a parse error would block every
`AskUserQuestion` in every session that loads this plugin.

CONTRACT. Block = `exit 2` with the reason on **stderr**. The session id comes from
the hook's stdin `session_id`, falling back to `$CLAUDE_CODE_SESSION_ID` —
**never `$$`**, which is the hook process's own pid and names no session.

THE PER-INVOCATION RECORD. Every invocation appends one JSON object, as a line, to

    ~/.claude/state/manager-menu-guard/<session_id>.jsonl

(`SUPERVISOR_MANAGER_MENU_GUARD_STATE_DIR` overrides the directory; a session with no
readable id — the malformed-stdin path — lands in `unknown-session.jsonl`.) ⚠️ **The
path and the shape are fixed: a reader depends on both, and the shape is pinned by a
test.** The fields, exactly:

    ts           ISO-8601 timestamp with an offset, UTC
    session_id   the session, or null when stdin could not be parsed
    decision     "allow" | "deny"
    reason       "manager-owned-action" (deny) · "no-manager-action" · "non-manager" ·
                 "kill-switch" · "fail-open: <cause>"
    manager      true | false | null — null when the verdict was never evaluated,
                 which is a different answer from "evaluated, and it was false"
    manager_arm  "root" | "name" | "colour" | null — WHICH arm of rule 1 answered
    verbs        the manager-owned verbs the options named, in label order; [] otherwise

⚠️ **Written on EVERY invocation — allow and deny, kill switch and fail-open alike —
because an absent record has to mean "the hook did not run".** A guard that recorded
only its denies could not be told from a guard that never fired, which is the defect
this file exists to remove one layer up. A `deny` record with `reason:
manager-owned-action` and its `verbs` is what makes the denial provable after the fact
without parsing prose.

⚠️ **The write is fail-open and CANNOT change the verdict.** It runs after the decision
is fixed, every failure inside it is swallowed, and its result is never consulted by the
decision path: an unwritable directory, a failed append or a failed trim all leave the
hook exiting with the code it computed. **A lost record is preferred to a wrong
verdict** — the record is evidence, never a gate.

⚠️ **Growth is bounded by a trim, not by hope.** A session's file is trimmed back to its
most recent `RECORD_KEEP` (200) lines once it passes either bound — 256 KB
(`RECORD_CAP_BYTES`) or 200 lines, whichever the record size makes bind first (~38 KB at
the measured ~190 B per record, so the line count is the ceiling a reader can rely on).
The trim is written through a temporary file and `os.replace`d, so a kill mid-trim
cannot leave a partial file a reader would mistake for a record that was never written.
A record is the cheapest thing to lose here; the alternative, an unbounded append before
every `AskUserQuestion`, is a slow disk leak.

Kill switch: `SUPERVISOR_MANAGER_MENU_GUARD=off` makes the hook allow at once. ⚠️ It
still writes its `kill-switch` record — one line on a rare event, and it is what lets a
reader tell a guard the operator switched off from one that broke.

KNOWN RESIDUALS, named rather than implied. The predicate asks whether an option
**names a manager-owned action**, so the residues are all in the allow direction:
a bare task title with no object noun, a manager-owned verb with no object at all
(`"Open"`), and a verb that is not in the leading position (`"Do not open the row"`,
which is a legitimate decline). Option `description` text is **not** scanned — only
the label, which is the choice the manager is offering — so a description that
mentions a verb does not deny an otherwise operator-only menu.

Usage: wired by `hooks/hooks.json` as a `PreToolUse` hook on `AskUserQuestion`.
No CLI surface — it reads one JSON document on stdin and exits.

Exit codes: 0 allow (including every fail-open path) · 2 deny.
"""
import datetime
import glob
import json
import os
import re
import sys
import unicodedata

# --- the manager-owned vocabulary ---------------------------------------------
#
# ⚠️ ONE home, and it may not be widened here. See the module docstring: the list is
# the two rule homes' own vocabulary, and the Manager Session runbook's § Gate triage
# class A is a closed list whose widening is an operator decision.
MANAGER_VERBS = ("open", "resume", "ready", "file", "repair")

# ⚠️ Word boundaries, so `profile` / `OpenClaw` / `filament` are not read as the verb —
# `\bfil(?:e|es|ed|ing)\b` cannot match inside `filament` because `a` is a word character.
#
# ⚠️ **The inflections are spelled out per verb rather than appended as a suffix group,
# because two of the five drop the `e` before `-ing`.** Measured 2026-10-16: a shared
# `(?:s|d|ing)?` group matched `opening` and `repairing` and silently missed `filing`
# and `resuming` — the two most common forms of two of the five verbs, and a miss here
# is a manager-owned action offered as a menu and allowed.
#
# Each verb is its own NAMED group, so a match maps back to the list member rather than
# to whatever surface form the manager happened to type: the deny names `open`, not
# `opening`. ⚠️ The group names and `MANAGER_VERBS` are one set — pinned by a test, since
# a verb added to the list without a form here would compile into a pattern that cannot
# match it.
#
# ⚠️ **A verb missing from `_VERB_FORMS` degrades to its bare spelling rather than
# raising.** A `KeyError` here would be raised at IMPORT, before `main()` can fail open,
# and a hook that crashes before it can allow is a gate that fails closed — the one
# direction this file is built never to take. The degradation is partial and safe: the
# bare verb still fires, only its inflections are missed, and the suite catches the
# omission (a listed verb that cannot match its own name is a red test).
_VERB_FORMS = {
    "open": r"open(?:s|ed|ing)?",
    "resume": r"resum(?:e|es|ed|ing)",
    "ready": r"read(?:y|ies|ied|ying)",
    "file": r"fil(?:e|es|ed|ing)",
    "repair": r"repair(?:s|ed|ing)?",
}
_MANAGER_VERB_RE = re.compile(
    r"\b(?:" + "|".join(
        f"(?P<{v}>{_VERB_FORMS.get(v, re.escape(v))})" for v in MANAGER_VERBS
    ) + r")\b",
    re.IGNORECASE,
)

# --- the predicate: a verb AND its manager-owned object ------------------------
#
# ⚠️ **A bare word-boundary match on the label is the WRONG predicate, and it was
# measured wrong on 2026-10-16.** It denied two legitimate OPERATOR-ONLY menus against
# the live `Sentry Agent Manager` session: `["keep the session open", "leave it
# unchanged"]` (denied on `open`) and `["Keep working", "resume tomorrow"]` (denied on
# `resume`). Those are standard operator-only option shapes, so the predicate has to ask
# whether the option **names a manager-owned ACTION**, not whether one of five words
# appears in it.
#
# A manager-owned action is a verb AND the object it acts on: open/resume an approved
# **row**, ready a **row**, file a **task**, repair a **page**/**box**. Both halves are
# required, in two positions:
#
#   1. the verb must **LEAD** the option — `keep the session open` and `Keep working`
#      put the verb late or not at all, and neither is an action being offered; and
#   2. a manager-owned **object** must follow within `OBJECT_WINDOW` characters —
#      `resume tomorrow` leads with the verb and names no manager-owned object, so it is
#      an operator-only option and must survive.
#
# ⚠️ **Both halves are load-bearing, and each alone is a defect.** Leading-only denies
# `"Resume tomorrow"`; object-only denies `"keep the session open"`. The regression cases
# for both sit beside the three graded cases in the test file.
MANAGER_OBJECTS = ("row", "task", "page", "box", "goal")
_MANAGER_OBJECT_RE = re.compile(
    r"\b(?:" + "|".join(MANAGER_OBJECTS) + r")s?\b", re.IGNORECASE
)

# ⚠️ **A named target in quotes is an object source too, and it is the one that catches
# the canonical manager menu.** Measured 2026-10-16 by `scripts/manager-menu-probe.py`:
# `Resume "Sentry Agent - Retire the Legacy Gate"` was ALLOWED, because a real task title
# carries no manager-owned noun — the object list above answers only the phrasings that
# happen to say "row" or "task", and a manager quoting the row's own title says neither.
# That is the shape the rule exists for: a manager offering to open or resume a NAMED
# thing is offering its own work whatever the name is.
#
# ⚠️ **Head-only, mirroring the page-title rule below.** `Resume the session — "keep
# going"` puts the quote late and stays operator-only; only a quote the verb takes as its
# direct object counts. Without the position test this source would deny any operator-only
# option that happened to quote something.
#
# ⚠️ **Cheap by construction, which is why it is preferred to listing `25 Tasks`** — the
# guard's own docstring on `names_manager_owned_action` records that listing that
# directory is 51,285 files / 47.7 ms, and this costs a regex match on a 40-character
# window already in hand.
_MANAGER_QUOTED_HEAD_RE = re.compile(
    r"[\"'“”‘’][^\"'“”‘’]"
)

# How far past the verb its object may sit. 40 characters carries every natural phrasing
# measured (`Resume the paused Sentry row` = 21, `Open the approved row` = 9, `Repair the
# page box` = 8) and stops the window reaching an unrelated noun in a long option.
OBJECT_WINDOW = 40

# ⚠️ An option label is not prose: it carries the enumerator, bullet or quoting an
# operator-facing menu adds, and `"1. Open the approved row"` leads with its verb just as
# plainly as `"Open the approved row"` does. Stripping these is what keeps the leading
# test from depending on how the manager happened to number the menu.
_DECORATION_CHARS = " \t -*•·>#`'\""
_LEADING_DECORATION_RE = re.compile(
    r"^[" + re.escape(_DECORATION_CHARS) + r"]*(?:\d+[.)]\s*)?["
    + re.escape(_DECORATION_CHARS) + r"]*"
)

# --- rule 1's inputs, mirrored from fleet-board.py ----------------------------
#
# ⚠️ Both generations of the goals directory are listed for the reason fleet-board.py
# gives: the 2026-09-13 renumbering (`23 Goals` -> `24 Goals`) left older vaults on
# the previous pair, and one reader must not disagree with another about where a
# vault keeps its pages.
TOPIC_SUBDIRS = ("23 Topics",)
GOAL_SUBDIRS = ("24 Goals", "23 Goals")

# The root of the tree, whose name resolves to no page and is matched exactly.
ROOT_NAME = "Fleet Manager"

# `fleet-board.py`'s `MANAGER_COLOUR` — the operator's own manager signal, assigned
# by the `Managers` role in `~/.cache/wezterm-role-map.json`.
MANAGER_COLOUR = "orange"

# `<X> Manager` is allowed to name page `<X>`; fleet-board.py's `manager_subject`
# strips it before the page lookup, and it is what makes `Sentry Agent Manager`
# resolve to the topic `Sentry Agent`.
_MANAGER_SUFFIX = re.compile(r"\s+manager$", re.IGNORECASE)

# The colour read's bounds. Measured 2026-10-16: a 256 KB tail window resolves the
# last `agent-color` record for all 18 live sessions, and the 8 MB cap bounds the
# worst case (a turn whose records are buried under tool output) to a read no
# larger than a few milliseconds.
COLOUR_WINDOW = 262144
COLOUR_CAP = 8388608

KILL_SWITCH_ENV = "SUPERVISOR_MANAGER_MENU_GUARD"

# The rule's own homes, quoted into the deny so the manager is pointed at the norm
# rather than only at this gate.
RULE_HOMES = "commands/manager-loop.md § Manager contract; commands/steps/manager-drive.md § Drive"

# --- the per-invocation record ------------------------------------------------
#
# ⚠️ **The record is what makes "the guard DENIED" provable rather than merely "the
# guard ran".** A gate that leaves no trace is indistinguishable from one that is
# broken or absent, and a live check that has only the guard's exit code cannot tell a
# quiet window from a dead hook.
#
# One JSONL file PER SESSION, addressed by session id, under `STATE_DIR_ENV`'s
# directory. ⚠️ **Per-session rather than one shared file, because the reader's question
# is always "what did the guard do for THIS session"** — a per-session path answers it
# with one `open`, no scan and no filter, which is also what bounds the file (below).
#
# The record is written on EVERY invocation, allow and deny alike, including the
# kill-switch and fail-open paths — that is what makes an absent record mean "the hook
# did not run" rather than "nothing happened".
STATE_DIR_ENV = "SUPERVISOR_MANAGER_MENU_GUARD_STATE_DIR"
STATE_SUBDIR = "manager-menu-guard"
UNKNOWN_SESSION = "unknown-session"

# ⚠️ **The growth bound, and why it is a trim rather than an unbounded append.** This
# fires before every `AskUserQuestion` in every manager session, so an unbounded JSONL
# is a slow disk leak. Once a session's file passes EITHER bound it is trimmed back to
# its most recent `RECORD_KEEP` lines, through a temporary file and `os.replace` — so a
# kill mid-trim cannot leave a partial file a reader would mistake for a record that was
# never written.
#
# ⚠️ **The file is bounded by the LARGER of the two, and both are named so that is
# visible rather than implied:** `RECORD_KEEP` lines (~38 KB at the measured ~190 B per
# record) is what actually binds in practice, and `RECORD_CAP_BYTES` catches a
# pathologically large record before the line count would. Either way the file stops
# growing — the line count is the ceiling a reader can rely on.
RECORD_CAP_BYTES = 262144
RECORD_KEEP = 200

# The decision and reason vocabulary — a fixed set, so a reader never parses prose.
DECISION_ALLOW, DECISION_DENY = "allow", "deny"
REASON_MANAGER_OWNED = "manager-owned-action"   # deny: an option names a manager-owned action
REASON_NO_ACTION = "no-manager-action"          # allow: no option names one
REASON_NON_MANAGER = "non-manager"              # allow: one is named, the session is not a manager
REASON_KILL_SWITCH = "kill-switch"              # allow: the guard is switched off
# Fail-open reasons are `fail-open: <cause>`; the causes are the arms below.
FAIL_OPEN_MALFORMED_STDIN = "fail-open: malformed-stdin"
FAIL_OPEN_NON_OBJECT = "fail-open: non-object-payload"
FAIL_OPEN_NO_SESSION = "fail-open: no-session-id"
FAIL_OPEN_UNKNOWN_SESSION = "fail-open: unknown-session"
FAIL_OPEN_NO_VAULT = "fail-open: unresolvable-vault"
FAIL_OPEN_INTERNAL = "fail-open: internal-error"

# The record's exact shape — pinned by a test, because a reader depends on it.
RECORD_FIELDS = ("ts", "session_id", "decision", "reason", "manager", "manager_arm", "verbs")

# Which arm of rule 1 answered, named in the record so a reader can tell a name-detected
# manager from a colour-only one without re-deriving the classification.
ARM_ROOT, ARM_NAME, ARM_COLOUR = "root", "name", "colour"


def sessions_dir():
    """The session registry's directory, read at call time so a test can point it elsewhere."""
    return os.environ.get("CLAUDE_SESSIONS_DIR") or os.path.expanduser("~/.claude/sessions")


def state_dir():
    """The record's directory, read at call time for the same reason."""
    return (os.environ.get(STATE_DIR_ENV)
            or os.path.expanduser(os.path.join("~/.claude/state", STATE_SUBDIR)))


def projects_dir():
    """The transcripts root, read at call time for the same reason."""
    return os.environ.get("CLAUDE_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")


def norm_session_name(name):
    """`fleet-board.py`'s `_norm_session_name`, mirrored — the shared name normalizer.

    NFKC → strip a **leading** decoration run → collapse whitespace → casefold. A
    registry label carries the `⚙ ` marker a page title does not (`server/supervisor.mjs`
    writes `` `⚙ ${label}` `` at spawn), so a raw equality test misses it. The strip is
    `^[^\\w(]+` and **leading only**, so a collision-suffixed name (`⚙ <title> (2)`) is
    not normalized by it — the same gap the canonical copy carries.

    ⚠️ This is the *mirror* of the canonical helper in `scripts/fleet-board.py`, not an
    import: this script ships as a plugin artifact and must run with no sibling on its
    path. `scripts/tests/test_manager_menu_guard.py` pins the two together.
    """
    text = unicodedata.normalize("NFKC", name or "").strip()
    text = re.sub(r"^[^\w(]+", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def registry_entry(session_id, directory=None):
    """The registry record for `session_id`, or `None`.

    ⚠️ **`None` is the fail-open answer, not an error.** An unreadable directory, an
    unparsable entry and a session absent from the registry all mean "not proven to be
    a manager", and this hook allows what it cannot prove.
    """
    if not session_id:
        return None
    for path in glob.glob(os.path.join(directory or sessions_dir(), "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            continue
        if entry.get("sessionId") == session_id:
            return entry
    return None


def vault_page_names():
    """`(topics, goals)` — lowercased page title -> title, over every vault.

    A missing `~/Documents/Obsidian` degrades to two empty maps rather than aborting:
    the caller reads that as an unresolvable vault and fails open, which is the
    documented direction. Only directory listings are read — never a page body — which
    is what keeps this cheap where `fleet-board.py` is not.
    """
    topics, goals = {}, {}
    root = os.path.expanduser(os.environ.get("OBSIDIAN_DIR", "~/Documents/Obsidian"))
    try:
        vaults = [os.path.join(root, d) for d in sorted(os.listdir(root))
                  if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return topics, goals
    for vault in vaults:
        for subdirs, out in ((TOPIC_SUBDIRS, topics), (GOAL_SUBDIRS, goals)):
            for sub in subdirs:
                directory = os.path.join(vault, sub)
                if not os.path.isdir(directory):
                    continue
                for path in glob.glob(os.path.join(directory, "*.md")):
                    title = os.path.basename(path)[:-3]
                    out[title.lower()] = title
    return topics, goals


def manager_subject(name, topics, goals):
    """`('topic'|'goal', title)` when `name` names a page, else `None`.

    ⚠️ **Four candidates, not one, and the suffix strip is the load-bearing half.** A
    manager session is named `<Subject> Manager` (measured 2026-10-16: every live
    manager that resolves by name does so through the strip), and a spawned label may
    additionally carry the `⚙ ` decoration, so the raw name, the normalized name, and
    the ` Manager`-suffixed form of each are all tried. A raw equality test misses both.
    """
    for candidate in (name, norm_session_name(name)):
        for form in (candidate, _MANAGER_SUFFIX.sub("", candidate or "")):
            key = (form or "").strip().lower()
            if not key:
                continue
            if key in topics:
                return ("topic", topics[key])
            if key in goals:
                return ("goal", goals[key])
    return None


def transcript_path(entry):
    """The session's transcript, from its own `cwd` first and a glob as the fallback.

    ⚠️ The cwd-derived path is preferred because the glob's `*/<sid>.jsonl` matches
    **two** project dirs for every session on this machine — `-Users-…-Personal` and
    `-Users-…-private-personal` are the same vault through a symlink — and taking an
    arbitrary one of a duplicate pair is a coin flip over which copy is current. The
    glob stays as the fallback for a cwd that munges to a directory that does not exist.
    """
    session_id = entry.get("sessionId")
    if not session_id:
        return None
    cwd = entry.get("cwd") or ""
    if cwd:
        escaped = cwd.replace("/", "-").replace(".", "-")
        path = os.path.join(projects_dir(), escaped, f"{session_id}.jsonl")
        if os.path.isfile(path):
            return path
    hits = glob.glob(os.path.join(projects_dir(), "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def last_colour(path, window=COLOUR_WINDOW, cap=COLOUR_CAP):
    """The last `agent-color` record in `path`, read backwards from the tail, or `None`.

    Mirrors `fleet-colours.py`'s `last_colour` for what counts as a record — the quoted
    type **value**, never `"type":"agent-color"`, because the latter assumes compact
    separators and a serialization change upstream would silently report every session
    as uncoloured. The difference is the read: this scans bounded chunks from the end
    rather than the whole file, because the file can be 500 MB and this runs before
    every `AskUserQuestion`.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    read = 0
    end = size
    try:
        with open(path, "rb") as fh:
            while end > 0 and read < cap:
                start = max(0, end - window)
                fh.seek(start)
                chunk = fh.read(end - start)
                read += len(chunk)
                colour = None
                for line in chunk.split(b"\n"):
                    if b'"agent-color"' not in line:
                        continue
                    try:
                        found = json.loads(line.decode("utf-8", "replace")).get("agentColor")
                    except ValueError:
                        continue
                    if found:
                        colour = found
                if colour:
                    return colour
                end = start
    except OSError:
        return None
    return None


def manager_arm(entry, topics, goals):
    """Which arm of rule 1 made this session a manager — `root` / `name` / `colour` / `None`.

    ⚠️ The ARM is returned rather than a bare boolean because the record has to say
    which one answered: measured 2026-10-16, two of the four live managers are detected
    by colour alone, and a reader that cannot see the arm cannot tell a name-resolved
    manager from one whose name resolves to no page at all.

    Every unreadable input falls through to the next arm rather than raising, so a
    session this cannot classify is not a manager.
    """
    name = (entry.get("name") or "").strip()
    if name and name == ROOT_NAME:
        return ARM_ROOT
    if manager_subject(name, topics, goals):
        return ARM_NAME
    colour = last_colour(transcript_path(entry) or "")
    if (colour or "").strip().lower() == MANAGER_COLOUR:
        return ARM_COLOUR
    return None


def is_manager(entry, topics, goals):
    """`fleet-board.py`'s rule 1 for ONE session, computed locally.

    Name resolves to a topic/goal page, **or** the session is the root, **or** its
    colour is orange. A thin wrapper over `manager_arm`, which names the arm.
    """
    return manager_arm(entry, topics, goals) is not None


def option_labels(tool_input):
    """Every option label across every question of an `AskUserQuestion` payload.

    ⚠️ Tolerant by construction: a payload of any other shape yields `[]`, which allows
    the call. A gate that raised here would block a tool it could not read.
    """
    labels = []
    if not isinstance(tool_input, dict):
        return labels
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return labels
    for question in questions:
        if not isinstance(question, dict):
            continue
        options = question.get("options")
        if not isinstance(options, list):
            continue
        for option in options:
            if isinstance(option, dict):
                label = option.get("label")
                if isinstance(label, str) and label.strip():
                    labels.append(label)
    return labels


def leading_manager_verb(label):
    """`(verb, rest)` when the label LEADS with a manager-owned verb, else `None`.

    ⚠️ `match`, not `search` — position 0 is the whole assertion. `keep the session open`
    is not an offer to open anything, and a `search` would read the verb wherever it fell.

    The returned `rest` is what the object test scans; the leading decoration an
    operator-facing menu adds (`1. `, `- `, `` ` ``) is stripped first, so the test does
    not depend on how the manager numbered its options.
    """
    text = _LEADING_DECORATION_RE.sub("", label or "", count=1)
    match = _MANAGER_VERB_RE.match(text)
    if not match:
        return None
    return match.lastgroup, text[match.end():]


def names_manager_owned_action(label, page_titles=()):
    """The manager-owned verb when this option NAMES a manager-owned action, else `None`.

    ⚠️ Both halves are required — a leading verb AND a manager-owned object in the window
    that follows it. See the predicate's comment block for the two operator-only menus a
    single-half predicate was measured denying on 2026-10-16.

    Three object sources, any one of which answers:

    1. a manager-owned noun (`row` / `task` / `page` / `box` / `goal`);
    2. a **quoted** named target at the head of the window — `Resume "<anything>"`, the
       shape a manager uses when it quotes the row's own title; or
    3. one of the vault's own topic and goal page titles — the subjects a manager opens,
       resumes and readies, already in hand because rule 1 needs them.

    Sources 2 and 3 are accepted only at the head of the window (after an optional
    article), which is what keeps `Open questions about Attention Board Polish` — an
    operator-only option that merely mentions a topic — out of the deny.

    ⚠️ **Task titles are deliberately NOT an object source.** They would be a fourth
    signal, but `25 Tasks` is **51,285 files / 47.7 ms** to list here (measured
    2026-10-16), which would triple this hook's cost before every `AskUserQuestion`.
    Source 2 is what covers that gap instead: a manager naming a row quotes its title,
    and the quote is free where the listing is not. The named residual is now only an
    option that names a bare task title with **no** quoting and no object noun
    (`Open A Docker Spawn's Create Reply …`), which no measured menu has used.
    """
    lead = leading_manager_verb(label)
    if not lead:
        return None
    verb, rest = lead
    window = rest[:OBJECT_WINDOW]
    if _MANAGER_OBJECT_RE.search(window):
        return verb
    head = window.strip()
    for article in ("the ", "a ", "an "):
        if head.casefold().startswith(article):
            head = head[len(article):]
            break
    if _MANAGER_QUOTED_HEAD_RE.match(head):
        return verb
    for title in page_titles:
        if title and head.casefold().startswith(title.casefold()):
            return verb
    return None


def manager_owned_actions_in(tool_input, page_titles=()):
    """The manager-owned verbs the payload's options NAME, in label order, deduped.

    ⚠️ The base verb is returned, never the surface form the label used — `match.lastgroup`
    names the group, so `Opening the approved row` and `Open the approved row` both answer
    `open` and the deny reads as the rule's own vocabulary rather than as a quotation of
    the menu.
    """
    found = []
    for label in option_labels(tool_input):
        verb = names_manager_owned_action(label, page_titles)
        if verb and verb not in found:
            found.append(verb)
    return found


def deny_reason(session_name, verbs):
    """The stderr text. Names the verbs found, the rule, and the action to execute instead."""
    return (
        f"DENIED: '{session_name}' is a manager session, and this AskUserQuestion offers a "
        f"manager-owned action as a menu option: {', '.join(verbs)}.\n"
        f"Manager-owned actions — opening or resuming an approved row, readying a row, filing a "
        f"task, repairing a page box — are the manager's own work: execute them and report them "
        f"as a table line, never as a menu.\n"
        f"Execute the action instead (e.g. /supervisor:open \"<name>\" for an approved row), then "
        f"report the result. The modal is reserved for the operator-only decisions — a "
        f"`phase: todo` approval, a production-touching or irreversible action, a TDR.\n"
        f"Manager-owned verbs: {' · '.join(MANAGER_VERBS)} — adding to this list is an operator "
        f"decision, not a manager one.\n"
        f"Rule: {RULE_HOMES}.\n"
        f"Kill switch: {KILL_SWITCH_ENV}=off\n"
    )


def session_id_from(payload, environ=None):
    """The hook's session id: stdin's `session_id` first, then `$CLAUDE_CODE_SESSION_ID`.

    ⚠️ **Never `$$`.** The pid of this hook process names no session, and a gate keyed on
    it would classify every call against a session that does not exist.
    """
    sid = (payload or {}).get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return ((environ if environ is not None else os.environ)
            .get("CLAUDE_CODE_SESSION_ID") or "").strip()


def evaluate(payload, environ=None):
    """The whole verdict for one invocation: `(exit_code, stderr_text, record)`.

    ⚠️ **Every path returns a record, including every fail-open one**, because the
    record's job is to distinguish "the guard allowed this" from "the guard never ran".
    An allow that returned no record would be indistinguishable from a dead hook — the
    exact blindness the record exists to remove.

    ⚠️ `manager` is `None` on the paths that short-circuit before the registry read (a
    menu that does not lead with one of the five verbs, and the kill switch): the verdict
    was not evaluated, which is a different answer from "evaluated, and it was false",
    and the record says so rather than guessing.
    """
    env = environ if environ is not None else os.environ
    session_id = session_id_from(payload, environ)

    def verdict(code, reason, manager=None, arm=None, verbs=(), text=""):
        return code, text, {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session_id": session_id or None,
            "decision": DECISION_DENY if code else DECISION_ALLOW,
            "reason": reason,
            "manager": manager,
            "manager_arm": arm,
            "verbs": list(verbs),
        }

    if (env.get(KILL_SWITCH_ENV) or "").strip().lower() == "off":
        return verdict(0, REASON_KILL_SWITCH)
    tool_input = (payload or {}).get("tool_input")
    # ⚠️ The cheap half of the predicate runs first, so a menu that does not even LEAD
    # with one of the five verbs costs no vault read and no transcript read. Only a
    # payload that clears it pays for the object test's page titles.
    if not any(leading_manager_verb(label) for label in option_labels(tool_input)):
        return verdict(0, REASON_NO_ACTION)
    if not session_id:
        return verdict(0, FAIL_OPEN_NO_SESSION)
    entry = registry_entry(session_id)
    if not entry:
        return verdict(0, FAIL_OPEN_UNKNOWN_SESSION)
    topics, goals = vault_page_names()
    if not topics and not goals:
        return verdict(0, FAIL_OPEN_NO_VAULT)
    verbs = manager_owned_actions_in(tool_input, tuple(topics.values()) + tuple(goals.values()))
    arm = manager_arm(entry, topics, goals)
    if not verbs:
        # The session's manager verdict is still recorded — it was paid for to get here.
        return verdict(0, REASON_NO_ACTION, manager=arm is not None, arm=arm)
    if not arm:
        # A worker offering a manager-owned menu. Allowed — this hook's scope is
        # managers — but the verbs are recorded, so the occurrence is visible.
        return verdict(0, REASON_NON_MANAGER, manager=False, verbs=verbs)
    name = (entry.get("name") or session_id).strip()
    return verdict(2, REASON_MANAGER_OWNED, manager=True, arm=arm, verbs=verbs,
                   text=deny_reason(name, verbs))


def decide(payload, environ=None):
    """`(exit_code, stderr_text)` — `evaluate` without the record, for a direct caller."""
    code, text, _ = evaluate(payload, environ)
    return code, text


def record_path(session_id, directory=None):
    """The record's path for one session. ⚠️ Fixed and documented: a reader depends on it."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or UNKNOWN_SESSION)) or UNKNOWN_SESSION
    return os.path.join(directory or state_dir(), f"{safe}.jsonl")


def trim_records(path, cap=RECORD_CAP_BYTES, keep=RECORD_KEEP):
    """Keep the most recent `keep` lines once `path` passes `cap` bytes. Never raises.

    ⚠️ Written through a temporary file and `os.replace`d, so a kill mid-trim leaves
    either the old file or the new one — never a half-written one, which a reader could
    not tell from a record that was never written.
    """
    try:
        if os.path.getsize(path) <= cap:
            return
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines[-keep:])
        os.replace(tmp, path)
    except Exception:
        return


def write_record(record, directory=None):
    """Append one record. ⚠️ **Never raises, and never touches the decision.**

    A lost record is preferred to a wrong verdict: if the directory is unwritable, the
    append fails, or the trim fails, the caller still exits with the code it computed.
    This is called AFTER the verdict is fixed, and its return value is never consulted
    by the decision path.
    """
    try:
        os.makedirs(directory or state_dir(), exist_ok=True)
        path = record_path(record.get("session_id"), directory)
        line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        # ⚠️ The bound is passed at the CALL SITE, not left to `trim_records`' defaults:
        # a default argument is bound at definition time, so a caller (or a test) that
        # changes `RECORD_CAP_BYTES` would otherwise be silently ignored.
        trim_records(path, cap=RECORD_CAP_BYTES, keep=RECORD_KEEP)
        return path
    except Exception:
        return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        write_record(_bare_record(FAIL_OPEN_MALFORMED_STDIN))
        return 0
    if not isinstance(payload, dict):
        write_record(_bare_record(FAIL_OPEN_NON_OBJECT))
        return 0
    try:
        code, text, record = evaluate(payload)
    except Exception:
        # An internal error is a fail-open path like any other — and it is RECORDED,
        # so a guard that is crashing is visible instead of merely quiet.
        write_record(_bare_record(FAIL_OPEN_INTERNAL))
        return 0
    write_record(record)
    if code == 0:
        return 0
    sys.stderr.write(text)
    return code


def _bare_record(reason):
    """A record for a path that failed before a payload could be parsed.

    ⚠️ `session_id` is `None` here — the stdin document that would have carried it is
    exactly what could not be read — so these land in the `unknown-session` file.
    """
    return {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session_id": None, "decision": DECISION_ALLOW, "reason": reason,
            "manager": None, "manager_arm": None, "verbs": []}


if __name__ == "__main__":
    sys.exit(main())
