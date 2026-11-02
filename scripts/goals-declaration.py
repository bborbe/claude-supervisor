#!/usr/bin/env python3
"""The `goals:` declaration reader — ONE normaliser for every shape the vault writes.

**One home for the read, because two consumers in this repo take it.**
`scripts/manager-predispatch.py` (`read_task` → `resolve_tracked` → the tracked set) and
`scripts/reset.py` (`goals_of` → the re-discovered tracked set) each carried their own
extractor, and they disagreed: the first read wikilinks only, the second read all four
shapes. Two readers of one declaration is the drift this module removes, on the same
reasoning `declared-wait.py` and `session-identity.py` are separate scripts rather than
private copies.

⚠️ **A THIRD copy exists outside this repository and cannot import this one.** The
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
name no goal and are correctly `[]`.

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

#: The block form — the lines immediately after a `goals:` carrying no inline value.
#: ⚠️ `[ \t]*` rather than `[ \t]+`: YAML allows a list item flush left, and this pattern
#: is anchored to the line right after `goals:`, so a column-0 `- ` there is a list item
#: and nothing else. The optional trailing newline is load-bearing — a caller that slices
#: frontmatter by delimiter can hand over a block ending mid-line.
_GOALS_BLOCK = re.compile(r"^goals:[ \t]*\n((?:[ \t]*-[^\n]*\n?)*)", re.M)

#: Every spelling of "declares nothing".
_EMPTY = frozenset({"", "[]", "null", "~"})


def _strip(value: str) -> str:
    """Reduce one declaration item to the bare page name, repeating until stable.

    Covers `- '[[X]]'`, `goals: X`, `- - X`, `['[[X]]']` and a bare `- X`. The loop is
    what makes the nested shape work: each round removes one layer of `- `, then one layer
    of quotes, then one wikilink wrapper, so a two-deep item needs two rounds.
    """
    previous = None
    while previous != value:
        previous = value
        value = value.strip()
        while value.startswith("-"):
            value = value[1:].strip()
        while len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1].strip()
        if value.startswith("[[") and value.endswith("]]"):
            value = value[2:-2].strip()
    # A wikilink's alias and heading are not part of the page name.
    return value.split("|")[0].split("#")[0].strip()


def _inline_names(rest: str) -> list[str]:
    """The names an inline `goals:` value declares.

    ⚠️ **The leading `[` decides whether to split, and that is not cosmetic.** A flow list
    (`goals: ['[[X]]', 'Y']`) separates its items with commas; a bare scalar (`goals: X`)
    does not, and a scalar may legitimately carry a comma of its own — task and goal names
    in this vault routinely do (*"Digest Watcher Judges File Arrival, So a Late-Landing
    Digest Trips a False Report on a Timely Round"*). Splitting a scalar on commas would
    shatter exactly the names the coverage gate was just fixed not to shatter.
    """
    if rest.startswith("[") and rest.endswith("]"):
        return [_strip(part) for part in rest[1:-1].split(",")]
    return [_strip(rest)]


def goal_names(frontmatter: str) -> list[str]:
    """Every goal name `goals:` declares, in file order, deduplicated.

    `frontmatter` is the YAML block's text with the `---` delimiters already removed.
    An absent key and every empty spelling return `[]`.

    YAML's `''` escape is undone on the returned names, so a declaration written
    `- '[[Bob''s goal]]'` resolves to `Bob's goal` and matches the page it names. Without
    it the name resolves to nothing, and "no page" is the UNMET direction.
    """
    if not frontmatter:
        return []
    match = _GOALS_KEY.search(frontmatter)
    if not match:
        return []
    rest = match.group(1).strip()
    if rest and rest not in _EMPTY:
        names = _inline_names(rest)
    else:
        block = _GOALS_BLOCK.search(frontmatter)
        if not block:
            return []
        names = [_strip(line) for line in block.group(1).splitlines()]
    out: list[str] = []
    for name in names:
        name = name.replace("''", "'")
        if name and name not in out:
            out.append(name)
    return out
