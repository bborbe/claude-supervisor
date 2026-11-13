---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Plan undriven boxes

When: every tick, after row 4b

Rationale and incident history: the Manager Session runbook § Command rationale — maintained.

This step **files** the task each undriven SC/DoD box from row 4b needs and **routes** it by its door. ⚠️ **Row 4b stays read-only** — this step consumes its rows; it never re-runs or widens the coverage dispatch.

**The decision has ONE home — `scripts/undriven-plan.py decide`.** This file gathers its input and executes its output; it never re-derives a route, a cap or a ranking. Run it once per tick:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/undriven-plan.py decide <<'PAYLOAD'
<payload JSON>
PAYLOAD
```

⚠️ **The heredoc delimiter is QUOTED, and that is load-bearing.** `rows[].box` is verbatim vault prose, so it can carry an apostrophe or `$(…)`; a single-quoted argument or an unquoted heredoc would end the literal at the first quote and hand the rest to the shell. The quoted form is also why the payload is never written to a file — a temp-file write parks on a permission prompt.

## 1. Build the payload

- **`coverage_ran`** — `true` when row 4b's gate exited **10** (one owner path per line) **or 2** (fail-open: the tracked set was unreadable and every owner was dispatched, so rows exist). Pass `false` only on **exit 0** (no change): the script then files nothing and only the `pending` half runs. ⚠️ Never pass `true` on a tick 4b did not run at all — a stale row set would re-file what last tick already filed.
- **`goal_phase`** — the subject goal's `phase`, read this tick. A two-way row opens only on a goal at `execution`.
- **`rows[]`** — one per 4b row whose driving cell is `none`, carrying the row's own `box` text (verbatim), `kind` (`sc` from `# Success Criteria`, `dod` from `# Definition of Done`), `order` (its position on the page), `driving`, and `proposal`. Then add the three fields only the manager can supply:
  - **`door`** — `two-way`, `one-way-self-contained`, `one-way-wide`, or `unsure`, classified with the vault's own checklist (`One-Way and Two-Way Door Decisions` § How to classify). ⚠️ **Classify; never ask the operator to classify** — that page rules it out as an added ask. **Unsure is a valid answer** and routes as one-way.
  - **`door_reason`** — one line naming the checklist item that decided it (*"production-touching: restarts the attention-controller"*, *"vault edit, autocommitted"*). An empty reason is refused by the script.
  - **`blast_radius`** and **`rollback`** — required for every row that can reach the card or the talk-through, which is **every row except a two-way row on a goal at `execution`**. A two-way row on any other goal phase routes to `card`, so it needs them too; without them the script refuses the row (`card-fields-missing`) rather than carding an ask the operator cannot judge.
  - **`dup_of`** — the name of an existing task, **in any status** (aborted included), that already quotes this box. Search before you file: `check_duplicates` with the box text, plus `grep -lF "<box text>"` over the vault's tasks dir. A box the operator already turned down stays turned down — that is how a skip is never re-asked across rounds.
- **`pending[]`** — every tracked task at `phase: todo` with its `task`, `planned_by`, `door` and `phase` frontmatter.
- **`cap`** — `{"mode": "<the mode a manager-opened row spawns in>", "live": <worker-sessions.py --count>, "soft": <worker-target.py>}` — the same pair row 9 compares. ⚠️ **The per-mode axes (headless, interactive) are enforced by the wrapper at spawn**, not here: a row approved below this target can still be refused at the spawn, and row 6 reports that refusal as held. The limits hold either way.
- **`card`** — `{"open": <the round's waiting-approval card is still unanswered>}`. `scripts/waiting-approval-state.py read --topic "<subject>"` yields only the recorded `card_item_id`, not a state, so `open` is the **poll** verdict:

  ```bash
  ITEM="$(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/waiting-approval-state.py read --topic "<subject>" | sed -n 's/^card_item_id: //p')"
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/attention-ask.py poll "$ITEM"
  ```

  `open` is true when that prints anything other than `ANSWERED:` / `DECLINED:` / `HIDDEN:` — i.e. while the card is still unanswered. ⚠️ **All three of those mean "this card will not be answered again"**, so all three set `open: false`; only an unanswered card blocks a second post. ⚠️ A box the operator already turned down needs no separate skip list: it is closed by `dup_of` above, because the filed task quotes the box and the next tick's search finds it.

## 2. Execute the output

- **`file[]`** — at most one row per tick (the script enforces it; *ranked, never bulk*, operator 2026-10-01). File it through `/vault-cli:create-task "<title>" --code|--docs-only` — the route `commands/goal-loop.md` § Guardrails already prescribes — linked to the subject goal, its body quoting the box verbatim. ⚠️ The title is derived from vault prose, so it takes the same quoted-heredoc treatment as the reason below; never interpolate it into a double-quoted argument. Then write the routing fields on the new file:

  ⚠️ **Never put free text into a double-quoted shell argument.** A task name, a title, a reason or a box quote is vault prose: it can carry a backtick, `$(…)` or an embedded `"`, all of which expand or break the command — the same hazard the payload heredoc above exists for. Every such value goes through a quoted heredoc:

  ```bash
  TASK="$(cat <<'TASK_NAME'
  <the filed task's name>
  TASK_NAME
  )"
  vault-cli task set "$TASK" planned_by goal-plan-undriven
  vault-cli task set "$TASK" door <door>
  vault-cli task set "$TASK" door_reason "$(cat <<'REASON'
  <reason>
  REASON
  )"
  ```

  `door` is the script's own token (`two-way` / `one-way-self-contained` / `one-way-wide`), so it needs no quoting. A newline inside a heredoc-fed value is fine for a frontmatter field; if the value must stay one line, strip it with `tr -d '\n'`.

  **For every row routed to `card` or `talk-through`** — that is, every row except a two-way row on a goal at `execution` — put `Blast radius:` and `Rollback:` lines in its `# Impact`. The card renders them from there on the next tick (agents/goal-drive.md clause (7)), so a row carded without them is an ask the operator cannot judge.
  - `route: talk-through` → **also** write a `### Decision: <title> — one-way` block with `Status: proposed` on the subject goal page (plan, blast radius, rollback, alternatives, cost of waiting), and post ONE card whose options are the script's `card.options` — `--risk risk`, dedup key `talk-through:<subject>:<task>`. ⚠️ **The card says a talk-through is waiting; it never asks for the decision** — no approve option, ever. Only the operator moves `Status: proposed` → `accepted`.
  - `route: card` → **post nothing here.** The filed row sits at `phase: todo`, so next tick's sweep buckets it `⌛ waiting-approval` and it rides the round's ONE existing approve card — the dedup, open-card and skip discipline that card already has applies unchanged.
  - `route: open` / `held-on-cap` → nothing more this tick; the `pending` half picks it up next tick.
- **`approve_by_manager[]`** — run `vault-cli task approve "<task>" --by manager` per name. ⚠️ **This is the only row a manager may approve**: one this step itself filed (`planned_by: goal-plan-undriven`) **and** labelled two-way, on a goal at `execution`, below the target. Every other `phase: todo` row stays the operator's inbox — never flipped, never opened (`docs/fleet-surface.md` § Approval provenance). Do not spawn here: the approved row reaches the drive leg's readiness audit on the next sweep and opens through row 6 like any approved row.
- **`held_on_cap[]`** — print `held-on-cap: <task>` per name; no card.
- **`decisions[]`** — print one line per entry (`<box> — <action> — <reason>`) in the round's table, so a tick that filed nothing says why.

⚠️ **A malformed payload exits non-zero and the step stops** — exit 2 for a payload that is not JSON or is missing a top-level key, and a non-zero exit with a traceback when a nested entry is malformed. Print the stderr line either way; never fall back to filing by hand.
