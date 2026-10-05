---
name: open-items
description: Read and write this session's open-items ledger — the operator's asks a manager session is still carrying (instructions not yet a task, questions not yet answered, tasks filed on their behalf). Use when a manager loop (/supervisor:fleet-loop, /supervisor:manager-loop, /supervisor:manager-drive) records, renders, names a task on, answers, notes, withdraws or closes an operator ask. Subcommands list | add | set | answer | note | withdraw | close | classify.
argument-hint: "<list|add|set|answer|note|withdraw|close|classify> [flags]"
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
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py set  --id <id> --task "<the task covering it>"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py add --kind asked-of-me --text "<verbatim>" --held-in-pane <pane-id>   # the ask lives in a worker's pane, not here — refused on asked-of-you
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py answer --id <id> --answer "<the operator's words>"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py note   --id <id> --text "<evidence / progress>"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py withdraw --id <id> --reason "<why — the operator's words>"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py close  --id <id> --evidence "<the on-disk fact>"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/open-items.py classify                            # which entries' recorded origin panes are gone / live / unknown
```

`scripts/open-items.py` stays at that path on purpose: vault-cli's `/post-compact` calls it directly, and `scripts/reset.py` imports it.

## Kinds

| Kind | What it is | Resolves on |
|---|---|---|
| `asked-of-me` | an operator instruction | its task file reads `status: completed`, or the operator withdraws it |
| `asked-of-you` | a question the manager put to the operator | the operator's explicit answer — nothing else. ⚠️ **Never for a gate held in a worker's pane** — see Rules. `add` also records **where the gate was raised** (the raising session and `$WEZTERM_PANE`), so `classify` can later tell a gone pane from a live one — provenance, not a resolution path |
| `pushed` | a task the manager filed or spawned on their behalf | that task file reads `status: completed`, or the operator withdraws it |

## Rules

- **An instruction becomes an entry THE MOMENT IT IS SAID** — `add` it *before* replying, not after deciding what to do about it. A task is an entry's resolution path, never its start.
- **Read at sweep/round start, render every sweep** under the fixed heading `📋 Open with the operator` — one line per open entry, `kind · what · state · age`. **Never omitted**, including on a no-change tick; print `(none open)` when the ledger is empty.
- **Act every sweep** on `asked-of-me` / `pushed`: no task → **`set` the covering task on the entry, or file one first if none exists** — and only if neither is possible, say so in the sweep and why; task with no worker → spawn (standing mandate, its cap); stalled owner → nudge; task reads `status: completed` → verify on disk this sweep, then `close --evidence`. `asked-of-you` → re-surface until answered; record the answer with `answer` in that turn, which closes it. ⚠️ **A row carrying `⚠️ NO TASK` is this rule's own trigger** — see the marker rule below.
- ⚠️ **An OPEN `asked-of-me` / `pushed` entry naming no task renders `⚠️ NO TASK`, and that marker is why the act step is observable.** Such an entry is the one whose close condition can never fire — `asked-of-me` / `pushed` resolve on a task file, and with no task named there is nothing for the condition to read — yet before this marker existed it printed exactly like a healthy entry, which is how six manager tick summaries sat open and unacted-on for a week (measured 2026-10-05). Act on it in the same sweep: `set` the covering task on the entry, or file one first. **The marker never appears on an `asked-of-you`**, which legitimately names no task because it resolves on the operator's answer — a marker there would fire on every question the ledger holds.
- ⚠️ **`withdraw` closes an entry that was never an ask — an OPERATOR act, and the third close path.** The three are distinct claims and none substitutes for another: `answer` (the operator replied to a question), `close --evidence` (the entry's own resolution condition was met, verified on disk), `withdraw` (it should never have existed). Reach for `close --evidence` by default; a withdrawal the operator did not voice forges the same attribution `answer`'s rule exists to prevent, so record `--reason` in their words. **REFUSED on `asked-of-you`**, whose only close path is `answer` — use `answer` if they replied, `close --evidence` if it was filed in error. It exists because an entry can otherwise be permanently open: a manager tick summary filed as `pushed` names no task, so no close condition could ever fire, and this is the only exit the ledger offers it.
- ⚠️ **A gate held in a worker's pane is handed over, never added as an `asked-of-you`.** This kind's only close path is `answer`, and `answer` needs *this* session to receive the operator's words — but a gate in a tab worker's pane is released by the operator's own keystroke there, and **a relay never releases a gate**, so this session never receives them and the entry could never close. It would sit open forever, indistinguishable from a question genuinely still outstanding. **Surface the gate and hand over the pane** (the `jump-link.py` output) and say a direct go is needed. Nothing is lost: a tab worker's gate is a *blocked session*, so `notify-gate.py` publishes it to the phone and the sweep renders it in the `waiting-on-human` bucket. `add --held-in-pane <pane-id>` **refuses** an `asked-of-you` for exactly this reason — pass the flag only on `asked-of-me` / `pushed`, where a pane origin is ordinary provenance because those kinds close on their task file. A question this session *can* receive the answer to — a relayable non-gate question, or a headless worker's gate answered over the supervisor's permission channel — is still an ordinary `asked-of-you`.
- **An `asked-of-you` records WHERE its gate was raised — the raising session and the pane (`$WEZTERM_PANE`) — automatically at `add` time, never via a flag.** This is provenance, not a close path: it is what lets a later reader tell a gate whose pane is gone from one still outstanding. The **session cannot** make that distinction — every entry in a ledger carries the ledger's own session, which stays LIVE long after one of its panes is gone — so the **pane** is the discriminator, and it is now stored. `classify` reads the field back and reports `gone` / `live` / `unknown` per entry; it renders **no marker** on the sweep's `📋 Open with the operator` lines, which stay exactly as the render rule above describes. A legacy entry written before this field existed reports `unknown`, never `gone` — "could not tell" must not read as "dead", the same rule the `UNRESOLVABLE` check follows.
- ⚠️ **`answer` is only for the OPERATOR's words — anything else is `note`.** On `asked-of-you`, `answer` writes `closed_evidence: "operator answered in session: …"`; using it for anything else forges an operator attribution a later reader cannot tell from a real one. On the other two kinds `answer` records a note and leaves the entry open. Reach for `note` by default.
- **`close` requires `--evidence` — an on-disk fact**, never belief and never a peer's claim of an operator decision. ⚠️ A `resolves_on` containing AND → verify every half on disk.
- **`⚠️ UNRESOLVABLE` on a `list` line** means the entry's `--task` backs no vault file — its close condition can never fire. Fix the entry's task with `set`, or file the task it names; do not wait on it.
- ⚠️ **A manager-loop tick summary is REFUSED as a `pushed` entry — `add` exits non-zero and writes nothing.** The tick's record belongs in the sweep output, not in the ledger: `pushed` is for a task the manager filed, and it resolves when that task reads `status: completed`, so a tick log naming no task can never close. If the tick *did* file a task, add an entry whose text is that task (`--text "<the task>" --task "<the task>"`). The refusal is scoped to `pushed` on purpose — `asked-of-me` carries the operator's words verbatim, and refusing there could block a genuine instruction that happens to quote a tick.
- **It replaces nothing.** A topic page's § Current Work stays the deep layer; the ledger is the asks layer above it. A worker manager's ledger carries its subject's asks; the fleet manager's ledger is the residual — an ask one ledger carries is not the other's to carry too.
