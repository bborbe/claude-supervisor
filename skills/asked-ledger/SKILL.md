---
name: asked-ledger
description: Claim, resolve and render the shared asked-ledger — which blocked session, or which session-less row, has already been batched to the operator, and by which layer. Use before batching blocked sessions into an AskUserQuestion in /supervisor:fleet-loop or /supervisor:manager-loop, and before the under-target card in /supervisor:manager-loop, so a subject both layers hold is asked exactly once and every open claim renders as one consolidated list. Subcommands claim | resolve | list | prune.
argument-hint: "<claim|resolve|list|prune> [flags]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/asked-ledger.py *)
---

The single home of the asked-ledger rules. Commands and agents point here instead of restating them or hardcoding the script path.

## What it is for

`fleet-loop` drops a blocked entry whose owning manager is live, and `manager-loop` batches its own topic's set. Each is correct alone — but the fleet and a live manager can both hold the **same** blocked session and neither sees the other's batch, so the operator receives one question **twice**. Measured twice on 2026-09-20; `scripts/notify-gate.py` records the same two duplicates from the gate side.

The ledger is the mark they were missing. Claim a subject before batching it; a second layer's claim is refused and it drops the subject from its own batch, while the subject stays visible in the consolidated list.

## Invocation

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/asked-ledger.py $ARGUMENTS
```

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/asked-ledger.py claim   --session <blocked-session-id> --layer <your-layer> --text "<the question>"
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/asked-ledger.py claim   --row "<the row's key>"      --layer <your-layer> --text "<the question>"
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/asked-ledger.py resolve --session <blocked-session-id>
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/asked-ledger.py resolve --row "<the row's key>"
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/asked-ledger.py list
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/asked-ledger.py prune
```

Storage is on disk, never in context — `~/.claude/state/asked-ledger.json`, written only through the script (flock, then atomic tmp+rename). Never hand-edit the file.

The `:-` fallback is load-bearing, not style: `CLAUDE_PLUGIN_ROOT` is unset in a Bash tool call (verified 2026-09-23), so the bare form expands to `/scripts/asked-ledger.py` and fails.

### Which `--layer` value you pass

⚠️ **`--layer` names your *layer*, and there are two layers to `docs/session-tiers.md`'s three tiers.** Read the value off this table, never off your tier's name.

| Your tier | `--layer` |
|---|---|
| **Fleet manager** | `fleet` |
| **Manager** (goal/topic manager) | `worker` |
| **Worker** | — never claims: a worker does not batch, so it never calls this ledger |

`--layer` is `required` and accepts exactly those two tokens (`LAYERS` in `scripts/asked-ledger.py`). **`--layer manager` is a usage error**, not a third option — argparse exits **2** with `invalid choice: 'manager'`, at the exact moment you are trying to avoid a duplicate ask. The goal/topic manager's value is `worker`: the same token `commands/manager-loop.md` passes to `scripts/notify-gate.py --layer` for that manager's own gate publishing, where `--layer worker` means *every* topic manager in the fleet rather than one manager.

## The claim contract — the one thing callers must act on

| Exit | Meaning | What the caller does |
|---|---|---|
| **0** | This asker owns the ask | Include the subject in its batch |
| **3** | Another asker holds an open claim | **Drop it from the batch** and say so in one line — it is not lost, it is in `list` |

`resolve` the subject once the answer is relayed, or the claim stays open and keeps suppressing the other layer.

**The same asker re-claiming is not refused** — that is its own cadence, not the cross-layer duplicate, and refusing it would deadlock the asker. A **resolved** subject is claimable again, so a session that unblocks and blocks again is not deadlocked by its own earlier mark.

## Keyed on the subject, never on the asker

The subject is the only join both layers can agree on: both see it, and neither sees the other's batch. An asker-keyed ledger would let each layer hold its own copy and the duplicate would survive.

**Two subjects, one slot.** `--session` names the blocked session — the join for the blocked-set batch, which every layer can see. `--row` names the **row** — the join for `manager-loop`'s `under-target` card, whose candidates are `phase: todo` rows that carry **no blocked session at all**, so `--session` has nothing it could be given. Exactly one of the two is required; they are the same slot, and a row key is stored in the entry's `session` field.

Naming the row is what makes the under-target disjointness check performable. Before this, that branch mandated a `--session` claim for rows that have no session, so a manager following it literally could not comply and silently substituted a hand-read of `list` — a substitute, not the check. With `--row`, two managers whose tracked sets overlap claim the **same row key**: the second is refused with exit 3 and drops the row from its card, so the duplicate is caught before either card is posted.

## Two marks — do not merge them

- **This ledger marks the ASK** (has this blocked session been batched yet, and by whom). It is **shared**, because "asked exactly once" is a cross-layer property a per-layer mark cannot express.
- **`scripts/notify-gate.py`'s ledger marks the gate NOTIFICATION, and stays per layer on purpose** — a shared cadence ledger would let one layer prune the other's gates as "cleared", after which they re-raise forever. See its docstring.

They answer different questions — *already asked?* versus *how often delivered?* — and one file serving both would break whichever answer it did not keep.

## Prune by age and liveness, never by sweep-absence

`prune` drops a resolved entry past `--max-age-hours`, and an open entry past that age **only once its subject session is gone from `~/.claude/sessions/`**.

This is the rule that keeps a shared file from re-introducing the per-layer bug it replaced. "Absent from my sweep" is a claim about that layer's **partial view** — a shared ledger pruned that way would let the fleet's sweep delete a manager's marks. A long-blocked session is exactly the one that must not be re-asked, so age alone never drops a live claim.

⚠️ **A `--row` claim has no liveness to read, so it prunes by age alone.** A row key is not a session id and never appears in `~/.claude/sessions/`, so the liveness guard above cannot fire for it. That is deliberate rather than an oversight — the alternative is inventing a liveness test for a row, and an invented test is worse than the honest absence of one. A row claim's bound is therefore `--max-age-hours` (24 h by default).

## Rendering

`list` prints every open claim across every layer as **one consolidated list**. Render it once per sweep under the needs-input section. This is what makes "one grouped decision" true even though the asks themselves stay per-layer: the operator sees one list, and no session in it is asked twice.
