# Fleet surface — spawn shape and table render spec

The canonical, self-contained home for the two operational specs the fleet commands
(`/supervisor:worker-manager`, `/supervisor:fleet-manager`, `/supervisor:fleet-status`,
`/supervisor:worker-status`) depend on.

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

**A manager is never started in a worker session.** A worker session carries a *task*; a manager session carries a *topic or goal* and a loop. Arming one inside the other collapses the two roles, and the collapse is silent: the session keeps its task name and its task anchor while its turns now sweep a topic's whole tracked set, and the operator sees a worker whose tab is quietly doing someone else's job. (Operator rule, 2026-09-21 — stated when a worker session proposed to exercise `/supervisor:worker-manager report-only` as its own end-to-end check.)

The direction that **is** allowed runs the other way: a manager starts workers (§ Spawn a worker), and a worker reaches its manager over `SendMessage`. **Starting a manager is a human act** — the operator invokes `/supervisor:worker-manager <subject>` in a session created for that purpose.

⚠️ **`report-only` does not make it safe — it suppresses the arming and nothing else.** The Guardrails still run, so a single report-only sweep may spawn up to 2 sessions on ready-to-start work, auto-resume a dead mid-flight worker, auto-compact a worker over 70%, and reconcile the topic page. **"One sweep" is a cadence limit, not a blast-radius limit**, and reading it as a read-only mode is the mistake this note exists to prevent (made, and caught, on 2026-09-21).

⚠️ **A worker session therefore has no end-to-end check of a manager command.** Exercising one belongs to a manager session's own runtime. A change whose verification is "run the manager and watch it behave" is verified by deployment (the installed copy carries the change) plus a lockstep grep across the copies — never by arming a loop from wherever the change was authored.

## Spawn a worker

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

## Sweep output — the fleet table

**This section is the single source for the fleet table.** `/supervisor:fleet-manager` and
`/supervisor:fleet-status` both render it and neither carries its own spec.

```
14:30 ✓ Fleet — 27 sessions · 6 busy · 3 shell · 4 waiting · 12 idle · 2 orphaned
  ┌────────────────────────────┬─────────────────────┬────────────────────────────────────┬─────────────┬───────────┐
  │ Session                    │ Status              │ Vault task                         │ Project     │ Last      │
  ├────────────────────────────┼─────────────────────┼────────────────────────────────────┼─────────────┼───────────┤
  │ Dark-Factory Refuses …     │ 🔄 progressing      │ Dark-Factory Refuses to Start …    │ personal    │ 2m ago    │
  │ Sentry Manager             │ ⏸️ parked           │ Map Sentry Projects to the Repo …  │ personal    │ 14m ago   │
  │ PR Review - 2026W38-tue    │ ⌛ waiting-on-human │ PR Review - 2026W38-tue            │ personal    │ 9m ago    │
  │ Complete Kafka Restore     │ ✅ done             │ Complete Kafka Restore             │ brogrammers │ 5h ago    │
  └────────────────────────────┴─────────────────────┴────────────────────────────────────┴─────────────┴───────────┘
```

- **Columns and widths:** Session 26 · Status 19 · Vault task 34 · Project 11 · Last 9 —
  **115 rendered characters**, the ceiling for a 119-column terminal. A box that wraps is
  worse than a truncated cell. **`Project` is the column to drop** if task titles need more
  room; cutting it buys the task column 10 characters.
- **Status** carries the bucket icon: 🔄 progressing · ⚠️ stalled · ⏸️ parked · ✅ done.
  **Orphaned is not a Status cell** — it is an action line *below* the box, because it
  describes the absence of a session rather than a live one's state.
- **Render with `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude}/scripts/box-table.py`** —
  stdin `{"header": [...], "rows": [[...]], "widths": [...]}`. **Never hand-draw the box.**
- **The marker line is timestamped and is always the first line of the tick's output:**
  `HH:MM ✓ Fleet — N sessions · <count by status> · <what changed or "no change">`. Silence
  is ambiguous — a quiet loop and a dead loop look identical from the outside.
  `/supervisor:fleet-status` is a one-shot snapshot and carries **no** marker, but indents
  its box the same two spaces under its own lead line.
- ⚠️ **Do not type a leading glyph.** The harness already bullets assistant output with `⏺`;
  a literal copy renders doubled.
- Below the box, only the non-empty action lines: the **blocked-by-you jump list**
  (`⌛ Blocked by you (N waiting …)` with `wezterm cli activate-tab` per row),
  `⚠️ ORPHANED: <task> — <why>`, and `⚠️ ACTION NEEDED: <the human decision>`. **Names lead**;
  the `[ref]` and tab id are secondary.

The table is the dashboard; TTS stays problem-only and voice-mode gated; the action lines
appear only when non-empty.

## Referencing vault notes

Several fleet commands cite the operator's Obsidian runbooks. The rule is a **wikilink by
title** (`[[Worker Manager Session]]`) — never a filesystem path, never an `obsidian://` URL.

Vaults number their folders differently — one vault's `65 Runbooks/` is another's
`70 Runbooks/`, one's `50 Knowledge Base/` is another's `50 Knowledge/` — so any path form is
wrong in some vault by construction, while the note's *filename* is stable. A title therefore
resolves wherever the note exists.

Where a note exists in only one vault, a wikilink would dangle everywhere else, so it is
written as plain prose marked *(operator's vault; not shipped with this plugin)*. Operational
content the commands genuinely need in order to run was moved into this file instead, rather
than left behind in a single vault's runbook — a plugin should not need a particular person's
vault to describe its own mechanics.
