# restart-worker.py

Restart ONE stale idle worker session under a narrow allow rule.

A manager's standing mandate (`65 Runbooks/Manager Session.md` § Gate triage class A)
covers killing and restarting a live WORKER whose loaded code is stale. This script is
the narrow, allowlistable way to exercise it: one session id in, one registry pid killed,
the same session resumed in a new tab — never a broad `kill` rule.

## Usage

```
scripts/restart-worker.py <session-id> [--dry-run] [--sessions-dir DIR]
```

`--dry-run` runs every check and reports what it *would* do, killing nothing. It is the
right way to ask "would this restart be allowed?" without acting.

The registry is read from `SUPERVISOR_SESSIONS_DIR`, else `~/.claude/sessions`. The load
path is read from `SUPERVISOR_LOAD_PATH`, else
`~/.claude/plugins/cache/claude-supervisor/supervisor`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — the pid was signalled and the session resumed (or, under `--dry-run`, would have been). |
| `1` | Refusal. The first line of stdout is the reason token below; the second is the sentence. **Or** an operational failure, printed with an `❌ error:` prefix instead of a token — see below. |
| `2` | Usage error (argparse) — a missing or empty session id, an unknown flag. |

A refusal is a printed reason, never a traceback.

## The seven refusals

Each exits `1` with its token as the first line of stdout. Every one fires **before**
anything is signalled, so no code path reaches `os.kill` on a target it has not cleared.

| Token | Fires when | Why it is a refusal |
|---|---|---|
| `unknown-session-id` | No registry entry carries that id — or the registry directory itself could not be read. The message says which. | An id that cannot be resolved cannot be safely acted on, and an unreadable registry cannot prove the id is absent. |
| `ambiguous-pid` | More than one registry pid claims that session id, or the entry carries no integer pid. | The pid must be unique. Killing one of two claimants is a coin flip on someone else's work. |
| `busy-target` | `status` is not `idle`. | A mid-turn kill loses the in-flight work. |
| `headless-target` | `kind` is not `interactive`. | A headless worker has no reliable resume guard, so its liveness stays undetermined — see `a separate task`. |
| `manager-target` | The target resolves as a manager. | A manager is NEVER restarted. The mandate is class A for workers only. |
| `role-undetermined` | No role signal could be read. | Fail closed. An unreadable index proves nothing about a role; never assume worker. |
| `stale-load-path` | None of the three inputs — newest load-path copy, the worker's MCP config file(s), the launcher script — is newer than the session's own start, or the session carries no parseable start time. | Nothing would load differently — the restart buys nothing. |

## Operational failures — not refusals

Exit `1` with an `❌ error:` prefix is an **operational failure**, not a refusal about the
target. The one case today is `PermissionError` from `os.kill`: the pid resolved and the
session id is known, but the caller may not signal it. It is deliberately *not* reported
under a refusal token — reporting it as `unknown-session-id` would send the operator
hunting for a typo in an id that was in fact found. The seven tokens above stay exactly
seven; this is a different class of failure, and the prefix is what distinguishes them.

## How "stale" is decided

Runbook precondition 4 says: *"Something must actually load differently. Verify the fix is
present in the copy that will be executed — the load path, not the marketplace clone —
before killing anything."*

The script enforces it rather than leaving it to memory. It takes the newest directory
under the **load path** (never the marketplace clone, which updates independently of what
a running session loaded) and compares its mtime against the session's own `startedAt`.
Code is not the only thing a restart reloads. Claude Code spawns MCP servers from the
config it read at session start, and `/mcp` Reconnect never re-reads that file — so a
changed `env` in the MCP config, or a changed launcher, reaches the worker only through a
restart. The script therefore compares **three** inputs against `startedAt`:

1. the newest load-path directory (as above);
2. every `--mcp-config` path in the worker's **live argv** — `ps -o args= -p <pid>`, read
   before the kill, because the registry carries no argv and afterwards there is no
   process left to read;
3. the launcher script the resume will run (`CLAUDE_SCRIPT`, resolved to a file).

Any one newer accepts, and the output's `changed:` line names which input changed. Only
when **all** are no newer does it refuse as `stale-load-path`, and the refusal lists what
it checked. An input that cannot be read — no argv, an unresolved `CLAUDE_SCRIPT`, a
missing file — contributes nothing: it can never turn a refusal into an accept, so the
check falls back to the load path alone. A session with no parseable start time refuses
too, because "cannot be shown to differ" is not "differs".

## How role is decided

The registry carries no role field, so role is **derived**, and the script derives it by
calling `fleet-board.py`'s own functions through the repo's existing importlib
sibling-import — `vault_index()`, `loop_slugs()`, `manager_subject()`, `colour_census()`.
It is not a reimplementation, so the script and the fleet board can never disagree about
who is a manager.

All four of `build_grouping()` Rule 1's signals are honoured:

1. **The root** — the registry `name` is `Fleet Manager`. There is no `Fleet Manager` page
   in `23 Topics`/`24 Goals`, so no other signal catches it, and missing it would restart
   the root manager.
2. **`manager_subject()` Source 1** — `slug(name)` is a member of `loop_slugs()`'s set
   (from the `*.cadence` / `*.stopped` records in `~/.claude/state/sweep-gate/`) *and* a
   topic/goal title slugs to the same value.
3. **`manager_subject()` Source 2** — the registry `name`, or the name with a
   `<X> Manager` suffix stripped, matches a topic/goal title exactly and case-insensitively.
4. **Colour** — the session's colour is orange (`MANAGER_COLOUR`).

A predicate covering only signals 2 and 3 would classify the root manager as a worker and
restart it. Note also that the colour comes from `fleet-colours.py census()`, which reads
the last `agent-color` record in the session **transcript** — *not* from
`~/.cache/wezterm-role-map.json`, which is read at spawn time by `server/config.mjs` and is
what *sets* a manager's colour in the first place.

## The allow rule to add

Scope the rule to this script's path only. Never allow bare `kill` / `killall` / `pkill` —
a rule on those is exactly what this script exists to avoid.

```json
{
  "permissions": {
    "allow": [
      "Bash(~/.claude/plugins/cache/claude-supervisor/supervisor/*/scripts/restart-worker.py:*)"
    ]
  }
}
```

Verify live, with two probes — the second must stay anchored on `Bash(`, because an allow
rule is shaped `Bash(<cmd>:*)` and a whitespace-delimited pattern matches no real rule:

```bash
jq -r '.permissions.allow[]' ~/.claude/settings.json | grep restart-worker.py   # exactly 1 line
jq -r '.permissions.allow[]' ~/.claude/settings.json | grep -E 'Bash\([^)]*p?kill(all)?'  # 0 lines
```

## What this script does NOT do

- **It never restarts a manager.** Class A is a closed list, and adding to it is an
  operator decision, not a manager one.
- **It does not fix the headless resume guard.** Headless workers are refused, not handled.
- **It is not the orchestrator.** `commands/worker-restart.md` collapses kill → resume →
  re-orient into one verb and calls THIS script for the kill+resume leg rather than
  reimplementing it, so exactly one kill+resume mechanism exists. Its read-only pre-kill
  probe is the sibling `scripts/restart-precheck.py`, which owns the worktree check and the
  cause-of-death classification; the two scripts share the registry read but not the
  refusal vocabulary — the seven tokens above stay exactly seven.

## The one deliberate copy

The resume recipe (`wezterm cli spawn … --resume <session-id>`, with the four
`CLAUDE_CODE_*` variables unset) is copied from `commands/open.md` Step 3.1. A markdown
command is not executable, so the two are kept in lockstep by hand — and that is precisely
why the orchestrator command must call this script instead of carrying its own copy.
