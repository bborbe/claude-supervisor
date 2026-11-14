"""The single predicate for "does this page's flag open it" — one rule, all three subjects.

Operator ruling 2026-11-10 (attention card `f8cd8b41…`): **unify on refuse** — an
agent-set flag opens nothing at any tier. Before this module the rule lived in four
places and disagreed with itself: a **task** refused an agent-set flag
(`flagged-rows.refusals()`), a **goal or topic** accepted any provenance
(`topic-spawn.flagged()`, `topic-keepalive`), and the round's own open leg read the flag
with no provenance at all (`open-queue.is_flagged()`).

`flag_admits()` is the only place that decides. Everything that opens a subject — the
task batch, its resolver, the goal/topic selection leg, the topic spawn and keep-alive
predicates, and the queue — calls it. **Do not restate it.**

⚠️ Provenance is read for **every** kind, not only for tasks. That is the whole change:
a `flag_set_by` outside `OPENING_ACTORS` — an absent field included — does not admit the
row at any tier. The 2026-10-30 ruling (*"in the end it should not matter how set the
flag"*) governs WHICH flagged row may be opened once it is operator-set; it never made an
agent-set flag an approval. The two rulings are consistent, and
`docs/fleet-surface.md` § Approval provenance is the page that says so.
"""

import re

#: The only `flag_set_by` values that admit a row. `legacy` is the 270 pre-field rows
#: dispositioned on 2026-09-30 — absence was converted into that explicit marker so that
#: a missing field could mean *refuse* without silently breaking the operator's own set.
OPENING_ACTORS = ("operator", "legacy")

#: A **task** flag must be a YAML boolean, because `commands/open.md` § Step 0.5 reads
#: `t.get('flag') is True` off a parsed file. A quoted scalar is a STRING in YAML and is
#: rejected here exactly as it is there.
YAML_TRUE = ("true", "True", "TRUE", "yes", "Yes", "YES",
             "on", "On", "ON", "y", "Y")

#: A **goal or topic** flag is read textually and tolerantly — those pages are edited by
#: hand and by several writers, and the goal/topic readers historically also accepted `1`.
FLAG_TRUTHY = ("true", "yes", "on", "1")

_FLAG_LINE = re.compile(r"^flag:[ \t]*(.*)$", re.MULTILINE)


def scalar(block, key):
    """One frontmatter scalar with quotes stripped — None when the key is absent."""
    match = re.search(r"^%s:[ \t]*(.*)$" % re.escape(key), block, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def flag_set(block, kind):
    """Is `flag:` set, read under its KIND's truthiness rule?"""
    raw = _FLAG_LINE.search(block or "")
    if not raw:
        return False
    value = raw.group(1).strip()
    if kind == "task":
        # The strict branch cannot strip quotes: `flag: "true"` is a string, not True.
        if value.startswith(('"', "'")):
            return False
        return value in YAML_TRUE
    return value.strip("\"'").lower() in FLAG_TRUTHY


def flag_admits(block, kind):
    """Does this page's `flag:` open it? The one decision, for every kind and tier."""
    if block is None:
        return False
    return flag_set(block, kind) and scalar(block, "flag_set_by") in OPENING_ACTORS


def flag_refusal(block, kind):
    """Why the flag does not admit this page — None when it does.

    Kept separate from `flag_admits()` so a refusal names its reason: an agent-set flag
    is not an unflagged row, and reporting it as `not flagged` would hide the very write
    this rule exists to refuse.
    """
    if block is None:
        return "unreadable page"
    if not flag_set(block, kind):
        return "not flagged"
    if scalar(block, "flag_set_by") in OPENING_ACTORS:
        return None
    return "agent-set-flag"
