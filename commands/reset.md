---
description: Re-discover this manager's state from disk rather than delete it. The reset contract is the vault's Worker Manager Session runbook § Reset; this file owns the invocation and the report.
allowed-tools:
  - Read
  - Bash(python3:*)
argument-hint: "[goal or topic name] [--dry-run]"
---

Re-baseline the manager **this session** is running.

**The reset contract — the steps, what each may change, and what it must never touch — is the vault's `Worker Manager Session` runbook § Reset. Read it and execute it from there; do not restate it here.** What this file owns is the invocation and the report. The four steps run in one script so the ledger's no-discard check and the digest rewrite happen where they can be tested, not in prose.

<process>
1. **Pick the subject argument.** `$1` given → pass it as `--subject "$1"` (source `explicit`). No `$1` → the most recent `/worker-manager`, `/worker-status` or `/supervisor:reset` argument, or goal/topic page referenced as a subject in this conversation, passed as `--subject "<name>" --source conversation`; none → pass no `--subject` and let the script fall through to this session's own state and name. **Never** read `~/.claude/state/worker-manager/last-<vault>.json` to pick it — the script prints that file's content only to show it was skipped.

2. **Run the reset:**

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/reset.py [--subject "<name>" [--source conversation]] [--dry-run]
   ```

   Add `--vault <name>` only when this session has no recorded vault. `--dry-run` computes and prints every step and writes none of reset's changes.

3. **Print the script's output verbatim** — it is the report: the subject with the sources used, missed and skipped; the digest before/after; the ledger's before/after entry count with the per-entry outcome (`CLOSED` with the cited path, `FLAGGED` with the reason, or open with why); and the tracked set with both of its sources. Do not summarise it; a summary is where a dropped entry hides.

4. **Tell the operator what the next tick will do** — a full sweep, not a diff — and name any `FLAGGED` entries as the ones needing their attention.
</process>

<constraints>
- **Write state only through the script.** Anything the contract permits, the script does; anything else is out of scope for this command.
- End the turn with the session's normal closer. This report is not one.
</constraints>

<error_handling>
- **`❌ No subject resolves`** (exit 3) — print it and stop. Do not fall back to `last-<vault>.json`.
- **`error: ledger id set changed`** — print it verbatim and say nothing was written. This is the no-discard guard firing; never retry around it.
- **`error: vault … not in vault-cli config`** — re-run with `--vault <name>`.
</error_handling>
