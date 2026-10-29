# Closer classes — what a session's last turn ended on

The canonical home for the `CLOSERS` class vocabulary, the routine-continue table, and
the join rule the fleet digest uses. `agents/fleet-sweep-reader.md` renders the rows and
states the class tokens inline; `agents/fleet-drive.md` step 4a reads them and points
here for the table. An agent body is re-injected on every invocation and is held to a
30 KB budget (`scripts/check-budget.py`), which is why the detail lives here.

## Why a class at all

`who-needs-me.py`'s `Rendered panels` section answers the feed's question — *is the
operator being asked something* — so it carries real asks only. A session whose turn
ended on `👤 You: nothing …`, and one parked on `later (on …)`, produce no panel at all.
That is correct for the feed and wrong for the digest: the drive leg fail-closes a
candidate with **no** `CLOSERS` row to `unverifiable`, so a routine turn end and a
lost capture were indistinguishable, and the round's `revive` count was structurally
zero. Measured 2026-10-29 on round `2026-10-29T08-28-15Z`: **7 parked rows against 4
`CLOSERS` rows**, and the drive leg read `6dd59be0` — whose pane renders
`👤 You: nothing — I continue on step 1` — as `unverifiable — no CLOSERS row (fail
closed)`.

`closer_class()` in `scripts/who-needs-me.py` answers the digest's question instead,
and `who-needs-me.py --section last-closers` prints it. The section is **opt-in**: the
no-flag render keeps its six sections byte-for-byte, so no existing consumer changes.

## The classes

| class | the turn ended on | drive-leg verdict |
|---|---|---|
| `ask` | any closer that is neither a routine turn end nor a clock-bound park — a real `pick` / `review:` / `you run:` / `approve:`, **or a plain status line** (`done — all green`, `waiting for CI`) | **blocked**, unless the routine-continue table below passes on its text |
| `routine` | `👤 You: nothing …` — the session asked nothing | **revive** — a read-only nudge is allowed |
| `parked-wait` | `later (on …)` — names the event that resumes the work | **unverifiable** — nothing to nudge until it fires |
| `none` | no **readable** closer line — a missing or unreadable transcript reads the same as a turn that rendered none | **unverifiable** — captured, and there is no closer |

`python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/closer-verdict.py --class <class>` prints the verdict and its reason. The table above and that script's `VERDICTS` are two copies of one mapping, and `scripts/tests/test_closer_verdict.py` pins them together (`set(CLOSER_CLASSES) == set(VERDICTS)`) so a class added to one cannot silently miss the other.

⚠️ **A `nothing …` closer that still names a destructive verb is classed `ask`, not `routine`.** No later test can catch it: a `nothing …` closer is never promoted to a `BLOCKED` row, so step 4a test 4 never fires, and the routine-continue table's test 3 would be skipped entirely. The capture therefore applies that blacklist itself, before it will call a row routine.

⚠️ **`ask` is a catch-all, not a verb list** — every closer that is neither a routine turn end nor a clock-bound park, a plain status line included. That is the safe direction: an unrecognised closer reaches the drive leg as **blocked**, never as revivable, and the routine-continue table is what re-admits the ones that only look like asks. Narrowing it to the documented verbs would make every unlisted closer revivable, which is the fail-open direction the `CLOSERS` fail-closed rule exists to prevent.

⚠️ **`routine` does not overrule step 4a test 4.** The class describes the turn's LAST
line, not the session's whole state: a session whose last closer reads `nothing` but
which also carries a `BLOCKED` row for some other gate is still **blocked**. Measured
2026-10-29: `41a950c7`'s last closer read `nothing` while `BLOCKED` held its
`approve nuke-prod replay` — the class alone would have revived it past a live gate.

⚠️ **`none` is a row, not an omission.** An absent `CLOSERS` row means *not captured*
and fail-closes to `unverifiable`; a `none` row means *captured, and the session has no
closer*. Do not collapse the two.

## The routine-continue table

A worker in `phase: execution` often parks on `approve: <go on with my own next step>`.
Its own rules (execution-no-reask) already let it take a written, reversible subtask
without re-asking, so that closer asks nothing only the operator can decide. Measured
2026-09-23: 12 of 16 ledger entries were `blocked`, several on exactly this shape
(`approve: start subtask 70`, `approve: proceed with recording …`), and none was ever
nudged. Treat an `approve:` closer as a **routine continue** — not a blocker — only
when **all** hold:

| # | test | evidence |
|---|---|---|
| 1 | task `phase:` is `execution` | `grep -m1 '^phase:' "<task file>"` |
| 2 | closer body (backticks stripped, lower-cased) starts with `proceed`, `continue`, `go ahead`, `start subtask`, or `start the next subtask` — nothing else; a closer naming its own step in free words is a gate | the closer line, quoted verbatim |
| 3 | the body names none of: `push`, `merge`, `deploy`, `release`, `tag`, `apply`, `buca`, `kubectl`, `delete`, `rm `, `trade`, `session-close`, `prod` | same line |
| 4 | the session has no `BLOCKED` row **other than the closer under test** — a permission prompt or an open question is a gate; a `BLOCKED` row whose text is this same `approve:` closer is not a second gate | the section, both rows quoted |

⚠️ Test 4 must exclude the closer itself. The attention feed lists a rendered
`approve:` closer under `BLOCKED` as well as `CLOSERS`, so "not in `BLOCKED` at all"
fails every `approve:` closer by construction and makes this table unreachable —
measured 2026-09-23 on v0.38.1: a live run reported `fails routine-continue test 2 and
test 4` on a session whose only `BLOCKED` row was its own closer, and 0 routine
continues across three runs.

Any test fails → it is a gate, **blocked**. A routine continue is not an answer: the
nudge says the operator has not answered and that the worker proceeds only under its
own execution rules.

## Joining a `Last closers` row to a parked row

**Join by PANE id, never by name.** The pane id is the one key both sides carry: a `Last closers` row renders it in its `[<pane>]` column, and a `CLASSIFICATION` row carries it inside the jump link it prints (`…/jump?pane=<id>`). Names are not a key — `row()` truncates a name at 50 characters and two sessions routinely share a prefix. A `Last closers` row rendering `[ hst]` or `[   ?]` owns no pane and no link to join on: carry it only where the session id resolves unambiguously, and otherwise emit no `CLOSERS` row for it — which fail-closes, correctly, because a pane-less closer was not observed.

Carry a `CLOSERS` row only for sessions that also appear in the digest's
`CLASSIFICATION` section; the digest's 40-line payload cap depends on that filter.
