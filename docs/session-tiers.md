# Session tiers — who runs which command

Every `/supervisor:*` command belongs to the tier of the session that invokes it, and its
prefix names that tier: `fleet-*` for the fleet manager, `manager-*` for a goal/topic manager,
`worker-*` for a worker. Two bare verbs are cross-tier. Ask one question — *which session am I
in?* — and the table below answers what you may run.

Load this page, with the rest of `docs/`, via `/supervisor:read-guides`.

## The three tiers

| Tier | Carries | Started by |
|---|---|---|
| **Fleet manager** | every session on the machine; one stateful loop | the operator, in a session created for it |
| **Manager** (goal/topic manager) | ONE goal or ONE topic and its tracked task set | the operator, in a session created for it |
| **Worker** | ONE anchored task | a manager (`/supervisor:manager-spawn`, or `/open` run from the manager's side) |

## Command → tier

Each command maps to exactly one tier. "Never run from" states the same boundary from the
other side.

| Command | Tier | Never run from |
|---|---|---|
| `/supervisor:fleet-loop` | fleet manager | worker; goal/topic manager (use `manager-loop`) |
| `/supervisor:fleet-status` | fleet manager | worker |
| `/supervisor:fleet-drive` | fleet manager | worker; goal/topic manager (use `manager-drive`) |
| `/supervisor:fleet-workers` | fleet manager | worker |
| `/supervisor:manager-loop` | manager | worker — arming it collapses the two roles silently |
| `/supervisor:manager-status` | manager | worker |
| `/supervisor:manager-drive` | manager | worker |
| `/supervisor:manager-spawn` | manager | worker — workers never start sessions |
| `/supervisor:manager-answer` | manager | worker |
| `/supervisor:manager-drain` | manager | worker |
| `/supervisor:stop` | manager — stands down *this* session's manager loop | worker (it has no loop to stop) |
| `/supervisor:reset` | manager — re-discovers *this* manager's state | worker |
| `/supervisor:worker-drive` | worker — drives its own anchored task | any manager — a manager moves workers with `manager-drive`, never by driving a task itself |
| `/supervisor:read-guides` | any | — |
| `/supervisor:jump` | any | — |
| `/supervisor:who-needs-me` | any | — |

Keep this table in lockstep with `commands/`: a command file with no row here, or a row with
no command file, is a defect. Check it with:

```bash
diff <(ls commands/*.md | sed 's|commands/||;s|\.md$||' | sort) \
     <(grep -oE '^\| `/supervisor:[a-z-]+`' docs/session-tiers.md | sed 's|.*:||;s|`||' | sort)
```

Empty output is a pass.

## The worker boundary

- **`/open` is manager-only.** Opening, resuming or spawning a session is dispatch, and
  dispatch authority is the manager's (`docs/fleet-surface.md` § Session roles). A worker
  never runs `/open` — nor `/supervisor:manager-spawn` — and never hands the operator a
  session-opening command.
- **A worker talks to its manager for anything out of scope.** Work outside its anchored task,
  a blocker another session must clear, a content question: the worker sends it to its
  manager over `SendMessage` and stays on its own task. Permission prompts are the exception —
  a peer message cannot release a harness gate, so a worker answers those in its own tab.

The full worker-side rule is the global rule `worker-does-not-open-sessions`
(`~/.claude/claude-md-rules/worker-does-not-open-sessions.md`); the two bullets above are the
summary a session needs to pick its commands, not a second copy of the rule.

## The manager boundary

- **A manager is never started in a worker session**, and starting a manager is a human act —
  `docs/fleet-surface.md` § Session roles is the canonical statement.
- **A manager does not execute a worker's task.** It spawns, drives, answers and reaps
  workers; the task's work happens in the worker's own session.
