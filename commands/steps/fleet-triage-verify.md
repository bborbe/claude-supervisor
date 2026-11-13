---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 6 — Verify: re-sweep and assert

When: every run — the last step, always

```bash
mkdir -p ~/.claude/state/fleet-triage
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-board.py" --json > ~/.claude/state/fleet-triage/after-${CLAUDE_CODE_SESSION_ID:-$CLAUDE_PID}.json
echo "EXIT=$?"
```

⚠️ **A non-zero exit or unparseable JSON here is a hard stop, exactly as in Step 1 — and it is
worse here, because this is the sweep that certifies the run.** The redirect truncates the
target **before** `python3` runs, so a failed re-sweep leaves an empty or partial file, not the
previous snapshot — and the delta then compares against nothing while the verdict block prints
`✅ Fleet is moving`. If `EXIT` is non-zero or
the file will not parse, print `⚠️ verify sweep failed — the run is unverified, not clean` and
stop; never fall through to the verdict block.

Re-derive the states from the fresh sweep and print one line. ⚠️ **`<n>` is re-derived from disk,
not recalled from memory** — it is the stuck count of this run's *before* sweep, which Step 1
wrote to `~/.claude/state/fleet-triage/sweep-${CLAUDE_CODE_SESSION_ID:-$CLAUDE_PID}.json`
(`commands/steps/fleet-triage-sweep.md`). ⚠️ **That file is the raw board document and holds no
STUCK column** — its rows carry `bucket` ∈ {`problem`, `needs-input`, `running`, `idle`}. So
`<n>` is **re-derived by re-applying § 3.4 to that snapshot**, never "counted" as a stored
value; after a compaction the number is gone from context and the delta cannot be reconstructed
from the fresh sweep alone. ⚠️ **If the before snapshot is missing or unparseable, the run is
unverified** — print `⚠️ verify sweep failed — the run is unverified, not clean` and stop; a
delta computed against an absent baseline is worse than no delta, because it reads green.

```
Stuck: <n> → <m> · Closed: <c> · Drove: <d> · Operator: <o> (uncarded <u>) · External: <e> · Finished-but-open: <f>
```

- **`m == 0 && u == 0 && f == 0`** → `✅ Fleet is moving.` Every session is working, closed,
  or waiting on a named producer or an open card — nothing else. Say nothing further.
  ⚠️ **This shorthand is not the whole green condition** — the verdict block further down
  requires all five: `STUCK == 0`, `UNCARDED == 0`, `FINISHED-OPEN == 0`, `NO-LOOP == 0`, and
  `accounted for == sessions`. The one-liner cannot carry the last two, so read the block
  before calling the run clean: a run whose counters are all zero but which left an
  unclassified session, or a manager with a live non-terminal subject and no loop, is **not**
  `✅ Fleet is moving` — it is a hole in the sweep.
- **any of them non-zero** → list every offending row with the reason it survived, and say
  plainly that the run did not meet its invariant. **Never report a non-zero count as a clean
  result.**

The two counters beyond `stuck` are the ones this command previously could not see:

- **`uncarded` (`u`)** — OPERATOR rows with **no open card** at the end of the run. Check each
  row by listing its card files and polling the **`item_id` each record carries** —
  `ls ~/.claude/state/attention/<session-id>*.open.json`, then read `item_id` from each match
  and `attention-ask.py poll <item_id>`. ⚠️ **Poll the `item_id`, never the dedup key** —
  `poll` takes an item id only (`scripts/attention-ask.py:1276-1277`), and a dedup key passed in
  its place resolves to nothing, which reads as "no card" for a row that is carded. Anything not
  `OPEN`/`ANSWERED` is uncarded. A non-zero `u` means the operator is waiting on questions they
  cannot see.
- **`finished-but-open` (`f`)** — sessions whose 3.1 evidence holds but which are **still
  live**: the close refused, or the headless branch was skipped. Measured 2026-10-29: five
  such workers had been alive 2 h–13 h, each counted as a healthy idle session on every sweep.
  Every one of these is a `close-worker.py` refusal that was reported as a skip instead of
  being taken to the headless branch.

⚠️ **`u` and `f` are computed from a fresh read, never carried forward from the act phase.** A
card posted at Step 4 and an answered one at Step 6 must not be able to disagree.

### The verdict block — the last thing printed

The one-line counter is for the log. **This block is what the operator reads**, and it must be
the final output of the run: a census of every session, then a single verdict word, then —
when the verdict is not green — the exact list of what is off. Never end a run without it.

```
Fleet triage — <YYYY-MM-DD HH:MM>

  ✅ WORKING        12   doing work, nothing needed
  ⚪ CLOSED          5   closed this run
  🟡 EXTERNAL        8   waiting on a named producer
  🔵 OPERATOR        2   carded, waiting on you
  🔒 LIVE-IDLE       1   headless, nudged this run (record on disk)
  ────────────────────────────────────────────────────────
  🟠 STUCK           0
  ⬜ UNCARDED        0
  ⬜ FINISHED-OPEN   0
  ⬜ NO-LOOP         0
  ────────────────────────────────────────────────────────
  sessions 28 · accounted for 28 · unclassified 0
  managers 7 · armed 5 · terminal 1 · auto-quiet 1

  RESULT: ✅ ALL GOOD — every session is working, closed, or waiting with a named reason.
```

**`🔒 LIVE-IDLE` is a census row, not a red counter** — it is the 4b outcome the router's
invariant calls `NUDGED` (`commands/fleet-triage.md` § The invariant, `commands/steps/fleet-triage-act.md`
§ 4b), and it is green **only while its nudge record exists** at
`~/.claude/state/fleet-triage/nudges/<session-id>.json`. A row with no record was not driven at
all and belongs in `STUCK`; a row whose record shows two consecutive unanswered nudges has already
become an OPERATOR card and appears there instead. That is why the row carries no counter of its
own: the record, not the count, is what makes it legitimate.

**A STUCK tab row counts as driven only on `pane:*`.** Its wake path (`commands/steps/fleet-triage-render.md`
§ Drove) must read `pane:<resolution>`, meaning `pane-wake.py --send` returned `delivered`. A tab
row whose only action was `sendmessage` stays in `STUCK`, because that message was never read. A
`none:<skip>` row stays STUCK unless the skip names a valid wait: `skip:operator-ask` or
`skip:open-card` → it belongs in OPERATOR with its card; `skip:machine-wait` or
`skip:sleeping` → EXTERNAL (a date-gated sleeper waits on its own clock, and `--state` is what
proves it); `skip:spinner` → WORKING. ⚠️ **`skip:state-unreadable` is deliberately NOT a valid
wait** — a subject resolved but its state could not be read, so the row stays STUCK and surfaces
the broken read rather than being excused by it. ⚠️ Never count `unverified` as driven.

**Green requires all five of:** `STUCK == 0`, `UNCARDED == 0`, `FINISHED-OPEN == 0`,
`NO-LOOP == 0`, and `accounted for == sessions` (every live session landed in exactly one row —
an unclassified session is a hole in the sweep, not a clean bill).

**`NO-LOOP` counts only § 3.6's one real case** — a manager with a live, non-terminal subject
carrying runnable work and no loop. Terminal subjects and auto-quiet stand-downs are counted
separately on the managers line and never make the verdict red. A run that reports a finished
goal as "manager without a loop" has replaced one wrong number with another.

When any of the four zero-counters is non-zero, **or `accounted for` ≠ `sessions`**, the same
block prints with the failure list instead of the green line:

```
  RESULT: ⚠️ 3 THINGS OFF
    1. STUCK — <label> (<sid-prefix>): idle 41m, 3 open boxes, nothing outside the fleet blocking
    2. UNCARDED — <label> (<sid-prefix>): parked on "pick — 1. …" with no board card
    3. FINISHED-OPEN — <label> (<sid-prefix>): task completed, 0 open boxes, pid 5120 still alive
```

⚠️ **Never print the green line when a count is non-zero.** A summary that says "all good"
over one uncarded question is worse than no summary: it is the operator being told the fleet is
clean while a decision rots. This is the same failure class as reporting a refusal as a close.

⚠️ **Every "what is off" line names three things** — the bucket, the session (label + sid
prefix), and the observable that put it there. "STUCK — private-personal-8f" without the
reason is a row the operator cannot act on and cannot check.

⚠️ **`--dry-run` prints the same block**, with the buckets as the *would-be* outcome and the
`CLOSED` row reading `0 (n closable)`. A dry run's verdict is a prediction, and it must be
labelled as one — never let a dry run print `✅ ALL GOOD` as though it had acted.
