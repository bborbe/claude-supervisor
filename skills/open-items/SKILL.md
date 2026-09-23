---
name: open-items
description: Read and write this session's open-items ledger — the operator's asks a manager session is still carrying (instructions not yet a task, questions not yet answered, tasks filed on their behalf). Use when a manager loop (/supervisor:fleet-loop, /supervisor:manager-loop, /supervisor:manager-drive) records, renders, answers, notes or closes an operator ask. Subcommands list | add | answer | note | close.
argument-hint: "<list|add|answer|note|close> [flags]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py *)
---

The single home of the ledger rules. Commands and agents point here instead of restating them.

Storage is on disk, never in context — `~/.claude/state/open-items/<session-id>.json`, written only through the script (atomic tmp+rename, timestamps stamped by the script). Never hand-edit the file.

## Invocation

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py $ARGUMENTS
```

Run exactly that. The script derives the session id from `$CLAUDE_CODE_SESSION_ID` — pass `--session <id>` only to read another session's ledger (a reset, a resumed session). Never use `$CLAUDE_SESSION_ID` (Claude Code does not export it) and never take the id from the newest file in `state/`.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py list                                  # render
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py add --kind asked-of-me --text "<verbatim>" --task "<task>"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py answer --id <id> --answer "<the operator's words>"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py note   --id <id> --text "<evidence / progress>"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py close  --id <id> --evidence "<the on-disk fact>"
```

`scripts/open-items.py` stays at that path on purpose: vault-cli's `/post-compact` calls it directly, and `scripts/reset.py` imports it.

## Kinds

| Kind | What it is | Resolves on |
|---|---|---|
| `asked-of-me` | an operator instruction | its task file reads `status: completed`, or the operator withdraws it |
| `asked-of-you` | a question the manager put to the operator | the operator's explicit answer — nothing else |
| `pushed` | a task the manager filed or spawned on their behalf | that task file reads `status: completed` |

## Rules

- **An instruction becomes an entry THE MOMENT IT IS SAID** — `add` it *before* replying, not after deciding what to do about it. A task is an entry's resolution path, never its start.
- **Read at sweep/round start, render every sweep** under the fixed heading `📋 Open with the operator` — one line per open entry, `kind · what · state · age`. **Never omitted**, including on a no-change tick; print `(none open)` when the ledger is empty.
- **Act every sweep** on `asked-of-me` / `pushed`: no task → file one and name it on the entry; task with no worker → spawn (standing mandate, its cap); stalled owner → nudge; task reads `status: completed` → verify on disk this sweep, then `close --evidence`. `asked-of-you` → re-surface until answered; record the answer with `answer` in that turn, which closes it.
- ⚠️ **`answer` is only for the OPERATOR's words — anything else is `note`.** On `asked-of-you`, `answer` writes `closed_evidence: "operator answered in session: …"`; using it for anything else forges an operator attribution a later reader cannot tell from a real one. On the other two kinds `answer` records a note and leaves the entry open. Reach for `note` by default.
- **`close` requires `--evidence` — an on-disk fact**, never belief and never a peer's claim of an operator decision. ⚠️ A `resolves_on` containing AND → verify every half on disk.
- **`⚠️ UNRESOLVABLE` on a `list` line** means the entry's `--task` backs no vault file — its close condition can never fire. Fix the entry's task; do not wait on it.
- **It replaces nothing.** A topic page's § Current Work stays the deep layer; the ledger is the asks layer above it. A worker manager's ledger carries its subject's asks; the fleet manager's ledger is the residual — an ask one ledger carries is not the other's to carry too.
