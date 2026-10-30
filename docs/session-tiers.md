# Session tiers — who runs which command

Every `/supervisor:*` command belongs to the tier of the session that invokes it, and its
prefix names that tier: `fleet-*` for the fleet manager, `topic-*` for a topic manager,
`goal-*` for a goal manager, `worker-*` for a worker. A few bare verbs are cross-tier —
**the table below is the list**, so it is never restated here. Ask one question — *which
session am I in?* — and the table answers what you may run.

⚠️ **One command breaks the prefix rule, and it is named in the table so it cannot be
mistaken for an oversight: `/supervisor:worker-restart`.** Its prefix names its **target**,
not its invoker — it is run by a manager, and the `worker-*` tier is the one tier whose own
rule forbids resuming or spawning a session. It cannot move tiers: a worker cannot restart
itself, because `restart-worker.py` refuses a `busy-target` and a session running the command
is by definition mid-turn.

**Since 2026-10-18 that command is a DEPRECATED ALIAS, and `/supervisor:restart` is the verb.**
The rename is what the exception was waiting for: the target set had already widened past
workers — `restart-worker.py` restarts a manager too — so a `worker-` name on the command
described a set the verb no longer had. The exception survives **only** because the alias
does: it is the alias's name that breaks the prefix rule, and removing the alias retires this
paragraph and its table row in the same change. `/supervisor:restart` carries no exception —
a bare verb names no tier, so its row states the tier directly.

Load this page, with the rest of `docs/`, via `/supervisor:read-guides`.

## The four tiers

The tiers form a **tree**, and the operator's hierarchy (2026-10-23, amended 2026-10-30) names
what each level may open: **the fleet manager is top of everything** — it opens topic managers,
and a worker or a goal manager **only when no lower-tier manager owns that subject**; **a topic
manager opens workers, and a goal manager only when that goal is flagged**; **a goal manager
opens workers**; **a worker opens nothing**. A tier opens a lower one through
`/supervisor:open`, never by a bare spawn.

⚠️ **The ownership condition survived the 2026-10-30 amendment, and it is not the same rule as
the one it sat beside.** The clause that was removed read *"each when no lower-tier manager owns
the subject"* and governed **goal managers** — a manager is never opened fresh at all now, so
that half is gone. The **worker** half is unchanged and still binds: a worker is opened directly
by the fleet manager only when no topic or goal manager owns its subject, which is the same
fallback `docs/fleet-surface.md` § Which manager owns a subject states. Do not read the removal
as licensing the fleet tier to open a worker under a live goal manager.

⚠️ **Restart is not open, and that distinction is what makes the tree coherent** (operator
ruling 2026-10-30). A restart resumes a session that already has a subject and a recorded id;
an open creates supervision where none existed. Restarting a broken manager therefore decides
nothing new, while opening one decides who supervises. Four rules follow:

1. **A crashed or lapsed manager or worker is restarted automatically** — it already owns its
   subject, so the restart decides nothing new.
2. **A flagged page is opened automatically by the tier above it** — `flag: true` means
   *"open me"*, and it opens only when `flag_set_by` is `operator` or `legacy`. Any other
   value, **including an absent one**, is refused: an absent `flag_set_by` is a writer that
   did not declare itself, which is the bypass (`commands/open.md`).
3. **An approved worker is opened automatically**, up to `spawn.maxConcurrent`.
4. **A manager is never opened fresh on the operator's behalf** — not by a loop, not by a
   round, not to fill a gap. Only restarted, or opened because it was flagged.

⚠️ **Rule 4 binds every tier.** The fleet tier removed its manager-proposal row on 2026-10-29
(`commands/fleet-drive.md`: *"No manager-proposal row — the round does not propose managers for
unmanaged goals"*). No other tier restores that behaviour under another name.

⚠️ **`flag` carries one meaning tree-wide.** It marks a page the operator wants opened. A leg
that reads it as *"parked / waiting-on-human"* and excludes it inverts the operator's mark; the
parked class needs its own marker, never this field.

⚠️ **The `-spawn` verbs are narrower than their tier, and that is not a contradiction.** A
`-spawn` verb starts the level directly below it and nothing else — `topic-spawn` starts a
**goal manager**, `goal-spawn` starts a **worker** — while the tier itself opens anything
below it through `/supervisor:open`: a worker opened from the topic tier goes through
`/supervisor:open --task`, the same path a goal manager uses. There is no `fleet-spawn` — a
topic manager is opened on the **operator's initiative** and the fleet manager performs the
open — and no `worker-spawn` at all.

| Tier | Manages | May start | Done condition |
|---|---|---|---|
| **Fleet manager** | every topic manager, and every goal with no topic | a topic manager (on the operator's initiative), a **worker** directly when no lower-tier manager owns it, and a goal manager **only when that goal is flagged** | the fleet has no unmanaged subject and no gate it owns is outstanding |
| **Topic manager** | ONE topic's declared goal set, the goal managers running on it, and the workers it opens | a **worker**; and a **goal manager only when that goal is flagged**, at most N at once per topic and never past the fleet manager ceiling | every goal in its declared set is either managed, terminal, or ranked out of the N slots with the reason stated |
| **Goal manager** | ONE goal's declared task set, and the workers running on it | a **worker**, under the spawn cap | every task in its declared set is terminal, waiting on a named party, or held with a stated reason |
| **Worker** | ONE anchored task | nothing — a worker never opens a session | its anchored task is terminal, or it reports the gate that stops it |

⚠️ **The tree starts at whatever level exists.** Not every piece of work needs a goal or a
topic: a worker whose task has no goal is managed by the nearest existing ancestor — its
topic's manager if the task sits directly under a topic, otherwise the fleet manager. A goal
with no topic is managed by the fleet manager. `docs/fleet-surface.md` § Which manager owns a
subject is the canonical statement of that fallback.

⚠️ **A worker whose task declares a goal is parented only to that goal's manager.** When the
goal has no live manager the worker falls to the fleet manager (the root) as a holding row —
never to the topic manager through the goal's topic, because a topic manager parents only the
workers it already owns (`commands/topic-loop.md`). ⚠️ **Opening a worker and parenting one
are different rights** — a topic manager may open a worker, but it does not thereby adopt the
worker of a goal it does not own. The topic manager's product is starting that goal's manager
(`docs/fleet-surface.md` § Which manager owns a subject tier 4); once it is live the worker
parents to it. Two documented exceptions keep a topic manager as a worker's parent: a task
whose `managed_by` names a topic, and a goal-less task listed directly under a topic.

⚠️ **`topic-*` commands hold no `spawn_agent` tool.** A topic manager starts a goal manager
through `/supervisor:open --goal`, whose resume and spawn branches already carry the hold
check, the liveness guard and the spawn-recipe rules. Handing the topic tier its own spawn
tool would create a second, unaudited route to a session — the exact failure
`commands/open.md` § Step 2C exists to prevent. `commands/topic-spawn.md` is the tier's own
front door and it delegates.

## Command → tier

Each command maps to exactly one tier. "Never run from" states the same boundary from the
other side.

| Command | Tier | Never run from |
|---|---|---|
| `/supervisor:fleet-loop` | fleet manager | worker; topic manager (use `topic-loop`); goal manager (use `goal-loop`) |
| `/supervisor:fleet-status` | fleet manager | worker |
| `/supervisor:fleet-drive` | fleet manager | worker; topic manager (use `topic-drive`); goal manager (use `goal-drive`) |
| `/supervisor:fleet-workers` | fleet manager | worker |
| `/supervisor:fleet-verify` | fleet manager | worker; topic manager (use `topic-verify`); goal manager (use `goal-verify`) |
| `/supervisor:topic-loop` | topic manager | worker; goal manager — arming it collapses the two roles silently |
| `/supervisor:topic-status` | topic manager | worker; goal manager |
| `/supervisor:topic-drive` | topic manager | worker; goal manager |
| `/supervisor:topic-verify` | topic manager | worker; goal manager |
| `/supervisor:topic-spawn` | topic manager — starts a goal manager | worker; goal manager — this verb belongs to the topic tier |
| `/supervisor:goal-loop` | goal manager | worker — arming it collapses the two roles silently |
| `/supervisor:goal-status` | goal manager | worker; topic manager (use `topic-status`) |
| `/supervisor:goal-drive` | goal manager | worker; topic manager (use `topic-drive`) |
| `/supervisor:goal-verify` | goal manager | worker; topic manager (use `topic-verify`) |
| `/supervisor:goal-spawn` | goal manager — spawns a worker | worker — workers never start sessions |
| `/supervisor:goal-answer` | goal manager | worker; topic manager — a goal manager is a tab session and answers its own prompts |
| `/supervisor:goal-drain` | goal manager | worker; topic manager — it serves headless workers only |
| `/supervisor:stop` | topic manager, goal manager — stands down *this* session's loop | worker (it has no loop to stop) |
| `/supervisor:reset` | topic manager, goal manager — re-discovers *this* manager's state | worker |
| `/supervisor:open` | fleet manager, topic manager, goal manager — resolves a name, then jumps, resumes or spawns | worker — workers never open sessions |
| `/supervisor:open-a2a` | fleet manager, topic manager, goal manager — resolves a name, then creates the worker on the a2a-worker-controller (the backend chosen by the row's `target:`) | worker — workers never open sessions |
| `/supervisor:worker-drive` | worker — drives its own anchored task | any manager — a manager moves workers with `goal-drive`, never by driving a task itself |
| `/supervisor:restart` | fleet manager, topic manager, goal manager — restarts ONE worker or ONE manager, in its own role colour, and tells a manager to re-arm its loop | worker — resuming a session is dispatch, and the script refuses `busy-target` for a session restarting itself |
| `/supervisor:worker-restart` | fleet manager, topic manager, goal manager — **DEPRECATED alias for `/supervisor:restart`, and the one exception to the prefix rule: `worker-` names the TARGET, not the invoker** | worker — same as `/supervisor:restart` |
| `/supervisor:read-guides` | any | — |
| `/supervisor:jump` | any | — |
| `/supervisor:close` | any | — |
| `/supervisor:who-needs-me` | any | — |
| `/supervisor:who-needs-my-help` | any — sweeps every live session and reports; its one outward action is a `SendMessage` per live manager | — |
| `/supervisor:attention-next` | any — relays the operator's own answer | — |
| `/supervisor:inbox` | any — the operator's own approval queue; relays their approve / reject / later, and never picks a row itself | — |
| `/supervisor:ready` | any — readies ONE named task to the spawn bar (audit → repair → re-audit in a sub-agent), then opens it or escalates | — |

Keep this table in lockstep with `commands/`: a command file with no row here, or a row with
no command file, is a defect. Check it with:

```bash
diff <(ls commands/*.md | sed 's|commands/||;s|\.md$||' | sort) \
     <(grep -oE '^\| `/supervisor:[a-z0-9-]+`' docs/session-tiers.md | sed 's|.*:||;s|`||' | sort)
```

⚠️ **The character class carries digits, and that is load-bearing rather than tidy.** It read `[a-z-]+` until 2026-10-17, which matched every command name then in the table — until `/supervisor:open-a2a`, whose `2` the class cannot match. The row was added, the pattern returned nothing for it, and the diff above reported a command file with no row *because of the checker's own regex* — a false failure whose remedy a reader would look for in the wrong file. Any future name with a digit needs this class, not a new special case.

Empty output is a pass.

## The worker boundary

- **`/supervisor:open` is manager-only.** Opening, resuming or spawning a session is dispatch, and
  dispatch authority is the manager's (`docs/fleet-surface.md` § Session roles). A worker
  never runs `/supervisor:open` — nor `/supervisor:goal-spawn` — and never hands the operator a
  session-opening command.
- **A worker talks to its manager for anything out of scope.** Work outside its anchored task,
  a blocker another session must clear, a content question: the worker sends it to its
  manager over `SendMessage` and stays on its own task. Permission prompts are the exception —
  a peer message cannot release a harness gate, so a worker answers those in its own tab.

The full worker-side rule is the global rule `worker-does-not-open-sessions`
(`~/.claude/claude-md-rules/worker-does-not-open-sessions.md`); the two bullets above are the
summary a session needs to pick its commands, not a second copy of the rule.

## Handing the operator a pane — jump links

When a manager or fleet session needs the operator in another session's pane, it hands over
a link, never a tab id.

- **Generate it, one line per pane:** `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py <pane-id>`
  (`--label` prefixes the pane title). The `:-` fallback is load-bearing, not style:
  `CLAUDE_PLUGIN_ROOT` is **unset** in a command's Bash (verified 2026-09-23 — it expands to the
  empty string), so the bare form resolves to `/scripts/jump-link.py` and fails. With the local
  fleet-jump server configured it prints
  `http://127.0.0.1:1337/jump?pane=<N>&t=<token>`, followed with SHIFT+CMD+click; without it,
  the `/supervisor:jump <N>` command. Either way the line is usable as printed.
- **Never hand-build the URL.** The token lives in `~/.claude/secrets/jump-token` (0600) and
  the server refuses a request without it — a hand-typed link with no `t=` returned
  `Forbidden — Missing or invalid token` (2026-09-23). A hand-built link is either broken or
  copies the token somewhere it should not be.
- **`/supervisor:jump` is the executor, not an emitter.** It *follows* a pane id; the
  managers' status commands emit links and never run it themselves.
- **A session started before a plugin update keeps the command text it already loaded** — so
  it can still hand over the old form after the plugin changed. ⚠️ **A restart is not the
  lever; `/reload-plugins` is, and a session can read which version it is serving.** The
  probe, the lever and the two checks that do *not* discriminate have one home:
  `docs/fleet-surface.md` § A plugin install does not reach a running session. Read it there;
  it is not restated here.

The row format each status table uses for these links is `docs/fleet-surface.md` § Sweep
output — the fleet table.

## The manager boundary

- **A manager is never started in a worker session.** ⚠️ **Starting a *topic* manager is the
  operator's decision — the fleet manager performs the open. A *goal* manager is started by
  its topic manager, or by the fleet manager when no topic manager is live** —
  `docs/fleet-surface.md` § Session roles is the canonical statement.
- **A manager does not execute a worker's task.** It spawns, drives, answers and reaps
  workers; the task's work happens in the worker's own session.
- **A goal manager starts or resumes a worker on a vault task with `/supervisor:open --task`** —
  the path that reads the task's recorded session and writes it back. Reach for a bare
  goal-spawn (a direct `spawn_agent` call) **only** when the brief has no task file;
  `commands/goal-spawn.md` is the canonical statement of that split. ⚠️ **This is a rule
  about dispatching a worker onto a task — it is not a general ban on `spawn_agent`.** Two
  documented direct-call paths sit outside it: the loops execute the rows their sweep already
  decided directly (`commands/goal-loop.md` § Procedure, the `To open` and `To resume`
  bullets; the act leg it runs is the `supervisor:goal-drive` agent, `agents/goal-drive.md`),
  and continuing an exited headless worker is a direct call by design
  (`docs/fleet-surface.md` § A headless worker exits at turn end). Neither is a hand-rolled
  dispatch. ⚠️ And it is a **policy, not a gate** — the same caveat `docs/fleet-surface.md`
  § Session roles attaches to the worker-side prohibition, so do not go hunting for an
  enforcement that does not exist.
- **A topic manager starts a goal manager with `/supervisor:open --goal "<goal>"`**, and only
  for a goal its own `## Goals` list declares. `commands/topic-spawn.md` owns the three
  refusals (`goal-not-in-scope`, `goal-not-startable`, `manager-cap`), the ranking that picks
  which goals get one of the N slots, and the keep-alive contract. It holds no `spawn_agent`
  tool.
