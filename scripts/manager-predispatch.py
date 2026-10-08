#!/usr/bin/env python3
"""manager-predispatch.py — the pre-dispatch gate for `/manager-drive` and `/manager-status`.

Why this exists
---------------
Both commands dispatch a full sweep agent on every run. Measured 2026-09-25 in the
Attention Routing manager session on a 26-task tree with 25 done: the
`supervisor:manager-sweep-reader` agent cost 91,598 tokens, the drive leg 71,275, and a
`manager-status` snapshot 125,423 — for zero new information. Three times in one session
the manager bypassed the agents by hand to avoid the cost.

The sweep is not the cost; the dispatch is. So this gate runs FIRST, model-free, and
answers one question: has anything the sweep would report actually moved since the last
persisted snapshot? If not, the command replays the stored table and dispatches nothing.

Contract — ported, not invented
-------------------------------
This is a PORT of `my-vault/.claude/scripts/sweep-gate.py`, which gates the *loop* tick.
The loop gate could not be reused as a file: it lives in the vault while this plugin is
vault-agnostic (commands resolve vaults via `vault-cli config` and reference notes by
title, never path). What carries over is the contract — the digest inputs, the state key,
the exit codes and the fail-open rule — reused verbatim so the two gates cannot drift
into disagreeing about what "unchanged" means.

  manager-predispatch.py --vault <path> --subject <name> --check
  manager-predispatch.py --vault <path> --subject <name> --print
  manager-predispatch.py --vault <path> --subject <name> --save     # dates from the payload

⚠️ Why this store is not the loop's store — checked before building, 2026-09-26
--------------------------------------------------------------------------------
A live per-subject snapshot already exists: `~/.claude/state/sweep-gate-loop/<vault>/
<subject>.snapshot.json`, written on CHANGE-WAKE ticks by the vault-side launchd job (the job ticks every 900 s; the snapshot advances only when the digest changes, so `recorded_at` is an identity, not a freshness value — see `docs/fleet-surface.md` § Session end)
`com.bborbe.sweep-gate-notify`. It was read and compared field-for-field, and the result
is worth keeping because it cuts both ways:

  IT CARRIES the digest inputs — `status`, `phase`, `progress_hash`, `session`,
  `liveness`, `stuck` per task. That WAS the exact set `digest_of()` below hashed when
  this was checked, so the contract was CONFIRMED rather than merely assumed: the loop's
  writer and this gate agreed on what "unchanged" means, which is the property that keeps
  two gates from drifting.

  ⚠️ **THAT AGREEMENT NO LONGER HOLDS AT ONE TERM, since 2026-10-05.** `digest_of` now
  hashes `liveness_change_term` rather than the raw `liveness` word, so the two stores
  agree on the field SET and disagree on the liveness term's DERIVATION — deliberately,
  because session churn must not authorise a dispatch while session death still must.
  The snapshot keeps the raw word for its render, and its writer
  `sweep-gate-classify.py` keeps its own `liveness -> parked` trigger (a different gate
  with a different job). ⚠️ **Recorded HERE and not only in `docs/fleet-surface.md`,
  because this block is the digest contract's declared single home** —
  `commands/manager-status.md` step 1 reads it rather than restating it, so a stale
  agreement here is what a reader would take as current.

  IT LACKS the rendered table, and it has no member list. The no-change branch must
  replay the table (the operator sees the frame on every run — silence would be
  ambiguous, and a quiet run and a dead run look identical from outside), and the loop
  snapshot carries no `table` key; the loop's table lives in the sibling
  `<subject>.tick.txt` instead.

  IT IS LOOP-ONLY AND VAULT-SIDE. It exists for subjects with an armed loop, in vaults
  where that launchd job is installed. This plugin is vault-agnostic and both commands
  run against arbitrary subjects, so reading it would fail open on every unarmed subject
  — a full sweep every run, reading as armed while saving nothing. That is the same
  failure `a frozen-tree remedy` closed for the
  loop, and reintroducing it here would be a regression dressed as reuse.

So: same contract, different surface, one deliberately diverged term. This gate owns its
own store; the two agree on the field SET and — since 2026-10-05 — disagree on the
liveness term's derivation by design. See the note above, which is this contract's single
home.

Exit codes (same three the loop gate uses)
  0   digest equal to the stored one AND no moved bucket set AND no actionable row in the
      stored classification AND the stored parked set still matches the tracked rows —
      nothing changed, dispatch no agent
  10  digest differs, OR a staged bucket set differs from the stored one, OR the stored
      classification places a row in an actionable bucket, OR a row's owner moved
      `live` <-> `parked` since the stored table was rendered, OR the record cannot say
      what that set was, OR any fail-open case fired — run the full sweep

      ⚠️ The actionable-row clause is not a passenger on the digest, and it cannot be one.
      Ready-to-start is a STEADY state: `digest_of()` hashes status, phase, the Progress
      hash, session, liveness and stuck, and none of them moves when a row merely sits
      approved and unstarted. Measured 2026-10-01 on `Managers Spawn Interactive Claude
      Workers in the Cluster`: NO-CHANGE since 18:04 with 2 ready-to-start and 1
      waiting-approval row on the board, 0 agents dispatched — so the act leg never ran and
      the rows never moved. The caller's own `bucket_sets` is the only place a bucket
      exists (`digest_of` covers the tracked set, and the snapshot schema has no bucket
      concept), so the clause reads that half and suspends the saving for exactly as long
      as actionable work is waiting. The saving returns on its own: a row the act leg
      actually opened reclassifies as progressing on the next sweep.

      ⚠️ ONE bucket, not two, and the second is the trap. `waiting-approval` looks
      actionable and is not: the row sits at `phase: todo`, and the only thing that moves
      it is the operator's `vault-cli task approve` — which the act leg is forbidden to
      run. Sweeping cannot clear it, so counting it does not stop the gate sleeping while
      work waits; it makes the no-change verdict UNREACHABLE for any topic carrying an
      approval queue, which is a managed topic's normal state. Measured 2026-10-03 on
      `Manager Layer`: 23 `waiting-approval` rows, `--check` answering CHANGE with this
      clause's reason on a tree whose digest had not moved — the clause permanently true,
      not intermittently. `ACTIONABLE_BUCKETS` therefore holds `ready-to-start` alone.

      ⚠️ The parked-set clause is a THIRD reason of the same shape, and it is the one that
      keeps the replay honest. `digest_of` hashes a DEATH-only liveness term, so a worker
      opening or closing a gate moves no digest input — while its row's Status cell really
      does move (`⌛ waiting-on-human` for a parked row, its ordinary bucket for a live
      one). Without this clause the exit-0 branch would print a stored table containing a
      cell that is no longer true. It is deliberately NOT a digest term, for the reason the
      sibling clause is not one: hashing the raw liveness word again would also refuse the
      replay, at the ~150k-token act-leg cost the collapse exists to remove. See
      `parked_names` and `parked_replay_reason` — and note that a record written before the
      field existed cannot say what the set was, which is a CHANGE rather than a replay,
      self-healed by the next `--save`.

      The per-bucket half is a save input in its own right, not a passenger on the digest:
      `digest_of()` covers the tracked set only, so a corrected re-stage against an
      unchanged tree used to return 0 and be discarded — leaving a bad `bucket_sets` in
      the store until the tree next moved. `--save` therefore also writes when the staged
      set differs from `stored_bucket_sets`. The digest itself is unchanged by this: it
      stays a pure function of the tracked set.
  2   usage error

Fail-open, by design
--------------------
A missing state file (first-ever run), an unreadable state file, a parse error, an
unresolvable subject, and a failed write all force a full sweep rather than reporting
"no change". A gate that silently reports "no change" when it cannot tell makes a
manager blind to its own subject, which is the one failure worth spending a dispatch
to avoid. A failed `--save` is the same shape: it returns CHANGE rather than passing
quietly, because failing to record the digest must never read as "no change" next run.

Digest inputs — verbatim from the loop gate
  per tracked task: name, status, phase, claude_session_id, a hash of its `# Progress`
  section, a DEATH-ONLY liveness term, and its stuck verdict — plus the sorted
  member-name list, so a task added to or removed from the tracked set moves the digest
  even if every remaining task is untouched.

⚠️ The liveness term is DEATH-ONLY, not the Session-column word. It was the raw word until
2026-10-05, and that made a worker starting or ending a turn — `live` <-> `parked` — move
the digest, so the gate answered CHANGE on ticks where no row's eligibility moved. See
`liveness_change_term`: churn must not authorise a dispatch, death still must.

⚠️ mtime is deliberately EXCLUDED as a raw digest input. A touch with no content change
is not a change the sweep reports, and including it would wake the model on every
unrelated rewrite.

⚠️ It reaches the digest through `stuck` alone, and that is deliberate rather than a
leak. Branch (b) of the stuck rule reads the task file's mtime — it IS the rule's
*"task file unchanged"* measure — so for the rows that rule actually measures (idle, in
`phase: execution`, holding an open box, not parked) a rewrite is not unrelated: it
resets the very clock the rule reads. Every other row is untouched, because
`apply_stuck` sets `stuck = False` for it and its mtime never enters the digest at all.

⚠️ Liveness is in the digest ON PURPOSE, and this is the criterion the whole gate is
graded on. A worker dying changes session liveness; a digest over `status`/`phase`/mtime
alone would see the dead worker's task as unchanged and replay the stored table straight
over the death. Liveness is therefore computed HERE, in the gate — never inside the
dispatched `manager-sweep-reader` agent, which is forbidden to probe liveness
(`agents/manager-sweep-reader.md`) and could not see it anyway.

⚠️ **But only its DEATH half belongs in the digest, and the two must not be conflated.**
"Liveness is in the digest" is true of the death transition and false of the churn: a
worker opening or closing a gate flips `live` <-> `parked` several times a turn, and
hashing the raw word made that churn authorise a dispatch it could not inform. The digest
carries `liveness_change_term`, which is identical for every ALIVE verdict and moves only
when an owner goes absent — so this criterion is still graded on death, and is no longer
graded on churn.

⚠️ The liveness verdict must not be over-eager, and that is a graded case too. Deciding
"dead" by heartbeat age alone satisfies "a dead worker is reported" while mis-reporting
live workers as dead. The registry is consulted first and is authoritative when it holds
a live pid; the heartbeat is the fallback for headless workers the registry structurally
cannot see. Both halves are exercised by the paired probe in the task's SC3.

Liveness is model-free and read from plain files
------------------------------------------------
  `~/.claude/sessions/<pid>.json` — the registry. Pruned on exit, so a record with a live
  pid is a POSITIVE signal; absence proves nothing (a headless worker has none).
  `<XDG_STATE_HOME>/claude-supervisor/live/<sid>.json` — the headless heartbeat, written
  by this plugin's own `server/heartbeat.mjs`. The verdict is the stamp's AGE against the
  TTL, never the file's existence: a server killed with `kill -9` never clears its stamps,
  so a stale file is a DEAD worker.
  `~/.claude/state/attention/*.needs.json` — the park probe, using the feed's own rule. A
  park is a RAISED gate; `state: "open"` alone is not a park (the dominant record shape is
  `kind: idle, state: open`, "turn ended, waiting for a prompt").

Both subject kinds
------------------
The loop gate is `--topic`-required and fail-opens on a goal, which is correct there (its
remedy is topic-only by an already-taken decision). This gate is not topic-only: both
commands it serves auto-detect a goal OR a topic, so a goal-branch manager must get the
same saving rather than fail-opening on every run while reading as armed.

State
-----
`~/.claude/state/manager-predispatch/<slug>.json`, keyed on the subject slug. Never inside
a repo — a file in a repo tree trips dirty-tree checks during somebody else's commit. The
stored table is what a no-change run replays, so the operator still sees the frame on
every run.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import re
import select
import sys
import time
import unicodedata
from datetime import datetime

STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_PREDISPATCH_STATE_DIR", "~/.claude/state/manager-predispatch")
)
REGISTRY_DIR = os.path.expanduser("~/.claude/sessions")
FEED_DIR = os.path.expanduser("~/.claude/state/attention")

# The headless-worker heartbeat store, written by this plugin's `server/heartbeat.mjs`.
HEARTBEAT_DIR = os.environ.get("SUPERVISOR_HEARTBEAT_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME")
    or os.path.join(os.path.expanduser("~"), ".local", "state"),
    "claude-supervisor",
    "live",
)
# Mirrors HEARTBEAT_TTL_MS in the server, exactly as the loop gate mirrors it. Two copies of
# one number, paid knowingly: this gate runs model-free and must not depend on a checkout
# being present, so it cannot import the constant. Changing one without the other fails in
# the safe direction — a longer TTL here reads a dead worker live, suppressing a report
# rather than inviting a duplicate spawn.
HEARTBEAT_TTL_SECONDS = 60

EXIT_NOCHANGE = 0
EXIT_CHANGE = 10
EXIT_USAGE = 2
# A stdin that never EOFs is neither a usage error nor the gate's verdict: nothing was
# read, so nothing was decided, and a caller must be able to tell that apart from both
# "unchanged" (0) and "changed" (10). Kept distinct from EXIT_USAGE deliberately — a
# caller branching on 2 is looking at an argument it got wrong, and this one is about the
# caller's stdin, which no argument describes.
EXIT_STDIN_TIMEOUT = 3
# `--write-tracked` is a different verb from the gate's three modes, so its success code
# is named separately even though it is also 0. Reusing EXIT_NOCHANGE would read to a
# caller branching on the gate's contract as "the digest is unchanged, dispatch no agent"
# — which is the opposite of what happened: a fresh set was just written.
EXIT_WRITE_OK = 0
# `--compare-tracked` reports a *disagreement between two memberships*, which is neither
# the gate's "changed" verdict nor a usage error. It shares 10 because the caller's
# response is the same shape — stop and look rather than proceed — while staying a
# separate name so a reader cannot mistake it for the digest verdict.
EXIT_DIVERGENT = 10

STUCK_SECONDS = 30 * 60

LIVENESS_LIVE = "live"
LIVENESS_PARKED = "parked"
LIVENESS_NONE = "none"

NO_CHANGE_MARKER = "NO-CHANGE"


# --------------------------------------------------------------------------- #
# vault reading
# --------------------------------------------------------------------------- #

_FM = re.compile(r"^---\n(.*?)\n---", re.S)
_PROGRESS = re.compile(r"^# Progress\s*\n(.*?)(?=\n# |\Z)", re.S | re.M)
_CHECKBOX = re.compile(r"^\s*-\s*\[([ xX/])\]", re.M)
_LIST_ITEM = re.compile(r"^\s*-\s*\[\[(.+?)\]\]")


def split_frontmatter(text: str) -> str | None:
    m = _FM.match(text)
    return m.group(1) if m else None


def fm_scalar(fm: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.M)
    return m.group(1).strip() if m else ""


def unescape_scalar(text: str) -> str:
    """Undo YAML's `''` escape for an apostrophe inside a single-quoted scalar.

    The vault writes these lists as `- '[[Name]]'`, and a name carrying an
    apostrophe is stored as `- '[[Supervisor''s spawn_agent]]'`. A raw regex
    returns the still-escaped `Supervisor''s spawn_agent`, which resolves to no
    page — and "no page" is the UNMET direction, so a COMPLETED blocker would
    still render blocked-upstream. Unescaping is a no-op for every name that
    carries no `''`, which is every other name in the vault.

    ⚠️ **Applied to the FRONTMATTER side of a cross-file name comparison, and
    only that side.** `fm_wikilinks` has two callers — `blocked_by` and `goals` —
    and the `goals` half is compared against the topic page's `## Goals` list
    (`declared_members` via `_LIST_ITEM`). That side reads markdown, not YAML, so
    it never carries the `''` escape and is deliberately NOT unescaped: doing it
    there would be a no-op, while doing it on neither side leaves a frontmatter
    `Bob''s goal` matching nothing. The asymmetry is the fix, not an oversight —
    a name carrying no `''` is unaffected either way, which is every other name
    in the vault. `TestBlockedByRead` pins the escape on `blocked_by`, and
    `test_escaped_apostrophe_is_unescaped_for_goals_too` pins the widening to
    `goals:` — so moving this call out of `fm_wikilinks` fails a test rather than
    going unnoticed.
    """
    return text.replace("''", "'")


# The unmet entry reported when `blocked_by` declares items and none parses as a
# name. Not a page name, deliberately — it names the CONDITION, so the reader's
# `⏸️ BLOCKED UPSTREAM:` line says what is wrong instead of naming a blocker that
# does not exist.
UNPARSED_BLOCKER = "<unparsed blocked_by entry>"


def fm_list_body(fm: str, key: str) -> str | None:
    """The text `key:` declares its items in — inline or block — else None.

    The ONE place the two list shapes are recognised. `fm_wikilinks` extracts names
    from what this returns; `blocked_by_verdict` ALSO asks whether it is non-empty,
    so it can tell "declares no blocker" from "declares a blocker that did not parse
    as a name". Those two answers must never come from separate shape logic: a shape
    this extractor misses would otherwise read as *declares nothing*, and an empty
    list satisfies the `ready-to-start` clause vacuously — the unsafe direction.

    `None` means the key is absent, or present with no items at all (`key:` alone,
    or `key: []`). A non-empty return that yields no `[[...]]` names is a different
    thing entirely, and the caller is the one that has to tell them apart.
    """
    m = re.search(rf"^{re.escape(key)}:(.*)$", fm, re.M)
    if not m:
        return None
    rest = m.group(1)
    if rest.strip() and rest.strip() != "[]":
        return rest
    block = re.search(rf"^{re.escape(key)}:\s*\n((?:[ \t]+-.*\n?)*)", fm, re.M)
    if not block:
        return None
    return block.group(1) or None


def fm_wikilinks(fm: str, key: str) -> list[str]:
    """All [[...]] under `key:`, whether inline (`key: ['[[X]]']`) or block.

    All three shapes the vault writes must resolve: inline list, block list, and
    empty/absent. Block form is `key:` then indented `- '[[X]]'` lines. YAML
    escaping is undone before the names are returned — see `unescape_scalar`.
    """
    body = fm_list_body(fm, key)
    if body is None:
        return []
    return [unescape_scalar(h) for h in re.findall(r"\[\[(.+?)\]\]", body)]


# The one vault layout this script gates, hardcoded rather than caller-supplied: a typo'd
# directory would let it classify every row against an empty tree, which reads as "no
# blockers found" rather than as an error. Named once because it now has three call sites
# and the failure it produces differs by site — on the classify path an empty tree hides
# blockers, on the write path (`--write-verdicts`) it stores every verdict name exactly as
# the caller spelled it, which is the defect that path's name derivation exists to remove.
TASKS_DIRNAME = "25 Tasks"


def task_index(tasks_dir: str) -> dict[str, str]:
    """`{lowercased filename: actual filename}` for `tasks_dir`, built once per run.

    Built once because `resolve_task_file` is called per row AND per blocker entry;
    a `listdir` inside that loop is O(rows x entries) full scans of a directory that
    runs to thousands of files. Hoisting it leaves the per-row `open` as the only
    per-row I/O, and the lookup is identical.
    """
    try:
        return {entry.lower(): entry for entry in os.listdir(tasks_dir)}
    except OSError:
        return {}


def tasks_index_for_write(tasks_dir: str) -> dict[str, str]:
    """`task_index`, but it SAYS SO when nothing resolved.

    `task_index` collapses "unreadable" and "empty" into the same `{}`, which is right for
    its other callers — an unreadable directory and an empty one both mean *nothing
    resolves*, and neither path can act on the difference. On the **write** path they are
    not the same fact, and both are the fact that matters: an empty index stores every
    verdict name exactly as the caller spelled it, which is precisely the defect this
    writer's name derivation exists to remove. Degrading silently back to the pre-fix
    behaviour is the one outcome that must be visible, so this reports it **whichever way
    the index came back empty** — a listing that raised, and a listing that succeeded and
    found nothing. The file already names its other degradations on stderr (the
    grandfathered-row note below).

    ⚠️ **Warning on the empty case is not pedantry.** An empty-but-readable `25 Tasks` is a
    real state — a mistyped `--vault`, a vault whose layout differs — and it produces
    exactly the same silent unnormalised write as an unreadable one. Guarding only the
    `OSError` branch would make the note a property of *how* the directory failed rather
    than of the outcome, which is the distinction that matters here.
    """
    try:
        entries = os.listdir(tasks_dir)
    except OSError as exc:
        # `exc.strerror or exc`, matching `load_stored` below: an OSError raised without a
        # message would otherwise render a literal `None` in the note.
        print(
            f"note: cannot read {tasks_dir} ({exc.strerror or exc}) — every verdict name is "
            "stored as sent, unnormalised",
            file=sys.stderr,
        )
        return {}
    if not entries:
        print(
            f"note: {tasks_dir} is empty — every verdict name is stored as sent, "
            "unnormalised",
            file=sys.stderr,
        )
    return {entry.lower(): entry for entry in entries}


def resolve_task_file(
    tasks_dir: str, name: str, index: dict[str, str] | None = None
) -> str:
    """`name`'s task file under `tasks_dir`, matched case-insensitively.

    The rule this read replaces specified **case-insensitive** resolution, and the
    reason it must be explicit rather than left to the filesystem is that APFS is
    case-insensitive: `open()` succeeds on a case-mismatched name here, so an
    exact-only build passes every local check and blocks permanently the moment it
    meets a case-sensitive filesystem. This repo's own CHANGELOG records the same
    asymmetry from the other side — a single capitalised letter passed unnoticed
    because `open()`, Obsidian's link resolver and `ls` all agree on APFS.

    Returns the directory entry's OWN spelling when one matches, never the case
    variant that was asked for: the canonical name is what makes this testable on
    the very filesystem that hides the defect, and a path that opens here is not
    evidence the lookup is right. Falls back to the plain join on a miss, which is
    the path a genuinely absent blocker must take — `open()` refuses it, and
    "cannot verify it is done" is the unmet direction.

    ⚠️ **Both halves of this mode go through here** — the row being classified and
    every blocker it names. Resolving only the blockers would leave the row on a
    plain join and so leave the very asymmetry this helper exists to remove: a
    case-mismatched tracked name would read `unreadable` on a case-sensitive
    filesystem. That fails toward a missed dispatch rather than a wrong spawn, so it
    is safe — but the two halves disagreeing is how a reader comes to trust one and
    not the other.
    """
    idx = task_index(tasks_dir) if index is None else index
    entry = idx.get(f"{name}.md".lower())
    return os.path.join(tasks_dir, entry if entry else f"{name}.md")


def cell(text: str) -> str:
    """A value safe to place in one tab-separated column of `--blocked-verdicts`.

    A task name is frontmatter-controlled and may carry a tab, which would shift
    every column after it for any consumer that splits on tabs — the reader's own
    reporting rule, and the tests. Newlines cannot reach here (`fm_wikilinks`'
    capture is `.+?` without DOTALL), but collapsing them costs nothing and keeps
    the guarantee in one place rather than resting on a regex two functions away.
    """
    return text.replace("\t", " ").replace("\n", " ")


def blocked_by_verdict(
    fm: str, tasks_dir: str, index: dict[str, str] | None = None
) -> dict:
    """Resolve every `blocked_by` entry against `tasks_dir`.

    The deterministic half of the sweep reader's `ready-to-start` /
    `blocked-upstream` decision, and the reason it is code rather than a clause:
    a first-entry-only read lands a genuinely blocked row in `ready-to-start`,
    which is the spawn offer, and a prose warning has twice failed to pin that
    read (v0.96.4 shipped the warning; the defect reproduced 2026-10-03 18:44).

    EVERY entry is evaluated, never just the first. A blocker whose file is
    missing, unreadable, or carries no parseable status counts as NOT completed —
    "cannot verify it is done" reads as blocked, never as permission to start.
    One status read per entry, and a blocker's own `blocked_by` is never
    followed, so a dependency cycle terminates.

    Returns `{"entries": [...], "unmet": [...], "blocked": bool}`. An empty
    `blocked_by` is unblocked — and that is exactly why an EMPTY read is not
    neutral: it satisfies the `ready-to-start` clause vacuously.
    """
    entries = fm_wikilinks(fm, "blocked_by")
    if not entries and fm_list_body(fm, "blocked_by"):
        # The key declares something and NOT ONE item parsed as a name. That is not
        # "declares no blocker": `blocked_by` entries are names OR `[[wikilinks]]`,
        # so a bare name is a legal entry this extractor does not resolve — and an
        # entry we cannot read is one we cannot verify. Reporting it as an empty list
        # would satisfy the `ready-to-start` clause VACUOUSLY, which is the exact
        # promotion this mode exists to stop, and the only place it could still fail
        # in the unsafe direction.
        return {
            "entries": [],
            "unmet": [UNPARSED_BLOCKER],
            "blocked": True,
        }
    unmet: list[str] = []
    for name in entries:
        try:
            with open(
                resolve_task_file(tasks_dir, name, index),
                encoding="utf-8",
                errors="replace",
            ) as fh:
                blocker_fm = split_frontmatter(fh.read())
        except OSError:
            blocker_fm = None
        if fm_scalar(blocker_fm or "", "status") != "completed":
            unmet.append(name)
    return {"entries": entries, "unmet": unmet, "blocked": bool(unmet)}


def progress_hash(text: str) -> str:
    r"""A hash of the task's `# Progress` section — the sweep's change signal.

    Hashing the section is what lets the digest move on a Progress write that leaves
    status and phase alone, and what lets the stuck verdict honestly assert "no Progress
    entry" rather than "no frontmatter change".

    ⚠️ **The derivation is stated here so a reader outside this module can reproduce a
    stored value instead of guessing at one** — the value is persisted in every snapshot
    and is meaningless to a reader who cannot recompute it. It is `sha256` over the **raw
    bytes of the section body**, truncated to the **first 16 hex characters**. The body is
    `_PROGRESS`'s group 1, and each of its two boundaries sits one byte off the obvious
    reading — which is why they are spelled out here rather than described:

    - **Start — after the `# Progress` heading line**, which is itself not hashed. ⚠️ What
      decides this is `\s*` **greed, not heading-ness**: `\s*` is greedy and gives back
      exactly one newline for the literal `\n` that follows it, swallowing all remaining
      whitespace before the body. One root cause, three consequences: a section that is
      empty and followed by anything — a heading, **or plain prose** — does NOT start empty
      but starts at that content; `# Progress` alone at EOF *does* start empty; and a `#`
      with no space after it (`#Other`) does not end a section at all, because the
      lookahead requires a literal `\n# `. Only an absent section, or one that runs to EOF,
      hashes the empty string.
    - **End — immediately BEFORE the newline that precedes the next top-level `# `
      heading.** That delimiter newline is not part of the body, so `foo\n\n# Other`
      hashes `foo\n` (the blank line is dropped) and `foo\n# Other` hashes `foo` (the
      content line's own trailing newline goes with it).
    - **Nothing else is normalised** — no YAML parse, no re-serialisation — and the text is
      read from the file rather than from a markdown re-render.

    ⚠️ **The empty-section backtrack is not a curiosity — it is a false "progress" signal,
    and that is the cost the start boundary carries.** This function gates the `busy_since`
    reset (`prev.get("progress") == t["progress_hash"]`) and is itself a digest input, so a
    task whose `# Progress` is empty and followed by another section reports *Progress
    activity* whenever that **following** section is edited — resetting the stuck clock on
    a worker that did nothing, and moving the gate's digest. Surfaced 2026-10-06 by
    `ben-s-pull-request-reviewer`; pre-existing rather than introduced by this docstring,
    and left alone here because fixing it is a behaviour change, not a documentation one.

    ⚠️ **The boundaries are the half a reader gets wrong, and the error is silent in the
    expensive direction.** Measured 2026-10-01: a reader that hashed the section
    *including* its `# Progress` heading reported a mismatch against the stored value for
    **37 of 38** tracked rows — a near-total false-positive rate that reads as movement
    on every row rather than as one wrong boundary.
    """
    m = _PROGRESS.search(text)
    return hashlib.sha256((m.group(1) if m else "").encode()).hexdigest()[:16]


def checkbox_count(text: str, section: str | None = None) -> str:
    """`n/m` ticked-vs-total checkboxes, optionally scoped to a `# <section>`.

    A section with no checkboxes returns `—` rather than `0/0`, which would read as a
    real reading of nothing.
    """
    body = text
    if section:
        m = re.search(rf"^# {re.escape(section)}\s*\n(.*?)(?=\n# |\Z)", text, re.S | re.M)
        if not m:
            return "—"
        body = m.group(1)
    boxes = _CHECKBOX.findall(body)
    if not boxes:
        return "—"
    return f"{sum(1 for b in boxes if b in 'xX')}/{len(boxes)}"


def session_id_set(fm: str) -> list[str]:
    """The row's WHOLE id set: `claude_session_id` plus every `metrics_sessions` id.

    ⚠️ **Extracted unanchored, deliberately.** `metrics_sessions` entries are indented
    (`    - session_id: …`), so a line-anchored `^session_id:` match collects only the
    frontmatter id and silently reproduces the very bug this exists to fix. Measured
    2026-10-01 in the Brogrammers vault: 64 of the 124 tasks carrying a `metrics_sessions`
    block carry no `claude_session_id` at all, so the single-field read left every one of
    them reading `none` while a live id sat in the next key.
    """
    ids: list[str] = []
    primary = fm_scalar(fm, "claude_session_id").strip("'\"")
    if primary:
        ids.append(primary)
    for sid in re.findall(r"^\s*-\s*session_id:\s*(\S+)", fm, re.M):
        sid = sid.strip("'\"")
        if sid and sid not in ids:
            ids.append(sid)
    return ids


def read_task(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
            # `fstat` on the descriptor we just read, never a second `stat(path)`: the
            # path could be replaced between the two calls, and the idle clock has to
            # describe the file whose contents we actually hashed.
            mtime = os.fstat(fh.fileno()).st_mtime
    except OSError:
        return None
    fm = split_frontmatter(text)
    if fm is None:
        return None
    return {
        "name": os.path.basename(path)[:-3],
        "status": fm_scalar(fm, "status"),
        "phase": fm_scalar(fm, "phase"),
        "session": fm_scalar(fm, "claude_session_id").strip("'\""),
        "sessions": session_id_set(fm),
        "goals": fm_wikilinks(fm, "goals"),
        "progress_hash": progress_hash(text),
        "met": checkbox_count(text),
        "mtime": mtime,
    }


def declared_members(topic_path: str) -> list[str]:
    """Read a topic page's `## Goals` list — declared membership, never inferred.

    Only list items count. Prose wikilinks inside the section are NOT members: a topic
    page names sibling pages inside a bullet's prose, and a naive `[[...]]` sweep over
    the section admits those as members.
    """
    with open(topic_path, encoding="utf-8") as fh:
        text = fh.read()
    # The section ends at the next `##` or `#` — NOT at a `###`. A topic page may
    # carry `###` prose notes or phase sub-headings INSIDE `## Goals` (measured
    # 2026-10-06: `23 Topics/Unattended Execution.md` splits its membership into
    # `### Phase 1` / `### Phase 2`), and terminating on `###` silently truncated
    # the member list — 52 of 59 entries on `Manager Layer`, and 0 of 4 on
    # `Unattended Execution`. This boundary matches `fleet-board.py`, which has
    # always stopped at `## ` and so never truncated.
    #
    # ⚠️ The same lookahead is duplicated in `reset.py` (same repo, mirrored below)
    # and in the VAULT's own gate at `<vault>/.claude/scripts/sweep-gate.py`. That
    # third copy is NOT in this repository — it lives in `bborbe/obsidian-personal`
    # — so it cannot be found by a grep from here, and a reader must not conclude
    # the trio is incomplete when `find` turns up only two.
    m = re.search(r"^## Goals\s*\n(.*?)(?=\n## |\n# |\Z)", text, re.S | re.M)
    if not m:
        raise ValueError(f"no `## Goals` section in {topic_path}")
    members = []
    for line in m.group(1).splitlines():
        hit = _LIST_ITEM.match(line)
        if hit:
            members.append(hit.group(1).strip())
    return members


def resolve_tracked(vault: str, members: list[str]) -> list[dict]:
    """A member may be a goal (admits tasks whose `goals:` name it) or a task.

    Vault-only and pure: it never touches the registry, so a fixture can run it against a
    throwaway vault and get the same values the live run gets.
    """
    tasks_dir = os.path.join(vault, TASKS_DIRNAME)
    member_set = set(members)
    tracked: list[dict] = []
    for entry in sorted(os.listdir(tasks_dir)):
        if not entry.endswith(".md"):
            continue
        task = read_task(os.path.join(tasks_dir, entry))
        if task is None:
            continue
        if task["name"] in member_set:  # declared directly as a task entry
            tracked.append(task)
        elif member_set.intersection(task["goals"]):
            tracked.append(task)
    return sorted(tracked, key=lambda t: t["name"])


def page_type_of(path: str) -> str:
    """The `page_type:` frontmatter value, scoped to the frontmatter block.

    Unscoped it would match a guide's YAML template quoted in prose.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fm = split_frontmatter(fh.read())
    except OSError:
        return ""
    return fm_scalar(fm, "page_type") if fm else ""


# The two directories `resolve_subject` reads. A root holding neither is not a vault this
# gate can read, and saying so beats reporting every subject as an unresolvable one.
VAULT_MARKERS = ("24 Goals", "23 Topics")


def vault_root_error(vault: str) -> str | None:
    """-> a message when `vault` is not a vault root, else None.

    A vault NAME is a relative path, so it resolves against the caller's cwd: from the
    vault's own directory it names nothing and every subject reads as a missing page,
    while from the vault's parent it silently resolves to whatever `<cwd>/<name>` is.
    Both are caller errors, and reporting either as an unresolvable *subject* sends the
    reader hunting for a page that is present on disk. Caught at the boundary so the
    subject message below stays true to its own words.
    """
    if not os.path.isabs(vault):
        return (
            f"--vault must be an absolute path: {vault!r} — a relative path (a vault "
            f"NAME included) resolves against the caller's cwd, so the same call gates a "
            f"different tree depending on where it runs"
        )
    if not os.path.isdir(vault):
        return f"--vault is not a directory: {vault!r}"
    if not any(os.path.isdir(os.path.join(vault, m)) for m in VAULT_MARKERS):
        return (
            f"--vault is not a vault root: {vault!r} — it holds no "
            + " or ".join(repr(m) for m in VAULT_MARKERS)
        )
    return None


def resolve_subject(vault: str, subject: str) -> tuple[str, list[str]]:
    """-> (branch, members). Raises ValueError unless exactly one page resolves.

    Both commands auto-detect goal vs topic; this gate must resolve the same way or it
    would gate a different tree than the command sweeps. Both-match and no-match are both
    refusals — never a silent preference, which is the failure the commands' own
    resolution section exists to prevent.

    The goal branch keys on the folder alone; the topic branch additionally requires
    `page_type: topic`. The asymmetry is measured, not stylistic. `page_type` was required
    on both sides to reject the convention guides that describe these page kinds, but
    those guides live outside the folders and an exact `-iname "$SUBJECT.md"` match inside
    the folder already excludes them. Measured 2026-09-27 in the primary vault: **88 of 258** goal
    pages carried no `page_type: goal` and were therefore unresolvable to this gate *and*
    to the commands that defer to it, while **11 of 11** topic pages carried
    `page_type: topic`. Requiring a field a third of the goal pages lack bought no
    discrimination and cost the whole goal branch.
    """
    goal_path = os.path.join(vault, "24 Goals", f"{subject}.md")
    topic_path = os.path.join(vault, "23 Topics", f"{subject}.md")
    is_goal = os.path.exists(goal_path)
    is_topic = os.path.exists(topic_path) and page_type_of(topic_path) == "topic"
    if is_goal and is_topic:
        raise ValueError(f"ambiguous subject: both {goal_path} and {topic_path} resolve")
    if is_goal:
        return "goal", [subject]
    if is_topic:
        return "topic", declared_members(topic_path)
    raise ValueError(f"no subject page for {subject!r} in 24 Goals/ or 23 Topics/")


# --------------------------------------------------------------------------- #
# liveness — model-free, from the registry, the heartbeat store and the feed
# --------------------------------------------------------------------------- #

PARKED_VERBS = ("later (on ",)
_ENTITY = re.compile(r"&(?:nbsp|#160|#xa0);", re.I)

# Distinguishes "read the store for me" from a caller passing its own verdict — including
# one that is `None`, which is a real answer ("could not read") and must not be mistaken
# for "you read it".
_HEARTBEAT_UNREAD = object()


def heartbeat_live(sid: str, now: float | None = None) -> bool | None:
    """True / False / None for one session id. None is "could not read", never "not live".

    ⚠️ The verdict is the stamp's AGE against the TTL, never the file's existence — see the
    module docstring. None and False stay distinct so an I/O error is never folded into a
    confident negative.
    """
    now = time.time() if now is None else now
    try:
        age = now - os.stat(os.path.join(HEARTBEAT_DIR, f"{sid}.json")).st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return age < HEARTBEAT_TTL_SECONDS


_LIVENESS = None


def _session_liveness():
    """Import session-liveness.py (hyphenated filename -> importlib) — the one registry reader."""
    global _LIVENESS
    if _LIVENESS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-liveness.py")
        spec = importlib.util.spec_from_file_location("session_liveness", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIVENESS = mod
    return _LIVENESS


_DECLARED_WAIT = None


def _declared_wait():
    """Import declared-wait.py — the shared `⏰ Ends:` reader.

    ⚠️ **Shared, not re-derived.** `agents/manager-drive.md:134` forbids *"a fourth
    definition of 'idle'"*, and `scripts/fleet-board.py` reads the same slot through this
    same module. A private copy here would be exactly the drift that rule names.
    """
    global _DECLARED_WAIT
    if _DECLARED_WAIT is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "declared-wait.py")
        spec = importlib.util.spec_from_file_location("declared_wait", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _DECLARED_WAIT = mod
    return _DECLARED_WAIT


def read_registry() -> dict[str, dict]:
    """`~/.claude/sessions/<pid>.json` -> {sessionId: {pid, status, name, alive}}.

    The registry is pruned on exit, so a record with a live pid is a positive signal. A
    missing record proves nothing: a headless worker has none.

    ⚠️ **One reader for the whole plugin.** The glob, the pid check and the `alive` rule now
    live in `scripts/session-liveness.py`. A second instrument over the same registry is what
    let an 8-char prefix read as `ABSENT` on 2026-09-26 while its session was live, and this
    reader was one of the copies.

    ⚠️ **`None` is flattened to `{}` here, preserving this caller's existing contract.** An
    unreadable registry therefore reads as "no live session" — the dangerous direction the
    shared reader refuses. Recorded as a residual rather than changed silently inside a
    conversion: flipping it is a rule change for this caller, not a refactor.
    """
    return _session_liveness().read_registry(REGISTRY_DIR) or {}


def read_feed() -> dict[str, dict]:
    """`~/.claude/state/attention/*.needs.json` -> {sessionId: record}."""
    out: dict[str, dict] = {}
    for path in glob.glob(os.path.join(FEED_DIR, "*.needs.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        sid = d.get("session_id")
        if sid:
            out[sid] = d
    return out


def is_open_gate(rec: dict | None) -> bool:
    """The attention feed's own park rule. A park is a RAISED gate the operator has not
    answered — `state: "open"` alone is not a park, since the dominant record shape is
    `kind: idle, state: open`, meaning "turn ended, waiting for a prompt"."""
    if not rec:
        return False
    if rec.get("state") == "answered":
        return False
    if rec.get("kind") not in ("permission", "question"):
        return False
    detail = " ".join(_ENTITY.sub(" ", rec.get("detail") or "").split())
    return not detail.startswith(PARKED_VERBS)


def liveness_of_sid(
    sid: str, registry: dict, feed: dict, heartbeat: bool | None = _HEARTBEAT_UNREAD
) -> str:
    """live / parked / none for ONE session id, from the registry PLUS the heartbeat.

    The registry answers for every session that holds a socket — every interactive one —
    and the heartbeat answers for the headless workers it structurally cannot see.
    Neither substitutes for the other, and the registry is consulted FIRST: it carries a
    real pid and a real status, so it outranks a heartbeat age threshold and is what keeps
    a live worker with a stale heartbeat from being reported dead.
    """
    if not sid:
        return LIVENESS_NONE
    # ⚠️ **A declared wait parks a LIVE row, and only a live one.** `⏰ Ends:` names a
    # machine the worker is waiting on — a dependency, which is what this file's park
    # rule exists to cover — read through the shared reader so this file cannot drift
    # from `scripts/fleet-board.py`'s own reading. It is applied only at the two park
    # points below and never before them: a session that is dead and once declared a
    # wait is still dead, and parking it here would resurrect it.
    declared = _declared_wait().declared_wait(sid, state_dir=FEED_DIR)
    rec = registry.get(sid)
    # ⚠️ **Only a PROVEN negative is death.** `alive` is a three-state since 2026-10-01: `None`
    # means "the pid is occupied but the record cannot prove the holder is this session" (see
    # `session-liveness.py:read_registry`). Reading that as dead here would fall through to the
    # heartbeat and then to `LIVENESS_NONE`, which is the value the auto-resume gate acts on —
    # so an unprovable record would permit a resume onto a conversation that may still be live.
    if rec is None or rec["alive"] is False:
        beat = heartbeat_live(sid) if heartbeat is _HEARTBEAT_UNREAD else heartbeat
        if beat is not True:
            return LIVENESS_NONE
        return LIVENESS_PARKED if (is_open_gate(feed.get(sid)) or declared) else LIVENESS_LIVE
    if is_open_gate(feed.get(sid)) or declared or rec["status"] == "waiting":
        return LIVENESS_PARKED
    return LIVENESS_LIVE


def _norm_session_name(name: str) -> str:
    """Casefolded, glyph-stripped session label, for the roster-name fallback.

    A roster label carries the `⚙` marker a task title does not, so a raw equality test
    never matches: measured 2026-09-30, both rows the fallback exists for held a live
    roster entry whose label was the task title *with* that prefix.
    """
    text = unicodedata.normalize("NFKC", name or "").strip()
    text = re.sub(r"^[^\w(]+", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


# A registry name is a *truncated* title, and the truncation is long — measured 46
# characters on 2026-10-07 (`⚙ A Renamed Task's Session Becomes Unaddressable` against a
# 108-character title). ⚠️ **`fleet-board.py` and `resolve-task-file.py` carry the same
# constant for the same rule and all three values must agree** — they are three readers of
# one predicate, and a drift between them would make the board, this gate and the sweep
# disagree about whether a name resolves. `test_manager_predispatch.py` pins all three.
_TRUNCATION_MIN_PREFIX = 20


def _names_the_same_session(wanted: str, held: str) -> bool:
    """Does a registry `name` denote the session whose task title is `wanted`?

    ⚠️ **Not equality, and the difference is the point.** The registry name is a
    *truncated* form of a long task title, so an equality test misses every long-titled
    task and `roster_owner` returns `none` for a row that is in fact owned.

    ⚠️ **The two errors are not symmetric, so the guard is on the HELD side.** A miss
    leaves the row ready-to-start — the pre-existing behaviour, and safe. A false match
    WITHHOLDS a legitimate spawn, which is the failure `roster_owner`'s own docstring
    warns about. So the prefix must be long enough to be a truncation rather than a
    coincidence: a short role name that happens to open a title (`boss`) does not
    qualify. An exact match is accepted at any length, so a genuinely short title still
    resolves.

    ⚠️ **There is deliberately NO exactly-one-title guard here, unlike `fleet-board.py`'s
    `has_task_file` — and it is not an omission.** `roster_owner` receives only
    `(name, registry, feed)`: it holds no vault title set to be unique *against*, so an
    exactly-one check is not expressible at this layer at all. The consequence is bounded
    rather than ignored — reaching two distinct held names that both prefix-match one
    title requires two sessions on the SAME title truncated at different lengths, and
    `roster_owner` returns a liveness verdict (`LIVE` / `PARKED` / `NONE`), not an owner
    identity, so in the reachable case both candidates are live and the verdict is the
    same either way. ⚠️ **The length floor is what carries the safety here**, not
    uniqueness.

    ⚠️ **The case the paragraph above does NOT cover, named rather than argued away:** two
    *different* titles sharing a ≥20-character prefix truncate to the same held string, so
    one live session's name prefix-matches **both** rows and the genuinely unowned one is
    withheld — the false-match direction this docstring calls the worse error. Unlikely at
    the measured 46-character truncation, but reachable at the 20-character floor.
    """
    # ⚠️ **Both sides normalized here, not just `held`.** `roster_owner` already normalizes
    # what it passes, so this is idempotent for it — but a second caller handing in a raw
    # title would otherwise get a silent no-match, which is the failure this whole rule
    # exists to remove.
    wanted = _norm_session_name(wanted)
    held = _norm_session_name(held)
    if not wanted or not held:
        return False
    if held == wanted:
        return True
    return len(held) >= _TRUNCATION_MIN_PREFIX and wanted.startswith(held)


def roster_owner(name: str, registry: dict, feed: dict) -> str:
    """The SUBORDINATE fallback for a row whose id set is EMPTY.

    A row with no id at all cannot be probed — there is no id to put to the registry or
    to the heartbeat — so a `none` verdict there says nothing about ownership, and
    reading it as *unowned* is what opens the duplicate spawn this gate exists to prevent.

    ⚠️ **One-directional by construction, and that is what keeps it safe.** A roster is
    empty-not-absence, so a MISS proves nothing and the row stays ready-to-start exactly
    as it does today. This can only ever REMOVE a row from the offer, never add one; a
    rule that could also mark a never-started task owned would block every legitimate
    spawn.
    """
    wanted = _norm_session_name(name)
    if not wanted:
        return LIVENESS_NONE
    for sid, rec in registry.items():
        # Same rule as `liveness_of`: only a PROVEN negative is skipped. `None` is an
        # unprovable identity, and skipping it would withhold the row from the offer's
        # remove-list — leaving a task that may be owned looking spawnable.
        if rec.get("alive") is False:
            continue
        if not _names_the_same_session(wanted, rec.get("name", "")):
            continue
        return LIVENESS_PARKED if is_open_gate(feed.get(sid)) else LIVENESS_LIVE
    return LIVENESS_NONE


def liveness_of(
    sid: str,
    registry: dict,
    feed: dict,
    heartbeat: bool | None = _HEARTBEAT_UNREAD,
    sessions: list[str] | None = None,
    name: str = "",
) -> str:
    """live / parked / none for one ROW, from its WHOLE id set PLUS the roster fallback.

    The canonical key is the **session-id set** — `claude_session_id` plus every
    `metrics_sessions` id — probed for liveness, with the roster name as a SUBORDINATE
    fallback when that set is empty.

    The set is probed id by id and the first non-`none` verdict wins: one live id owns the
    row whatever the others say. Only a genuinely EMPTY set reaches the fallback — a row
    whose ids are all dead is `none` by evidence, not by absence of input.
    """
    ids = list(sessions) if sessions else ([sid] if sid else [])
    for one in ids:
        verdict = liveness_of_sid(one, registry, feed, heartbeat)
        if verdict != LIVENESS_NONE:
            return verdict
    if ids:
        return LIVENESS_NONE
    return roster_owner(name, registry, feed)


def enrich_liveness(tracked: list[dict], registry: dict, feed: dict) -> None:
    for t in tracked:
        t["liveness"] = liveness_of(
            t["session"],
            registry,
            feed,
            sessions=t.get("sessions"),
            name=t.get("name", ""),
        )


def open_box_count(met: str) -> int:
    """Open boxes from the `n/m` reading `checkbox_count` emits.

    `checkbox_count` returns `—` for a section holding no boxes. That is zero open
    boxes, not a parse failure, and the runbook's qualifier is `>=1 open box`, so both
    read the same way here.
    """
    if "/" not in met:
        return 0
    done, _, total = met.partition("/")
    try:
        return int(total) - int(done)
    except ValueError:
        return 0


def idle_stuck(t: dict, now_ts: float) -> bool:
    """Branch (b): idle past the threshold, in `execution`, holding an open box.

    Four gates, all required. The `phase: execution` and `>=1 open box` qualifiers are
    the runbook's own. The idle duration is the task file's mtime — the rule's
    *"task file unchanged"* measure, since a write to the file is precisely what makes
    it changed, and the reader's shipped branch (b) derives the same duration from the
    same signal.

    ⚠️ The WAITING guard is not decoration. The runbook is explicit that a row
    demonstrably parked on a human or a dependency is NOT stuck, and that the
    file-unchanged proxy cannot tell the two apart. The parked carrier is already
    computed in this file (`liveness_of` -> `LIVENESS_PARKED`), so a branch (b) that
    ignored it would flag exactly the workers the rule exists to leave alone.
    ⚠️ **`LIVENESS_PARKED` carries two shapes now, not one:** an open gate, and a
    session whose newest `Stop` record declares `⏰ Ends:` (read through the shared
    `scripts/declared-wait.py`). The second is the one that regressed in practice — a
    worker whose turn ended inside a `run_in_background` watcher is inactive *by
    design*, and the file-unchanged proxy read that as stuck.
    """
    if t.get("liveness") == LIVENESS_PARKED:
        return False
    if t.get("phase") != "execution":
        return False
    if open_box_count(t.get("met", "—")) < 1:
        return False
    mtime = t.get("mtime")
    if mtime is None:
        return False
    return (now_ts - float(mtime)) >= STUCK_SECONDS


def apply_stuck(tracked: list[dict], registry: dict, prev_busy: dict, now_ts: float) -> dict:
    """Set `t['stuck']` and return the busy-since map to persist.

    ⚠️ Both branches of the runbook's rule, not just the busy one.

    **(a) busy > ~30 min** — the registry gives a session's current status but not how
    long it has held it, so the clock is observed across runs; a Progress write resets
    it, which is what keeps a working worker off the stuck row.

    **(b) idle > ~30 min in `phase: execution` with an open box** — see `idle_stuck`.
    It carries no persisted map, because its duration is the task file's mtime and an
    mtime is absolute: there is no clock to observe across runs.

    The branches are disjoint — (b) is reached only when the session is not busy — so
    no row can be flagged by both.
    """
    new_busy: dict[str, dict] = {}
    for t in tracked:
        rec = registry.get(t["session"]) if t["session"] else None
        # `is not False`, not a truthiness test: `alive=None` is an unprovable identity, and a
        # busy worker misread as idle gets flagged `stuck`, which is what feeds a resume.
        busy = bool(rec and rec["alive"] is not False and rec["status"] == "busy")
        if not busy:
            t["stuck"] = idle_stuck(t, now_ts)
            continue
        prev = prev_busy.get(t["name"])
        if prev and prev.get("progress") == t["progress_hash"]:
            since = float(prev.get("since", now_ts))
            t["stuck"] = (now_ts - since) >= STUCK_SECONDS
            new_busy[t["name"]] = {"since": since, "progress": t["progress_hash"]}
        else:
            t["stuck"] = False
            new_busy[t["name"]] = {"since": now_ts, "progress": t["progress_hash"]}
    return new_busy


# --------------------------------------------------------------------------- #
# digest
# --------------------------------------------------------------------------- #


HOLDS_PATH = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSION_HOLDS", "~/.claude/state/session-holds.json")
)


def read_holds() -> dict:
    """The operator's session holds, keyed on session id. `{}` on any read failure.

    Inlined rather than shelling out to session-holds.py: the plugin's scripts do not
    import each other, and a subprocess dependency on a plugin path is a fail-open.
    Reads are lock-free because the writer lands every change through `os.replace`.

    A missing or corrupt store reads as *nothing held*. Here that direction is the safe
    one: inventing a hold would move the digest and wake the loop for a hold nobody set,
    while reading a real store as empty merely leaves the digest unchanged.
    """
    try:
        with open(HOLDS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    holds = data.get("holds") if isinstance(data, dict) else None
    return holds if isinstance(holds, dict) else {}


def is_held(session_id: str) -> bool:
    """True when this session carries a hold. Never raises."""
    if not session_id:
        return False
    return isinstance(read_holds().get(session_id), dict)


def liveness_change_term(t: dict) -> int:
    """The digest's liveness contribution: DEATH only, never churn.

    ⚠️ **The raw liveness word used to be hashed here, and collapsing it is this
    function's whole job.** `liveness_of` returns `parked` while a worker holds an open
    gate and `live` the moment it closes one, so a worker merely starting or ending a
    turn moved the digest and the gate answered CHANGE (exit 10) on a tick where no
    row's *eligibility* moved. Measured 2026-09-30: three consecutive act legs returned
    the identical decision set -- `to open 0 · to resume 0 · reaped 0 · nudged 0` -- at
    roughly 150k subagent tokens each, because the fleet's normal state is churn.

    ⚠️ **Death must survive, and that is what the gate is graded on.** The module
    docstring's *"a worker dying changes session liveness ... this is the criterion the
    whole gate is graded on"* is why the term is COLLAPSED rather than deleted: `live`
    and `parked` are both *alive* and hash identically, while an owner that has gone
    `LIVE -> ABSENT` still moves the digest. Deleting the term satisfies the churn half
    and regresses this one -- see `TestLiveness.test_a_worker_dying_still_moves_the_digest`.

    ⚠️ **It is deliberately UNGUARDED, and that is load-bearing rather than lax.** An
    earlier cut of this function returned `0` for a row with no `session` key, to keep a
    never-started row off the death term. That silently dropped death detection for every
    row whose ids live only in `metrics_sessions`: `read_task` sets `session` from the
    frontmatter `claude_session_id` ALONE (line 555) while `sessions` carries the whole
    id set (line 556, `session_id_set`), and `liveness_of` probes `sessions` FIRST with
    `roster_owner` as the fallback when that set is empty. **Any guard keyed on a subset
    of the ownership signals regresses whatever shape it excludes**, because pre-fix
    EVERY row's `live -> none` moved the digest -- so the guard's cost is a silently
    missed death, and its only benefit is suppressing a transition that cannot occur.

    ⚠️ **No guard is needed, because a never-started row cannot churn.** Such a row reads
    `liveness_of` -> `roster_owner`, which is `none` unless a live roster name matches --
    and `none` on both sides of a comparison is the same value, so it moves nothing. The
    only transitions this term can make are `alive -> absent` and `absent -> alive`, both
    real ownership changes. `TestLiveness.test_death_is_detected_for_every_ownership_shape`
    pins the shapes; `test_liveness_churn_alone_does_not_move_the_digest` pins the churn
    half.
    """
    return int(t.get("liveness", LIVENESS_NONE) == LIVENESS_NONE)


def parked_names(tracked: list[dict]) -> list[str]:
    """The rows whose rendered Status cell reads `⌛ waiting-on-human`, sorted.

    ⚠️ **This is the Status cell, not the Session cell, and the difference is the whole
    reason this function exists.** `agents/manager-sweep-reader.md:136` makes a *parked*
    row render `⌛ waiting-on-human` whatever its bucket, while a live one keeps its
    ordinary bucket — so a `live` <-> `parked` flip moves the Status cell. The Session
    cell does NOT move with it: step 7 is explicit that `parked` is ALIVE and therefore
    takes the live-shaped `[<sid8>]` delimiter, and `session-liveness.py` never reads the
    registry's `status` field, so a parked session probes LIVE.

    Read from `liveness`, which `enrich_liveness` has already populated on every row.

    ⚠️ **This reads THIS script's `liveness_of`, while the cell it protects is rendered by
    the caller's sweep reader from a different input** — `session-liveness.py --check` over
    the row's id set (`agents/manager-sweep-reader.md` input 10). The repo's two-renderer
    rule keeps those two verdicts independent on purpose, and this clause does not close
    that gap: a row the reader renders `⌛ waiting-on-human` while the gate reads `live`
    moves the cell with the parked set unchanged, and the replay is still served. Named
    here rather than left for rediscovery — it is the same class of residual as the
    Session cell's own route, and like that one it is deliberately NOT covered.
    """
    return sorted(t["name"] for t in tracked if t.get("liveness") == LIVENESS_PARKED)


def parked_replay_reason(stored: object, now: list[str]) -> str | None:
    """`None` when the stored table's parked set is still the set that would render.

    Otherwise the reason the replay is refused. ⚠️ **It returns WHICH of the two situations
    it hit, rather than a bare bool**, because "the tree moved" and "this record cannot
    say" are different facts about the store and a reader who cannot tell them apart cannot
    tell a real change from a first run after an upgrade. Evaluating the predicate once is
    also what keeps the reason line from drifting from the verdict: a caller that
    re-derived `isinstance(stored, list)` alongside this would name the wrong situation the
    moment either half was edited alone.

    `stored` not being a list means the record predates the field — the same fail-open rule
    `load_stored` applies to a missing or unreadable state file, and for the same reason: a
    gate that reports "no change" when it cannot tell is the one failure worth spending a
    dispatch to avoid. Bounded and self-healing: the next `--save` writes the field.
    """
    if stored is None:
        return "record predates the parked set — this replay cannot be trusted"
    if not isinstance(stored, list):
        # ⚠️ Distinct from the branch above on purpose. A `parked` key holding a dict or a
        # string is not a record that predates the field — it is one that cannot say either,
        # but for a different reason, and naming the wrong one sends a reader hunting for an
        # upgrade that already happened. Unreachable from `save_stored` (0600, always a
        # list); it takes a hand-edited store, and the verdict is the safe one regardless.
        return "record's parked set is malformed — this replay cannot be trusted"
    if sorted(str(n) for n in stored) != now:
        return "a row's owner moved live <-> parked since the stored table"
    return None


def digest_of(tracked: list[dict]) -> str:
    """What the sweep would render, plus the Progress signal it reports.

    `liveness` and `stuck` are set by the caller before this runs; both default to a
    STABLE value for a caller that has not enriched them (a fixture). ⚠️ **But the
    liveness default is no longer "absent", and that changed with the collapse below:**
    it routes through `liveness_change_term`, so an unenriched row *with* a session id
    hashes the DEATH term (1) rather than an absent value. Still stable, and unreachable
    in production -- the sole call site (`evaluate`) calls `enrich_liveness` first -- but
    the value differs from pre-collapse, so it is named rather than left to be inferred.

    ⚠️ **Liveness reaches the digest through `liveness_change_term`, never raw.** The
    split is deliberate: a session-liveness *churn* delta must not authorise a dispatch,
    while a session-*death* transition still must. See that function.

    ⚠️ **That split NARROWS THIS DOCSTRING'S OWN CONTRACT, and the narrowing is named
    here rather than left to be rediscovered.** "What the sweep would render" no longer
    holds for one cell: `parked` is what makes a row render `⌛ waiting-on-human`
    (`agents/manager-sweep-reader.md:136`), so a worker newly opening a gate no longer
    moves the digest.

    ⚠️ **The narrowing is REAL and is covered ONE LEVEL UP, not here.** The exit-0 replay
    path (`commands/manager-status.md` step 1, `commands/manager-drive.md` step 2) would
    otherwise reproduce the stored table with whatever cell the row last had, so
    `evaluate` carries a separate `parked_set_moved` clause that refuses the replay on
    exactly that transition. ⚠️ **It is deliberately NOT a term in this function**:
    hashing the raw liveness word again would also refuse the replay, and would put
    session churn back into the digest — the ~150k-token act-leg cost
    `liveness_change_term` exists to remove. The clause rides BESIDE the digest, the same
    way `actionable_names` does, so this function stays a pure function of the tracked
    set.

    ⚠️ **The cell that goes stale is the STATUS cell, never the Session cell** — named
    because the two were conflated in the caveats that clause replaced. `agents/manager-sweep-reader.md`
    step 7 makes a parked session take the LIVE-shaped `[<sid8>]` delimiter, and
    `session-liveness.py` never reads the registry's `status` field, so a `live` <-> `parked`
    flip renders the SAME Session cell on both sides. The Session cell's own stale routes
    — a row whose ids live only in `metrics_sessions`, gaining its first id without moving
    any digest input — are NOT covered by this clause and remain open.

    ⚠️ **`/manager-loop` is NOT one of the replaying surfaces and is NOT a mitigation:**
    it never calls `--print` or `--check`, so it never consults this digest at all, and
    its attention-feed read is step 4's first bullet (`commands/manager-loop.md:186`),
    which runs before any verdict. The independent surfacing that does hold is step 7's
    `manager-attention-watch.py` / feed-Monitor arms, which fire `NEW GATE` off the
    registry and the feed rather than off this digest.

    ⚠️ **The churn suppression is NOT absolute, and the second route is named here rather
    than left to be inferred from the term alone.** `stuck` is a SEPARATE digest input
    (the same tuple, below), and `idle_stuck` returns `False` for a PARKED row -- so a row
    already flagged stuck (idle past `STUCK_SECONDS`, `phase: execution`, holding an open
    box) that then opens a gate flips `stuck` 1 -> 0 and **still moves the digest on a
    pure `live -> parked` flip**. That is intended, not a leak: a stuck row that parks on
    a human genuinely stops being stuck, so its rendered state really did change and
    replaying a stale table over it would be the worse failure. What the collapse removes
    is the churn that informed nothing -- not every transition whose *cause* happens to be
    a gate.

    ⚠️ **The first run after an UPGRADE reports one spurious CHANGE, and that is expected
    rather than a fault.** The liveness term's shape changed (the raw word -> a bare
    `0`/`1`), and the stored record carries no version, so every subject holding a
    pre-collapse digest compares unequal exactly once: `load_stored` returns the old
    string and `digest_moved` fires with nothing about the tree moved. Bounded and
    fail-open -- a CHANGE authorises a sweep, it never suppresses one -- and self-healing:
    the loop's own `--save` rewrites the digest in the new format on its first tick.

    ⚠️ **The hold is a digest input, and deliberately NOT a suppression here.** This gate
    decides whether the sweep runs at all, so suppressing it for a held session would
    stop the sweep and the held row would never render -- the exact failure the hold
    design forbids, where a row that disappears from a sweep is indistinguishable from a
    row that got fixed. Including it as an input is what makes a hold being ADDED or
    RELEASED move the digest, so the loop wakes and re-renders the row.
    """
    h = hashlib.sha256()
    for t in sorted(tracked, key=lambda x: x["name"]):
        h.update(
            (
                f"{t['name']}|{t['status']}|{t['phase']}|{t['session']}"
                f"|{t.get('progress_hash', '')}|{liveness_change_term(t)}"
                f"|{int(bool(t.get('stuck')))}|{int(is_held(t['session']))}\n"
            ).encode()
        )
    h.update(b"--members--\n")
    for t in sorted(tracked, key=lambda x: x["name"]):
        h.update(f"{t['name']}\n".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# bounded stdin
# --------------------------------------------------------------------------- #

# How long `--save` will wait for stdin to reach EOF before giving up on it.
# Deliberately small: every legitimate caller (`< /dev/null`, a regular file, a closed
# fd 0) EOFs in microseconds, so only a stdin that is never closed reaches this. The
# number is a ceiling on a stall, not a budget for one.
STDIN_WAIT_SECONDS = 5


def read_stdin_bounded(timeout: float | None = None) -> str | None:
    """Read stdin to EOF, but never wait longer than `timeout`. `None` means it did not end.

    `sys.stdin.read()` blocks until EOF, and a caller whose stdin is an open pipe that is
    never closed blocks it forever. Measured 2026-09-30 against the Manager Layer store:
    45 013 ms and killed, versus 654 ms for the same call with `< /dev/null` — and the
    store was left byte-for-byte unchanged, because the save never ran. That is the whole
    cost: the caller's tick gets a stale record, and the next drive leg holds its batch.

    Reading in a `select` loop rather than one `select` followed by `read()`: a caller that
    writes part of a table and holds the pipe open passes a single readability check and
    then blocks *inside* `read()` — the same stall, one layer down.

    The harness's own stdin wiring is not stable across calls (measured in one session,
    minutes apart: `sock` with a read that blocked past 3 s, then the same read returning
    0 bytes immediately), so a caller cannot be asked to redirect its way out of this.
    The bound has to live here.
    """
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        # No fd behind stdin: a closed fd 0, or an in-process caller that swapped
        # `sys.stdin` for a plain object — the test suite does exactly that with
        # `io.StringIO`, and `save_with_table` relies on it to drive the refusal below.
        # Neither can stall, because neither is a pipe, so the plain read is correct
        # here; only a real fd can be an open pipe that never EOFs.
        try:
            return sys.stdin.read()
        except (AttributeError, OSError, ValueError):
            return ""
    # Resolved here rather than as a default argument so a test can lower the module
    # constant and exercise the deadline in milliseconds instead of seconds.
    if timeout is None:
        timeout = STDIN_WAIT_SECONDS
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            return ""
        if not ready:
            return None
        try:
            # `os.read` on the fd rather than `sys.stdin.read()`: the text wrapper would
            # re-block waiting for EOF, which is the very thing this bounds.
            chunk = os.read(fd, 65536)
        except OSError:
            return ""
        if not chunk:
            return b"".join(chunks).decode("utf-8", errors="replace")
        chunks.append(chunk)


# --------------------------------------------------------------------------- #
# stored state + fail-open
# --------------------------------------------------------------------------- #


def slug(subject: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")


def state_path(subject: str) -> str:
    return os.path.join(STATE_DIR, f"{slug(subject)}.json")


def tracked_path(subject: str) -> str:
    """Where the caller's own tracked set lands, keyed by the same subject slug.

    Deliberately a sibling of `state_path`, not a field inside it: the two hold
    different things. The state file holds this gate's *verdict* — digest, table,
    `recorded_at` — while this holds the caller's *input*, the names it resolved
    from the page. They can disagree, and that disagreement is the point.
    """
    return os.path.join(STATE_DIR, f"{slug(subject)}.tracked.txt")


def buckets_path(subject: str) -> str:
    """Where the caller's per-bucket classification lands before `--save` folds it in.

    A staging file rather than an argument because a bucket set is a dict of every
    tracked name the caller classified — hundreds of names, which cannot ride a
    command line, and which must not be concatenated onto `--save`'s stdin, whose
    whole content is the table the no-change branch replays verbatim.
    """
    return os.path.join(STATE_DIR, f"{slug(subject)}.buckets.json")


def verdicts_path(subject: str) -> str:
    """Where the drive leg's `Audit` block lands, keyed by the same subject slug.

    The cache clause (1) names — `<slug>.verdicts.json`, beside the `<slug>.buckets.json`
    the caller already writes its `bucket_sets` into — and the counterpart of
    `buckets_path`. Clause (1) assigns the write to the *caller* ("The write is the
    caller's … let the caller persist them") and the leg holds no write tool, so the
    caller needs a writer it can reach without `Write` or a shell redirect: that is
    `--write-verdicts`.

    Unlike `buckets_path` this is not a staging file — nothing folds it into the record.
    The store is the gate's verdict over the *tree*; this is the leg's verdict over *rows*,
    and clause (1) reads it back on the next tick to decide what to re-audit. Each entry
    also carries the auditor's `reason` on the verdicts that quote it to the operator
    (`VERDICTS_NEEDING_REASON`), because a cache hit has no dispatch to quote from.
    """
    return os.path.join(STATE_DIR, f"{slug(subject)}.verdicts.json")


def payload_path(subject: str) -> str:
    """Where the render writes the table it just rendered, for `--save` to read back.

    A sibling of `state_path` for the same reason `tracked_path` is one: the two hold
    different things and can disagree. The state file holds the gate's *verdict*; this
    holds the render's *output*, and it carries the render's own mtime — which is what
    lets a save be dated from when the table was produced rather than from when the
    caller got round to storing it. That gap is the whole defect: a payload held for 23
    minutes and then saved lands as a fresh record unless the two moments are tied.
    """
    return os.path.join(STATE_DIR, f"{slug(subject)}.table")


def loop_snapshot_path(vault: str, subject: str) -> str:
    """The model-free loop's own snapshot for this subject.

    Derived rather than passed, because the whole point of the comparison is to
    check the caller's set against a membership the caller did NOT produce. A
    caller that hands over the path can hand over the wrong one, and a wrong path
    that reads clean is indistinguishable from agreement.

    The convention is the loop tick's, read from `sweep-gate-notify-tick.sh`:
    `<basename(vault) lowercased>/<slug(subject)>.snapshot.json` under
    `$SWEEP_GATE_BASE` — the same env var that script honours, so a host running
    the loop elsewhere is compared against the loop it actually runs.
    """
    base = os.path.expanduser(
        os.environ.get("SWEEP_GATE_BASE", "~/.claude/state/sweep-gate-loop")
    )
    vname = os.path.basename(os.path.normpath(vault)).lower()
    return os.path.join(base, vname, f"{slug(subject)}.snapshot.json")


def print_provenance(subject: str, vault: str, buckets_staged: bool) -> None:
    """Name the two provenance files `--save` sits between, labelled.

    The drive leg's dispatch requires `recorded_at` from the *store* — the file `--save`
    reads and writes, and the same record that carries `bucket_sets` — while the snapshot
    is the leg's *row* source. The two paths sit one directory apart and read alike, so
    the wrong half is plausible-looking, and both directions have been measured: on
    2026-09-30 the store's value was passed where the snapshot's was required, and on
    2026-10-05 the snapshot was named while the store's value was passed, which held all
    49 rows of a batch at the leg's clause (0). Printing both here, with each half's role
    named, makes them unambiguous at the point the caller reads it, rather than leaving
    the command's prose to disambiguate a value already in hand.

    ⚠️ **The store label is conditional, because the half it names is.** `save_stored`
    omits `bucket_sets` entirely when the caller staged none — *"Omitted rather than
    defaulted … so a record that lacks the half reads as 'not persisted' instead of as
    'persisted and empty'"* — so an unconditional `(bucket_sets)` would assert a half the
    file does not carry, on the one branch where it is absent. Each label names where its
    half *lands*; neither is a promise that the file exists or is complete.
    """
    carries = "bucket_sets" if buckets_staged else "no bucket sets staged this tick"
    print(f"  store:    {state_path(subject)}  (recorded_at source, {carries})")
    print(f"  snapshot: {loop_snapshot_path(vault, subject)}  (row source)")


# The shapes a caller/gate disagreement can take — and the two that are not disagreements
# at all. Named separately because the whole defect this discriminates is that ONE line
# rendered for all of them: a scan that is too small looks exactly like a gate that gained
# a row, and a scan that is too large looks exactly like a gate that is missing one, so the
# direction that matters is the silent one.
# Each value **is** the label it renders under — one name per concept, because a constant
# reading `caller-dropped` while the report prints `caller-scan-suspect` is the same
# one-line-two-readings split this discriminator exists to remove, one level down. One
# glyph, distinct words: the convention the Manager Session runbook already uses for
# causally different states (`⏸️ blocked/hold` vs `⏸️ blocked/upstream`), because two causes
# sharing a word is how they become one cause to the reader again. `DIVERGENCE_HEALTHY` is
# never rendered under the `⚠️` glyph at all — it is not a divergence.
DIVERGENCE_HEALTHY = "healthy"
DIVERGENCE_CALLER_FORMAT = "caller-format"
DIVERGENCE_CALLER_SCAN_SUSPECT = "caller-scan-suspect"
DIVERGENCE_CALLER_OVERSHOOT = "caller-overshoot"
DIVERGENCE_BOTH_MOVED = "both-sides-moved"
DIVERGENCE_UNCLASSIFIED = "unclassified"


def classify_divergence(mine, gate, members) -> tuple[str, str]:
    """-> (kind, sentence) for a caller set that disagrees with the gate's membership.

    `members` is the page's *declared* membership — the same list `resolve_subject` hands
    the gate — or None when that declaration could not be read. First match wins, and the
    order is the argument rather than an accident:

    - **healthy** is tested first, because it and `caller-scan-suspect` can both hold
      `only_mine <= members`; the healthy one is the one with an *empty* gate-only, and it
      must not fall through to a divergence label;
    - **disjoint** next, because it is a caller-side format error wearing the largest
      possible disagreement's clothes — two independently-derived memberships cannot each
      hold every name the other lacks. Measured 2026-10-02 (Work Approval, tick #90): a
      normalizer whose BSD `sed` never fired wrote all 11 names with their `— description`
      tails, and the report rendered `11 caller-only · 11 gate-only`, a shape that reads as
      total disagreement and is total garbage. ⚠️ Healthy and disjoint are **mutually
      exclusive** — disjointness forces `only_gate` non-empty, which already disqualifies
      healthy — so their relative order is behaviourally inert; only
      healthy-before-`caller-scan-suspect` carries weight.

    `members is None` returns UNCLASSIFIED rather than a guess. "I could not check" and
    "they agree" are the two states this mode exists to tell apart, and collapsing them one
    level down is the same defect the fail-open branch below refuses.

    ⚠️ **A heuristic, not a proof, and every reader of a label must hold it that way.** It
    names which side is *more likely* wrong from the shape alone; it never establishes which
    side *is* wrong, and it holds no input that could — the caller's scan and the gate's
    membership are the only two sets it ever sees. `caller-overshoot` in particular renders
    two causes it cannot separate and says so. Read a label as *the side to check first*,
    never as *the side already convicted*.
    """
    only_mine = set(mine) - set(gate)
    only_gate = set(gate) - set(mine)
    if members is None:
        return DIVERGENCE_UNCLASSIFIED, (
            "the page's declared membership could not be read, so the shape is "
            "unclassified and neither side is exonerated"
        )
    member_set = set(members)
    if not only_gate and only_mine <= member_set:
        return DIVERGENCE_HEALTHY, (
            f"caller-only is within the {len(member_set)} declared member(s) and gate-only "
            "is empty — the known healthy asymmetry, not a divergence"
        )
    if mine and gate and not (set(mine) & set(gate)):
        return DIVERGENCE_CALLER_FORMAT, (
            "the two sets share no name at all — two independently-derived memberships "
            "cannot each hold every name the other lacks, so this is a caller-side format "
            "error (suffixes, casing, or an extractor that kept a description tail), not a "
            "membership disagreement"
        )
    if only_gate and only_mine <= member_set:
        return DIVERGENCE_CALLER_SCAN_SUSPECT, (
            "the caller carries nothing beyond the declared membership while the gate "
            "carries names it lacks — the caller's scan is the side more likely wrong: it "
            "dropped something"
        )
    if not only_gate:
        return DIVERGENCE_CALLER_OVERSHOOT, (
            "the caller carries names the gate does not, beyond the declared membership — "
            "the caller's scan is too large, or the gate's snapshot is a refresh behind"
        )
    return DIVERGENCE_BOTH_MOVED, (
        "the caller carries names beyond the declared membership and the gate carries "
        "names the caller lacks — neither side is exonerated"
    )


def load_state(subject: str) -> dict:
    """The raw stored payload, or {} — used for the cross-run busy-since map."""
    try:
        with open(state_path(subject), encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_stored(subject: str) -> tuple[str | None, str, str | None]:
    """-> (digest, stored_table, fail_reason). fail_reason set => force a full sweep."""
    path = state_path(subject)
    if not os.path.exists(path):
        return None, "", "first run — no state file"
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, "", f"state unreadable ({exc.strerror or exc})"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, "", f"state parse error ({exc.msg})"
    if not isinstance(data, dict) or "digest" not in data:
        return None, "", "state parse error (no digest key)"
    table = data.get("table") or ""
    if not isinstance(table, str) or not table.strip():
        # A snapshot with no table cannot be replayed, so it cannot make a run free.
        return None, "", "state has no stored table"
    return str(data["digest"]), table, None


def strip_links(text: str) -> str:
    """Drop OSC 8 hyperlink escapes, keeping the text they wrapped.

    The jump link's URI carries the jump token, so anything written to disk must not
    carry the escape — and the stored table IS written to disk, to be copied, synced or
    pasted. `box-table.py` emits OSC8_OPEN + url + OSC8_ST + text + OSC8_OPEN + OSC8_ST,
    so removing the sequences leaves the visible table intact and the token absent. Same
    function, same reason, as the loop gate's.
    """
    return re.sub(r"\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)", "", text)


def save_stored(
    subject: str,
    branch: str,
    digest: str,
    table: str,
    tracked: list[dict],
    busy: dict,
    bucket_sets: dict | None = None,
    recorded_at: str | None = None,
) -> None:
    """Persist atomically (tmp + os.replace).

    The replace is the same discipline `fleet-snapshot.py` documents: a plain
    `open(path, "w")` truncates first, so a crash or a serialisation error part-way
    through leaves a truncated snapshot where a valid one stood — and the next run then
    diffs against nothing. `os.replace` is atomic within a filesystem: a reader sees
    either the old snapshot or the new one, never a half-written file.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {
        "subject": subject,
        "branch": branch,
        "digest": digest,
        # Stored link-free — see strip_links. The replay prints this text, so a stored
        # OSC 8 escape would hand the operator a jump coordinate resolved at the earlier
        # render, and pane ids are renumbered by a WezTerm restart without moving any
        # digest input. The no-change branch therefore replays no coordinates at all.
        "table": strip_links(table),
        "members": len(tracked),
        "busy_since": busy,
        # The rows whose Status cell reads `⌛ waiting-on-human`. The digest is deliberately
        # DEATH-ONLY (see `liveness_change_term`), so a `live` <-> `parked` flip moves no
        # digest input -- and the exit-0 branch would then replay a table whose Status cell
        # is no longer true. This is the field that makes that replay refusable, and it is
        # read by a SEPARATE clause in `evaluate` rather than hashed here, so `digest_of`
        # stays a pure function of the tracked set. A record written before this field
        # existed carries no key, which `parked_replay_reason` reads as "cannot say" -- a
        # CHANGE, self-healed by the next `--save`, on the same fail-open rule `load_stored`
        # applies.
        #
        # ⚠️ It is computed from the tracked set `evaluate` has just enriched, while the
        # `table` beside it is read back from the payload file an EARLIER `--write-payload`
        # wrote. A flip landing between those two moments bakes a stale cell into a record
        # whose parked set matches, and the next run then replays it at exit 0. That is the
        # same T1/T2 window the digest already has -- pre-existing in kind rather than
        # introduced here, and named because the obvious reading of this field is stronger
        # than the code delivers.
        "parked": parked_names(tracked),
        # Dated from the payload's own mtime whenever the caller read the table back from
        # `payload_path`, so a record can never postdate the render it stores — the
        # invariant that turns a stale payload into a visible old date instead of a
        # silent fresh one. Falls back to now for a caller still piping the table in.
        "recorded_at": recorded_at
        or datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    # The caller's per-bucket classification, persisted so it survives a compaction and a
    # fresh manager. This is the half the drive leg's clause (0) requires and the snapshot
    # schema cannot supply — buckets are the *caller's* classification, so a per-bucket set
    # is never derivable from the snapshot (single home: `docs/fleet-surface.md` § Session
    # end). Omitted rather than defaulted when the caller passed none, so a record that
    # lacks the half reads as "not persisted" instead of as "persisted and empty".
    if bucket_sets is not None:
        payload["bucket_sets"] = bucket_sets
    tmp = state_path(subject) + ".tmp"
    try:
        # 0600 at creation rather than by a chmod after it: the file is never
        # world-readable, not even for the instant between the write and the chmod.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, state_path(subject))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #

# Buckets whose presence means the act leg has work it can do *right now*, so a no-change
# verdict would starve it. `waiting-approval` is deliberately NOT here: that row waits on
# the operator's `vault-cli task approve`, which the act leg may not run, so sweeping
# cannot move it and counting it makes the no-change verdict unreachable for any topic
# carrying an approval queue. See the module docstring's exit-code note.
ACTIONABLE_BUCKETS = ("ready-to-start",)


def actionable_names(bucket_sets) -> list[str]:
    """The rows the stored classification places in an actionable bucket, sorted.

    Read from the CALLER's own per-bucket half, because that is the only place a row's
    bucket exists: `digest_of()` covers the tracked set, and the snapshot schema has no
    bucket concept. A missing or malformed half returns `[]` and changes no verdict — the
    gate then behaves exactly as it did before this clause existed, which is the safe
    direction: an absent classification costs a replay, never a wrong dispatch.
    """
    if not isinstance(bucket_sets, dict):
        return []
    names: list[str] = []
    for bucket in ACTIONABLE_BUCKETS:
        rows = bucket_sets.get(bucket)
        if isinstance(rows, list):
            names.extend(n for n in rows if isinstance(n, str) and n.strip())
    return sorted(set(names))


def evaluate(vault: str, subject: str) -> tuple[bool, str, dict, str]:
    """-> (changed, reason, payload, stored_table).

    Every failure path returns `changed=True` — the fail-open rule. A gate that reports
    "no change" when it cannot tell is the one failure worth a dispatch to avoid.
    """
    try:
        branch, members = resolve_subject(vault, subject)
    except (ValueError, OSError) as exc:
        return True, f"fail-open: subject unresolvable ({exc})", {}, ""

    try:
        tracked = resolve_tracked(vault, members)
    except OSError as exc:
        return True, f"fail-open: tracked set unreadable ({exc})", {}, ""

    registry = read_registry()
    feed = read_feed()
    stored, stored_table, fail_reason = load_stored(subject)
    prev = load_state(subject)
    prev_busy = prev.get("busy_since") or {}
    stored_bucket_sets = prev.get("bucket_sets")

    now_ts = time.time()
    enrich_liveness(tracked, registry, feed)
    busy = apply_stuck(tracked, registry, prev_busy, now_ts)
    digest = digest_of(tracked)

    # Four independent reasons to sweep, and the last two are deliberately NOT folded into
    # `digest_of`: that function's contract is "what the sweep would render", a pure
    # function of the tracked set, and neither a caller-supplied classification nor a
    # render-truth check about a single cell has any business moving it. See the module
    # docstring's exit-code note.
    digest_moved = stored != digest
    actionable = actionable_names(stored_bucket_sets)
    parked_reason = parked_replay_reason(prev.get("parked"), parked_names(tracked))
    changed = (
        fail_reason is not None
        or digest_moved
        or bool(actionable)
        or parked_reason is not None
    )
    if fail_reason:
        reason = fail_reason
    elif digest_moved:
        reason = "digest differs"
    elif actionable:
        shown = ", ".join(actionable[:3])
        if len(actionable) > 3:
            shown += f" +{len(actionable) - 3} more"
        reason = f"{len(actionable)} actionable row(s) waiting: {shown}"
    elif parked_reason:
        reason = parked_reason
    else:
        reason = "digest equal"
    payload = {
        "branch": branch,
        "digest": digest,
        "tracked": tracked,
        "busy": busy,
        "recorded_at": prev.get("recorded_at", ""),
        # The per-bucket half as it stands in the record, so `--save` can tell a freshly
        # staged classification from the one already stored. The comparison lives at the
        # save decision — see `--save`.
        "stored_bucket_sets": stored_bucket_sets,
    }
    return changed, reason, payload, stored_table


RUNBOOK_RELATIVE_PATHS = (
    os.path.join("65 Runbooks", "Manager Session.md"),
    os.path.join("70 Runbooks", "Manager Session.md"),
)

# Anchored on the runbook's own prose shape, never on a line number. Measured 2026-10-03:
# both declarations moved by one line inside a single day, so a line-anchored read would
# have silently stopped finding them. The sibling vault's copy also sits under `70
# Runbooks/` and declares a different count, which is why the path is a candidate list
# and the count is never carried as a constant.
BUCKET_DECLARATION_MARKER = "Step 4 — Classify into the full bucket set"
DISPOSITION_MARKER = "Non-bucket dispositions"


def strip_emphasis(token: str) -> str:
    """`token` with the runbook's markdown emphasis removed from its ENDS only.

    The live `65 Runbooks/Manager Session.md` renders four of its bucket names bold —
    `**ready-to-start**`, `**blocked-upstream**`, `**close-me**`, `**orphaned**` — so a
    parser that keeps the markers declares a vocabulary no renderer agrees with. ⚠️ **No
    bucket COUNT is stated here, deliberately:** the set is vault-relative and has moved
    twice in a week, so a number in prose goes stale while reading as a measurement.
    Measured 2026-10-03: the
    declared set carried `**ready-to-start**` while `ACTIONABLE_BUCKETS` held the plain
    name, so **no key spelling satisfied both halves** — plain keys were refused at the
    write door, and bolded keys would have made `actionable_names()` find nothing and
    silently disable the drive leg's clause (0). Every sweep's classification went stale
    behind the refusal, and the refusal was the *correct* behaviour: the reader declined to
    write a second vocabulary. The fix belongs here, on the declaring side.

    ENDS only, never interior characters. `waiting_on_human` is a plausible bucket
    spelling, and a blanket `_`-removal would rewrite it to `waitingonhuman` — a name
    nobody declared, admitted by the very check that exists to refuse those. `_` is in the
    strip set for the ENDS half specifically, and that is not the interior rule applied
    twice: stripping it at the ends is what de-emphasises `__bold__` / `_italic_` if the
    runbook ever renders them that way, and no plausible bucket or disposition spelling
    ends in `_`, so nothing real is rewritten. `👤 YOURS`
    carries an emoji and survives verbatim, which the comparison depends on: normalising it
    would re-admit the `yours` / `YOURS` synonyms the store has been measured emitting.
    """
    return token.strip().strip("*_`").strip()


def runbook_path(vault: str, override: str | None) -> str | None:
    """Where the vault's own bucket declaration lives, or None when neither candidate does."""
    if override:
        return override
    for rel in RUNBOOK_RELATIVE_PATHS:
        candidate = os.path.join(vault, rel)
        if os.path.exists(candidate):
            return candidate
    return None


def declared_bucket_names(path: str) -> tuple[set[str] | None, str | None]:
    """(names, error) — the vocabulary the runbook declares, or why it could not be read.

    TWO declarations, and both are load-bearing. The parenthesised run on the Step 4 line
    is the bucket set; the dispositions clause names the three non-bucket keys the
    classification also carries (`hold`, `backlog`, `👤 YOURS`). Refusing the second group
    would reject a name the runbook declares — and the live store keys `backlog` for real,
    so a check built from the parenthesised run alone refuses correct work.

    Parsed by SHAPE, not by line number and not by collecting words on the line: the
    clause carries prose naming superseded buckets, so a whole-line read admits exactly the
    drift this check exists to catch. `👤 YOURS` carries an emoji, so the comparison is on
    the runbook's own token verbatim — normalising it would re-admit the synonym spellings
    (`yours`, `YOURS`) the store has already been measured emitting.

    Both declarations are read through `strip_emphasis`, because the runbook RENDERS four
    of its bucket names bold and the markers are not part of the vocabulary. See that
    function for the measured cost of keeping them.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return None, f"could not read the bucket declaration at {path!r}: {exc}"

    buckets: set[str] = set()
    for line in lines:
        if BUCKET_DECLARATION_MARKER in line:
            match = re.search(r"\(([^()]*)\)", line)
            if match:
                for token in match.group(1).split("/"):
                    name = strip_emphasis(token)
                    if name:
                        buckets.add(name)
            break
    if not buckets:
        return None, (
            f"could not parse the bucket set from {path!r} — expected a parenthesised, "
            f"`/`-separated run on the {BUCKET_DECLARATION_MARKER!r} line"
        )

    dispositions: set[str] = set()
    for line in lines:
        if DISPOSITION_MARKER in line:
            # The LEAD CLAUSE only. The rest of the line is commentary that backticks the
            # cell label (`⏸️ blocked/hold`) as well as the disposition name, so a
            # whole-line backtick sweep admits `⏸️ blocked/hold` as a legal key.
            head = line.split(".**", 1)[0]
            for token in re.findall(r"`([^`]+)`", head):
                name = strip_emphasis(token)
                if name:
                    dispositions.add(name)
            break
    if not dispositions:
        return None, (
            f"could not parse the non-bucket dispositions from {path!r} — expected a "
            f"backticked run on the {DISPOSITION_MARKER!r} line"
        )

    return buckets | dispositions, None


def declared_bucket_names_for(
    vault: str, override: str | None
) -> tuple[set[str] | None, str | None]:
    """The declared vocabulary for a vault, or why it could not be established.

    Fail-closed by design. A check that skips when it cannot read the declaration is the
    defect this refusal exists to remove — the runbook *is* the source, so failing to read
    it is failing to have a check at all.
    """
    path = runbook_path(vault, override)
    if path is None:
        return None, (
            f"no bucket declaration found under {vault!r} — expected one of "
            f"{', '.join(repr(rel) for rel in RUNBOOK_RELATIVE_PATHS)}; "
            f"pass --runbook <path>"
        )
    return declared_bucket_names(path)


def bucket_shape_error(parsed, declared: set[str] | None = None) -> str | None:
    """Why `parsed` cannot gate, or None when it is a usable classification.

    A dict of bucket -> list of names is the only shape that can satisfy the drive leg's
    clause (0). A count, a bare list, or a bucket mapped to a non-list would all *look* like
    a classification and gate nothing.

    ⚠️ **A bucket mapped to `[]` is ACCEPTED, and that is the direction-(2) reversal of what
    this function used to refuse.** `[]` is a declaration of absence — "evaluated, nothing
    in it" — and it is the only way the record can distinguish that from "not evaluated".
    Before the reversal the rule text's *"every declared bucket must appear"* and this
    check's *"non-empty list"* could not both hold whenever any bucket was empty, which on
    the live store is most of them (measured 2026-10-04: 6 of 13 declared names present,
    `recorded_at 2026-10-04T12:39:32+02:00`). A producer facing an unsatisfiable pair wrote
    a sparse map, and nothing in the record said which buckets it had skipped.

    ⚠️ **An ALL-EMPTY set is still refused, so the 2026-09-28 hole stays closed.** That
    defect was `all(...)` over `[]` being vacuously True, so `{"done": []}` passed the very
    check whose message said "non-empty", staged at exit 0 and was saved. The refusal
    survives the reversal because it rests on a fact rather than on a shape preference: a
    non-empty tracked set always populates at least one bucket, so every-value-`[]` is the
    mis-parse signature and never a quiet tick.

    ⚠️ **The guard the reversal gives up is real, and it is recorded rather than glossed.**
    The old refusal also caught a PARTIAL mis-parse that emitted a key with an empty list.
    The completeness half below catches a mis-parse that *omits* a key; it cannot catch one
    that emits the key empty. *Declared means evaluated* is the whole point of the trade.

    Shared by BOTH doors into the record. Validating only `--write-buckets` left the
    `--save --buckets` read path a bare `json.load`, so a hand-written or stale staging
    file reached `save_stored` with a set that cannot gate — the same defect by the other
    door.

    `declared` adds the VOCABULARY and COMPLETENESS halves, and `None` skips both. Shape
    alone accepted any key at all: measured 2026-10-03, `{"hold": ["Alpha"], "orphan":
    ["Beta"], "ready": ["Gamma"]}` staged verbatim at exit 0, and the live store carried
    `backlog` / `yours` against a runbook that declares neither spelling. A set keyed by a
    vocabulary nobody declared gates the drive leg's clause (0) on names no renderer agrees
    with, which is the drift the runbook's two-renderer rule forbids. Completeness is the
    other half of the same clause: the prose always said every declared bucket must appear
    and nothing enforced it, which is exactly the gap the reversal above exposed. The names
    come from the runbook itself — never a list here — because the declared set is
    vault-relative and has moved twice in a week.
    """
    if not isinstance(parsed, dict) or not parsed:
        return "bucket sets must be a non-empty JSON object"
    for bucket, names in parsed.items():
        if not isinstance(names, list) or not all(
            isinstance(n, str) and n.strip() for n in names
        ):
            return f"bucket {bucket!r} must map to a list of names"
    # An all-empty set is the mis-parse signature, not a quiet tick: a non-empty tracked
    # set always populates at least one bucket, so `{"done": []}` and every sibling shape
    # reach here only from a broken parse. This is the 2026-09-28 vacuous-`all()` hole and
    # it stays closed under direction (2).
    if all(not names for names in parsed.values()):
        return (
            "bucket set maps every bucket to an empty list — a non-empty tracked set "
            "always populates at least one bucket, so this is the shape a broken parse "
            "produces rather than a quiet tick"
        )
    if declared is not None:
        undeclared = sorted(bucket for bucket in parsed if bucket not in declared)
        if undeclared:
            listed = ", ".join(repr(bucket) for bucket in undeclared)
            return (
                f"bucket set carries keys the runbook does not declare: {listed} — "
                f"declared: {', '.join(sorted(declared))}"
            )
        missing = sorted(name for name in declared if name not in parsed)
        if missing:
            listed = ", ".join(repr(name) for name in missing)
            return (
                f"bucket set omits buckets the runbook declares: {listed} — every "
                f"declared bucket must appear, mapped to its names or to an empty list"
            )
    return None


# The verdicts whose act hands the operator the auditor's *own words* — the `UNFIXABLE:`
# grounds for the first two, the posted card's `--context` gaps for the last two. On a
# cache hit there is no dispatch to re-derive either from, so an entry stored without its
# reason cannot render them at all: that is the loss `reason` exists to remove, and it is
# why the field is required here rather than merely allowed.
VERDICTS_NEEDING_REASON = ("needs-you", "unfixable", "below-bar", "reframe")


# The marker the drive leg's `Audit` block leaves when it renders a name short for width.
# A key carrying it is a name the caller never read in full — so it is neither stored as
# it stands nor expanded into a guess, because a guess is what put **six stale titles** in
# the live caches on 2026-10-02: the caller reconstructed the full titles from the
# abbreviated render and got six of them wrong. `…` (U+2026) is the marker the leg emits;
# the ASCII `...` is deliberately not included, because a real title may contain it.
ABBREVIATION_MARKER = "…"


def verdicts_shape_error(parsed, stored=None) -> str | None:
    """Why `parsed` cannot serve as a verdicts cache, or None when it is usable.

    A dict of task name -> `{verdict, score, content_key, reason?}` is the only shape
    clause (1) can read. The load-bearing key is `content_key`: clause (1) re-audits a row
    when its *file content* changes, so a stored verdict carrying no key can never be told
    from a stale one — and it would be read back as a cache hit on every tick forever.
    `reason` is the second required key, on `VERDICTS_NEEDING_REASON` alone: those are the
    verdicts whose act quotes the audit to the operator, and a hit carries no dispatch to
    quote from. `score` is deliberately allowed to be null, because `blocked` rows carry
    none and the sibling caches on disk show exactly that shape.

    ⚠️ **`stored` is the cache already on disk, and passing it is what stops this rule
    bricking every cache written before `reason` existed.** The requirement is on a *fresh
    audit*, not on the schema: an entry whose `content_key` is unchanged from the stored
    one is a carry-forward, and its verdict was decided before the field existed — there
    is nothing to re-derive it from and backfilling is out of scope, so it is
    grandfathered rather than refused. Refusing it instead makes the whole write fail for
    every subject still holding one, which on 2026-10-02 was **four of five caches**; the
    cache then goes stale and the *next* tick audits cold — the exact cost the cache
    exists to remove. An entry whose key is new or changed *is* a fresh audit and must
    carry the reason. Omitting `stored` is the strict reading, correct only for a caller
    that genuinely has no prior cache.

    A key carrying `…` is refused outright, and it is the only name rule here that reads
    the *name* rather than the entry: a name rendered short is not a name. `ABBREVIATION_MARKER`
    above carries both the reasoning for why that marker and not the ASCII `...`, and the
    measurement behind the refusal — stated once, there, so a correction has one home.

    The empty-object case is refused for the reason `bucket_shape_error` gives: `{}` gates
    nothing while looking like a cache that was written.
    """
    if not isinstance(parsed, dict) or not parsed:
        return "verdicts must be a non-empty JSON object"
    stored = stored if isinstance(stored, dict) else {}
    for name, entry in parsed.items():
        if not isinstance(name, str) or not name.strip():
            return "each key must be a non-blank task name"
        if ABBREVIATION_MARKER in name:
            return (
                f"entry {name!r} carries the abbreviation marker "
                f"{ABBREVIATION_MARKER!r} — a name rendered short is not a name, and "
                "guessing its remainder is what stored stale titles; "
                "read the row's own title and re-send it"
            )
        if not isinstance(entry, dict):
            return f"entry {name!r} must be an object"
        verdict = entry.get("verdict")
        if not isinstance(verdict, str) or not verdict.strip():
            return f"entry {name!r} must carry a non-blank verdict"
        score = entry.get("score")
        if score is not None and (isinstance(score, bool) or not isinstance(score, int)):
            return f"entry {name!r} score must be an integer or null"
        key = entry.get("content_key")
        if not isinstance(key, str) or not key.strip():
            return f"entry {name!r} must carry a non-blank content_key"
        reason = entry.get("reason")
        if verdict in VERDICTS_NEEDING_REASON:
            if not _carried_forward(stored, name, key):
                if not isinstance(reason, str) or not reason.strip():
                    return (
                        f"entry {name!r} is {verdict!r} and must carry a non-blank reason "
                        "— the auditor's own words are what the operator is handed"
                    )
        elif reason is not None and not isinstance(reason, str):
            return f"entry {name!r} reason must be a string when present"
    return None


def _carried_forward(stored: dict, name: str, key: str) -> bool:
    """True when `name`'s stored entry is the same one, by content key.

    An unchanged key means the row's file has not moved since the stored verdict was
    written, so this entry is that verdict re-staged rather than a new audit of it.
    """
    prior = stored.get(name)
    return isinstance(prior, dict) and prior.get("content_key") == key


def verdicts_grandfathered(parsed, stored) -> list[str]:
    """Names carried forward unchanged that the `reason` rule would otherwise refuse.

    Reported rather than silently accepted: such an entry cannot render its grounds, so a
    reader should be able to see which rows are in that state now rather than discover it
    from an empty `UNFIXABLE:` line later. It is a migration backlog, not a failure — the
    row re-audits and gains a reason the first time its file changes.
    """
    stored = stored if isinstance(stored, dict) else {}
    out = []
    for name, entry in (parsed or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("verdict") not in VERDICTS_NEEDING_REASON:
            continue
        if str(entry.get("reason") or "").strip():
            continue
        if _carried_forward(stored, name, entry.get("content_key")):
            out.append(name)
    return sorted(out)


def derive_verdict_names(parsed, index: dict[str, str], *, strict: bool = True):
    """`parsed` re-keyed onto the task files' own titles, or the collision that stops it.

    The name half of the cache key. The cache is keyed by task *name*, so the key is the
    one thing a later tick matches a row by — and until this ran, it was whatever spelling
    the caller sent. Where a name resolves against the tasks dir, the file's own basename is
    used instead, so **case** drift cannot key an entry. ⚠️ **Whitespace is deliberately
    *not* normalised, and the claim is scoped to case for that reason:** a title may
    legitimately contain a space — including a trailing one — so a `strip()` would make
    `Foo ` miss the very file it names. The index lookup is case-insensitive because the
    filesystem is, not because names are normalised. A caller sending the
    filename rather than the stem (`ATask.md`) is stripped to `ATask` before the lookup, so
    both spellings reach the same row.

    Resolution is a lookup in the tasks index — the `{lowercased name: name}` dict that
    `task_index` and `tasks_index_for_write` both build from `os.listdir` of the tasks dir
    — rather than a stat of a path assembled from the name. That keeps the contract below
    exactly true: a name carrying a separator is not reduced to its basename, and nothing
    outside the tasks dir is ever stat-ed.

    A name that does *not* resolve is left as the caller sent it rather than refused —
    except that a `.md` suffix is still stripped, so an unresolvable `Foo.md` is stored as
    `Foo`, canonicalising it the same way the resolvable path does. A refusal is not the
    alternative: `--vault` names one vault, and a subject's rows may live in another
    (`bro-21389-mdm-via-rest`'s 13 rows are all in `seibert-brogrammers/25 Tasks`), so
    refusing the unresolvable would brick every write for those subjects — and a row whose
    file was deleted would do the same to this one. The detectable sub-case is the
    abbreviation, and `verdicts_shape_error` refuses that one.

    ⚠️ **`strict` is the destination/lookup split, and it is not cosmetic.** The payload is
    a **destination** — two keys resolving to one row would drop one — so a collision is
    refused. `stored` is a **lookup source** only: it is never written, and every write
    replaces the whole file, so the entry already present answers a lookup by that title
    and the second is dropped rather than refused. Refusing it would be worse than useless,
    because the caller discards that reason: the cache is left un-rekeyed,
    `_carried_forward` then misses, and a legacy `needs-you` entry is refused for a missing
    reason — **the write-bricking cost the grandfather rule exists to remove, reached by
    the repair itself.**

    ⚠️ **The stored-side drop is named on stderr, and its residual is stated rather than
    claimed away.** First spelling wins, so a cache holding two spellings of one row with
    *different* `content_key`s still misses the carry-forward when the matching entry lost
    the race, and the write is refused for a missing reason. That fails **safe** — a
    refusal leaves the previous cache untouched. ⚠️ **The writer introduced here cannot
    produce such a cache** — every write replaces the whole file and a dict holds one entry
    per title, so no sequence of writes by *this* writer accumulates two spellings — but a
    cache written by the **pre-fix** writer could already hold them, if a caller ever sent
    both spellings on one tick. That is the cache the note exists to name, and saying
    "unreachable" without that qualifier would be the overstatement this docstring is
    otherwise careful to avoid.

    Returns `(rekeyed, None, renamed)`, or `(parsed, reason, {})` when two keys resolve to
    one row. `renamed` maps each changed title back to the spelling the caller sent, so a
    refusal downstream can name both — a caller who sent `atask` and is refused otherwise
    reads a message about a title it never typed.
    """
    out: dict = {}
    source: dict[str, str] = {}
    renamed: dict[str, str] = {}
    for name, entry in parsed.items():
        # ⚠️ A marker-bearing key is NEVER re-keyed, and this is the one path where the
        # refusal could otherwise leak. `verdicts_shape_error` tests the DERIVED key, so a
        # caller's `Foo…Bar` that resolved against a literal `Foo…Bar.md` in the tasks dir
        # would come back as `Foo…Bar` — identical, but only because that file exists. The
        # refusal must rest on the caller's own name, not on the measurement that no
        # `…`-bearing file lives in a tasks dir today.
        if isinstance(name, str) and ABBREVIATION_MARKER in name:
            out[name] = entry
            continue
        stem = (
            name[:-3]
            if isinstance(name, str) and name.lower().endswith(".md")
            else name
        )
        found = index.get(f"{stem}.md".lower())
        title = os.path.splitext(found)[0] if found else stem
        if title in out:
            if not strict:
                # A lookup source: the entry already under this title answers the lookup, so
                # this one is dropped rather than refused. Named rather than silent — it is
                # the only place a stored entry can be lost, and naming it is what lets a
                # reader tell a hand-edited two-spelling cache from a clean one.
                print(
                    f"note: stored cache holds {source[title]!r} and {name!r}, both naming "
                    f"{title!r} — keeping {source[title]!r}; if that is not the entry this "
                    "payload matches, the write is refused for a missing reason",
                    file=sys.stderr,
                )
                continue
            return parsed, (
                f"{name!r} and {source[title]!r} both name {title!r} — one row cannot "
                "carry two verdicts"
            ), {}
        out[title] = entry
        source[title] = name
        if title != name:
            renamed[title] = name
    return out, None, renamed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="manager pre-dispatch change gate")
    ap.add_argument("--subject", required=True, help="goal or topic name")
    ap.add_argument(
        "--vault", required=True, help="vault root PATH (never the vault name)"
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verdict only")
    mode.add_argument(
        "--print", action="store_true", help="verdict; replay the stored table on no-change"
    )
    mode.add_argument(
        "--save",
        action="store_true",
        help=(
            "persist the digest + the payload `--write-payload` wrote (a table on stdin is "
            f"refused, and a stdin that does not EOF within {STDIN_WAIT_SECONDS} s exits "
            f"{EXIT_STDIN_TIMEOUT})"
        ),
    )
    mode.add_argument(
        "--write-tracked",
        action="store_true",
        help="write the caller's own tracked set (names on stdin) to <slug>.tracked.txt",
    )
    mode.add_argument(
        "--write-buckets",
        action="store_true",
        help="stage the caller's per-bucket classification (JSON on stdin) for --save",
    )
    mode.add_argument(
        "--write-verdicts",
        action="store_true",
        help="persist the drive leg's Audit block (JSON on stdin) to <slug>.verdicts.json",
    )
    mode.add_argument(
        "--write-payload",
        action="store_true",
        help="write the rendered table (on stdin) to <slug>.table, for --save to read",
    )
    mode.add_argument(
        "--compare-tracked",
        action="store_true",
        help="compare the staged tracked set against the loop snapshot's membership",
    )
    mode.add_argument(
        "--blocked-verdicts",
        action="store_true",
        help=(
            "one `<name>\\t<blocked|ready|unreadable>\\t<unmet…>\\t<entry count>` line per "
            "staged tracked row, for the sweep reader's ready-to-start / blocked-upstream "
            "decision"
        ),
    )
    ap.add_argument(
        "--buckets",
        default=None,
        help="with --save: path to the staged bucket JSON (see --write-buckets)",
    )
    ap.add_argument(
        "--runbook",
        default=None,
        help=(
            "path to the runbook declaring the bucket vocabulary; defaults to whichever "
            "of <vault>/65|70 Runbooks/Manager Session.md exists"
        ),
    )
    args = ap.parse_args(argv)

    vault_error = vault_root_error(args.vault)
    if vault_error:
        # A usage error is not a verdict. Exit 2 is the code the commands' own branch
        # already documents as "fix the call; do not read it as changed", so the caller
        # is told the argument was wrong instead of being handed a fail-open sweep.
        print(vault_error, file=sys.stderr)
        return EXIT_USAGE

    if args.write_tracked:
        # The caller's OWN scan is the only producer of this file — it is never derived
        # here, because `evaluate()` computes a set from the same declarations and the two
        # can disagree (measured 2026-09-27: 154 declared vs 153 derived, differing by a
        # case-only name mismatch nothing else could see). What this mode buys is the
        # *transport*: routing the write through this script keeps the first token
        # `python3`, which matches the `Bash(python3:*)` grant all three manager commands
        # already hold — so a 15-minute loop writes its set without a permission prompt,
        # and never needs `Write` (granted by none of them) or a shell redirect.
        names = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
        if not names:
            # Same discipline as `--save`'s empty-table guard: a failed read must not
            # clobber a good set, and a set with no names can never be dispatched against.
            print(
                "refusing to write an empty tracked set: nothing on stdin, so the "
                "previous file is left unchanged.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        path = tracked_path(args.subject)
        tmp = path + ".tmp"
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(names) + "\n")
            # Atomic, for the same reason the snapshot write is: a reader sees either the
            # old set or the new one, never a half-written file — and the whole point of
            # this artifact is that a dispatch can trust what it reads.
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        # The path on stdout is the value the dispatch carries; print it alone so the
        # caller can capture it without parsing.
        print(path)
        return EXIT_WRITE_OK

    if args.write_buckets:
        # Staged, not stored: `--save` is what folds this into the record, and it runs
        # later in the same tick. Keeping the two apart means a caller that classifies but
        # never saves leaves no half-written record, and `--save`'s no-change branch —
        # which writes nothing at all — cannot silently drop a bucket set it was handed.
        try:
            parsed = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            print(f"bucket sets must be JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE
        # The vocabulary half, read from the runbook — fail-closed, because a check that
        # skips when it cannot read the declaration is the defect this refusal removes.
        declared, declaration_error = declared_bucket_names_for(args.vault, args.runbook)
        if declaration_error:
            print(declaration_error, file=sys.stderr)
            return EXIT_USAGE
        shape_error = bucket_shape_error(parsed, declared)
        if shape_error:
            print(shape_error, file=sys.stderr)
            return EXIT_USAGE
        path = buckets_path(args.subject)
        tmp = path + ".tmp"
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(parsed, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        print(path)
        return EXIT_WRITE_OK

    if args.write_verdicts:
        # The writer clause (1) delegates to the caller and the tree never provided. The
        # leg reports each row's verdict, score, content key and reason under its `Audit`
        # block and holds no write tool; the caller is told to persist them, but the only tool
        # the manager commands are granted is `Bash(python3:*)` — no `Write`, no shell
        # redirect. So the write is routed through this script for the same transport
        # reason `--write-tracked` is, and the first token stays `python3`.
        #
        # Stored as its own file rather than folded into the record: the store is the
        # gate's verdict over the *tree*, this is the leg's verdict over *rows*, and
        # clause (1) reads it back by content key on the next tick.
        try:
            parsed = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            print(f"verdicts must be JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE
        # The cache already on disk, read so the `reason` rule can tell a fresh audit from a
        # carry-forward. Without it a subject holding even one entry written before the field
        # existed can never be written again — measured 2026-10-02, four of five caches.
        stored = {}
        try:
            with open(verdicts_path(args.subject), encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                stored = loaded
        except (OSError, json.JSONDecodeError):
            stored = {}
        # The name half of the key, and the reason a rename no longer strands an entry —
        # see `derive_verdict_names`. It runs *before* the shape check because the `reason`
        # carry-forward compares payload keys against stored ones, so both sides have to be
        # re-keyed onto the files' own titles for that comparison to mean anything. A
        # non-dict payload is left alone: `verdicts_shape_error` owns that refusal, and it
        # is the only place the message lives.
        renamed: dict[str, str] = {}
        if isinstance(parsed, dict):
            index = tasks_index_for_write(os.path.join(args.vault, TASKS_DIRNAME))
            parsed, collision, renamed = derive_verdict_names(parsed, index)
            if collision:
                print(f"refusing to write these verdicts: {collision}", file=sys.stderr)
                return EXIT_USAGE
            # `strict=False`: the stored cache is a lookup source, never a destination, so
            # a collision there is tolerated rather than refused — the reason is provably
            # None on this path, which is why it is discarded rather than reported.
            stored, _, _ = derive_verdict_names(stored, index, strict=False)
        shape_error = verdicts_shape_error(parsed, stored)
        if shape_error:
            # A refused write must not clobber a good cache. The reason is sharper here
            # than for the other staging files: a cache that reads back as present but
            # unusable is indistinguishable from one that was never written, and every
            # later tick then re-audits cold while believing it has a cache — the exact
            # state this writer exists to end.
            print(f"refusing to write these verdicts: {shape_error}", file=sys.stderr)
            if renamed:
                # Every refusal above names the DERIVED title, because that is what the
                # shape check reads. A caller who sent `atask` would otherwise read a
                # message about a title it never typed, so the mapping is printed with it.
                print(
                    "  (keys re-keyed onto the task files' own titles: "
                    + ", ".join(
                        f"{new!r} ← {old!r}" for new, old in sorted(renamed.items())
                    )
                    + ")",
                    file=sys.stderr,
                )
            return EXIT_USAGE
        for name in verdicts_grandfathered(parsed, stored):
            # Not a failure and not silent: a row written before `reason` existed cannot
            # render its grounds, and a reader should see which rows are in that state
            # rather than discover it from an empty `UNFIXABLE:` line later.
            print(
                f"note: carrying {name!r} forward with no reason — its stored content_key is "
                "unchanged, so there is no audit to re-derive the auditor's words from",
                file=sys.stderr,
            )
        path = verdicts_path(args.subject)
        tmp = path + ".tmp"
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            # 0600 at creation rather than by a chmod after it, matching `save_stored`:
            # the file is never world-readable, not even for the instant between the
            # write and the chmod.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(parsed, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        print(path)
        return EXIT_WRITE_OK

    if args.write_payload:
        # The render's own transport, the same shape `--write-tracked` uses and for the
        # same reason: routing the write through this script keeps the first token
        # `python3`, which matches the `Bash(python3:*)` grant the sweep reader already
        # holds. What it changes is *when* the write happens — at render time, to a path
        # derived here rather than passed in — so `--save` can read the table back
        # instead of depending on the caller to hand-pipe it, and the two moments the
        # record's halves are stamped stop coming apart.
        table = sys.stdin.read()
        if not table.strip():
            # Same discipline as `--save`'s empty-table guard and `--write-tracked`'s
            # empty-set guard: a failed render must not clobber a good payload, because a
            # payload with no table in it can never be replayed.
            print(
                "refusing to write an empty payload: nothing on stdin, so the previous "
                "file is left unchanged.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        path = payload_path(args.subject)
        tmp = path + ".tmp"
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            # Both halves of the pattern `save_stored` already holds, and for its reasons.
            # The rendered frame's OSC 8 jump link carries the jump token, and this file is
            # a disk artifact in a shared state dir — so it is written link-free and 0600
            # *at creation*, never world-readable even for the instant between the write and
            # the replace. Stripping here rather than at the caller is the point: one
            # `render_table()` frame serves both the operator's screen and this file, so a
            # caller-side strip would take the on-screen links with it. The frame the caller
            # prints keeps its links; only the copy on disk loses them.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(strip_links(table))
            # Atomic, for the reason `--write-tracked` gives: a reader sees either the old
            # payload or the new one, never a half-written file.
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        print(path)
        return EXIT_WRITE_OK

    if args.compare_tracked:
        # A dispatch whose tracked set disagrees with the gate's own membership is
        # reported, not accepted. The two memberships are produced by different code from
        # the same declarations, so each is self-consistent while they disagree — measured
        # 2026-09-27: 154 declared against 153 in the snapshot, differing by one name whose
        # only fault was a capital `The` against a case-sensitive membership compare.
        # Nothing else could see it: APFS is case-insensitive, so `os.path.exists`,
        # `open()`, Obsidian's link resolver and `ls` all succeed on the capitalised path,
        # and no sweep notices because neither source is internally inconsistent.
        # ⚠️ What that framing got wrong is that the *caller's* half is not a derivation at
        # all — it is a scan the manager writes fresh every tick, specified nowhere and
        # shipped nowhere. So one `⚠️ DIVERGENCE` line rendered for three causally
        # different situations: a wrong caller scan, a stale gate snapshot, and a genuine
        # membership disagreement. `classify_divergence` tells them apart, and the healthy
        # shape (caller-only within the declared membership, gate-only empty) stops being a
        # warning at all.
        try:
            with open(tracked_path(args.subject), encoding="utf-8") as fh:
                mine = {ln.strip() for ln in fh if ln.strip()}
        except OSError as exc:
            print(f"no staged tracked set to compare ({exc})", file=sys.stderr)
            return EXIT_USAGE
        snap = loop_snapshot_path(args.vault, args.subject)
        try:
            with open(snap, encoding="utf-8") as fh:
                snap_payload = json.load(fh)
            gate = set(snap_payload.get("tasks") or {})
        except (OSError, json.JSONDecodeError) as exc:
            # Fail-open in the gate's own sense: an unverifiable membership is never
            # reported as agreement. "I could not check" and "they match" are precisely
            # the two states this mode exists to tell apart, so collapsing them would
            # reintroduce the defect one level down.
            print(f"COMPARE unavailable — {snap} unreadable ({exc})")
            print(f"  caller {len(mine)} names · gate unknown — NOT compared")
            return EXIT_DIVERGENT
        if not gate:
            print(f"COMPARE unavailable — {snap} carries no `tasks` membership")
            print(f"  caller {len(mine)} names · gate 0 — NOT compared")
            return EXIT_DIVERGENT
        only_mine = sorted(mine - gate)
        only_gate = sorted(gate - mine)
        print(f"COMPARE caller {len(mine)} names · gate {len(gate)} names")
        if not only_mine and not only_gate:
            print("  memberships identical")
            return EXIT_NOCHANGE

        # Which of the three causes this is. The declaration is read through the same
        # resolver the gate itself uses, so "healthy" is a comparison against the page's
        # own declaration rather than against a remembered shape — and an unreadable page
        # yields UNCLASSIFIED rather than a guess.
        try:
            _branch, members = resolve_subject(args.vault, args.subject)
        except (ValueError, OSError) as exc:
            print(f"  declared membership unreadable ({exc})", file=sys.stderr)
            members = None
        kind, sentence = classify_divergence(mine, gate, members)

        # Every reading this verdict rests on, printed rather than assumed: the two counts
        # and the two clocks. A gate snapshot that predates the caller's own scan is cause
        # (2) made visible — stated, not claimed, because a stale snapshot and a too-large
        # scan render the same shape. ⚠️ Note a quiet subject's `recorded_at` ALWAYS predates the
        # caller's scan — not because it is stale, but because it is an identity that froze — so
        # the pair below reports when the digest last changed, not how old the snapshot is.
        try:
            # `.astimezone()` so both clocks on the `reading:` line carry an offset — the
            # line exists to be compared, and a naive local mtime beside a tz-aware
            # `recorded_at` cannot be compared across a boundary.
            scanned_at = (
                datetime.fromtimestamp(os.path.getmtime(tracked_path(args.subject)))
                .astimezone()
                .isoformat(timespec="seconds")
            )
        except OSError:
            scanned_at = "unstated"
        print(
            f"  reading: caller-only {len(only_mine)} · gate-only {len(only_gate)} · "
            f"gate snapshot last digest change {snap_payload.get('recorded_at') or 'unstated'} · "
            f"caller scan written {scanned_at}"
        )

        if kind == DIVERGENCE_HEALTHY:
            print(f"✅ COMPARE healthy — {sentence}")
            return EXIT_NOCHANGE
        print(f"⚠️ DIVERGENCE ({kind}): {sentence}")
        for label, names in (("caller-only", only_mine), ("gate-only", only_gate)):
            for n in names[:10]:
                print(f"  {label}: {n}")
            if len(names) > 10:
                print(f"  … and {len(names) - 10} more {label}")
        return EXIT_DIVERGENT

    if args.blocked_verdicts:
        # The reader's own read, handed to it as a verdict rather than left to its prose.
        #
        # `blocked_by_verdict` was landed with no caller, and a tested function nothing
        # calls ships nothing: the sweep reader went on deciding `ready-to-start` from its
        # own frontmatter read, which is nondeterministic — measured 2026-10-02, four
        # misreads across eleven relevant ticks, and reproduced again 2026-10-03 18:44 on
        # two rows whose first entry had shipped and whose later entries had not. The
        # warning against exactly that read has sat in `agents/manager-sweep-reader.md`
        # since v0.96.4 and did not prevent it, so the decision moves to code and the
        # agent renders the bucket from the answer.
        #
        # Reads the caller's STAGED set — `--write-tracked`'s file, the same membership
        # the caller passed the reader — so the verdicts and the table cover one set by
        # construction. That is also its one hazard, and the reader is told to check it:
        # a staged file left over from an earlier tick would classify the wrong rows, so
        # the reader compares the names printed here against the names it was handed and
        # refuses the read on a mismatch rather than classifying a set it cannot vouch for.
        try:
            with open(tracked_path(args.subject), encoding="utf-8") as fh:
                names = [ln.strip() for ln in fh if ln.strip()]
        except OSError as exc:
            # Fail-closed, like `--compare-tracked`: "I could not read the set" and "every
            # row is unblocked" must never render the same way, and the second is the one
            # that opens a spawn.
            print(f"no staged tracked set to classify ({exc})", file=sys.stderr)
            return EXIT_USAGE
        # Hardcoded, and the reason now lives once — with `TASKS_DIRNAME`, which both this
        # path and the write path use.
        tasks_dir = os.path.join(args.vault, TASKS_DIRNAME)
        index = task_index(tasks_dir)
        for name in names:
            try:
                with open(
                    resolve_task_file(tasks_dir, name, index),
                    encoding="utf-8",
                    errors="replace",
                ) as fh:
                    fm = split_frontmatter(fh.read())
            except OSError:
                fm = None
            if fm is None:
                # An unreadable ROW is not an unreadable BLOCKER, and the two must not
                # share a word. `blocked_by_verdict` would call this row unblocked — it
                # reads an empty frontmatter and finds no entries — which is the vacuous
                # promotion this whole mode exists to stop. Name it instead, and let the
                # reader treat any word but `ready` as "not offered".
                print(f"{cell(name)}\tunreadable\t\t")
                continue
            verdict = blocked_by_verdict(fm, tasks_dir, index)
            # The entry count is a fourth column so the reader can pick the rows it must
            # quote back without parsing `blocked_by` itself — which is the read this mode
            # exists to replace. `0` and "every entry met" both render an empty unmet list,
            # so without this the two are indistinguishable downstream and a reporting
            # rule keyed on the unmet column would silently skip the rows that matter.
            print(
                "{}\t{}\t{}\t{}".format(
                    cell(name),
                    "blocked" if verdict["blocked"] else "ready",
                    cell(", ".join(verdict["unmet"])),
                    len(verdict["entries"]),
                )
            )
        return EXIT_WRITE_OK

    changed, reason, payload, stored_table = evaluate(args.vault, args.subject)

    if args.save:
        table = read_stdin_bounded()
        if table is None:
            print(
                f"refusing to wait on stdin: no EOF within {STDIN_WAIT_SECONDS} s. `--save` "
                f"reads the payload the render wrote ({payload_path(args.subject)}), so it "
                "does not need a table on stdin — re-run it with `< /dev/null`, or with no "
                "redirect at all.",
                file=sys.stderr,
            )
            return EXIT_STDIN_TIMEOUT
        # A table on stdin is refused, never recorded. `--save` reads the payload the
        # render wrote and dates the record from *its* mtime — the hop this gate used to
        # leave to the caller's memory. A caller that hands the table over itself moves the
        # stamp to save time instead, so a stale table reads as fresh and the next tick
        # replays it. Measured 2026-09-30 on the MDM Bugs record: `recorded_at` 11:41:29
        # against a payload written 11:35:22, written by a `--save … < <payload>.table`
        # redirect. The shape is in no shipped command block (the heredoc left the three at
        # 0.63.0), so nothing legitimate depends on it — but the CLI's own help and
        # docstring still advertised it, which is what invited the call.
        #
        # The read is bounded (see `read_stdin_bounded`). It used to be a bare
        # `sys.stdin.read()`, which stalled a manager tick for 45 s on a stdin that never
        # EOFs and left the store a full tick stale — [[The Pre-Dispatch Save Blocks on an
        # Open Stdin Pipe and Silently Stalls the Loop]]. Refusing on a non-empty read is
        # what makes the caller-supplied table impossible; the bound is what makes the
        # refusal reachable at all.
        if table.strip():
            print(
                "refusing a table on stdin: `--save` reads the payload the render wrote "
                f"({payload_path(args.subject)}) so the record is dated from its mtime, not "
                "from save time. Write the table with `--write-payload`, then re-run "
                "`--save` with no stdin.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        # An absent payload falls through to the empty-table guard below, which already
        # refuses rather than clobbering a good snapshot.
        rendered_at = None
        try:
            with open(payload_path(args.subject), encoding="utf-8") as fh:
                table = fh.read()
        except OSError:
            table = ""
        else:
            rendered_at = (
                datetime.fromtimestamp(os.path.getmtime(payload_path(args.subject)))
                .astimezone()
                .isoformat(timespec="seconds")
            )
        if not payload:
            # `evaluate()` returns an empty payload on both of its fail-open paths, where
            # there is no branch, digest or tracked set to record. `--check` already
            # reports that as a CHANGE verdict; `--save` must report the same one rather
            # than dereferencing `payload["branch"]` and raising. The snapshot cannot be
            # written either way, and a traceback reads to the caller as a failure rather
            # than as a verdict to gate on — so the subject would be re-swept on every
            # tick, which is the cost the fail-open rule exists to avoid. On stdout, to
            # match `--check`; the two refusals below stay on stderr, where they belong.
            print(f"CHANGE {reason}")
            return EXIT_CHANGE
        if not table.strip():
            # Same discipline as fleet-snapshot.py's empty guard: a failed render must not
            # clobber a good snapshot, and a snapshot with no table can never be replayed.
            print(
                "refusing to save an empty table: the snapshot is left unchanged, and this "
                "run reports CHANGE so the next one re-sweeps rather than replaying nothing.",
                file=sys.stderr,
            )
            return EXIT_CHANGE
        bucket_sets = None
        # Default to the gate's own bucket path when the caller names none, the same way
        # the payload half is found without being named. The *producer* writes it now
        # (the sweep reader's own `--write-buckets` call), so the caller has no path to
        # carry and none to get wrong — the hop this default removes. An explicitly named
        # path keeps the strict refusal below, because a caller that names one is
        # asserting it exists; a defaulted path that is absent means "no bucket half was
        # produced this tick", which is the pre-existing behaviour of omitting the flag.
        buckets_from = args.buckets or buckets_path(args.subject)
        if args.buckets or os.path.exists(buckets_from):
            try:
                with open(buckets_from, encoding="utf-8") as fh:
                    bucket_sets = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                # A usage error, not a verdict — and deliberately NOT a fail-open. The
                # gate's fail-open rule exists so an unreadable *state* file re-sweeps
                # rather than reporting a false "no change"; here the caller has handed
                # over a path it believes holds the classification. Writing the record
                # without the half would let it read back as persisted when it is not,
                # and clause (0) would then hold an entire batch on a half the caller
                # thought it had supplied. Refuse, and let the caller fix the path.
                print(
                    f"could not read bucket sets from {buckets_from!r}: {exc}",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            # The same refusal `--write-buckets` makes, and for the same reason — this is
            # the other door into the record, and validating only the staging path left a
            # hand-written or stale file able to reach `save_stored` with a set that
            # cannot gate. A record written that way reads back as persisted when it is
            # not, which is precisely what clause (0) would then hold a batch on.
            declared, declaration_error = declared_bucket_names_for(
                args.vault, args.runbook
            )
            if declaration_error:
                print(declaration_error, file=sys.stderr)
                return EXIT_USAGE
            shape_error = bucket_shape_error(bucket_sets, declared)
            if shape_error:
                print(
                    f"bucket sets at {buckets_from!r} cannot gate: {shape_error}",
                    file=sys.stderr,
                )
                return EXIT_USAGE
        # The bucket half is a save input in its own right, not a passenger on the digest.
        # `digest_of` covers the tracked set only, so a tick whose tree did not move reports
        # `digest equal` and used to write nothing at all — discarding a freshly staged,
        # *correct* classification and leaving a corrected re-stage unable to heal a bad
        # record. Measured 2026-09-29 on the Attention Routing loop: a correct 21/4/89 set
        # was staged, `--save` returned `SAVED no-change (digest equal)` at exit 0, and the
        # store kept the previous tick's sets, missing a task filed that minute.
        buckets_moved = bucket_sets is not None and bucket_sets != payload.get(
            "stored_bucket_sets"
        )
        if changed or buckets_moved:
            try:
                save_stored(
                    args.subject,
                    payload["branch"],
                    payload["digest"],
                    table,
                    payload["tracked"],
                    payload["busy"],
                    bucket_sets,
                    rendered_at,
                )
            except OSError as exc:
                print(f"fail-open: could not record state ({exc})", file=sys.stderr)
                return EXIT_CHANGE
            print(f"SAVED {reason if changed else 'bucket sets differ'}")
            print_provenance(args.subject, args.vault, bucket_sets is not None)
            return EXIT_CHANGE
        print(f"SAVED no-change ({reason})")
        print_provenance(args.subject, args.vault, bucket_sets is not None)
        return EXIT_NOCHANGE

    if args.check:
        print(("CHANGE " + reason) if changed else "NOCHANGE")
        return EXIT_CHANGE if changed else EXIT_NOCHANGE

    # --print: the whole point. On no-change the operator still sees the frame — silence
    # would be ambiguous, and a quiet run and a dead run look identical from outside.
    if changed:
        print(f"CHANGE {reason}")
        return EXIT_CHANGE

    print(
        f"⏸ {NO_CHANGE_MARKER} since {payload['recorded_at'] or 'the last sweep'} — "
        "prior sweep replayed, 0 agents dispatched"
    )
    print(stored_table.rstrip("\n"))
    return EXIT_NOCHANGE


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
