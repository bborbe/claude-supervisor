# fleet-drive — the rationale behind the drive leg's rules

`agents/fleet-drive.md` states the rules. This page holds the **evidence and the reasoning** behind them, so the agent body keeps only what it must act on. Read a section here when you are changing the rule it belongs to, or when a round behaved in a way the body's rule does not explain.

The body is a context budget, not a wiki: it is re-injected on every dispatch, so rationale a reader consults once has no business riding along on every round.

## Verbatim paste — why a summary line is a failure

The body requires `RETAINED` and `PARKED` to carry one line per row, and requires `python3 $P/drive-retained.py render` output to be pasted byte-for-byte. Prose alone did not hold that line, which is why the rule is stated as a rendering requirement rather than an aspiration.

Measured 2026-10-21 on round `2026-10-21T05-07-35Z`: **99 retained rows were collapsed into one summary line**, even though the body has forbidden that shape since `v0.194.0`. The script renders 99 lines from the same ledger, and `scripts/tests/test_drive_retained.py` counts them.

Measured 2026-10-20 on the first `v0.194.0` round (`2026-10-20T21-16-09Z.drive`): **74 retained rows** were collapsed into `74 rows, all unverifiable: … Full list: python3 drive-retained.py reprobe`, with **zero** per-row lines. It carried none of the three things the section exists to show — the round tag, that round's command, its result.

The shape of the failure is the argument: a hand-written summary passes any content check while losing exactly the evidence the block exists to carry, and it shortens only by deleting lines a reader counts. A rendered block shortens the same way, which is why the block itself is the artifact.

## Why the task file is the authority, and the entry must carry its path

`session_id` is an 8-char display handle — prefix-matching it against a task's `claude_session_id:` is the anti-pattern `agents/fleet-sweep-reader.md` step 8 forbids.

`name` is the session display name, and it need not equal the filename. Measured 2026-10-20: the ledger carried `Row Whose Body Demands…` against the file `A Row Whose Body Demands…`. That is why an entry with an empty `task_file` is **unverifiable** rather than guessed.

## Why the parked signal is keyed on `reprobe_verdict` / `reprobe_probe`

`probe()` emits `needs-step-4` for a live row, and the ledger deliberately does not store that value — the agent resolves it to `revive` / `blocked` / `unverifiable` in step 4. Comparing the script's observation against the agent's own conclusion therefore can never match, and the row re-signals on every round.

Measured 2026-10-21: two consecutive rounds produced **byte-identical `PARKED` blocks for the same 6 rows**.

## Why the reap precedes any nudge

Measured 2026-09-23: the fleet snapshot had not been written for **~14h**, and the ledger held two `finished` verdicts that nothing ever messaged. A finished session that is nudged instead of reaped is told to continue with nothing left to do.

### No owning-manager skip

There is no "this task belongs to an armed manager, so skip it" rule, and the reason is that the fact is not derivable here. The gate-loop state records only a member **count** — `sweep-gate-loop/<vault>/<subject>.json` → `"members": N` — so it names no members and cannot answer which manager owns which task.

A second reap message from a manager that also reaps is harmless: it is the same non-authorising evidence, and the ledger (step 3) stops this layer repeating it. That is why the remedy for a duplicate is suppression, never a skip.

## Why the box count is never re-counted

The digest row's count is the one authority for the round. A second count from a different read disagrees with it — measured 2026-09-23: **row 3, re-count 7, disk 3** — and the suppression rule then compares against the wrong value, which makes an unchanged row look changed and re-signals it.

## The ledger's one writer

`scripts/drive-ledger.py` owns both the read and the merge, and the body forbids reading or writing the file by hand.

**Why the merge is a script.** It starts from disk, updates or adds this round's entries, keeps an absent session **as-is**, takes a `.bak`, and replaces the ledger atomically — so the written file never has fewer sessions than the one read, and a round cannot shrink it by omission.

**Why that matters.** Measured 2026-10-12: a hand-rolled merge emptied the file, and the re-nudge / re-reap suppression history — the whole reason the file exists — went with it.

**Why a corrupt ledger is not an empty one.** An **exit 2** from `read` means the file exists but cannot be parsed. Reading that as "no prior drive ledger" is how the 2026-09-23 run lost 29 entries: a round read the digest's `LEDGER (open): none` line — the **open-items** ledger, a different file — as "no prior drive ledger", rewrote the ledger from scratch, and dropped it from **37 entries to 8**, erasing re-nudge suppression.

The two cases stay deliberately different: an absent file prints `{"round": null, "entries": []}` and exits 0, a corrupt one refuses.

## Why a missing `CLOSERS` row fails closed

Absence means the closer was **not captured**, not that there is none. A `pick` asked only in chat, with no rendered panel, reaches neither `CLOSERS` nor the task file.

Measured 2026-09-23 on v0.39.1: a session parked on a chat-only three-option `pick` had no `CLOSERS` row, was revived, and the nudge went past a live operator decision.

Measured 2026-09-23: a session parked on `review:` was revived because only `# Progress` was read — the closer lived in the session's chat, never in the task file.

A row whose class is `none` is **not** a missing row: it is a captured absence and follows the class rule.

## Why `routine` does not skip the `BLOCKED` check

The class describes the turn's **last** line. A session whose last closer reads `nothing` but which carries another `BLOCKED` row is still blocked.

Measured 2026-10-29: `41a950c7`.

## Why the `FREEABLE TABS` task column is omitted when it repeats the name

A worker's session name is usually its task name, and printing both repeats the same string twice in one row.

Measured 2026-10-19, round 55: `Start Day - 2026-10-19 · Start Day - 2026-10-19 · finished · …`.

## Thresholds

- **"Idle"** = roster status `idle` (turn ended). This agent adds **no age floor** — the sweep-reader's `parked` bucket is the idle test.
- **"One tick"** = one `fleet-loop` round, **~15 min**. The ledger suppression (step 3) keeps a still-idle session from being re-nudged or re-reaped every round.

## Stale waits — why step 0c exists, and why it is not the `PARKED` signal

`Session Lifecycle.md:43` and edge case E5 (`:95`) require one card to the operator when a session's declared wait has had no observable change for 24 h. Nothing implemented it, and the omission was structural rather than an oversight: `scripts/declared-wait.py` answers a **boolean**, so a declared wait suppresses every other reader — `fleet-board.py` withholds `nudge`, `manager-predispatch.py` returns `LIVENESS_PARKED` — and nothing expires it. That reader's own docstring records the result as *"the honest gap"*: nothing in a declaration says when the wait began, so a reader cannot expire it. A wake that never fires waits forever, silently.

**The 24 h needs no live probe.** The declaration is already on disk. The newest `Stop` record carries `ts` (when it was rendered) and a `detail` holding the `⏰ Ends:` text (what it waits on). A resumed session that re-parks writes a newer record, so *"the pair has not moved for 24 h"* is simply *"that record is still the newest"*. `declared-wait.py` gained `wait_record()` and `--detail-json` so this keeps one home for the `⏰ Ends:` read rather than adding a second parser.

**Why the dedup key is the `(ts, ends)` pair and not the timestamp alone.** A re-declaration on the *same* observable writes a new `ts`, which correctly resets the clock — the session was touched. A declaration re-rendered with a *different* deadline is a different wait and must be cardable again. Keying on the pair makes both true without a third field.

**Why the state is not in the drive ledger.** `drive-ledger.py merge` replaces a session's **whole** entry through `canonical()`. A row that is `waiting` this round and a `parked` candidate the next is written by two callers, and the second one's entry carries no wait fields — the card dedup would be silently dropped and the same stall re-carded every time the row changed class. `~/.claude/state/fleet-drive/waits.json` has one writer.

**Why the liveness guard is load-bearing.** Measured 2026-11-10 against the operator's real attention store: **317** sessions carry a newest `Stop` record declaring a wait older than 24 h, and the overwhelming majority exited days ago — the logs are append-only and outlive the process. Without a filter the first round posts 317 cards for waits nobody holds, a worse failure than the silence this step exists to end. `session-liveness.py --list` narrows the population in **one** call; probing per session (`--check`, as step 0 does for a handful of retained rows) took **3 m 40 s** for the same 317, a third of the round's budget. After the guard: 290 stale declarations, **0 of them live**.

**Why it cannot be folded into `PARKED`.** `PARKED` is a **change** signal — it fires when a row's parking verdict or probe changed this round. Step 0c is an **absence** signal — it fires when nothing changed for 24 h. A row can appear in both, in neither, or in one alone; a single block would silence exactly one of the two failures.
