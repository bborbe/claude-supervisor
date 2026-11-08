#!/usr/bin/env python3
"""A class-C `AskUserQuestion` park is routed from 5d to step 7, and the hand-off cannot be reworded away.

Before this, `commands/steps/own-parks.md` answered only class-B routine parks and named class C
as the operator's. An `AskUserQuestion` park carries no `allowlist_entry`, so the scoped wrangler
escalated it, the escalation sat, and the 15-minute timeout auto-denied it. Measured 2026-11-07:
a fleet manager's worker hit a plan gate, nobody answered, and its task file was found untouched
twenty minutes later.

The hand-off is four pieces of prose in three files, and each one fails silently on its own:

1. **5d reports the count.** Without `routed … (class C → step 7)` in `own-parks.md`, step 7 has
   no trigger, and the round prints a line that reads as a complete report.
2. **5d reports the ids and questions.** The summary carries a count only; the `request_id`s live
   on the per-park lines. A step that "takes the ids from that line" has nothing to take.
3. **Step 7 parses the same token.** If either side is reworded alone the two agree on nothing,
   and nothing in the tree notices — the round still runs, still prints, still answers nothing.
4. **The wrangler hands over the question.** `list_agents.pending_permissions` carries ids only
   and 5d holds no `pending_permissions` tool, so the wrangler's escalation text is 5d's only view
   of the question. An escalation that names only `C` cannot be classified, so the park stays
   pending and the worker times out — the original defect, reached through a different door.

**What this proves, and what it does not.** It proves the four texts are present and mutually
consistent, and that the skip case is defined rather than left ambiguous. It does **not** prove a
round routed anything: a tree whose prose is right and whose round never reaches the dispatch
passes here. The live evidence is the round's own `own-parks:` line; this guard only stops the
hand-off being deleted or softened without a test noticing.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STEPS = REPO / "commands" / "steps"
OWN_PARKS = STEPS / "own-parks.md"
WORKER_CHANNELS = STEPS / "worker-channels.md"
WRANGLER = REPO / "agents" / "goal-wrangler.md"

#: The summary token. A plain substring, so reflowing the surrounding prose leaves it intact;
#: rewording it is a deliberate act, and the failure is the reminder that both sides move together.
ROUTED_TOKEN = "routed <routed> (class C → step 7)"
#: The per-park routed line's shape. The `|` cells are what step 7 reads an id from.
ROUTED_ROW = "| AskUserQuestion | route | class C —"
#: The skip case must be defined, because a skip line carries no `routed` field at all.
SKIP_EQUIVALENCE = "Read a skip as zero routed"
#: The wrangler's obligation, stated in its own output format.
WRANGLER_OBLIGATION = "carries the question itself"
#: The safety boundary and its tie-break. Without these the routing rule is "the manager answers
#: whatever it likes", which is the one direction this change must never fail in.
BOUNDARY_HEADING = "### Is the class-C question the manager's?"
BOUNDARY_TIEBREAK = "it is the operator's"
#: The fallback for a routed park nobody answered. A routed park that comes back is escalated.
ROUTED_FALLBACK = "escalated, not routed again"
#: Wrapper parks are surfaced, never routed — measured: `claude-wrapper` denies the tool before
#: the message is read, so routing one reports an answered park the worker never saw.
WRAPPER_NOT_ROUTED = "park is never routed"


def require(path: Path, needle: str, failures: list[str], why: str) -> None:
    rel = path.relative_to(REPO)
    if not path.exists():
        failures.append(f"{rel}: missing — {why}")
        return
    if needle not in path.read_text(encoding="utf-8"):
        failures.append(f"{rel}: does not carry {needle!r}.\n      {why}")


def main() -> int:
    failures: list[str] = []

    require(
        OWN_PARKS,
        ROUTED_TOKEN,
        failures,
        "Without the routed token the class-C hand-off is invisible, and step 7 has no trigger.",
    )
    require(
        OWN_PARKS,
        ROUTED_ROW,
        failures,
        "The summary carries a count only; the request_ids and questions live on this line, and "
        "step 7 takes its ids from it.",
    )
    require(
        OWN_PARKS,
        SKIP_EQUIVALENCE,
        failures,
        "A skip line carries no `routed` field, so the skip case must say what it means rather "
        "than leave the reader to infer an unknown.",
    )
    require(
        WORKER_CHANNELS,
        ROUTED_TOKEN,
        failures,
        "Step 7 must name the same token 5d prints — two spellings of one hand-off agree on "
        "nothing, and neither side fails loudly.",
    )
    require(
        WORKER_CHANNELS,
        "per-park routed line",
        failures,
        "Step 7 must take its ids from the per-park lines, never from the count.",
    )
    require(
        WRANGLER,
        WRANGLER_OBLIGATION,
        failures,
        "5d holds no `pending_permissions` tool, so an `AskUserQuestion` escalation that does not "
        "carry the question leaves the park unrouteable and the worker waiting for the timeout.",
    )
    require(
        OWN_PARKS,
        BOUNDARY_HEADING,
        failures,
        "The operator/manager boundary is the safety half of this change — without it the rule "
        "reads as 'the manager answers whatever it likes'.",
    )
    require(
        OWN_PARKS,
        BOUNDARY_TIEBREAK,
        failures,
        "The tie-break must default to the operator. A wrong manager answer is worse than a "
        "timeout, because the worker acts on it and the round reports it as answered.",
    )
    require(
        OWN_PARKS,
        ROUTED_FALLBACK,
        failures,
        "A routed park nobody answered must escalate on the second tick. Without it a broken "
        "answer path is indistinguishable from a slow worker.",
    )
    require(
        OWN_PARKS,
        WRAPPER_NOT_ROUTED,
        failures,
        "A `pending_question` park must stay unrouted. `claude-wrapper` denies the tool before "
        "the message is read (measured 2026-11-08, two probes), so routing one reports an "
        "answered park the worker never saw — worse than the timeout it replaces.",
    )

    if failures:
        print(f"own-parks-routing FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        "own-parks-routing ok: 5d prints the routed token and its per-park lines, step 7 parses "
        "the same token and takes ids from those lines, the skip case is defined, and the "
        "wrangler's AskUserQuestion escalation carries the question"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
