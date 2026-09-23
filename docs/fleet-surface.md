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

**A manager is never started in a worker session.** A worker session carries a *task*; a manager session carries a *topic or goal* and a loop. Arming one inside the other collapses the two roles, and the collapse is silent: the session keeps its task name and its task anchor while its turns now sweep a topic's whole tracked set, and the operator sees a worker whose tab is quietly doing someone else's job. (Operator rule, 2026-09-21 — stated when a worker session proposed to exercise `/supervisor:manager-loop report-only` as its own end-to-end check.)

The direction that **is** allowed runs the other way: a manager starts workers (§ Spawn a worker), and a worker reaches its manager over `SendMessage`. **Starting a manager is a human act** — the operator invokes `/supervisor:manager-loop <subject>` in a session created for that purpose.

**Dispatch authority is the manager's.** Only a manager opens sessions — the manager-loop for its topic or goal, the fleet-loop fleet-wide — each under its own spawn cap (§ Spawn a worker). The worker-side rule lives once, in the global rule `worker-does-not-open-sessions` at `~/.claude/claude-md-rules/worker-does-not-open-sessions.md`; that file carries the prohibition, and it is **not restated here**. A worker that finds work outside its anchored task routes it to its manager, per the rule above.

⚠️ **`report-only` does not make it safe — it suppresses the arming and nothing else.** The Guardrails still run, so a single report-only sweep may spawn up to 2 sessions on ready-to-start work, auto-resume a dead mid-flight worker, auto-compact a worker over 70%, and reconcile the topic page. **"One sweep" is a cadence limit, not a blast-radius limit**, and reading it as a read-only mode is the mistake this note exists to prevent (made, and caught, on 2026-09-21).

⚠️ **A worker session therefore has no end-to-end check of a manager command.** Exercising one belongs to a manager session's own runtime. A change whose verification is "run the manager and watch it behave" is verified by deployment (the installed copy carries the change) plus a lockstep grep across the copies — never by arming a loop from wherever the change was authored.

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

- **The model-free gate loop.** Armed outside the session — verified live 2026-09-22: the running loop's parent is a detached supervisor at `ppid 1`, so it outlives the session that started it — it is what keeps `~/.claude/state/sweep-gate/<topic>.tick.txt` and `/manager-status` truthful. `stop` prints its pid and the tick file's mtime as evidence it survived, and never signals it. Killing it leaves the operator a frozen table and a `/manager-status` reporting a tree nobody is watching.
- **The session.** The cadence is session-scoped, but the asks ledger is keyed by session id — `~/.claude/state/open-items/<session-id>.json` — so closing the session and re-opening the topic mints a new id and an empty ledger, and every open entry vanishes silently, including the `asked-of-you` entries only the operator can resolve. Measured 2026-09-22: session `433c856d` held **10 open entries, 2 of them `asked-of-you`**, and the session that replaced it carries the same 10 re-added **by hand** — the workaround, not a mechanism.

**`stop` is not a close.** It writes no page, the topic's `status` is byte-identical before and after, and it never offers `/vault-cli:session-close`. **Restart is the same command that started the loop** — `/supervisor:manager-loop "<subject>"` — so there is no `start` verb and none is needed.

⚠️ **Probe the gate loop by the script argument's basename, never by a substring of the command line.** `pgrep -f sweep-gate` matches any process whose argv merely *mentions* the path, and a manager's spawn prompt quotes it — measured 2026-09-22, the substring probe returned **three** pids for one loop, two of them the worker sessions spawned from a prompt naming the script. **The count is transient; the mechanism is not** — re-run hours later those two workers had exited and it returned one, while a bystander process whose argv merely carried the string reproduced the spurious pid on demand. `scripts/stop-probe.py` matches the second argv token's basename against the two known script names, which also fails in the safe direction: a path containing a space mis-splits and the probe under-reports rather than inventing a loop.

## Spawn a worker

**Readiness precondition — author and score the task before any spawn, or the worker's own gate parks.** A task the manager hand-writes usually ships without `# Tasks` and `# Definition of Done`, so the worker's own `plan-task` gate stops and asks the operator to supply the decomposition — inside the worker's pane, as a multi-question wizard that cannot safely be relayed. Measured 2026-09-19: three hand-written task files produced **three 3-question wizards**, nine operator decisions, none of which needed the repo open. So, before spawning:

1. **Author the task through `/vault-cli:create-task`** — the wrapper that dispatches the `task-creator` agent, which emits exactly the sections `plan-task` needs: `# Success Criteria`, `# Definition of Done`, `# Tasks`.
2. **Score it with the `task-auditor` agent** (`vault-cli:task-auditor`) — **the bar is 9/10, and it is the same bar the worker's own gate applies.** A manager gate looser than the worker's gate is decorative: an 8/10 task clears the manager and still parks the worker's `plan-task`.
3. **Check the three sections exist** before the spawn — `grep -cE '^# (Success Criteria|Definition of Done|Tasks)' <task-file>` returns **3**. A task authored through this path does, by construction; a hand-written one usually does not.
4. **Keep the split — the gate is readiness, never planning.** *Authoring* — sections, subtask decomposition, DoD, naming and SC evidence shapes — needs no repo access and belongs to the manager. *Execution planning* — which file, which mechanism, what the system actually permits — needs ground truth a manager does not have and stays with the worker. Measured counter-example 2026-09-19: a manager told a worker to "narrow the rule" on *The git push Ask-Rule Fires on Feature Branches*, and the worker found the ask-list does literal-prefix matching only and **cannot express that distinction at all**. A manager-side planning pass would have produced the same wrong plan with no wizard left to catch it.

⚠️ **This block is the one authoritative home for the rule.** Every spawn site references it rather than restating it — the fleet command, the fleet runbook, and the manager-loop command all point here.

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
decided. Pass `interactive` only as a per-call override: `true` to watch one worker's screen
live, `false` to force one headless worker while the fleet runs in tabs.

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
wezterm cli spawn ${WINDOW_ID:+--window-id "$WINDOW_ID"} -- bash -lc 'unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; exec "<claude_script>" --resume <session_id> -n "<title>" "/color '"$CHIP"'"'
```

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

**2 — Drive, by cause of death:**

| The session died… | Then |
|---|---|
| **mid-work** | the resumed pane lands at an empty composer with nothing driving it — **deliver the work**: as the `prompt` argument on A, or typed after the colour on B |
| **holding a human gate** | **drive nothing.** The pane comes up holding its own unanswered gate; idling there is the *correct* state |

⚠️ **A resume onto an unanswered operator gate must never be driven.** The two cases are
indistinguishable from the session id alone — read the transcript tail before choosing. The
error is not symmetric: an undriven mid-work resume wastes a session, a driven gate-death
resume **answers a question on the operator's behalf**.

**3 — `cwd` is not inherited on a resume — pass it explicitly.**

`spawn_agent(resume="<id>", interactive=false, cwd="<dir>")`. The resumed session starts in the
`cwd` you pass, **not** the directory the original session ran in: the session id names a
conversation, not a working directory, and nothing carries the old one across. A resume that
omits `cwd` lands in the caller's directory, so a worker resumed to finish repo work silently
starts somewhere else and its first file operation goes to the wrong tree. Pass the original
task's directory; if you cannot determine it, read `cwd` from the session registry
(`~/.claude/sessions/<pid>.json`) rather than guessing.

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
exited, and `spawn_agent(resume=…)` cannot answer a question that is still parked. Check which
state the worker is in — `agent_status` or `pending_permissions` — before choosing.

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

**This section is the single source for the fleet table.** `/supervisor:fleet-loop` and
`/supervisor:fleet-status` both render it and neither carries its own spec.

```
14:30 ✓ Fleet — 42 sessions · 9 running · 23 needs-input · 10 idle · 0 problem · 1 residual · no change
  ┌────────────────────────────┬──────────────────┬────────────────────────────────────┬─────────────┬───────────┐
  │ Session                    │ Bucket           │ Vault task                         │ Project     │ Last      │
  ├────────────────────────────┼──────────────────┼────────────────────────────────────┼─────────────┼───────────┤
  │ Sentry Manager             │ ⌛ needs-input   │ Map Sentry Projects to the Repo …  │ personal    │ 14m ago   │
  │ Dark-Factory Refuses …     │ 🔄 running       │ Dark-Factory Refuses to Start …    │ personal    │ 2m ago    │
  │ Complete Kafka Restore     │ ⏸️ idle          │ Complete Kafka Restore             │ brogrammers │ 5h ago    │
  │ Wedge Probe                │ ⚠️ problem       │ Wedge Probe                        │ personal    │ 41m ago   │
  └────────────────────────────┴──────────────────┴────────────────────────────────────┴─────────────┴───────────┘
```

- **Columns and widths:** Session 26 · Bucket 16 · Vault task 34 · Project 11 · Last 9 —
  **112 rendered characters** (`sum(widths) + 3n + 1`) against a 119-column terminal. A box
  that wraps is worse than a truncated cell. The bucket column **replaced** the old `Status`
  column rather than joining it — a sixth column lands at 132 — and the raw `busy` / `shell` /
  `idle` counts still ride the marker line. **`Project` is the column to drop** if task titles
  need more room; cutting it buys the task column 10 characters.
- **Bucket** carries the four-way classification, one bucket per live session, in precedence
  order **problem → needs-input → running → idle** so the classification is **total**: every
  registry row lands in exactly one bucket. ⚠️ problem · ⌛ needs-input · 🔄 running · ⏸️ idle.
  Each bucket consults a **second signal** the registry status cannot supply, and
  `fleet-board.py` is the single source for the rule — `problem` = inside one tool call ≥ 20m;
  `needs-input` = an open gate in the attention store; `running` = status `busy` or `shell`,
  the only two the status table calls conclusive; `idle` = everything else, carrying the
  transcript age. **Orphaned is not a Bucket cell** — it is an action line *below* the box,
  because it describes the absence of a session rather than a live one's state.
- **Build the rows with `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-board.py --json`, render them with the same root's `scripts/box-table.py`** —
  stdin `{"header": [...], "rows": [[...]], "widths": [...]}`, with the board's extra keys
  (`counts`, `residual`, `coverage_ok`) ignored by the renderer. **Never hand-draw the box.**
  ⚠️ The board **asserts its own coverage** and exits non-zero rather than printing a table
  that omits a session — one row per registry entry, plus every transcript-fresh session the
  registry carries present among the rows. A **residual** line reports transcript-fresh
  sessions the registry does *not* carry (a headless worker holds no registry entry at all).
  Never read a short table as a clean fleet, and never read an empty one as an empty fleet.
- **The marker line is timestamped and is always the first line of the tick's output:**
  `HH:MM ✓ Fleet — N sessions · <count by bucket> · <what changed or "no change">`. Silence
  is ambiguous — a quiet loop and a dead loop look identical from the outside.
  `/supervisor:fleet-status` is a one-shot snapshot and carries **no** marker, but indents
  its box the same two spaces under its own lead line.
- ⚠️ **Do not type a leading glyph.** The harness already bullets assistant output with `⏺`;
  a literal copy renders doubled.
- Below the box, only the non-empty action lines: the **blocked-by-you jump list**
  (`⌛ Blocked by you (N waiting …)` with one `jump:` target per row),
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
