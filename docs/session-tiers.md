# Session tiers — who runs which command

Every `/supervisor:*` command belongs to the tier of the session that invokes it, and its
prefix names that tier: `fleet-*` for the fleet manager, `topic-*` for a topic manager,
`goal-*` for the goal scope, `worker-*` for a worker. A few bare verbs are cross-tier —
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

## The three tiers

The tiers form a **tree**, and the operator's hierarchy (2026-10-23, amended 2026-10-30; goal tier merged into the topic tier 2026-11-24) names what each level may open: **the fleet manager is top of everything** — it opens topic managers, and a **worker** only when no lower-tier manager owns that subject; **a topic manager opens workers** and drives each goal it declares directly (there is no goal-manager tier); **a worker opens nothing**. A tier opens a lower one through `/supervisor:open`, never by a bare spawn.

⚠️ **The worker half of the ownership condition still binds after the goal tier merged into the topic tier.** A worker is opened directly by the fleet manager only when no topic manager owns its subject — a goal's declaring topic is its owner, which is the fallback `docs/fleet-surface.md` § Which manager owns a subject states. Do not read the tier merge as licensing the fleet tier to open a worker under a live topic manager.

⚠️ **Restart is not open, and that distinction is what makes the tree coherent** (operator
ruling 2026-10-30). A restart resumes a session that already has a subject and a recorded id;
an open creates supervision where none existed. Restarting a broken manager therefore decides
nothing new, while opening one decides who supervises. Four rules follow:

1. **A crashed or lapsed manager or worker is restarted automatically** — it already owns its
   subject, so the restart decides nothing new. ⚠️ **The fleet tier is the one exception: its
   lapse is REPORTED, never restarted** (operator decision 2026-11-01). `scripts/fleet-keepalive.py`
   detects a lapsed fleet round, and `scripts/fleet-keeper.py` posts one attention card once it
   has read `stale` continuously for 40 minutes. The keeper holds no kill path and starts nothing
   on its own judgement. **The one exception is answer-gated** (operator decision 2026-11-06): the
   card for a *dead* fleet manager offers `resume it`, and only that answer — after the death is
   corroborated — runs `restart-worker.py --absent` once; a lapsed (live) manager is never offered
   it. See `scripts/fleet-keeper.py`'s docstring. **Why the exception:** a restart-on-stale keeper killed a healthy Fleet Manager,
   because the trigger cannot tell a dead loop from a slow round. Measured healthy arm→digest
   ran median 18 min, p90 38 min and max 51 min, against an 18-minute `stale` threshold, so about
   half of healthy rounds cross it. No settle window fixes that: one under ~40 min kills healthy
   managers, and one above it leaves any restart cap unreachable. Restarting the Fleet Manager
   stays a human decision, prompted by the card.
2. **A page the operator flagged is opened automatically by the tier above it** — `flag: true` means
   *"open me"*, and **provenance is read at every tier**. Ruled 2026-09-28 (*"an agent-set
   `flag: true` is never an approval and opens nothing"*) and generalised to every tier on
   2026-11-10, attention card `f8cd8b41`: the 2026-10-30 ruling governs *which* flagged row may
   be opened, not *who* may set the flag. ⚠️ **The consequence is deliberate:** an agent that
   flags its own page gets nothing opened, at any tier, and the round prints a refusal rather
   than a silent `OPEN`. **The surfaces, the rulings and the measured basis have one home —
   `docs/fleet-surface.md` § Approval provenance; read it there.** A flag on
   a page no leg reads is inert, which is the defect `--topic-open` was added to remove.
3. **An approved worker is opened automatically**, up to `spawn.maxWorkerSoft`.
4. **A manager is never opened fresh on the operator's behalf** — not by a loop, not by a
   round, not to fill a gap. Only restarted, or opened because it was flagged — or opened by
   the operator's own `/supervisor:open`, which checks no flag (ruling 2026-11-15: unflagged
   never opens *automatically*: the rounds check the flag before they call it, and the
   command keeps the check on its undecidable-resume arm).

⚠️ **Rule 4 binds every tier.** The fleet tier removed its manager-proposal row on 2026-10-29
(`commands/fleet-drive.md`: *"No manager-proposal row — the round does not propose managers for
unmanaged goals"*). No other tier restores that behaviour under another name.

⚠️ **The fleet tier opens no flagged goal or topic, and that is rule 4, not a retreat from
rule 2.** The round once opened every flagged goal and topic with no live manager; that leg was
retired, so a flagged goal or topic is now opened only by the tier that owns it — the topic
manager drives a flagged goal directly (there is no goal manager to start), declining `phase:
todo` (the operator's approval inbox, reported `WAITING`). The fleet tier opens nothing itself: the wrapper's
flagged tick opens flagged rows, so a flagged goal or topic reaches the fleet round as a row to
report, never as an open.
Proposing is what rule 4 forbids — asking the operator *which* unmanaged goal deserves a
manager — and a goal with no topic is the operator's own `/supervisor:open --goal`, not
the round's.

⚠️ **The fleet keeper obeys rule 4 trivially: it opens nothing and resumes nothing.** There is
exactly one fleet manager, and a second would be a double writer over every subject. The
report-only keeper cannot create one: its source holds no `restart-worker.py` call, no
`wezterm` spawn and no `os.kill`. `scripts/tests/test_fleet_keeper.py` pins that absence with
a grep, plus a positive control that fires on `restart-worker.py` itself. It reads the
`Fleet Manager` registration only to name the session a card is about.

⚠️ **`flag` carries one meaning tree-wide.** It marks a page the operator wants opened. A leg
that reads it as *"parked / waiting-on-human"* and excludes it inverts the operator's mark; the
parked class needs its own marker, never this field.

⚠️ **The `-spawn` verbs are narrower than their tier, and that is not a contradiction.** `goal-spawn` starts a **worker**; `topic-spawn` no longer starts a goal manager or a worker (there is none) — it is the topic manager ceiling check (`--topic-open`) only. There is no `fleet-spawn` — a topic manager is opened on the **operator's initiative** and the fleet manager performs the open — and no `worker-spawn` at all.

| Tier | Manages | May start | Done condition |
|---|---|---|---|
| **Fleet manager** | every topic manager, and every goal with no topic | a topic manager (on the operator's initiative), a **worker** directly when no lower-tier manager owns it — the round opens no goal or topic | the fleet has no unmanaged subject and no gate it owns is outstanding |
| **Topic manager** | ONE topic's declared goal set and the workers it opens | a **worker** | every goal in its declared set is terminal, or its workers are all driven |
| **Worker** | ONE anchored task | nothing — a worker never opens a session | its anchored task is terminal, or it reports the gate that stops it |

⚠️ **The tree starts at whatever level exists.** Not every piece of work needs a goal or a
topic: a worker whose task has no goal is managed by the nearest existing ancestor — its
topic's manager if the task sits directly under a topic, otherwise the fleet manager. A goal
with no topic is managed by the fleet manager. `docs/fleet-surface.md` § Which manager owns a
subject is the canonical statement of that fallback.

⚠️ **A worker whose task declares a goal is parented to that goal's declaring topic manager.** With no goal-manager tier, the topic manager that declares the goal is its only manager, so the worker parents to it; a goal in no topic falls to the fleet manager (the root) as a holding row. A goal-less task listed directly under a topic parents to that topic, and a task whose `managed_by` names a topic parents there too.

⚠️ **`topic-*` commands hold no `spawn_agent` tool.** With the goal-manager tier removed, the topic manager drives each declared goal's rows itself and starts only **workers**, through `/supervisor:open --task`, whose resume and spawn branches already carry the hold check, the liveness guard and the spawn-recipe rules. Handing the topic tier its own spawn tool would create a second, unaudited route to a session — the exact failure `commands/open.md` § Step 2C exists to prevent.

## Keep-alive

**This section is the single home of the keep-alive contract:** which tier keeps which session
alive, what counts as an owner, and what happens to a session nobody keeps. Every other file
that touches it points here and carries only its own commands, flags and thresholds —
`agents/topic-drive.md`, `agents/goal-drive.md`,
`commands/topic-drive.md`, `commands/topic-loop.md`, `commands/open.md`,
`docs/fleet-surface.md` § Which manager owns a subject, `scripts/topic-spawn.py`, and the
docstrings of `scripts/fleet-keepalive.py`, `scripts/topic-keepalive.py`,
`scripts/worker-keepalive.py` and `scripts/owner.py`.

**Keeping a session alive is one act at every level: when the session dies or its loop lapses,
its keeper resumes the same `claude_session_id`.** It never opens a second session on that
subject — two sessions on one subject are a double writer.

### The chain

| Level | Opened by | Kept alive by | Mechanism | Guard |
|---|---|---|---|---|
| **Fleet manager** | the operator | **the operator** | `scripts/fleet-keepalive.py` reads the digest, never the heartbeat; `scripts/fleet-keeper.py` posts one card after 40 min `stale`, and resumes only on the operator's `resume it` answer | report-only — rule 1's one exception (§ The three tiers) |
| **Topic manager** | the fleet manager, on the operator's initiative | the **wrapper's keep-alive tick** (the round's leg retired in v0.367.0, `0bfff171`) | `scripts/topic-keepalive.py --branch topic`, for every topic page `scripts/topic-candidates.py` selects — every verdict but `loop-stopped`, never filtered on `claude_session_id` | 3 resumes in 30 min → `crash-loop`, escalate once |
| **Goal** | its declaring topic's manager | its **declaring topic manager's round**; otherwise the **wrapper's flagged tick**, when the goal is operator-flagged and eligible | `scripts/flagged-rows.py` emits an `open-manager` row for an eligible operator-flagged page and the wrapper opens or resumes the goal's manager; `scripts/keeper-census.py` reports that keeper | the goal tier is merged into the topic tier (2026-11-24), so a goal page's `claude_session_id` is a manager the flagged tick opens, never a goal round; an agent-set flag opens nothing (`scripts/flag_provenance.py`) |
| **Worker** — task with a goal | its declaring topic's manager, or the fleet manager when the goal has no topic | its **declaring topic manager's round**; otherwise the **fleet round** | `agents/goal-drive.md` auto-resume gate, run inside the topic round; the fallback is `scripts/worker-keepalive.py` (the wrapper's keep-alive tick) | `last_auto_resume` cap on the topic tier, `worker--<slug>` cap on the fleet tier; `worker-keepalive.py` returns `jump` for a row the topic tier owns, so the two never resume one row |
| **Worker** — task with no goal | the fleet manager, or the topic manager listing the task | the **wrapper's keep-alive tick** | `scripts/worker-keepalive.py` | `worker--<slug>` cap; a task with no recorded id is reported, never opened |
| **Background sub-agent** | its worker | **no keeper, by decision 2026-11-08** | — | it dies with its parent session, so a worker never parks a wait on one |

⚠️ **A worker crash loop is a manager decision, and no card reaches the operator.** A crash
loop is by definition already stopped, so `scripts/worker-keepalive.py`'s card recommended
`keep stopped` — the state the row is in — and asked the operator to ratify the no-op the
system performs on its own: 189 cards in 24 h, not one answered. `post_card()` therefore
refuses a card whose recommendation equals the row's current state, which suppresses the mint
for **every** row whatever its parentage. The `crash-loop:` row in the keep-alive hand-back is
the channel, and the fleet round carries the action — restart or resume the goal's manager
through the same `lapsed-managers` mechanism, or reap/stand down the worker. A row whose goal
manager is dead is manager-owned all the same; a goal-less row is the fleet leg's own. Asking
the operator to decide a resume the manager tier is already obliged to make is the defect this
boundary removes.

Infrastructure outside the session tree is owned by **launchd** (the `claude-wrapper` daemon,
the attention watcher, the model-free gate loop) and cluster work by its **controller**. Those
are kept alive; they are just not kept by a session.

### Owner

**Owner:** a manager owns a subject only when its session reads `LIVE` and its loop reads `OK`, `WORKING`, `IDLE` or `STALE`.
`WORKING` (no round, but the transcript moved) and `IDLE` (armed, no round and no work) still hold the subject, so no second writer is opened for them.
A loop reading `STOPPED`, `SLEEPING`, `UNARMED` or `INVALID`, or a probe that cannot answer,
is **not** an owner: the session may still run, but nothing runs the round that would keep its
workers alive. The session is read with `scripts/session-liveness.py --check`, the loop with
`scripts/manager-liveness.py --state`.

⚠️ **`scripts/owner.py` is this definition's one implementation, and every keeper and filter
resolves ownership through it.** `worker-keepalive.py`, `topic-keepalive.py`,
`gate-owner-filter.py` and `session-scope.py` each used to combine the two signals themselves,
and they disagreed: `worker-keepalive.py` read the session alone, so a manager stood down with
`/supervisor:stop` (session `LIVE`, loop `STOPPED`) still counted as an owner there, its worker
was deferred to a goal round that had stopped, and nothing kept it. The resolver returns one of
three words — `owner`, `not-owner`, `cannot-tell` — and a caller chooses its ACTION from those,
never a second ownership verdict from the raw facts:

- **`owner`** — session `LIVE` **and** loop `OK`, `WORKING`, `IDLE` or `STALE`.
- **`not-owner`** — loop `STOPPED` (a deliberate stand-down, whatever the session reads),
  session `ABSENT`, or no session recorded on the page. This is the only answer that licenses a
  caller to hand the subject to another keeper.
- **`cannot-tell`** — an unreadable probe, **and** the loop states that mean "deliberately not
  running": `SLEEPING` (date-gated), `UNARMED` (nothing ever armed it) and `INVALID` (a record
  that cannot be read). Never read as `not-owner`: handing a subject on during a failed read is
  how a second writer lands on a live one.

### Adopt or close

**A session no tier keeps is adopted or closed.** The tier above takes it on as a keeper, or
it is closed; it never stays live and unkept. A worker whose task is terminal or deferred is
reaped by its goal round or by `scripts/close-worker.py`; a manager whose subject is terminal,
or that was stood down while its session lives, is closed so its workers fall to the fleet.

**The stood-down half is implemented, in the fleet keep-alive leg.** `manager-liveness.py
--stood-down-past <MINUTES>` selects a manager whose loop has read `STOPPED` past the bound
(60 min — a fixed bound, no longer "4× the round's cadence": the goal tier arms no cadence now), and the wrapper's keep-alive tick joins each row to
session liveness, hands the page over, proves the handover through `close-worker.py`'s own
`page_verdict`, and closes. ⚠️ **The marker outlives the session it describes** — measured
2026-11-09, 83 `.stopped` markers against 2 whose session was still `LIVE` — so the liveness
join, not the marker, is what makes a row a close candidate. ⚠️ **Closing a stood-down topic
manager is the first step of a chain, not a single-step fix**: the topic page becomes
`fleet round` but its goals stay unkept, because a goal whose declaring topic has no live
armed manager reads `"declaring topic(s) have no live armed manager"`
(`scripts/keeper-census.py` `keepers_of()`). **One keeper does reach such a goal: the
wrapper's flagged tick.** When the goal page is operator-flagged and eligible
(`scripts/flag_provenance.py` `flag_admits()` **and** `scripts/flagged-rows.py` `eligible()`),
the tick emits `open-manager` for it and opens or resumes the goal's manager, so the census
reports `wrapper flagged tick` and the row is kept. An unflagged or agent-flagged goal has no
such resumer and stays unkept; a `cannot-tell` declaring topic, and a declaring topic whose
session is live, are never handed on — the first is the second-writer guard, the second is the
tick's own `open-manager` condition.

⚠️ **The handover no longer strands the page — the `STOPPED` rule is what keeps the chain
open.** § 1 selects on `scripts/owner.py`'s verdict through `scripts/topic-candidates.py` and
refuses only `loop-stopped`, so a page this leg has handed over **stays in the population**
unless it still carries its `.stopped` marker, and a re-manageable topic gets a replacement
manager. A `STOPPED` topic is still never re-opened,
so for one of those the goals stay unkept — a round must not report them kept after a close on
the assumption that it follows. The one exception is the flagged tick above: a goal the operator
flagged is kept when its declaring topics are a definite `not-owner` **and** none of them records
a live session — the tick's own `open-manager` branch fires only when the subject has no live
manager, so a live topic that merely stopped its round is not a resumer.

### Three invariants

- **Restart is not open.** A restart resumes a session that already has a subject and a
  recorded id, so it decides nothing new and runs automatically; an open creates supervision
  and is the operator's act. The rulings are § The three tiers, rules 1 and 4.
- **The flag gates an open, never a restart.** An unflagged goal whose manager died is still
  resumed (rule 2).
- **The per-topic N start cap is retired with the goal tier.** There is no goal-manager session to start, so `scripts/topic-spawn.py`'s `goal-manager at running >= N` refusal no longer applies; the topic manager drives each declared goal directly, and no keep-alive verdict reads N.

## Command → tier

Each command maps to exactly one tier. "Never run from" states the same boundary from the
other side.

| Command | Tier | Never run from |
|---|---|---|
| `/supervisor:fleet-loop` | fleet manager | worker; topic manager (use `topic-loop`) |
| `/supervisor:fleet-status` | fleet manager | worker |
| `/supervisor:fleet-drive` | fleet manager | worker; topic manager (use `topic-drive`) |
| `/supervisor:fleet-workers` | fleet manager | worker |
| `/supervisor:fleet-verify` | fleet manager | worker; topic manager (use `topic-verify`) |
| `/supervisor:fleet-triage` | **Fleet Pusher** — a dedicated session the operator opens and names `Fleet Pusher`, doing nothing but this. One pass over every live session: classify, close, drive (including the guarded pane wake), card. Ends green only on all five of zero stuck, zero uncarded, zero finished-but-open, zero no-loop, and every live session accounted for | **Fleet Manager** (it must stay shallow, and a fleet-wide pass with its pane reads fills the manager's context until it can no longer do its own job); worker; topic manager (use `topic-drive` — it serves one subject) |
| `/supervisor:topic-loop` | topic manager | worker — arming it collapses the two roles silently |
| `/supervisor:topic-status` | topic manager | worker |
| `/supervisor:topic-drive` | topic manager | worker |
| `/supervisor:topic-verify` | topic manager | worker |
| `/supervisor:topic-spawn` | topic manager — keeps a topic's declared goals driven | worker — this verb belongs to the topic tier |
| `/supervisor:goal-loop` | topic manager — goal scope | worker — arming it collapses the two roles silently |
| `/supervisor:goal-status` | topic manager — goal scope | worker |
| `/supervisor:goal-drive` | topic manager — goal scope | worker |
| `/supervisor:goal-verify` | topic manager — goal scope | worker |
| `/supervisor:goal-spawn` | topic manager — goal scope, spawns a worker | worker — workers never start sessions |
| `/supervisor:goal-answer` | topic manager — goal scope | worker — the topic manager is a tab session and answers its own prompts |
| `/supervisor:goal-drain` | topic manager — goal scope | worker — it serves headless workers only |
| `/supervisor:stop` | topic manager — bare, stands down *this* session's loop; by name, stands down a subject this session owns | worker — bare only; the by-name form stands down the worker's **own** task and refuses any subject it does not own |
| `/supervisor:reset` | topic manager — re-discovers *this* manager's state | worker |
| `/supervisor:fleet-reset` | **the operator** — destructive, confirm-gated; stops every session and clears the fleet's five remembered-state stores, with the task-link strip behind a separate opt-in, and gated on the session reading `ABSENT` | every manager and worker — a fleet-wide stop is the operator's act, never a session's; it also must not be auto-invoked |
| `/supervisor:open` | fleet manager, topic manager — resolves a name, then jumps, resumes or spawns | worker — workers never open sessions |
| `/supervisor:open-a2a` | fleet manager, topic manager — resolves a name, then creates the worker on the a2a-worker-controller (the backend chosen by the row's `target:`) | worker — workers never open sessions |
| `/supervisor:worker-drive` | worker — drives its own anchored task | any manager — a manager moves workers with `goal-drive`, never by driving a task itself |
| `/supervisor:restart` | fleet manager, topic manager — restarts ONE worker or ONE manager, in its own role colour, and tells a manager to re-arm its loop | worker — resuming a session is dispatch, and the script refuses `busy-target` for a session restarting itself |
| `/supervisor:worker-restart` | fleet manager, topic manager — **DEPRECATED alias for `/supervisor:restart`, and the one exception to the prefix rule: `worker-` names the TARGET, not the invoker** | worker — same as `/supervisor:restart` |
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
  it can still hand over the old form after the plugin changed. ⚠️ **A manager needs *another*
  session to pick up the version, and the route it offers is `/supervisor:restart <sid>` —
  `/reload-plugins` only where no addressable session exists.** The probe, the route rule and
  the two checks that do *not* discriminate have one home: `docs/fleet-surface.md` § A plugin
  install does not reach a running session. Read it there; it is not restated here.

The row format each status table uses for these links is `docs/fleet-surface.md` § Sweep
output — the fleet table.

## The manager boundary

- **A manager is never started in a worker session.** ⚠️ **Starting a *topic* manager is the
  operator's decision — the fleet manager performs the open. There is no goal-manager session
  to start: the tier is merged into the topic tier (2026-11-24)** —
  `docs/fleet-surface.md` § Session roles is the canonical statement.
- **A manager does not execute a worker's task.** It spawns, drives, answers and reaps
  workers; the task's work happens in the worker's own session.
- **The goal scope (the topic manager's goal round) starts or resumes a worker on a vault task with `/supervisor:open --task`** —
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
- **The goal round files the task an undriven SC/DoD box needs, and routes it by its door** —
  goal-round row 4d (`commands/steps/goal-plan-undriven.md`, decision in
  `scripts/undriven-plan.py`). At most one file per goal per tick. A two-way row it filed
  is approved `--by manager` and opens through the ordinary `To open` path; a one-way row
  waits on the round's approve card; a wide-reach row gets a `### Decision:` block and a
  talk-through. Every other `phase: todo` row stays the operator's —
  `docs/fleet-surface.md` § Approval provenance owns the boundary.
- **The topic manager drives each declared goal directly — there is no goal-manager session to start.** A goal declared by a topic is driven by that topic manager; a goal in no topic falls to the fleet manager. The old `topic-spawn` goal-start path (`/supervisor:open --goal`, the five refusals `goal-not-in-scope` / `goal-not-startable` / `goal-terminal` / `goal-unflagged` / `manager-cap`, and the per-topic N ranking) is retired with the goal tier, as is the flag-gates-the-open rule it enforced — there is no open to gate.
