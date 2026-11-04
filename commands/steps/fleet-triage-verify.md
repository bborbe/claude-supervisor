---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 6 — Verify: re-sweep and assert

When: every run — the last step, always

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-board.py" --json > /tmp/fleet-triage-after.json
```

Re-derive the states from the fresh sweep and print one line:

```
Stuck: <n> → <m> · Closed: <c> · Drove: <d> · Operator: <o> (uncarded <u>) · External: <e> · Finished-but-open: <f>
```

- **`m == 0 && u == 0 && f == 0`** → `✅ Fleet is moving.` Every session is working, closed,
  or waiting on a named producer or an open card — nothing else. Say nothing further.
- **any of them non-zero** → list every offending row with the reason it survived, and say
  plainly that the run did not meet its invariant. **Never report a non-zero count as a clean
  result.**

The two counters beyond `stuck` are the ones this command previously could not see:

- **`uncarded` (`u`)** — OPERATOR rows with **no open card** at the end of the run. Poll each
  row's dedup key; anything not `OPEN`/`ANSWERED` is uncarded. A non-zero `u` means the
  operator is waiting on questions they cannot see.
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
  ────────────────────────────────────────────────────────
  🟠 STUCK           0
  ⬜ UNCARDED        0
  ⬜ FINISHED-OPEN   0
  ⬜ NO-LOOP         0
  ────────────────────────────────────────────────────────
  sessions 27 · accounted for 27 · unclassified 0
  managers 7 · armed 5 · terminal 1 · auto-quiet 1

  RESULT: ✅ ALL GOOD — every session is working, closed, or waiting with a named reason.
```

**Green requires all five of:** `STUCK == 0`, `UNCARDED == 0`, `FINISHED-OPEN == 0`,
`NO-LOOP == 0`, and `accounted for == sessions` (every live session landed in exactly one row —
an unclassified session is a hole in the sweep, not a clean bill).

**`NO-LOOP` counts only § 3.6's one real case** — a manager with a live, non-terminal subject
carrying runnable work and no loop. Terminal subjects and auto-quiet stand-downs are counted
separately on the managers line and never make the verdict red. A run that reports a finished
goal as "manager without a loop" has replaced one wrong number with another.

When any of the four is non-zero, the same block prints with the failure list instead of the
green line:

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
