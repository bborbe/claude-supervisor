# The drive audit budget

The single home for the manager drive leg's wall-clock bound: why it exists, why it is a
clock rather than a row cap, and what its three branches mean. `agents/manager-drive.md`
clause (1) states the rule and points here; the script is `scripts/drive-audit-budget.py`.

## The defect

`agents/manager-drive.md` clause (1) scores every non-terminal row with no live worker by
dispatching **one `vault-cli:task-auditor` subagent per row**, sequentially. Nothing bounded
that loop, so its duration was a function of the tracked set's size and of the audit cache's
warmth — and a cold tick ran to completion however long that took.

On a round with **zero actionable rows** the audit is the *whole* of the remaining work, so
the leg's duration equals the cold audit's duration. That is the case this bound exists for.

Measured **2026-10-16** on the zero-actionable subject `Recurring Operator Gates Are Fixed
Once, Not Answered Every Session` — its store record carries `ready-to-start → 0` and
`stuck → 0`, so there was nothing to open and nothing to nudge:

| artifact | mtime |
|---|---|
| `.tracked.txt` | 11:05:04 |
| `.table` | 11:14:49 |
| store `.json` | 11:17:43 |
| `.verdicts.json` | **11:39:01** |

≈ 34 minutes, the cold audit being all of it — the cache that tick wrote is that same tick's
output. Corroborated on `attention-controller-is-the-fleet-s-single-source-of-session-state`,
09:24 → 09:43 ≈ 19 minutes. `commands/steps/manager-drive.md` had already recorded the
sibling measurement: **27 dispatches, 0 cache hits, ~246k subagent tokens, 29 minutes.**

## Why a wall clock, and not a row cap

Clause (1) forbids a per-tick row cap in as many words:

> Do not invent a budget, a batch size or a sampling rule: there is no per-tick cap in this
> clause, and a leg that introduces one silently narrows the set the caller asked it to cover.

That prohibition is about **silence**. A row cap truncates coverage and reports nothing, so a
narrowed sweep is indistinguishable from a complete one.

A wall clock that ends the loop **loudly** is the opposite shape, and it is the one clause (1)
already blesses — the `Cap failure` block, *"a FAILURE, not a `Held`"*: the bound is named, the
rows audited are counted, and every row left is reported as unaudited. A caller reading the
report can tell a bounded stop from a complete sweep, which is the whole of what the
prohibition protects.

**The bound can only ever shorten a walk.** It admits no row the audit would have skipped, and
it licenses no act on a row it did not score.

## The contract

Read the leg's own start once (`date -u +%Y-%m-%dT%H:%M:%SZ`) and, **before every
`task-auditor` dispatch**, run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/drive-audit-budget.py \
  --started-at "<that stamp>" --audited <n> --total <m>
```

The verdict is the leading token:

| token | exit | act |
|---|---|---|
| `CONTINUE` | 0 | dispatch the next row |
| `TERMINAL:` | 3 | **stop dispatching** — print the line verbatim on the report's `Drive:` block, and name every unaudited row under `Cap failure` |
| `REFUSED` | 2 | the read failed, so there is **no** bound — say so, and take **neither** branch |

⚠️ **`REFUSED` is a third state, not a flavour of the other two.** A caller that reads a
refused clock as `CONTINUE` has no bound at all while every check reads green — the exact
unbounded state the fix exists to end. A caller that reads it as `TERMINAL` stops a sweep on a
typo.

⚠️ **An unaudited row gets no verdict and is never opened.** A row the bound left is reported,
never acted on — the same rule the `Cap failure` block states for a row the dispatcher refused.

## Single homes

- **The bound's value** is the `DEFAULT_BUDGET_SECONDS` constant in
  `scripts/drive-audit-budget.py`, and nowhere else. A second copy in prose is one a `grep`
  cannot tell from a real one.
- **The rule** is `agents/manager-drive.md` clause (1).
- **This page** is the rationale. `scripts/check-drive-audit-budget.py` keeps the clause wired
  to the script — the callee exists, the script is named, all three branch tokens survive, the
  value is stated nowhere in the prose, and clause (1)'s row-cap prohibition is intact (a
  reader who resolves the apparent tension by deleting the prohibition has removed the
  distinction the fix rests on).
