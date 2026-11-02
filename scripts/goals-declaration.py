#!/usr/bin/env python3
"""The `goals:` declaration reader — ONE normaliser for every shape the vault writes.

**One home for the read, because THREE consumers in this repo take it.**
`scripts/manager-predispatch.py` (`read_task` → `resolve_tracked` → the tracked set),
`scripts/reset.py` (`goals_of` → the re-discovered tracked set) and
`scripts/topic-spawn.py` (`goals_of` → the startable-task count per goal) each carried
their own extractor, and they disagreed: `manager-predispatch` read wikilinks only,
`reset` read all four shapes, and `topic-spawn` read the shapes but blank-line-tolerantly.
Three readers of one declaration is the drift this module removes, on the same reasoning
`declared-wait.py` and `session-identity.py` are separate scripts rather than private
copies.

⚠️ **A FOURTH copy exists outside this repository and cannot import this one.** The
vault-local `<vault>/.claude/scripts/sweep-gate.py` carries the same reader as a
deliberate fork — it runs where `${CLAUDE_PLUGIN_ROOT}` is not set, so importing the
plugin's copy would be a fail-open. Its own docstring calls `manager-predispatch.py`
"the plugin's port", and the two must be edited in lockstep by hand. Fixing one of them
alone leaves `--compare-tracked` divergent while every test in either repo stays green.

⚠️ **The defect this module exists to remove is a SILENT DROP, not an error.** A `goals:`
block item written as a bare list entry —

    goals:
        - No Session Sits Idle Unnoticed and a Lapsed Manager Is Detected

— carries no brackets, so a `[[...]]`-only extractor returns `[]` for it. `resolve_tracked`
admits a task only when its goals intersect the member set, so that task vanished from the
tracked set with no row, no warning and no failed assertion. Measured 2026-11-02 on the
live `private-personal` vault: the caller's scan read 61 names and the gate's snapshot read
60, differing by exactly the one task that declared its goal in this shape.

The shapes, all live on disk (`commands/goal-loop.md`'s table lists the first three; the
fourth is the one it omits and the one that was dropped):

    goals:                          block list of wikilinks
        - '[[X]]'
    goals:                          block list of bare items   <- the dropped one
        - X
    goals:                          nested list-of-list
        - - X
    goals: X                        inline scalar
    goals: ['[[X]]', 'Y']           inline flow list

An empty declaration is not a shape: `goals:`, `goals: []`, `goals: null` and `goals: ~`
name no goal and are correctly `[]` — case-insensitively, because YAML's null is.

⚠️ **A comma is not always a separator, in either half of the read.** The block branch
learned this first (a bare `- Name` is one item). The inline branch has the mirror trap: a
quoted flow-list item may carry a comma of its own —
`goals: ['[[Digest Watcher Judges File Arrival, So a Late-Landing Digest Trips a False
Report on a Timely Round]]']` is ONE name. Splitting the flow list before locating its
item boundaries shatters it into fragments that match no member, which is the same silent
drop one shape over. `_split_outside_quotes` is what keeps the two apart.

⚠️ **The normaliser strips until it stops changing, and that is what collapses the shapes
to one answer.** A nested item needs two rounds — `- - '[[X]]'` → `- '[[X]]'` → `'[[X]]'`
→ `[[X]]` → `X` — and a single pass leaves a stray `-` on the name, which then matches no
page and reads as UNMET. That is the same silent-drop class one level down, which is why
the loop is here rather than a fixed sequence of three `.strip()` calls.
"""

from __future__ import annotations

import re

#: The key line. `(.*)$` under `re.M` so an inline value is captured whole.
_GOALS_KEY = re.compile(r"^goals:(.*)$", re.M)

#: A block item: optional indent, then a `-`. `[ \t]*` rather than `[ \t]+` because YAML
#: allows a list item flush left, and the walk below is anchored to the line after `goals:`
#: so a column-0 `- ` there is a list item and nothing else.
_BLOCK_ITEM = re.compile(r"[ \t]*-")

#: Every spelling of "declares nothing", compared case-insensitively.
_EMPTY = frozenset({"", "[]", "null", "~"})

_QUOTES = "'\""


def _strip(value: str) -> str:
    """Reduce one declaration item to the bare page name, repeating until stable.

    Covers `- '[[X]]'`, `goals: X`, `- - X`, `['[[X]]']` and a bare `- X`. The loop is
    what makes the nested shape work: each round removes one layer of `- `, then one layer
    of quotes, then one wikilink wrapper, so a two-deep item needs two rounds.

    ⚠️ **Two narrowings, both of them the silent-drop class in reverse.** A leading `-` is
    stripped only when it is a list marker — followed by whitespace or at end-of-string —
    so a name that legitimately begins with a hyphen survives. And the alias/heading split
    applies **only inside a wikilink**: `[[Page#Heading]]` names `Page`, while a bare name
    carrying `#` or `|` is a name, and truncating it would be the same silent drop this
    module exists to remove.
    """
    value = _strip_comment(value)
    previous = None
    while previous != value:
        previous = value
        value = value.strip()
        while value.startswith("-") and (len(value) == 1 or value[1] in " \t"):
            value = value[1:].strip()
        while len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1].strip()
        if value.startswith("[[") and value.endswith("]]"):
            value = value[2:-2].split("|")[0].split("#")[0].strip()
    return value


def _opens_quote(buffer: list[str]) -> bool:
    """Whether a quote character here starts a quoted item rather than being literal.

    ⚠️ **Only an item-initial quote opens.** Toggling on every `'` reads the apostrophe in
    `The Manager Loop's Coverage Step` as an opening quote, swallows the comma after it,
    and merges two names into one that matches no page — the same silent drop this module
    exists to remove. An apostrophe is punctuation inside a name; a quote at the start of
    an item is YAML quoting its value.
    """
    return not "".join(buffer).strip()


def _strip_comment(value: str) -> str:
    """Drop a YAML inline comment: a `#` preceded by whitespace and outside a quoted item.

    ⚠️ **Not cosmetic — a comment-bearing declaration used to work.** Both readers this
    module replaces extracted `X` from `- '[[X]]' # note`; without this the unified reader
    normalises the whole line to `'[[X]]' # note`, which matches no page, and the task
    leaves the tracked set. That is the silent-drop class reintroduced by the unification.
    YAML requires whitespace before `#` for it to begin a comment, which is what keeps a
    `#` inside a name or inside a wikilink heading intact.
    """
    quote: str | None = None
    for index, char in enumerate(value):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in _QUOTES and not value[:index].strip():
            quote = char
            continue
        if char == "#" and index > 0 and value[index - 1] in " \t":
            return value[:index].rstrip()
    return value


def _split_outside_quotes(text: str) -> list[str]:
    """Split on commas that are not inside a quoted item.

    ⚠️ **A quoted item's own comma is not a separator.** `['[[Name, With Comma]]']` is ONE
    name; splitting first yields `"'[[Name"` and `" With Comma]]'"`, neither of which
    matches a member, so the task leaves the tracked set silently. Locating item boundaries
    before looking inside them is the same insight as the block branch's bare-item fix,
    stated for the other half of the read.
    """
    parts: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            buffer.append(char)
            if char == quote:
                quote = None
        elif char in _QUOTES and _opens_quote(buffer):
            quote = char
            buffer.append(char)
        elif char == ",":
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return parts


def _inline_names(rest: str) -> list[str]:
    """The names an inline `goals:` value declares.

    ⚠️ **The leading `[` decides whether to split.** A flow list
    (`goals: ['[[X]]', 'Y']`) separates its items with commas; a bare scalar
    (`goals: X`) does not, and a scalar may legitimately carry a comma of its own — task
    and goal names in this vault routinely do. Splitting a scalar on commas would shatter
    exactly the names the coverage gate was just fixed not to shatter.
    """
    if rest.startswith("[") and rest.endswith("]"):
        return [_strip(part) for part in _split_outside_quotes(rest[1:-1])]
    return [_strip(rest)]


def _block_items(frontmatter: str, after: int) -> list[str]:
    """The lines of a block-form declaration, walking from just past the `goals:` key.

    A line walk rather than a regex over the whole block, for two measured reasons:

    - **The block can end mid-line.** A caller that slices frontmatter by its `---`
      delimiter hands over a value whose last line carries no trailing newline, and a
      regex requiring one returns `[]` for the entire block — every shape, silently.
    - **A blank line does not end the block.** YAML permits one between the key and its
      items, and `topic-spawn.py`'s previous reader tolerated it while the other two did
      not. That divergence would have become a silent drop the moment the readers were
      unified. Blank lines are collected and then filtered, never treated as a terminator.

    The walk stops at the first line that is neither blank nor a `- ` item, which is where
    a following frontmatter key begins.
    """
    out: list[str] = []
    for line in frontmatter[after:].splitlines():
        if not line.strip():
            out.append(line)
            continue
        if not _BLOCK_ITEM.match(line):
            break
        out.append(line)
    return out


def goal_names(frontmatter: str) -> list[str]:
    """Every goal name `goals:` declares, in file order, deduplicated.

    `frontmatter` is the YAML block's text — with the `---` delimiters removed, or without
    them, since the key is matched by line either way. An absent key and every empty
    spelling return `[]`.

    YAML's `''` escape is undone on the returned names, so a declaration written
    `- '[[Bob''s goal]]'` resolves to `Bob's goal` and matches the page it names. Without
    it the name resolves to nothing, and "no page" is the UNMET direction.
    """
    if not frontmatter:
        return []
    match = _GOALS_KEY.search(frontmatter)
    if not match:
        return []
    # The comment is stripped before the empty test, or `goals: [] # note` reads as a goal
    # named `[] # note` instead of as a declaration that names nothing.
    rest = _strip_comment(match.group(1)).strip()
    if rest and rest.lower() not in _EMPTY:
        names = _inline_names(rest)
    else:
        names = [_strip(line) for line in _block_items(frontmatter, match.end())]
    out: list[str] = []
    for name in names:
        name = name.replace("''", "'")
        if name and name not in out:
            out.append(name)
    return out
