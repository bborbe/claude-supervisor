# Fleet surface — spawn shape and table render spec

The canonical, self-contained home for the two operational specs the fleet commands
(`/supervisor:manager-loop`, `/supervisor:fleet-loop`, `/supervisor:fleet-status`,
`/supervisor:manager-status`) depend on.

These specs previously lived in an Obsidian vault runbook and were referenced by absolute
path, because Obsidian wikilinks do not resolve across vaults. A plugin cannot depend on a
particular person's vault to describe its own mechanics, so the operational content lives
here and the vault runbooks keep only the per-vault operating procedure.

Change the shape here, then update every call site, then re-run the grep that proves no
copy diverged.

## How commands are addressed

A plugin's commands are namespaced by its install id. This one installs as
`supervisor@claude-supervisor` — `.claude-plugin/plugin.json` names the plugin `supervisor`,
`.claude-plugin/marketplace.json` names the marketplace `claude-supervisor` — so every command
it ships is addressed `/supervisor:<name>`: `/supervisor:jump`, never `/jump`.

**This section is the single statement of the rule.** The bare form resolves to nothing —
Claude Code answers `Unknown command` — and the managers hand `/supervisor:jump` to a blocked
worker as their last resort, so a bare form fails exactly when it is needed (measured
2026-09-19: the operator followed a printed handover, typed the bare form with a pane id, and
got `Unknown command`). The namespace follows the install id, so renaming the plugin or the
marketplace changes the prefix with it — a one-place edit here, not a sweep of the commands.

## Session roles — who may start what

**A manager is never started in a worker session.** A worker session carries a *task*; a manager session carries a *topic or goal* and a loop. Arming one inside the other collapses the two roles, and the collapse is silent: the session keeps its task name and its task anchor while its turns now sweep a topic's whole tracked set, and the operator sees a worker whose tab is quietly doing someone else's job. (Operator rule, 2026-09-21 — stated when a worker session proposed to exercise a one-sweep `/supervisor:manager-loop` run as its own end-to-end check.)

The direction that **is** allowed runs the other way: a manager starts workers (§ Spawn a worker), and a worker reaches its manager over `SendMessage`. **Starting a manager is a human act** — the operator invokes `/supervisor:manager-loop <subject>` in a session created for that purpose.

**Dispatch authority is the manager's.** Only a manager opens sessions — the manager-loop for its topic or goal, the fleet-loop fleet-wide — each under its own spawn cap (§ Spawn a worker). The worker-side rule lives once, in the global rule `worker-does-not-open-sessions` at `~/.claude/claude-md-rules/worker-does-not-open-sessions.md`; that file carries the prohibition, and it is **not restated here**. A worker that finds work outside its anchored task routes it to its manager, per the rule above.

⚠️ **There is no one-sweep mode — use `/manager-status` to look and `/manager-drive` to act once.** `manager-loop` once carried a flag that suppressed the arming and nothing else: the Guardrails still ran, so a single sweep could spawn up to 2 sessions, auto-resume a dead worker, auto-compact one over 70% and reconcile the topic page. **"One sweep" is a cadence limit, not a blast-radius limit.** The flag was removed because its name promised a read-only look it never gave (read that way, and caught, on 2026-09-21); `manager-status` + `manager-drive` split the look from the act honestly.

⚠️ **The fleet side now has the same split — use `/fleet-status` to look and `/fleet-drive` to act once.** `fleet-loop` once required a `loop` argument to repeat; a bare invocation ran one round and stopped, and a round dispatched nothing at all — no drive leg, so parked-but-unblocked sessions sat until someone ran `/fleet-drive` by hand. That is the manager-layer shape missing one leg, not a smaller blast radius: the round still swept, classified and wrote the snapshot, so "one round" was a cadence limit there too. The argument was removed on the same reading as the manager flag — a bare `/supervisor:fleet-loop` repeats by default and dispatches the drive leg every round, and `/fleet-drive` is the by-hand single pass. **The two layers are mirrors: `manager-loop` : `manager-drive` :: `fleet-loop` : `fleet-drive`.**

⚠️ **A worker session therefore has no end-to-end check of a manager command.** Exercising one belongs to a manager session's own runtime. A change whose verification is "run the manager and watch it behave" is verified by deployment (the installed copy carries the change) plus a lockstep grep across the copies — never by arming a loop from wherever the change was authored. **Handing that run to the manager is not a wait:** an idle manager drains `SendMessage` only on its next turn, and nothing gives it one. Send with `notify_when_idle: true` and close the worker's turn 🔵 READY with `you run: <command>` for the operator to type in the manager's tab — never 🟡 WAITING on the reply. (Measured 2026-09-23: a `/supervisor:reset` handoff sat unread ~4.5 h.)

## Session end — the disarm contract

`/supervisor:manager-loop` arms a loop. **`/supervisor:stop` stands it down**, and this section is the contract that command points at: what it must disarm, which harness surface reaches each driver and which it cannot, and what must survive.

**The four drivers, and what reaches each.** They do not share a kill path, and the reachable set is smaller than the list.

| Driver | Wakes the model? | Reached by | Reachable from inside the session? |
|---|---|---|---|
| a `CronCreate` job | yes | `CronDelete <id>` | **yes** — `CronList` enumerates this session's own jobs |
| a `ScheduleWakeup` loop | yes | `ScheduleWakeup {stop:true}` | **yes** |
| a `Monitor` / background task | yes — each emitted line is a turn | `TaskStop <task_id>` | **only while the id is still in the conversation.** The harness exposes no enumeration of in-session background tasks, so the command cannot discover one it cannot name. A `Monitor` is also **bounded** (≤30 min), so it is not a durable cadence driver whatever the tick uses it for |
| the detached gate loop | **no** — 0 model tokens, ~330 ms a tick | `kill <pid>` | reachable, and **deliberately not touched** |

**The three model-waking drivers do not share a read surface, and that is what decides what `stop` may claim.** Only a `CronCreate` job can be **read** — `CronList` enumerates this session's own jobs, so the command knows both before and after whether one existed, and may report `disarmed` or `not armed`. A `ScheduleWakeup` loop and a `Monitor` can be **stopped but not enumerated**, so neither can ever be reported as disarmed. A driver the command cannot read is not a driver it may report as clean, and the command's report carries a third form for exactly this case. ⚠️ **But the two are not the same unconfirmed case, and the third form must say which one it is.** A `ScheduleWakeup` loop is reachable without being readable — a stop is sent and the harness returns a receipt, which is still not a read surface, so the report says *stop sent*. A `Monitor` may be **neither**: with no task id left in the conversation there is nothing to name, so no stop is sent at all and the honest line is *no id to stop*. Reporting the second as the first claims an act that never happened — the same over-claim as a `✓` the harness could not support, one form down. Measured 2026-09-22, on the first live run of the command: the model reached for `~` for the `Monitor` line, found "stop sent" false, and printed a form the template did not yet carry rather than assert it.

**Interrupting a turn disarms nothing.** Every driver above survives it, so the next firing arrives on schedule. That is why the verb exists rather than an interrupt.

**What must survive `stop`:**

- **The model-free gate loop.** Hosted by launchd (`com.bborbe.sweep-gate-notify`, a 900s `StartInterval` job) since 2026-09-22, when the detached `sweep-gate-ledger-loop.sh` host was retired. It writes **per-vault** state under `~/.claude/state/sweep-gate-loop/<vault>/` — the launchd host exports `SWEEP_GATE_STATE_DIR` to place it there — and it is what keeps `<vault>/<subject>.tick.txt` truthful. ⚠️ **It is not the store `/manager-status` replays from.** That command's no-change path reads its own plugin-owned snapshot at `~/.claude/state/manager-predispatch/<subject>.json`, written by `scripts/manager-predispatch.py` when the command itself runs. The two carry the **same digest inputs** — the loop's per-subject snapshot was compared field-for-field against the gate's on 2026-09-26 and they agree — but they are different files serving different surfaces, and the loop's is loop-only: it exists for subjects with an armed loop, so it cannot back a command that runs against arbitrary subjects. Killing this job freezes the tick file; it does not touch the gate's snapshot. **The per-subject snapshot is `<vault>/<subject>.snapshot.json`, its writer is `~/.claude/scripts/sweep-gate-classify.py` (`save_snapshot()`, which writes the snapshot and appends the history record), and its schema has its single home here:** `recorded_at` (the sweep's timestamp — the value that identifies *which* sweep a row set came from), `tasks` keyed by task name with `{status, phase, met, stuck, liveness, progress_hash, session}`, plus `events` and `fail_open`. Beside it sits an append-only `<subject>.snapshot.history.jsonl` — one record per sweep, never overwritten, which is what makes the prior sweep's state readable at all. ⚠️ **The schema carries no bucket concept** — buckets are the *caller's* classification (`agents/manager-sweep-reader.md`), computed from the vault runbook, so a per-bucket name set is never obtainable from this file. ⚠️ **This block is the schema's one home; cite it, never restate it** — the same rule § Spawn a worker item 5 carries for the cap. Note what the schema does **not** carry: there is no task-file mtime, so a freshness reading cannot be taken from it (`agents/manager-drive.md` step 2 Check 1 takes it from the caller's report instead). `stop` prints the heartbeat's age and the tick file's mtime as evidence it survived, and never signals it. Killing it leaves the operator a frozen table and a `/manager-status` reporting a tree nobody is watching. ⚠️ **The flat `~/.claude/state/sweep-gate/<topic>.*` tree is NOT retired — the earlier claim here was wrong, and the error mattered.** This line previously read *"nothing writes it any more … (verified 2026-09-23: no detached loop running, every topic's state there stale)"*. Re-measured **2026-09-26**: the tree is **live** — `attention-routing.tick.txt` written 17:43, `attention-routing.{arms,cadence,json}` at 17:43–17:44. What the original line got right is the *kind* of state: the flat tree carries **loop-cadence records** (`.cadence`, `.arms`, `.stopped` — see `docs/restart-worker.md:83`), not gate state, and the gate loop does not write it. All three gate scripts resolve the per-vault tree instead (`sweep-gate-notify-tick.sh:52` and `sweep-gate-notify-stale.sh:13` both `BASE="$HOME/.claude/state/sweep-gate-loop"`; `sweep-gate-adhoc.py:49` likewise), which is why a reader resolving **gate** state in the flat tree finds nothing. ⚠️ **The writer of the flat tree's cadence records is not yet identified in this repo** — treat "who writes `~/.claude/state/sweep-gate/<topic>.cadence`" as open rather than assuming the 900s job, and do not let a tick-file *absence* test in the flat tree stand in for a gate-state check. A `*.tick.txt` **does** exist there for live topics, so the file is real, not a fossil.
- **The session.** The cadence is session-scoped, but the asks ledger is keyed by session id — `~/.claude/state/open-items/<session-id>.json` — so closing the session and re-opening the topic mints a new id and an empty ledger, and every open entry vanishes silently, including the `asked-of-you` entries only the operator can resolve. Measured 2026-09-22: session `433c856d` held **10 open entries, 2 of them `asked-of-you`**, and the session that replaced it carries the same 10 re-added **by hand** — the workaround, not a mechanism.

**`stop` is not a close.** It writes no page (its one state write is the `<slug>.stopped` marker, so the liveness watcher reads the silence as a stop rather than a lapse), the topic's `status` is byte-identical before and after, and it never offers `/vault-cli:session-close`. **Restart is the same command that started the loop** — `/supervisor:manager-loop "<subject>"` — so there is no `start` verb and none is needed.

⚠️ **Probe the gate loop by the script argument's basename, never by a substring of the command line.** `pgrep -f sweep-gate` matches any process whose argv merely *mentions* the path, and a manager's spawn prompt quotes it — measured 2026-09-22, the substring probe returned **three** pids for one loop, two of them the worker sessions spawned from a prompt naming the script. **The count is transient; the mechanism is not** — re-run hours later those two workers had exited and it returned one, while a bystander process whose argv merely carried the string reproduced the spurious pid on demand. `scripts/stop-probe.py` matches the second argv token's basename against the two known script names, which also fails in the safe direction: a path containing a space mis-splits and the probe under-reports rather than inventing a loop.

## A plugin install does not reach a running session — the probe and the lever

**A plugin command body is read from disk when the session starts, not at each invocation**, so
installing an updated plugin does not change what a running session executes. It keeps serving
the body it loaded, indefinitely, with nothing announcing the change. The failure is silent in
both directions: a session can hand the operator a stale procedure for hours while reporting
success, and the lever everyone reaches for by default — a full restart — is heavier than the
job needs.

**The probe: a running session can read which version it loaded.** The harness writes
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/.in_use/<pid>` at session start — a
JSON file `{"pid":<n>,"procStart":"<date>"}` naming the version directory that session holds.
`CLAUDE_PID` is exported into the session's own Bash, so the directory holding your own pid is
the version you are serving:

```bash
ls -d ~/.claude/plugins/cache/claude-supervisor/supervisor/*/.in_use/"$CLAUDE_PID"
```

Compare that against `installPath` in `~/.claude/plugins/installed_plugins.json`. A version
directory that is not the installed one means this session predates the install. Measured
2026-09-27: session pid `83726`, `procStart` `16:13:06Z`, was recorded in
`0.61.1/.in_use/83726` while the install read `0.62.1` — the plugin moved four versions inside
that one session, and nothing in the session said so.

**The lever is `/reload-plugins`; a restart is not required.** It reloads plugins, skills
(including every `commands/` entry), agents, hooks, plugin MCP servers and plugin LSP servers,
and it re-reads plugins from disk, so it also switches to a new version's cache path. Restart
only if, after `/reload-plugins`, the served body still cites the old version path. The vault's
*Claude Code Reload vs Restart Guide* owns the per-artifact-class lever table; this section
names the lever and points at that table rather than copying it.

⚠️ **Two probes that do not discriminate — both measured 2026-09-27.** The **skill listing**
cannot tell a stale session from a current one: command frontmatter descriptions are routinely
unchanged between releases, and all 21 of this plugin's commands were byte-identical across
three consecutive versions (`diff -rq` over `commands/` and `docs/` → no differences). And the
**MCP server** is a different artifact class from `commands/*.md` — this plugin's runs from a
workspace checkout (`bun run --cwd …/claude-supervisor/server`), never from the version-pinned
cache, so probing it says nothing about the command body. A check that is green on both copies
is not a check.

**This section is the single statement of the rule.** `docs/session-tiers.md` and the vault's
*Claude Code Plugin Development Guide* point here; neither restates it.

## Spawn a worker

**Readiness precondition — author and score the task before any spawn, or the worker's own gate parks.** A task the manager hand-writes usually ships without `# Tasks` and `# Definition of Done`, so the worker's own `plan-task` gate stops and asks the operator to supply the decomposition — inside the worker's pane, as a multi-question wizard that cannot safely be relayed. Measured 2026-09-19: three hand-written task files produced **three 3-question wizards**, nine operator decisions, none of which needed the repo open. So, before spawning:

1. **Author the task through `/vault-cli:create-task`** — the wrapper that dispatches the `task-creator` agent, which emits exactly the sections `plan-task` needs: `# Success Criteria`, `# Definition of Done`, `# Tasks`.
2. **Score it with the `task-auditor` agent** (`vault-cli:task-auditor`) — **the bar is 9/10, and it is the same bar the worker's own gate applies.** A manager gate looser than the worker's gate is decorative: an 8/10 task clears the manager and still parks the worker's `plan-task`.
3. **Check the three sections exist** before the spawn — `grep -cE '^# (Success Criteria|Definition of Done|Tasks)' <task-file>` returns **3**. A task authored through this path does, by construction; a hand-written one usually does not.
4. **Keep the split — the gate is readiness, never planning.** *Authoring* — sections, subtask decomposition, DoD, naming and SC evidence shapes — needs no repo access and belongs to the manager. *Execution planning* — which file, which mechanism, what the system actually permits — needs ground truth a manager does not have and stays with the worker. Measured counter-example 2026-09-19: a manager told a worker to "narrow the rule" on *The git push Ask-Rule Fires on Feature Branches*, and the worker found the ask-list does literal-prefix matching only and **cannot express that distinction at all**. A manager-side planning pass would have produced the same wrong plan with no wizard left to catch it.
5. **Respect the spawn cap.** New-session spawns are capped at **2 per sweep** and **4 per rolling 30 min**, counted **per layer** — the manager-loop for its topic or goal, the fleet-loop fleet-wide (§ Dispatch authority above). **Auto-resumes are excluded**: they answer to the auto-resume gate's own 30-min crash-loop cap, and counting them here would leave a sweep that revived two dead workers unable to start any new one. At the cap, open nothing further and print `⏸️ SPAWN CAP: <n> ready, <m> over cap`; the remainder is picked up next sweep.
6. **Decide the mode before you spawn — `headless` only on positive evidence, and `interactive` whenever it is unclear.** The task's own `mode:` frontmatter is the storage: a spawn site reads it and passes the argument **whenever the field is present** — `interactive=false` for `headless`, `interactive=true` for `interactive` — and omits it only when the field is absent. ⚠️ **A non-task-anchored spawn — a bare brief with no task file — has no field to read, so it classifies the brief itself and passes the argument explicitly; it never omits.** Omission is reserved for a *task* whose `mode:` is absent, and that case is resolved upstream by Step 0.6 writing the field before the spawn. A bare brief has no such step, so omitting would mean the spawn carried **no decision at all** and reported `mode_source=config` — indistinguishable in the ledger from a site that never decided. Absent, classify the task's body now, write the field back with `vault-cli task set "<task>" mode <interactive|headless>`, and spawn accordingly — never re-derive over a value already on disk. **The classifying question is neither "is this dangerous" nor "can this run unattended".** The first is what a keyword grep measures (`kubectl`, `ssh`, `make apply`, `gh pr merge` say a task is *worth watching*, not that it *cannot run unattended*), and the second nothing in a task body reliably answers — `Cleanup Email Inbox` names no infrastructure command and still needs a human. Ask instead: *does finishing this task raise questions mid-flight that only a human can settle?* Yes, or the body gives no basis to decide → **`interactive`**. `role: human` and `role: manager` both force `interactive` — a person needs a screen, a manager is a tab you jump to — while `role: agent` leaves the question genuinely open.

**What positive evidence looks like** — the task runs a fixed procedure end to end, decides nothing a human would want to weigh in on, and carries no approval step, no triage judgement and no "ask if unsure" in its own body. **Expect this to be rare.** Most recurring work triages, files, or decides something: an inbox sweep judges what is actionable, an alert check judges what to silence. Those are `interactive`, and a classifier that finds many headless rows is mis-reading the bodies, not finding an optimisation.

**The asymmetry is deliberate, and it is measured.** A wrongly-interactive task costs one idle tab the operator closes; a wrongly-headless one burns a whole session on gates nobody can answer (measured 2026-09-20: two workers stranded and four gates expired across three workers in ~90 minutes). The operator's own framing, 2026-09-21: *a session that needs interaction and is headless is hard to manage.* So `interactive` is a **floor, not a tie-break** — *unclear* is not a category that resolves to headless, and neither is *probably fine*.

⚠️ **A headless worker's two standing constraints, and both are the spawner's to carry, not the worker's.** (a) **Its gates park with the session that spawned it** — no other session can answer them, so the spawner must serve them via `await_permission` / `answer_permission`. A spawner that cannot answer a parked prompt must not open a headless worker, because the prompt will outlive it. (b) **Its turn ends at READY, and a turn end is not completion** — continue it with `spawn_agent(resume="<session-id>", interactive=false, cwd="<explicit>")`; a headless worker that has exited is neither finished nor restarted. Both constraints are why the `interactive` fallback is aggressive rather than polite.

⚠️ **This block is the one authoritative home for the rule.** Every spawn site references it rather than restating it — the fleet command, the fleet runbook, and the manager-loop command all point here. It owns **three constants**: the **readiness bar (9/10)**, the **spawn cap** (item 5 above), and the **mode decision** (item 6 above — the classifier, the `mode:` field, and both headless constraints). Each appears once, there, and is referenced everywhere else. ⚠️ **A restated copy of any of them is not a harmless comment — it is a second counter.** The cap was restated in `commands/manager-loop.md`, `commands/manager-verify.md` and the manager runbook's Guardrail 2 until 2026-09-24: four homes for one number, which is how a single cap becomes two caps the day one home is edited and the others are not, with no error and no diff to catch it. The mode rule carried the mirror-image defect until the same day: it lived in `commands/open.md` § Step 0.6 and was consumed only there, so every other spawn site silently fell through to the fleet config — measured 2026-09-23, **63 new-worker spawns in one day and 0 of them headless**, 57 sourced from `config` rather than from any decision.

**A — `spawn_agent` (preferred).** The prompt is a spawn *argument*, so the task never goes
over keystrokes:

```
mcp__supervisor__spawn_agent(prompt="...", cwd="/path", label="alpha", role="agent")
```

⚠️ **`interactive` is resolved from the fleet's config — omit it.** With no argument the
server resolves, highest first: `SUPERVISOR_SPAWN_MODE` → `spawn.mode` in
`~/.config/claude-supervisor/config.json` → its built-in `interactive`. **Do not pass it on
a fresh spawn.** Passing it is what made the config unreachable before 2026-09-18, when each
manager command file hardcoded `interactive=false`, so changing the fleet's mode meant
editing N instruction files and course-correcting every manager already running (measured
that morning: 3 workers killed, 7 more found under two other managers). The file is read
once at server start — restart the MCP server after editing it. The spawn response reports
`mode_source` (`argument`/`env`/`config`/`default`) when you need to know which source
decided. Pass `interactive` as a per-call override in one case only: `true` to watch one
worker's screen live.

⚠️ **The `mode:` case is a carve-out, not an override, and it runs in both directions.** Pass the argument whenever the task's `mode:` field is present — `interactive=false` for `headless`, `interactive=true` for `interactive` (item 6 above). An absent `mode:` still means *omit the argument entirely* — never *pass `interactive`* — so the config keeps deciding for every task that has not opted out, and the one-place-to-change property survives. **If this file writes the field, this file must honour it in both directions.** Passing it only for `headless` — which is what this file did until 2026-09-24 — leaves `mode: interactive` inert: a task that explicitly declared itself interactive flips to headless the moment `spawn.mode` moves, and the omitted argument reports `mode_source=config`, so a **wired spawn becomes indistinguishable from an unwired one in the ledger**. **A `mode:` honoured in one direction is the deleted mode column wearing a different hat.**

⚠️ **A headless worker is told its own mode — it never has to infer it.** The resolved mode
and its source are passed into the worker's environment as `SUPERVISOR_WORKER_MODE` and
`SUPERVISOR_WORKER_MODE_SOURCE`, so the worker reads them instead of reading the fleet
config. That matters because the config describes the **fleet, not this worker**: a per-call
`interactive: false` opens a worker headless while the file still reads `interactive`, and
before 2026-09-20 that file was the only signal a worker could reach — so every
headless-by-override worker mis-modelled itself, reported it was in an **interactive tab**,
and waited for a keystroke that could never be typed (measured 2026-09-20: two in one hour,
one ending `done`/`success` with its task file unedited). A headless worker is an in-process
SDK `query()` with no pid and no argv, so it cannot probe this for itself: the value is
handed over, never re-derived. ⚠️ The SDK's `env` option **replaces** the subprocess
environment rather than merging, so `process.env` is spread explicitly — without it the
worker loses `PATH`, `HOME` and `ANTHROPIC_BASE_URL`, the last of which stops it routing
through the router while looking like nothing at all.

⚠️ **`role` resolves BOTH the colour and the window — pass it, and prefer it over
`window_id`.** The server reads the map the WezTerm config publishes on its reconcile tick
(`~/.cache/wezterm-role-map.json`) and resolves `manager` → orange/Managers, `agent` →
pink/Agents, `human` → cyan/Direct. **Omit it for an agent**, which is the correct default
for every task that has not declared a role.

```
mcp__supervisor__spawn_agent(prompt="...", cwd="/path", label="alpha", role="manager")
```

**A role is a WORD, and that is the whole point.** A `window_id` has to cross the MCP tool
boundary, and `window_id: 0` did not survive that crossing reliably: measured 2026-09-20 it
reached the server 4 times in 6 and silently inherited the caller's window the other times —
both failures were a run's first spawn, and no reproducible trigger was found. A role cannot
be dropped that way, and the id is looked up in-process at the moment of spawn. The CLI
itself is not implicated: `wezterm cli spawn --window-id 0` from a shell landed in window 0
six times out of six.

The spawn response reports the resolved `role` and `window_id`, so routing is **observed**
rather than inferred from wherever the tab happened to land. An explicit `window_id` still
wins when passed — reach for it only for a window the role map does not describe.

The two failure modes are deliberately different. An **unusable map degrades**: absence is
normal, since the map publishes on a reconcile tick and a headless worker has no window at
all, so the spawn proceeds on the caller's explicit window and `SUPERVISOR_WORKER_COLOR`,
with a warning logged. An **unknown role is refused**, because it is a caller mistake and a
worker opened with the wrong colour is discovered only by noticing it.

`SUPERVISOR_WORKER_COLOR` remains an explicit operator override and wins over the resolved
chip; `off` means send no colour. There is deliberately **no built-in colour default** — the
colour is a role signal, so a hardcoded one is the defect it replaced.

**Fresh start — the worker creates its own session.** Spawn with the work command as the
`prompt` argument and **no pre-minted `session_id`**:

```
mcp__supervisor__spawn_agent(prompt='/vault-cli:work-on-task "<task>"', cwd="<dir>", label="<task>")
```

The worker runs its own planning turn, in its own pane. **Never mint the session first:**
`vault-cli task work-on "<task>" --mode headless` runs that turn inside the *caller's*
session — blocking it for minutes — and the `session_id` it returns is the value the very
next documented step consumes. Measured 2026-09-19: exit 124, an empty output file, and pids
still running after the caller had given up. With `interactive` omitted the config decides,
and on the fleet's current setting the worker lands in a real tab, so its progress is
visible.

**A fresh start needs no `session_id`; a resume cannot work without one.** A resume continues
an existing conversation, so it must name which one; a fresh start has no prior conversation
to name, and the worker mints its own. That asymmetry is why the two paths diverge below.

`policy` is headless-only, which is why a tab-producing call site never carries it.

The launcher is resolved from `vault-cli config` (`claude_script`), falling back to the
`personal` entry. Never invoke the bare `claude` binary — that routes around the router, the
MCP config and the model selection. Override with `SUPERVISOR_CLAUDE_CMD`.

**Precondition, and it fails silently:** `supervisor` must be in the launcher's MCP config
allowlist. Launchers that pass `--strict-mcp-config` exclude plugin-provided MCP servers —
the plugin reads enabled and `claude mcp list` reports healthy while the tools are absent
from every session. If `mcp__supervisor__*` is unavailable, use path B.

**B — raw `wezterm cli spawn` (fallback, and the path for resuming a live session).**

```bash
wezterm cli spawn ${WINDOW_ID:+--window-id "$WINDOW_ID"} --cwd "<cwd>" -- bash -lc 'unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; exec "<claude_script>" --resume <session_id> -n "<title>" "/color '"$CHIP"'"'
```

**`--cwd` is required, and a resume does not inherit it.** The session id names a
conversation, not a working directory, so `wezterm cli spawn` falls back to *wezterm's own*
working directory — `$HOME` for a server started from a home directory. Claude Code then stops
on *"Accessing workspace /Users/&lt;user&gt; — do you trust this folder?"* and registers **no pid
at all**, so a resume that worked perfectly reads as a no-op: a caller checking the registry
finds nothing and reads "did not take", a caller checking the exit code reads "took", and
neither reading names the trust dialog. The value comes from the session registry —
`~/.claude/sessions/<pid>.json` carries a `cwd` field (verified 2026-09-24: 26 of 26 records on
this host) — and is resolved **before** the spawn, never guessed or defaulted: a session whose
record carries no `cwd` is refused rather than resumed into the wrong tree. Path A states the
same requirement for `spawn_agent` at item 3 below; this is its path-B twin, and the two must
not drift into two different resolution rules.

**`$WINDOW_ID` and `$CHIP` are a role-resolved PAIR**, read from the published map before
spawning (`~/.cache/wezterm-role-map.json`): `manager` → Managers / orange, `agent` →
Agents / pink, `human` → Direct / cyan, with `role:` absent resolving to `agent`. Both are
role signals, so a hardcoded colour or a missing `--window-id` **is** the defect this shape
was corrected for on 2026-09-20 — the old `"/color pink"` painted a manager as a worker, and
the missing flag dropped the tab into whatever window the caller happened to occupy.
`${WINDOW_ID:+…}` keeps the flag off entirely when the map is unavailable, so the fallback
degrades to the old behaviour instead of passing an empty `--window-id`.

Two load-bearing details:

- **Keep the `unset`; its reason is NOT established.** The peer-registration explanation was
  refuted by experiment. It is harmless and cheap, so it stays — do not restate the socket
  story as its justification.
- **The colour goes in the spawn; the work command is typed afterwards.** Claude Code parses
  one submitted message as one command, and `/color` takes the *entire* trimmed argument, so
  seeding it inside a larger prompt yields `Invalid color`. Type the work command after:
  `wezterm cli send-text --pane-id <N> --no-paste $'<work command>\r'`. The trailing `\r` is
  load-bearing; a bare `\n` leaves the text unsubmitted.

⚠️ **Typing is legitimate only into a pane this recipe just spawned, while it is still idle.**
Answering another session's operator gate by `send-text` is forbidden. Typing into a pane
showing `Enter to select` turns any keystroke into a menu selection.

**Why A is preferred over B:** A removes the typed *task*; B only hardens the typist. **B is a
fallback, never the default** — it types into a pane and steals focus, and it has **no target at
all for a headless worker**. Reach for it only where A cannot do the job: a resume of a session
whose liveness cannot be determined, or a resume where headless is not permitted for the phase.
`send_agent_message` is not an alternative channel — it types and steals focus. Two limits on
A: it can only supervise sessions **it created**, and `spawn_agent({resume})` refuses a
session that is still live. Measured 2026-09-18: a call site that omitted the follow-up
`send-text` left a worker (BRO-21462, spawned 09:03) idle for 16 min while its task file
stayed untouched.

⚠️ **The `resume` refusal is a property of `mcp__supervisor__spawn_agent`, not of the
platform.** `resumeSupportError` (`server/tab.mjs`) rejects `resume` alongside
`interactive:true` because the *tool's* tab path launches the `cc-*` launcher without handing
it `--resume` — the flag would be dropped and you would get a fresh conversation while
believing you were continuing one. Path B hands `--resume` to the launcher directly, so it is
**interactive and resuming at once**. Read the refusal as *"this tool's tab path cannot carry
a resume"*, never as *"a resume cannot be interactive"*.

**Resume takes two independent decisions — first the path, then the drive.** Liveness picks
the path; *why the session died* picks whether anything drives the resumed pane. They do not
substitute for one another.

**1 — Path, by liveness:**

| Resuming… | Path | Why |
|---|---|---|
| a session **proven dead** | **A**, `interactive=false, resume="<id>"` — or **B** where headless is not permitted (below) | A is headless, so prompts park for the manager. On A, `resume` **requires** `interactive:false` — the pair is refused there, not silently downgraded |
| a session whose liveness **cannot be determined** | **B**, `wezterm cli spawn --resume` | A refuses an unverifiable resume by design |

⚠️ **Where headless is not permitted, the proven-dead row moves to B.** Some phases forbid
headless fleet-wide — the operator's standing constraint during manager-system development:
*"we are currently not running headless ever because we are in the development of the manager
system."* That constraint is **phase-scoped, not permanent**; do not delete path A on account
of it. Under it a proven-dead session resumes via **B**, exactly as an indeterminate one does.
The row's preference for A is a preference, not a requirement, and it is the only row affected.

⚠️ **A refused path is terminal — never fall back to the other one.** If the chosen path's spawn
is refused, the resume did not happen: report the refusal and stop. Falling back from B to A (or
A to B) is not a retry — it is a **second resume the gate never authorised**, and it lands a
worker in the caller's directory with no pane, its prompt parked on the manager, under a stamp
that now records a resume that never took. The refusal is the safe direction and it is the
design: path A already refuses `resume` + `interactive:true` rather than silently downgrading it,
and a refused resume costs one sweep where a wrong-path spawn corrupts a conversation.

**2 — Drive, by cause of death:**

| The session died… | Then |
|---|---|
| **mid-work** | the resumed pane lands at an empty composer with nothing driving it — **deliver the work**: as the `prompt` argument on A, or typed after the colour on B |
| **holding a human gate** | **drive nothing.** The pane comes up holding its own unanswered gate; idling there is the *correct* state |

⚠️ **A resume onto an unanswered operator gate must never be driven.** The two cases are
indistinguishable from the session id alone — read the transcript tail before choosing. The
error is not symmetric: an undriven mid-work resume wastes a session, a driven gate-death
resume **answers a question on the operator's behalf**.

**The discriminator is the transcript tail, and the pane then vetoes.** The tail decides the
branch: its last `👤 You:` line classifies, and a closer that is absent, `nothing`, or a parked
`later (on …)` means **mid-work** — nothing is waiting on a human. Only a real `pick` /
`approve:` / `you run:` / `review:` line is **gate-held**.

⚠️ **There is no third branch for the restart-request death, and none is needed.** The
2026-09-20 case — a session that died holding a gate that was its own restart request,
satisfied by the restart itself — lands in **mid-work** by construction: a harness
`restart Claude Code` gate is not a `👤 You:` line, so the tail reads as "nothing waiting". Do
not add a restart-detection heuristic to catch it; the classifier already agrees.

⚠️ **The pane text vetoes the tail, and the veto wins.** Read the resumed pane before typing
anything. A selection modal (`Enter to select`) or a non-empty composer means **type
nothing** — a keystroke into a modal selects a menu option, and a resume landing on one is
exactly the state the 2026-09-20 run misread as a live gate. The signals can disagree: the
tail is what the session *wrote*, the pane is what it is *holding*. Report the veto and hand
over the pane instead of answering for it.

Both halves are implemented in `scripts/restart-precheck.py` (its `classification:` line),
with the orchestration in `commands/worker-restart.md`. Do not restate the classifier
elsewhere — two copies of a transcript-tail read drift silently, because both keep returning a
string.

**3 — `cwd` is not inherited on a resume — pass it explicitly.**

`spawn_agent(resume="<id>", interactive=false, cwd="<dir>")`. The resumed session starts in the
`cwd` you pass, **not** the directory the original session ran in: the session id names a
conversation, not a working directory, and nothing carries the old one across. A resume that
omits `cwd` lands in the caller's directory, so a worker resumed to finish repo work silently
starts somewhere else and its first file operation goes to the wrong tree. Pass the original
task's directory; if you cannot determine it, read `cwd` from the session registry
(`~/.claude/sessions/<pid>.json`) rather than guessing.

**Confirming a headless spawn took — and what to read instead of `.status` — is owned by
§ A headless worker exits at turn end.** Do not restate it here: a restated copy is what let
four surfaces prescribe the same wrong field.

### The tab name is the join — and it is made unique before the process starts

A tab worker is a separate process the supervisor does not create, so it never learns that
worker's session id the way it does for a headless one. The registry carries `name`, and the
spawn sets the tab title, so **the name is the only join back from a tab to its session id**:
`findRegisteredByName("⚙ " + label)`, polled for up to 8s after the pane opens.

A name is not unique, and `find` returns the FIRST entry matching it. A label reused while an
earlier worker still answered to it therefore resolved the new spawn to **that** worker — and
the old session's id is what went into the new worker's ledger record. So the name is derived
*before* the process starts: `uniqueTabName` suffixes `(2)`, `(3)`, … until nothing holds it,
and the poll carries the holders snapshotted before the spawn as an `exclude` set, so a name
taken in the race between that snapshot and the poll cannot be matched either.

Two consequences worth knowing when reading a roster:

- **A suffixed tab title means the base name was taken.** A worker spawned under a label
  already in use comes up as `⚙ <label> (2)`, not `⚙ <label>` — the suffix is the guard
  working, not a naming mistake.
- **`sessionId: null` on a spawn response is the honest "never registered".** The poll could
  not resolve the name to a session that did not already exist, so no ledger record is
  written for it — `writeLedger` skips a falsy id and logs `WARNING: no ledger record for
  <id>`. A spawn **can** return `sessionId: null` while still opening a working tab.

⚠️ The guard is on the **name**, not on one cause of a collision: it makes the join
unambiguous whatever produced the earlier holder, including a cause not yet identified.

## A headless worker exits at turn end — that is not "finished"

Two exits look like completion and are not. A headless worker **ends its turn on a READY
panel**, and a parked question **times out (~11 min)**, after which it exits with the question
unanswered. Neither is an error, and neither leaves a trace beyond the roster row disappearing.
**A headless worker that has exited is a parked process, not a finished one** — read it as
"waiting for a continuation", never as "done".

The continuation is a new turn, not a restart:

```
mcp__supervisor__spawn_agent(
  prompt="<the answer, or the next instruction>",
  resume="<session-id>",
  interactive=false,
  cwd="<explicit — see above>",
)
```

The prompt is a **plain user turn** — no relay prefix, no provenance wrapper. That is what
separates continuing a worker from answering one:

| The worker is… | Whose answer | Channel | Call |
|---|---|---|---|
| **still parked** on a question | the **operator's** | the permission channel | `answer_permission(deny, message="Operator answer, via supervisor: <option>")` |
| **still parked** on a question | the **manager's own** | the permission channel | `answer_permission(deny, message="Manager answer, via supervisor: <option>")` |
| **already exited** (timeout, or turn end) | either | a fresh turn | `spawn_agent(resume=<id>, interactive=false, cwd=<explicit>)` |

They do not substitute for one another: `answer_permission` cannot reach a process that has
exited, and `spawn_agent(resume=…)` cannot answer a question that is still parked. Decide which of the two applies from `agent_status` **plus the transcript's mtime** — never
from `agent_status` alone, because `.status` is terminal only once the turn has ended
(`num_turns: 0` beside `subtype: "success"` is the tell that it has not).

### The two prefixes are not interchangeable

A parked gate can be answered in one of two voices, and the prefix is what declares which. Both
are honoured **on an `AskUserQuestion` only** — a denial on `Bash`, `Edit` or `Write` stays a
denial whatever prefix it carries.

- **`Operator answer, via supervisor:`** makes a **provenance claim**: the operator answered this
  question, in the manager session, in the current exchange. Use it only when that is literally
  true. A manager deciding on its own under this prefix is **forging an operator answer**, which
  is why the worker-side rule is written to reject it — measured 2026-09-20, a worker that
  received a manager's own inference under the operator form returned `ANSWER=NONE` and did not
  act, correctly.
- **`Manager answer, via supervisor:`** makes **no provenance claim**. It is the manager's own
  decision, on a question the manager owns — a choice between alternatives, not a question that
  was ever the operator's to answer.

⚠️ **Neither prefix releases an irreversible or production-touching action.** Those still need
the operator's own confirmation, obtained directly. A relay launders precisely the thing that
makes such a confirmation worth having — the operator's own wording naming the target and the
command — so a manager-prefixed denial that names one must leave the gate unanswered. This is
stated as a rule rather than a preference because it is the one branch where getting it wrong is
unrecoverable.

⚠️ **Why a parked gate needs the prefix channel at all.** `resume` refuses a session that is
still running, so the fresh-turn channel reaches only an **exited** worker. A parked gate is a
live one, and before 2026-09-21 a manager had no honest way to answer it in its own voice: the
only documented prefix asserted operator provenance. See
[[A Headless Worker's Gate Has No Channel a Manager May Honestly Use]].

⚠️ **A worker that exited on a question timeout still holds an unanswered question.** Its exit
does not answer it. Resuming with an empty prompt, or with "continue", leaves the question
unanswered and the worker parked again at the same gate; the continuation prompt must carry the
operator's actual answer, exactly as `answer_permission` would have.

## The session roster — `fleet-sessions.py`

`scripts/fleet-sessions.py` prints one row per session transcript on the machine, newest
first. It is an **input** to the sweep table above, not the sweep table itself: the manager
commands join it to `ListAgents` on the session id and re-render. The roster is always every
project, with no scope to pass.

```
LAST-ACTIVE  PROJECT      LIVE SESSION    WORKING ON                              SPAWN MODE  ATTRIBUTION
7h ago       Brogrammers  ●    dfa12e37   MDM Merge Modal Silently Omits Rela…    headless    MDM Merge Modal Silently Omits …
1s ago       Personal          46647e0e   Show Spawn Mode and Fleet Attribution…  interactive Show Spawn Mode and Fleet …
```

- **Columns, in this order:** `LAST-ACTIVE · PROJECT · LIVE · SESSION · WORKING ON ·
  SPAWN MODE · ATTRIBUTION`. The first five keep their positions and widths — the manager
  commands parse `SESSION` (the join key) and `WORKING ON` by name against them, so the two
  new columns are **appended**, never interleaved.
- **`SPAWN MODE` and `ATTRIBUTION` come from the spawn ledger**, joined on the session id.
  The ledger is the only store recording the spawn edge, so without it a headless worker and
  a human tab render identically.
- **Ledger directory:** `SUPERVISOR_LEDGER_DIR`, else `$XDG_STATE_HOME|~/.local/state` +
  `/claude-supervisor/sessions` — resolved from the writer's own override
  (`server/config.mjs`), never hardcoded. ⚠️ Deliberately **not** `SUPERVISOR_SESSIONS_DIR`,
  which names the live registry (`~/.claude/sessions`) — a different store with a different
  lifetime.
- **`unknown` is a value, not a blank.** A session with no ledger record was never
  *recorded*, which is a different claim from *not spawned*. Both new cells render the
  literal `unknown`; a blank would collapse the two and read as a value the ledger supplied.
- **`ATTRIBUTION` is `parent_session` + `label` when both are present**, falling back to
  whichever exists. ✅ Fixed 2026-09-22: `parent_session` now resolves to the nearest
  registered ancestor, so the column names the manager for records written since. ⚠️ The
  store's **existing** records keep `null` — measured that day at 398 of 398, and they are
  deliberately not backfilled, because a repaired edge would assert an attribution nobody
  observed at spawn time. So a label-only cell means *the manager was not recorded*, not
  *there was no manager* — and since the two vintages are indistinguishable by field alone,
  compare `spawned_at` against the release that added the ancestor walk before reading a
  `null` as a defect in the current build.
- **Two spawn counts, both labelled:** `spawned today (UTC): N` and `spawned today (local): M`.
  `spawned_at` is UTC-only, so the day boundary has two defensible readings and the view
  states which is which rather than silently picking one. They legitimately differ by the
  records straddling the boundary.
- ⚠️ **No count and no column rests on `status` / `ended_at`.** Those never close reliably
  for any mode (see the runbook's Step 7), so they are not a liveness source and are not
  read here. Every count is over `spawned_at`.
- **Retired flags stay no-ops.** `--all`, `--minutes N` and `--vault NAME` are accepted and
  ignored — the roster is always every project.

## Sweep output — the fleet table

**The frame is owned by the Fleet Manager Session runbook (per-vault) § Sweep output — the
fleet table, and this file deliberately does not restate it.** The columns, the widths, the
icons, the tree layout in the Session column, the marker line and the action lines are all
specified there; `/supervisor:fleet-loop`, `/supervisor:fleet-status` and this file point at
that one section rather than carrying a second copy. Two files each claiming to be the single
source is exactly the drift this pointer removes — measured 2026-09-24, both claimed it.

What this file still owns is the part that is about the plugin rather than the frame:

- **Build the rows with `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-board.py --json`, render them with the same root's `scripts/box-table.py`** —
  stdin `{"header": [...], "rows": [[...]], "widths": [...]}`, with the board's extra keys
  (`counts`, `sessions`, `residual`, `coverage_ok`) ignored by the renderer. **Never hand-draw the box.**
  ⚠️ The board **asserts its own coverage** and exits non-zero rather than printing a table
  that omits a session — one row per registry entry, plus every transcript-fresh session the
  registry carries present among the rows, plus every session row present **exactly once** in
  the drawn tree. A **residual** line reports transcript-fresh sessions the registry does
  *not* carry (a headless worker holds no registry entry at all). Never read a short table as
  a clean fleet, and never read an empty one as an empty fleet.
- **`--json` carries one entry per session under `sessions`** — `session_id`, `label`, `role`
  (`manager` / `worker` / `unmanaged`), `parent` (a session id, or `unmanaged`) and `bucket`.
  The tree is drawn from `parent`, so a consumer that needs the structure reads the document
  rather than parsing the glyphs.
- **The bucket rule's single source is `scripts/fleet-board.py` itself** — its module
  docstring defines the four buckets and the precedence that makes the classification total.
  Neither this file nor the runbook restates it.
- ⚠️ **Do not type a leading glyph.** The harness already bullets assistant output with `⏺`;
  a literal copy renders doubled.
- Below the box, only the non-empty action lines: the **blocked-by-you jump list**,
  `⚠️ ORPHANED: <task> — <why>`, and `⚠️ ACTION NEEDED: <the human decision>`. **Names lead**;
  the `[ref]` and pane id are secondary. Each `jump:` target is the one-line output of
  `scripts/jump-link.py <PANEID>` — a clickable `http://127.0.0.1:1337/jump?pane=<N>&t=…`
  link when the local fleet-jump server is configured, and the `/supervisor:jump <N>` command
  when it is not. **Never hand-write the URL**: the token lives in a 0600 file outside every
  repo, so a hand-built link is either broken or leaks it. ⚠️ **Hand over a pane id, never a
  tab id** — a tab that moves windows is renumbered, so a handed-over `--tab-id` goes dead
  (measured 2026-09-18: tabs 158/159/160 in window 0 became 163/164/165 in window 2, and
  `activate-tab --tab-id 159` failed outright while `activate-pane --pane-id 239` worked
  immediately). `/supervisor:jump` stays the executor behind the link; a raw
  `wezterm cli activate-tab` line is not a handover.

The table is the dashboard; TTS stays problem-only and voice-mode gated; the action lines
appear only when non-empty.

## Session stamps

A task's `claude_session_id` names the session working it, and a session answers "what am I
working on" by resolving that stamp. Reading a stamp as ownership — whose task it is, and when a
foreign one blocks you — is [[One Task Per Session Contract]]'s rule. This section is the
multiplicity rule: how many stamps one session may carry.

- **A session may carry many stamps, but at most one on an open task.** A session that finished
  task A and moved on to task B legitimately stamps both, and the stamp on A records who did the
  work, so it is **never rewritten to clear a count**. Two *open* tasks behind one stamp are an
  ambiguous answer to the resolution question, and that is the defect.
- **Task creation never stamps.** The session that works a task stamps it, not the session that
  wrote the file. Session-connect writes a stamp only when the field is empty, so a creator's
  stamp is never replaced by the real worker's — the worker never takes ownership.
- **A manager never stamps a worker's task.** Managers route work; they do not claim it.

Measured 2026-09-25 over `25 Tasks/`: 33 stamps spanned 2+ files. Zero were manager stamps; 30
were one ended session's finished tasks in turn (permitted history); 3 were a creating session's
stamp on an open task, one of them later worked by a different session whose id the task never
took. Counting "2+ files" called all 33 defects. The rule calls none of the 30 a defect.

`scripts/stamp-check.py <tasks_dir>` applies the rule. It exits 1 on a **violation** (one stamp on 2+
open tasks) and lists a **suspect** without failing: a stamp on one open task whose
`metrics_sessions` names other workers but not the stamp. `/supervisor:fleet-verify` check 3 reads
it. The field is read from the frontmatter block only, and an empty `claude_session_id:` is no
stamp.

## Referencing vault notes

Several fleet commands cite the operator's Obsidian runbooks. The rule is a **wikilink by
title** (`[[Manager Session]]`) — never a filesystem path, never an `obsidian://` URL.

Vaults number their folders differently — one vault's `65 Runbooks/` is another's
`70 Runbooks/`, one's `50 Knowledge Base/` is another's `50 Knowledge/` — so any path form is
wrong in some vault by construction, while the note's *filename* is stable. A title therefore
resolves wherever the note exists.

Where a note exists in only one vault, a wikilink would dangle everywhere else, so it is
written as plain prose marked *(operator's vault; not shipped with this plugin)*. Operational
content the commands genuinely need in order to run was moved into this file instead, rather
than left behind in a single vault's runbook — a plugin should not need a particular person's
vault to describe its own mechanics.
