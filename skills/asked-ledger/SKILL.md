---
name: asked-ledger
description: Claim, resolve and render the shared asked-ledger — which blocked session has already been batched to the operator, and by which layer. Use before batching blocked sessions into an AskUserQuestion in /supervisor:fleet-loop or /supervisor:manager-loop, so a session both layers hold is asked exactly once and every open claim renders as one consolidated list. Subcommands claim | resolve | list | prune.
argument-hint: "<claim|resolve|list|prune> [flags]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/asked-ledger.py *)
---

The single home of the asked-ledger rules. Commands and agents point here instead of restating them or hardcoding the script path.

## What it is for

`fleet-loop` drops a blocked entry whose owning manager is live, and `manager-loop` batches its own topic's set. Each is correct alone — but the fleet and a live manager can both hold the **same** blocked session and neither sees the other's batch, so the operator receives one question **twice**. Measured twice on 2026-09-20; `scripts/notify-gate.py` records the same two duplicates from the gate side.

The ledger is the mark they were missing. Claim a subject before batching it; a second layer's claim is refused and it drops the subject from its own batch, while the subject stays visible in the consolidated list.

## Invocation

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/asked-ledger.py $ARGUMENTS
```

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/asked-ledger.py claim   --session <blocked-session-id> --layer <fleet|worker> --text "<the question>"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/asked-ledger.py resolve --session <blocked-session-id>
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/asked-ledger.py list
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/asked-ledger.py prune
```

Storage is on disk, never in context — `~/.claude/state/asked-ledger.json`, written only through the script (flock, then atomic tmp+rename). Never hand-edit the file.

## The claim contract — the one thing callers must act on

| Exit | Meaning | What the caller does |
|---|---|---|
| **0** | This asker owns the ask | Include the subject in its batch |
| **3** | Another asker holds an open claim | **Drop it from the batch** and say so in one line — it is not lost, it is in `list` |

`resolve` the subject once the answer is relayed, or the claim stays open and keeps suppressing the other layer.

**The same asker re-claiming is not refused** — that is its own cadence, not the cross-layer duplicate, and refusing it would deadlock the asker. A **resolved** subject is claimable again, so a session that unblocks and blocks again is not deadlocked by its own earlier mark.

## Keyed on the subject, never on the asker

The blocked session is the only join both layers can agree on: both see it, and neither sees the other's batch. An asker-keyed ledger would let each layer hold its own copy and the duplicate would survive.

## Two marks — do not merge them

- **This ledger marks the ASK** (has this blocked session been batched yet, and by whom). It is **shared**, because "asked exactly once" is a cross-layer property a per-layer mark cannot express.
- **`scripts/notify-gate.py`'s ledger marks the gate NOTIFICATION, and stays per layer on purpose** — a shared cadence ledger would let one layer prune the other's gates as "cleared", after which they re-raise forever. See its docstring.

They answer different questions — *already asked?* versus *how often delivered?* — and one file serving both would break whichever answer it did not keep.

## Prune by age and liveness, never by sweep-absence

`prune` drops a resolved entry past `--max-age-hours`, and an open entry past that age **only once its subject session is gone from `~/.claude/sessions/`**.

This is the rule that keeps a shared file from re-introducing the per-layer bug it replaced. "Absent from my sweep" is a claim about that layer's **partial view** — a shared ledger pruned that way would let the fleet's sweep delete a manager's marks. A long-blocked session is exactly the one that must not be re-asked, so age alone never drops a live claim.

## Rendering

`list` prints every open claim across every layer as **one consolidated list**. Render it once per sweep under the needs-input section. This is what makes "one grouped decision" true even though the asks themselves stay per-layer: the operator sees one list, and no session in it is asked twice.
