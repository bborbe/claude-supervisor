# The blocked-row re-read

The single home for the rule that a `Waiting on your keystroke` row is verified against the live
probe before the fleet round renders it. `agents/fleet-drive.md` § `Waiting on your keystroke`
states the rule and points here; the probe is `scripts/who-needs-me.py`.

## The defect

The digest's `BLOCKED (feed, raised — not verified open)` section is written by the sweep, and
the drive leg renders it afterwards. Between those two moments a gate can be answered, reaped,
or simply stop mattering because the worker moved on — and the round then tells the operator
they are "waiting on your keystroke" for something already closed.

Measured **2026-10-29**: of four `BLOCKED` rows, **three had already resolved** before the round
rendered — one keystroke ask was gone, one pane was waiting on a bot review with `nothing`
owed, one had finished planning and moved to a `pick` on its own subtask. Establishing that by
hand cost three pane checks. The operator cannot tell a real ask from a stale one, and the
section is *the operator's* to-do list, so a stale row is not cosmetic noise — it is the list
being wrong.

## The rule

**Per row, against the live probe — never a re-run of the feed.** The feed is what was stale.
Run the probe once per round and match each row on its **pane id**.

- **The join key is the pane id, and it is the only one both sides carry.** The digest's
  `BLOCKED` row renders `<name> · pane <id> · <link> · <gate text>` (`agents/fleet-sweep-reader.md`
  § `<output_format>`) and the probe renders `[<pane>] <age> <name> …` — **neither carries a
  session id**, and names are not unique across a fleet. A rule that matched on session id
  could not be executed at all; that was this document's first draft, caught in review
  2026-10-29. A row with `pane —` (the digest's "no pane" form) has no key and is **kept**
  rather than dropped, with the re-read marked `no pane to re-read`.
- **`--section needs-you` is the only section that is gates.** The feed's other sections —
  `rendered-panels`, `stuck`, `reapable`, `idle` — are not gates, and `last-closers` is opt-in
  precisely because arming on it would fire on every peer's rendered closer. Read `--all`, so
  a row past the default 20 is not dropped for being late in the list.
- **Stamp the read.** Take the wall clock at the moment the probe runs and carry that stamp on
  every row the probe confirmed. It is the only timestamp that says *when* the row was
  verified, and without it the render cannot be distinguished from the digest it came from.
- **A row the probe does not list is dropped — and the drop is printed**, beneath the section:
  `re-read <stamp>: no open gate for <name> [pane <id>]`. A section that quietly shrinks is
  indistinguishable from a fleet that never had a gate, so the operator must be able to see
  that a row existed and was cleared.
- **A probe that errors leaves every row standing**, and the section carries
  `re-read failed: <why> — rows unverified`. An unreadable store is not a clearance. Folding an
  error into "no open gate" is the one direction that would let a real ask vanish silently, and
  it is the same suppression the rule exists to prevent.

## Why both directions are load-bearing

Deleting the section satisfies the "no stale row" half and none of the intent. A gate the probe
**does** list still renders, with its live stamp. The two directions guard each other: without
the drop, a closed gate renders as an ask; without the retention, a live ask is deleted along
with the stale ones. Either alone is a regression, and a change that does only one of them
should be refused in review.

## What this is not

It is not a per-row *audit* — the re-read is one probe call for the whole round, not one
subagent per row, so it does not touch the wall-clock bound `docs/drive-audit-budget.md`
describes. It is also not a re-verification of the digest's other sections: `ESCALATION`,
`RETAINED` and `PARKED` are the manager's, and they carry their own evidence.
