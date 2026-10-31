# Sweep output — the status table

**This file is the single source for the table, and it carries two lists that get referenced separately** — the **frame** (columns, widths, icon legend) and the **action lines** printed under the box. Name which one you mean when changing either: editing one and missing the other is exactly how they drift apart.

`/supervisor:goal-loop` and `/supervisor:goal-status` both render it and neither carries its own spec — they point here. They diverged once because each described the table in its own words (`goal-status` said only "build the table with bucket icons" and named no frame, columns, or widths), so the same sweep rendered two different ways depending on which command ran.

**Provenance.** This spec previously lived in a personal Obsidian vault runbook and was referenced by absolute path (`65 Runbooks/Manager Session.md` § Sweep output — the status table). A plugin cannot depend on a particular person's vault to describe its own mechanics — the same rule `docs/fleet-surface.md` § Referencing vault notes states for vault notes generally — so the frame moved here on 2026-10-13. **This copy is canonical.** The vault runbook follows it; where the two disagree, this file wins and the disagreement is a bug worth reporting.

## Print the full table on every tick

Every firing, no-change ticks included. **Every tick also prints a one-line liveness marker: timestamped, and always the first line of the tick's output:**

```
19:55 ✓ Billing — 4 tasks · 2 ⌛ · 1 🔄 · 1 ✅ · no change
```

Silence is ambiguous: a quiet loop and a dead loop look identical from the outside, and the wakeup is session-scoped so it can genuinely stop — the marker answers *alive or stuck?* at a glance down the transcript. For one on-demand table without a loop, run `/supervisor:goal-status`.

**The marker leads, and the box hangs two spaces under it.** The marker line is what a glance down a long transcript catches — it says *this is a tick*, not prose — and the two-space indent nests the table beneath its own header so the two read as one block. Both are part of the frame, not decoration: a tick that drops either one stops matching every other tick in the transcript. `/supervisor:goal-status` carries **no** marker (it is a one-shot snapshot, not a tick) but indents its box the same two spaces under its own `Tracked (N):` line, so the two commands still render one table one way.

⚠️ **Do not type a leading glyph.** An earlier revision of this rule specified a literal `⏺ ` prefix; the harness already bullets assistant output with `⏺`, so a literal copy renders as `⏺ ⏺`, doubled. The marker's distinctiveness comes from its **shape** (timestamp + `✓` + topic + counts), not from a glyph the terminal supplies.

## The frame

- **Columns and widths:** Topic / Goal / Task 42 · Session 10 · Status 34 · Phase 9 · Met 17. ⚠️ **These are CONTENT widths, and this line is the normative width statement in this spec** — the renderer contract below quotes the same values so a caller need not scroll back; where the two ever disagree, **this line wins**. The renderer adds one space of padding each side, so the first column *draws* 44 cells wide and the second 12. A reader who measures the rendered frame and finds every column two wider has measured a different quantity, not caught this line stale.
- **Header and cells are both left-aligned.**
- Over-long cells truncate with `…`.

### The Status cell — the icon legend

Status carries the bucket icon per the operator's `Icons` note *(operator's vault; not shipped with this plugin)*; the legend is restated below so this file is self-contained:

| Icon | Bucket |
|---|---|
| 🔄 | progressing |
| ⌛ | waiting-on-human |
| ⏸️ | blocked/hold |
| ✅ | done |
| ⚠️ | problem / orphaned |
| 🚀 | ready-to-start |
| 🔧 | close-me |

⚠️ **`🔧 close-me` is the one glyph here the sweep cannot compute, so the caller applies it *after* the liveness verdict** — the bucket's predicate contains a verdict the reader may not make, so a terminal task renders `✅ done` from the sweep and gains the `🔧 close-me` overlay only once the caller has confirmed a live session against the session-heartbeat endpoint. Read it as an **overlay on `done`**, the shape `aborted` already has, not as a bucket the sweep derives.

Non-bucket dispositions: `hold` uses `⏸️ blocked/hold`; `backlog` renders with **no** glyph.

⚠️ **The `blocked-upstream` bucket shares the `⏸️` glyph with `hold` deliberately — the label word is the discriminator**, exactly as it already is between `⏸️ HOLD:` and `⏸️ BLOCKED UPSTREAM:` on the action lines: `⏸️ blocked/upstream` (a mechanical unmet `blocked_by`, nobody decided anything) vs `⏸️ blocked/hold` (a human parked the task).

⚠️ **An `aborted` task renders `✅ done · aborted`** — the `done` glyph plus a literal ` · aborted` suffix — so it is never byte-identical to a completed row's `✅ done`. The suffix is an overlay on `done`, **not** a seventh icon.

⚠️ **The same holds for the `blocked-upstream` bucket and for the `deferred` overlay — both reuse `⏸️`, with the label word doing the discrimination, so the legend gains no eighth icon.** A deferred row renders `⏸️ deferred` and emits no action line.

⚠️ **The `orphaned` bucket shares the `⚠️` glyph with `problem` for the same reason.** It is **candidate-only by construction**, which is why no renderer prints its *action line* on its own: no match on any id makes it a **candidate, never a verdict**, and the caller confirms the candidate against the registry before printing an `ORPHANED:` row. So a renderer showing no `⚠️ ORPHANED` action line on a candidate alone is **not** diverging — the reader's **Status cell** for that row still reads `⚠️ ORPHANED` (it is the classification, and the reader is this bucket's only renderer — `scripts/sweep-gate.py` carries no `orphaned` key), while the *line* is the caller's to print after the liveness probe.

⚠️ **The disposition is per `status`, at every level — a goal row and a topic row carry the same meaning a task row does.** A goal whose frontmatter reads `status: next` therefore renders `🚀 ready-to-start` exactly as a `next` task does; the glyph names the *state*, not a spawnable unit. **Only tasks are spawned**, and the `🚀 READY TO START:` action line stays **task-only**, so a goal row never produces a spawn instruction.

⚠️ **The manager dispositions `stood-down` and `lapsed` reuse `⏸️` and `⚠️` and add no eighth icon** — they are **classification classes**, not Status-cell buckets, and the label word is again the discriminator. Both are read from `python3 scripts/manager-liveness.py --state --topic "<subject>"`, **never** from `--check`: `--check` is a *sweep* that prints only the topics that **turned** stale, so `STOPPED` — the state a deliberate stand-down produces — never appears in its output at all, and a reader that asked it, saw nothing and concluded the manager was fine has it exactly backwards. `STOPPED` → `⏸️ stood-down`, a **correct** stand-down; `STALE` → `⚠️ lapsed`, the cadence expired with **no** `<slug>.stopped` marker, so nothing stood this manager down and the loop simply stopped.

⚠️ **`stood-down` ranks above `stalled` and `parked`, and that ordering is the whole point of the class.** A stood-down manager is idle, its cadence file is frozen and its subject's task file is untouched — so every signal below reads it as a fault: `stalled` (mtime unchanged for ≥2 sweeps) or `parked` (idle with open boxes), both **⚠️ problem rows**, and a manager reading one is invited to nudge **by hand**, which is the exact act the stand-down exists to prevent. Same shape as the `held` row, same reason: a state that is *correct* must not render as work. ⚠️ **`lapsed` is the opposite and must never be folded in with it** — no marker means nothing stood this manager down, and a loop that stopped without saying so is precisely what this mechanism exists to surface. Two states one call apart, opposite verdicts: that is why they are two classes and not one.

⚠️ **A third disposition, `unarmed`, is a fault and ranks with `lapsed` — never with `stood-down`.** `--state` returns `UNARMED` when there is no cadence record under **either** the prefixed `goal--<slug>` / `topic--<slug>` name or the pre-split `<slug>` one: no manager was ever armed for that subject. It is **not** `INVALID`, which means a record exists and cannot be read — a probe failure — and the two must never be collapsed, which is exactly what they were before 2026-10-20: every never-armed goal read `INVALID`, so "needs a manager started" was indistinguishable from "cannot measure". `⏸️` and `⚠️` are reused, and the label word discriminates again: `⚠️ unarmed`. Its recipient is the fleet round's own `unarmed goals:` reading (`commands/steps/readings.md`), which reports a goal only when it has **both** a live session and no record — a goal with no live session is never reported, so an unarmed goal nothing is working on stays quiet.

⚠️ **The rows themselves live in `agents/fleet-sweep-reader.md`'s classification table, and getting them there cost prose.** That file sits against a **30720-byte body budget**, and `scripts/check-budget.py` refuses a new oversized body outright rather than ratcheting it — so the rows had to be **funded** by cutting existing text in the same file, not merely added. Read the table and this section together: the table names the classes and their rank order, this section says what each one means and why the ranking holds. ⚠️ **The budget is why the table is the place to look rather than this file** — and it is also why a future change wanting to add a class should expect to pay for it in that file rather than assume room exists.

The legend is complete — `optional` is **not** in it and gets no glyph.

### The Session cell

Session shows `[<sid8>]`, `(<sid8>)` or `—` — the **sessionId prefix** (8 hex chars), never the roster `[ref]`. **Three states, and the delimiter carries which:**

| Cell | Meaning |
|---|---|
| `[<sid8>]` | an id is recorded AND the session is alive |
| `(<sid8>)` | an id is recorded AND the session is not alive |
| `—` | no id is recorded in either place |

⚠️ **`—` is a PROVENANCE claim, never a liveness one, and this cell joins nothing.** It means exactly *the task records no id in either place* (`claude_session_id`, or any `session_id` under `metrics_sessions`) — and nothing else.

⚠️ **The verdict is the CALLER's, never the renderer's.** The sweep-reader may not probe liveness (`agents/goal-sweep-reader.md` § constraints), so the calling command runs `session-liveness.py --check <id>` over each row's **whole** id set — frontmatter `claude_session_id` **and** every `metrics_sessions` id, **one id per invocation** — and passes the verdicts in; the renderer formats the one it was handed. A verdict that is missing or `UNKNOWN` renders `[<sid8>]` — an unreadable registry cannot prove a session dead — and the row is **never dropped** for this.

⚠️ **Roster presence is a *join*, never a probe.** A renderer that consulted the roster instead would call a live-but-off-roster session dead; the caller's `session-liveness.py` probe is what decides the cell.

⚠️ **The `live` / `parked` words are retired** — dropped 2026-09-20 when the fifth column took six characters off Session (16 → 10). **No word is reintroduced and Session stays 10 content cells**; the **delimiter** carries the state instead. The roster ref is an ephemeral per-connection handle: persisted nowhere, not derivable from the sessionId, socket, pid or name, and **unstable across a session's life**. Printing it in a column the operator is meant to act on makes a dead handle. The sessionId prefix **does** resolve — `jump.py` reads the session record's `name` and matches it to the tab title — so `/supervisor:jump 2fd1132d` reaches the tab the row describes.

⚠️ **The cell is a link — SHIFT+CMD+click it to reach the pane.** `scripts/box-table.py` wraps a `[<sid8>]`-shaped cell in an **OSC 8 hyperlink** to that session's jump target. **The visible text is unchanged:** the escape bytes are zero-width and the terminal consumes them, so the cell still reads `[<sid8>]` and still measures 10 cells. **The wrap lives in the renderer, not in a cell producer** — every producer emits the bare cell text and no escape bytes of its own.

⚠️ **The link carries the jump token.** Set **`BOX_TABLE_NO_LINKS=1`** before rendering anything **destined to be written to disk** — a git-tracked file *and* a state artifact such as a tick file carry the same token and the same exposure; git-tracking is what makes a leak reachable from a repo, not what makes it a leak. A tick file is no longer the caller's problem: `scripts/sweep-gate.py`'s `write_tick()` strips the OSC 8 escapes from the on-disk frame itself and creates the file `0600`. Do **not** reach for the env var for it — one `render_table()` frame serves both the tick file and the printed output, so an exported env var strips the operator's on-screen links too.

⚠️ **The operator cannot see a table an agent printed.** Claude Code renders Bash tool output **collapsed**, so a table an agent session renders is verifiably in the terminal's cell buffer and still **not on the operator's screen** — the links are real and unclickable at once, and the agent's own read-back will confirm them every time. Hand over the **render command** for the operator to run themselves, never the agent's own printed output.

### The Met cell

- **`Met` counts a task's TOTAL checkboxes — and a STRUCK `# Tasks` row is excluded from every count and every walk.** The unit is SC + DoD + Tasks, not the SC count alone.
- ⚠️ **A struck row is `- [ ] ~~[[Task]]~~`**, a goal page's convention for a task deliberately removed from the tracked set. It is still a checkbox item carrying a wikilink, so a literal token count reads it as an **outstanding** subtask and the leading-`[[…]]` next-open walk names it as the goal's **next task** — both wrong, and both in the direction that reports work where there is none. Exclude struck rows from the `Met` cell, the subtask fraction and the next-open walk — **and say so when you override**, because a hand-corrected cell with no note reads as an instrument defect rather than as a convention the frame does not know about.
- **The cell's unit is per level, and the topic row is the only two-set one.** A **task** row counts that task's TOTAL checkboxes (SC + DoD + Tasks, struck rows excluded). A **goal** row counts its `# Success Criteria` checkboxes. A **topic** row carries **two labelled sets**, `SC n/m · Gate n/m` — `SC` counts the page's `# Success Criteria` checkboxes, `Gate` counts its `# Completion Gate` checkboxes — because a topic is the only level carrying both.
- ⚠️ **A `# Completion Gate` that is not entirely checkbox items renders the `Gate` half `—` — whether it carries none at all or mixes `- [ ]` items with an ordered list; never `0/0`, never `0/<n>`, and never `n/m` over the checkboxes alone**, which would silently drop the numbered items from the denominator. `—` is this frame's value for *nothing recorded*, so it says exactly what is true. ⚠️ **The rule is per half, not per column, and it holds at both levels.** ⚠️ **The TASK row's `Met` cell is deliberately not covered by this rule:** its unit is the TOTAL-checkbox count, and a task carrying no checkbox items at all is a separate question this rule does not decide — do not read the `0/0` prohibition as reaching it. Counting an ordered list as a denominator and rendering a `0` numerator asserts a reading no source supports: *"An absent field is not evidence of zero; it is evidence of nothing"*. ⚠️ **A gate in that shape is out of compliance, not a second supported shape** — do not add a counting rule here to make the frame total over a shape the writing guide does not define.
- ⚠️ **This rule has a second producer, and the two agree on the empty case only:** `scripts/sweep-gate.py`'s `checkbox_count()` already returns `—` for a section with no checkboxes. ⚠️ **A MIXED gate is where the two diverge** — `checkbox_count()` finds checkboxes and returns `n/m` over them, silently dropping the numbered items, while this rule renders `—`. Bringing the gate to parity is a **follow-on**, explicitly not part of deciding the rule, and the rule must not be narrowed to the empty case to restore agreement — agreement bought with a silent drop is the defect this rule exists to remove.

### Two sections when the topic declares optional scope

`Essential (first iteration)` then `Optional (phase 2)`, each a separate box under its own label line at the same two-space indent, same columns and widths. Rows are placed by declared membership, **regardless of their Status cell**: an optional row keeps `🔄 progressing` or `⏸️ blocked/hold` exactly as an essential row would. The `Essential` section **is** the first-iteration completion bar; the `Optional` section is excluded from it.

- **Omit the split entirely when the topic declares nothing optional** — one unlabelled box. An empty `Optional (phase 2)` box is noise, and labelling a single box `Essential (first iteration)` implies a phase-2 scope the operator never declared.
- ⚠️ **The split is not a status filter.** Grouping by `hold`/`backlog` reproduces the exact table this rule replaced and is wrong even when it happens to match: on a topic whose optional tasks happen to be `hold` and `backlog`, a status-keyed implementation renders correctly there and silently mis-groups every other topic. Membership is declared, never inferred from status.

## Rendering

**Render with `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/box-table.py`** — stdin `{"header": [...], "rows": [[...]], "widths": [...]}`. **Never hand-draw the box.** Every misaligned table this spec's history carries was hand-drawn.

**The renderer's full stdin contract** — this is what a fallback needs in order to render without the agent. It takes **JSON on stdin** and nothing else:

```json
{"header": ["<cell>", "…"], "rows": [["<cell>", "…"], "…"], "widths": [<int>, "…"]}
```

- **`widths` is optional and defaults to `20` per column** — omit it and every column is 20 wide, which is almost never what the frame wants, so pass the documented widths (Topic / Goal / Task 42 · Session 10 · Status 34 · Phase 9 · Met 17) rather than relying on the default.
- ⚠️ **`widths` are CONTENT widths, not rendered column widths — the renderer adds one space of padding on each side, so every column draws two cells wider than its `widths` value.** This is the one number that gets "corrected" in the wrong direction: a reader who measures the worked example counts the RENDERED width, finds every column two wider than the *Columns and widths* line, concludes that line is stale, and edits it — the line is right and the measurement is of a different quantity. Settle it by running `box-table.py`, never by counting cells in prose.
- **It has no `--help` and no argument parsing of any kind** — `python3 box-table.py --help` reads stdin, finds no JSON, and dies with `KeyError: 'header'`; it never prints usage. The schema is discoverable only by reading the script or this block. A well-formed payload renders the box; an empty `{}` raises `KeyError: 'header'`.
- **Padding is computed on display width, not `len()`** — most bucket icons are double-width and count as one character in Python, so a hand-rolled renderer that pads by `len()` produces a ragged right border.
- ⚠️ **But not all of them are two cells wide, and assuming they are is its own defect.** Measured with a DSR probe (`\x1b[6n` — write the glyph, ask the terminal for the cursor column): **`⏸️` (U+23F8 U+FE0F) and `⚠️` (U+26A0 U+FE0F) paint in ONE cell** — VS16 is zero-width and does not widen a Neutral base — while `🟢` `🔄` `⌛` `✅` `🚀` `🔧` paint in two. A renderer that widens any character followed by VS16 over-counts **every `⏸️` row by one cell** and steps its right border in. **OSC 8 escapes are likewise zero-width and are stripped before measuring** — which is what lets the Session cell carry a link without gaining phantom cells. **Use the script; do not reimplement it.**

**This is also the documented fallback renderer.** When `/supervisor:goal-loop` or `/supervisor:goal-status` cannot get a table out of `supervisor:goal-sweep-reader`, the command renders the box with this script directly and **says so in its output**. The agent owns the *classification*; it does not own the drawing, and a sweep that cannot classify is still better served by a correctly-drawn box than by no box or a hand-drawn one.
