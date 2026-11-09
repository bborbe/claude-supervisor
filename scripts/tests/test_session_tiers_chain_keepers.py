#!/usr/bin/env python3
"""Conformance test: every chain tier in `docs/session-tiers.md` § Keep-alive has a keeper.

`docs/session-tiers.md` § Keep-alive is the single home of the keep-alive contract — its own
opening paragraph says so. The chain table under `### The chain` is prose, so a row whose
*Mechanism* cell names a script that was renamed, split or deleted still reads as covered and
nothing fails. This test is the mechanical half: it parses the table and fails when a tier
loses its keeper.

## The parse contract

Read from `docs/session-tiers.md`, section `## Keep-alive`, subsection `### The chain`: the
first markdown table in it. The header line and the `|---|` separator are discarded; every
following `|`-line up to the first non-`|` line is a tier row.

**Columns read.** The table has five, in this order: `Level`, `Opened by`, `Kept alive by`,
`Mechanism`, `Guard`. Only `Level`, `Kept alive by` and `Mechanism` are asserted on; the other
two are read so the cell indices stay honest.

**Row count is an independent literal.** `TIER_COUNT = 7` is not derived from the parse, so a
parser that silently drops a row cannot also supply the number it is graded against — the
count test fails on `6 ≠ 7` instead of agreeing with itself.

**Artifacts live in `Mechanism`.** The `Kept alive by` column names an *actor* — `the operator`,
`the fleet round`, `its goal manager's round` — never an artifact, so reading that column as
the artifact carrier fails six of seven rows. Both cells are scanned anyway, because a future
row could name a path there; in the table as it stands every hit comes from `Mechanism`.

**Allowed artifact shapes.** A backticked token is an artifact only when its first path segment
is `scripts/`, `commands/` or `agents/`. The cells are full of other backticked tokens —
`stale`, `resume it`, `STOPPED`, `SLEEPING`, `UNARMED`, `jump`, `crash-loop`,
`last_auto_resume`, `worker--<slug>`, `claude_session_id` — and an extractor that took "any
backticked token" would grade prose as a missing file.

**The `--flag` span rule.** A token may be a command plus flags, e.g.
`scripts/topic-keepalive.py --branch topic`. The path is the first whitespace-delimited word of
the token; the rest is a flag span and is not part of the path.

**The multi-artifact rule.** A cell may name two or three artifacts and every one of them must
exist. Checking only the first backticked token passes while a second artifact is dead, which
is the mutation `test_a_dead_non_first_artifact_is_named` pins.

**The sub-agent decision literal.** The background sub-agent row has no keeper *by decision*:
its `Kept alive by` cell reads `no keeper, by decision 2026-11-08` and its `Mechanism` cell is
`—`. That exact pair is a pass. Any other row with an empty `Mechanism` is a keeperless tier
and fails — the row is the one deliberate exception, not a precedent.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_DOC = os.path.join(_REPO, "docs", "session-tiers.md")

SECTION = "## Keep-alive"
SUBSECTION = "### The chain"
COLUMNS = ("Level", "Opened by", "Kept alive by", "Mechanism", "Guard")
ARTIFACT_PREFIXES = ("scripts/", "commands/", "agents/")
EMPTY_CELLS = ("", "—", "–", "-")
SUBAGENT_DECISION = "no keeper, by decision 2026-11-08"
SUBAGENT_TIER = "Background sub-agent"

TIER_COUNT = 7

TIER_NAMES = {
    "Fleet manager",
    "Topic manager",
    "Goal manager — goal declared by a topic",
    "Goal manager — goal in no topic",
    "Worker — task with a goal",
    "Worker — task with no goal",
    "Background sub-agent",
}

#: Measured distinct artifacts at v0.264.1. A floor, not an equality: adding an artifact to a
#: row must not break a passing test, but an extractor that returns almost nothing must not
#: read as a clean table either.
MIN_DISTINCT_ARTIFACTS = 9

BACKTICK = re.compile(r"`([^`]+)`")


def read_doc():
    with open(_DOC, encoding="utf-8") as handle:
        return handle.read()


def _section_lines(text, heading):
    """Lines under `heading`, up to the next heading of the same or a higher level."""
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if line.strip() == heading:
            break
    else:
        return []
    level = len(heading) - len(heading.lstrip("#"))
    out = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            depth = len(stripped) - len(stripped.lstrip("#"))
            if depth <= level:
                break
        out.append(line)
    return out


def _table_rows(lines):
    """Data rows of the first markdown table in `lines`, as raw `|`-lines."""
    rows = []
    seen_header = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if seen_header and rows:
                break
            continue
        if not seen_header:
            seen_header = True
            continue
        if re.fullmatch(r"\|[\s:|-]+\|", stripped):
            continue
        rows.append(stripped)
    return rows


def _cells(row_line):
    return [cell.strip() for cell in row_line.strip().strip("|").split("|")]


def parse_chain(text):
    """Tier rows of the keep-alive chain table, as dicts keyed by column name."""
    chain = _section_lines("\n".join(_section_lines(text, SECTION)), SUBSECTION)
    rows = []
    for raw in _table_rows(chain):
        cells = _cells(raw)
        if len(cells) != len(COLUMNS):
            raise AssertionError(f"chain row has {len(cells)} cells, want {len(COLUMNS)}: {raw}")
        rows.append(dict(zip(COLUMNS, cells)))
    return rows


def tier(row):
    return row["Level"].replace("*", "").strip()


def artifacts(cell):
    """Backticked artifact paths in one cell; `--flag` spans are stripped to the path."""
    found = []
    for token in BACKTICK.findall(cell):
        token = token.strip()
        if not token.startswith(ARTIFACT_PREFIXES):
            continue
        found.append(token.split()[0])
    return found


def row_artifacts(row):
    return artifacts(row["Kept alive by"]) + artifacts(row["Mechanism"])


def is_keeperless(row):
    return row["Mechanism"].strip() in EMPTY_CELLS


def keeper_problems(rows):
    """Every way the table fails its contract, as human-readable lines."""
    problems = []
    for row in rows:
        level = tier(row)
        if is_keeperless(row):
            plain = row["Kept alive by"].replace("*", "").strip()
            if SUBAGENT_DECISION not in plain:
                problems.append(
                    f"{level}: no keeper and no decision — Mechanism is empty but "
                    f"Kept alive by does not state {SUBAGENT_DECISION!r}"
                )
            continue
        if "no keeper" in row["Kept alive by"].lower():
            problems.append(f"{level}: Kept alive by claims no keeper but Mechanism names one")
        for path in row_artifacts(row):
            if not os.path.exists(os.path.join(_REPO, path)):
                problems.append(f"{level}: keeper artifact does not exist: {path}")
    return problems


class ChainTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read_doc()
        cls.rows = parse_chain(cls.text)

    def _chain_row_index(self, prefix):
        """Index of the first line in the chain subsection starting with `prefix`.

        Scoped to the subsection on purpose: `| **Fleet manager**` also opens a row of the
        tier-summary table in `### The four tiers`, above § Keep-alive, and an unscoped
        `startswith` search mutates that table instead — which leaves the chain intact and
        makes the control test read 7 rows, i.e. green for the wrong reason.
        """
        lines = self.text.splitlines()
        start = next(i for i, line in enumerate(lines) if line.strip() == SUBSECTION)
        for index in range(start, len(lines)):
            if lines[index].startswith(prefix):
                return index
        self.fail(f"no chain row starts with {prefix!r}")

    def _rewrite_subagent(self, kept_alive):
        lines = self.text.splitlines()
        index = self._chain_row_index(f"| **{SUBAGENT_TIER}**")
        cells = _cells(lines[index])
        cells[2] = kept_alive
        lines[index] = "| " + " | ".join(cells) + " |"
        return "\n".join(lines)

    # --- SC1: the parse yields the named tier set, from the table itself -------------

    def test_table_parses_into_seven_named_tier_rows(self):
        self.assertEqual(
            len(self.rows),
            TIER_COUNT,
            f"{len(self.rows)} ≠ {TIER_COUNT} tier rows — a row was added, removed, or the "
            "table moved. The count is an independent literal, so a parser that drops a row "
            "cannot grade itself.",
        )
        self.assertEqual({tier(row) for row in self.rows}, TIER_NAMES)

    def test_a_removed_row_reads_six(self):
        """Control for SC1: the rows come from the table, so deleting one is visible."""
        lines = self.text.splitlines()
        index = self._chain_row_index("| **Fleet manager**")
        mutated = "\n".join(lines[:index] + lines[index + 1:])
        self.assertEqual(len(parse_chain(mutated)), TIER_COUNT - 1)

    # --- SC2: every keeper artifact resolves, all of them ---------------------------

    def test_every_keeper_artifact_exists(self):
        found = [(tier(row), path) for row in self.rows for path in row_artifacts(row)]
        self.assertGreaterEqual(
            len({path for _, path in found}),
            MIN_DISTINCT_ARTIFACTS,
            "the extractor found almost no artifacts — a broken pattern is "
            "indistinguishable from a clean table",
        )
        missing = [
            f"{level}: keeper artifact does not exist: {path}"
            for level, path in found
            if not os.path.exists(os.path.join(_REPO, path))
        ]
        self.assertFalse(missing, "\n".join(missing))

    def test_every_keeper_row_names_at_least_one_artifact(self):
        bare = [tier(row) for row in self.rows if not is_keeperless(row) and not row_artifacts(row)]
        self.assertFalse(bare, f"keeper rows naming no artifact: {bare}")

    def test_the_table_satisfies_the_keeper_contract(self):
        """The whole contract over the real table, as one verdict.

        The per-row tests above cover artifact existence and the sub-agent decision
        individually, and between them they miss one case: a *different* row losing its
        keeper. A row whose Mechanism cell is blanked contributes no artifact to miss,
        is excluded from the "names at least one artifact" check by `is_keeperless`, and
        `test_the_subagent_row_is_keeperless_by_decision` only ever inspects the
        sub-agent row — so all of them stay green while a tier has no keeper, which is
        the exact failure this file exists to catch. This is the only assertion that
        runs `keeper_problems` against the parsed rows rather than a mutated copy.
        """
        problems = keeper_problems(self.rows)
        self.assertFalse(problems, "\n".join(problems))

    def test_a_dead_non_first_artifact_is_named(self):
        """SC2's red case: rename the SECOND artifact of a three-artifact row, leaving the
        first valid. A first-token-only check stays green and is caught here."""
        row = next(r for r in self.rows if tier(r) == "Worker — task with a goal")
        paths = row_artifacts(row)
        self.assertEqual(len(paths), 3, f"expected three artifacts in {tier(row)}, got {paths}")
        dead = "scripts/this-artifact-was-renamed.py"
        mutated_cell = row["Mechanism"].replace(f"`{paths[1]}", f"`{dead}", 1)
        self.assertNotEqual(mutated_cell, row["Mechanism"], "the mutation did not apply")
        problems = keeper_problems(parse_chain(self.text.replace(row["Mechanism"], mutated_cell, 1)))
        self.assertTrue(problems, "a dead non-first artifact went unreported")
        self.assertTrue(
            any(dead in problem for problem in problems),
            f"the failure does not name {dead}: {problems}",
        )

    # --- SC3: the sub-agent row is deliberate, not a gap ---------------------------

    def test_the_subagent_row_is_keeperless_by_decision(self):
        row = next(r for r in self.rows if tier(r) == SUBAGENT_TIER)
        self.assertTrue(is_keeperless(row), f"{SUBAGENT_TIER} Mechanism should be empty")
        self.assertIn(SUBAGENT_DECISION, row["Kept alive by"])

    def test_a_keeperless_row_with_the_decision_passes(self):
        """Positive control: the same row shape, carrying the decision literal."""
        mutated = self._rewrite_subagent(f"**{SUBAGENT_DECISION}**")
        problems = [p for p in keeper_problems(parse_chain(mutated)) if SUBAGENT_TIER in p]
        self.assertFalse(problems, "\n".join(problems))

    def test_a_keeperless_row_without_the_decision_fails(self):
        """Negative control: the same row shape, keeperless and undecided."""
        mutated = self._rewrite_subagent("**no keeper**")
        problems = keeper_problems(parse_chain(mutated))
        self.assertTrue(
            any(SUBAGENT_TIER in problem for problem in problems),
            f"a keeperless row without the decision literal passed: {problems}",
        )


if __name__ == "__main__":
    unittest.main()
