---
name: hold
description: Mark a session as held — the operator's durable "do not work on this, push it forward, or have any manager handle it". A hold is keyed on the SESSION, not on a task, and it is read by every consumer that can act on a session. Use when the operator wants a session left alone, when releasing a hold, or when listing what is currently held.
argument-hint: "<session|pane> --reason \"...\" | --release <session|pane> | --list"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-holds.py *)
---

The single home of the hold rules. Commands and agents point here instead of restating them.

Storage is on disk, never in context — `~/.claude/state/session-holds.json`, keyed on session id, each entry carrying `reason` / `held_at` / `held_by`. Written ONLY through the script (flock on a sidecar, then atomic tmp+rename). Never hand-edit the file.

## Invocation

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-holds.py $ARGUMENTS
```

Run exactly that. The script takes the flag-first shape directly, so `$ARGUMENTS` passes through verbatim — there is no translation to get wrong.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-holds.py <session-id|pane-id> --reason "why"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-holds.py --release <session-id|pane-id>
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-holds.py --list
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-holds.py is-held <session-id>   # exit 0 held, 1 not
```

A **pane id** resolves through its WezTerm tab title to the registry `name` — the same join the server's `findRegisteredByName` uses. The registry carries no pane id, and pane ids do not survive a WezTerm restart, so re-resolve after one.

## Rules

- **A hold is keyed on the SESSION, never on a task.** It is operator *policy*, not a work disposition: `status: hold` on a task already means "blocked/paused, no resume date", and the two are orthogonal. The operator's gesture is on a session, and a held session may have no task at all — a side quest. Task rows join to a hold through their own `claude_session_id`.
- ⚠️ **The row stays visible; only the message stops.** A hold does NOT hide a row and does NOT pause the session. Every consumer must still RENDER the held row — `⏸️ HELD — <reason> · <age>` — and must stop every ACT on it: no nudge, no reap, no auto-resume, no open, no escalation, no board publish. Suppressing the *row* would remove the evidence, and a row that disappears from a sweep is indistinguishable from a row that got fixed. Do not conflate the two.
- **A hold is removed only by `--release`.** Pruning is by age and liveness, never by sweep-absence: *"absent from my sweep"* is a claim about the sweep's own partial view. A hold whose session has died is **surfaced** by `--list` as a release candidate, not silently dropped.
- **A re-hold keeps the original `held_at`.** The age of a hold is how long the operator's policy has stood, not how long ago the reason was last edited; only the reason is replaced.
- ⚠️ **Reads are lock-free, and consumers inline their own reader.** Every write lands through `os.replace`, so a reader sees the whole old file or the whole new one and never a partial one. A consumer must NOT shell out to this script — a subprocess dependency on a plugin path is a fail-open, and a silent fail-open in a gate is its worst failure mode. `sweep-gate.py` in the vault is the worked example: it is vault-local and cannot inherit `${CLAUDE_PLUGIN_ROOT}`, so it carries its own reader for this file exactly as it already does for the registry, the attention feed and the heartbeat store.
- **It is not a work disposition and it does not stop the session.** A hold suppresses what *managers* do. The held session keeps running.
