# claude-supervisor — agent context

How to change this repo safely. The user-facing documentation is [README.md](README.md) — install, usage and configuration. This file is for agents working **in** the tree: the architecture map, the operational rules that are not a user's business, and the known gaps.

Audience split follows `coding/docs/readme-guide.md`: README answers *"can a new user get this running in 5 minutes?"*, this file answers *"how do I change this without breaking it?"*.

## Session tiers

Every command's prefix names the tier of the session that runs it: `fleet-*` for the fleet manager, `topic-*` for a topic manager, `goal-*` for a goal manager, `worker-*` for a worker; `jump`, `who-needs-me`, `read-guides`, `attention-next`, `close` and `ready` are cross-tier. The tiers form a **tree**, and the operator's hierarchy (2026-10-23) names what each level may open: the fleet manager is top of everything — it opens topic managers, goal managers and workers, each when no lower-tier manager owns the subject; a topic manager opens goal managers and workers; a goal manager opens workers; a worker opens nothing. The `<tier>-spawn` verbs are narrower than their tier: `topic-spawn` starts a goal manager, `goal-spawn` a worker, and a worker opened from the topic tier goes through `/supervisor:open --task`. A worker never opens or spawns sessions (`/supervisor:open` is manager-only) and routes anything out of scope to its manager. The full command → tier table, with "never run from" guidance, is [`docs/session-tiers.md`](docs/session-tiers.md) — the single source; run `/supervisor:read-guides` to load it.

## How it works

Each worker is a `query()` session from `@anthropic-ai/claude-agent-sdk`, started with `permissionMode: 'default'`, so every approval-requiring tool reaches the server: the `PermissionRequest` hook answers what the policy knows, and anything it does not cover falls through to `canUseTool`, which parks the request for the manager to resolve through `answer_permission`.

⚠️ `permissionMode: 'default'` is a **request, not a guarantee**. An escalating `permissions.defaultMode` from a trusted settings tier wins over it and bypasses the hook and `canUseTool` both, which makes the policy inert — see README § The approval policy.

⚠️ **The reverse direction has its own gate.** The manager's *outgoing* `answer_permission` call is itself subject to the **manager session's** permission mode. Under `auto` the classifier can refuse that call before the fleet is involved — the refusal reads like a worker-side denial but is not one (measured 2026-09-19, corrected here from an earlier reading that called it a structural gate). The fix is in the manager session: Shift+Tab → `accept edits`, then retry the same call.

```
manager session ──MCP──► supervisor server ──query()×N──► workers
                              ▲                              │
                              └──── canUseTool ──────────────┘
```

**Escalation chain:** worker → policy → `goal-wrangler` → manager → human. Routine approvals should never reach the manager, and genuine forks should never reach the human.

## Where a worker runs

⚠️ **`server/supervisor.mjs` is NOT the supervisor tool a session binds — do not fix spawn behaviour there.** The live tool is the Go **`claude-wrapper`** daemon at `http://127.0.0.1:18090/supervisor/mcp`: every `cc-*` launcher loads `~/.claude/mcp-private-personal.json` with `--strict-mcp-config`, and this plugin ships no `.mcp.json`. Nothing on the operator's machine starts the JS server: it is reachable only from the drill scripts and from `scripts/mcp-start.py`, the plugin's own launcher for a deployment that wires an `.mcp.json`. A spawn, model or permission fix belongs in `bborbe/claude-wrapper` — `pkg/roster_mcp.go` (spawn handler, `headlessModel`) and `pkg/spawn_cwd.go` (launcher + model resolution). Measured 2026-11-09: a headless-spawn model fix was written and tested in `server/supervisor.mjs`, then discarded — the live path was the Go daemon. A `PreToolUse` hook on the operator's machine now refuses edits to that file, so the redirect lands before the write.

`spawn_agent` requires `vault` or `cwd` and **refuses when neither is given**. `server/spawn-cwd.mjs` owns that resolution, and it is a pure function — no filesystem, no environment, no clock — so it is unit-testable without a server.

Both halves used to be silent, and the failure is worth keeping in view when editing it. An absent `cwd` became this server's own working directory; an unresolvable launcher became the vault named `personal` and then the bare `claude` binary. Measured 2026-10-03: **27 workers** spawned that way in one batch, every one in the supervisor repo and on Opus instead of the vault's `cc-private-deepseek` — and the response named neither the launcher, nor the vault, nor the model, so a wrong spawn was indistinguishable from a correct one.

⚠️ **Do not add a fallback here.** Every branch of `resolveWorkerTarget` refuses rather than defaulting, for the same reason `spawn-mode.mjs` refuses an unknown mode or target: a worker started in the wrong place under the wrong launcher is discovered only by noticing it. The same rule governs `parseLauncherModel`, which returns `null` rather than a guess when the launcher script cannot be read.

The spawn response carries `vault`, `launcher` and `model`. The model is read from the launcher script's own `--model` argument — vault-cli config does not hold it — with a `${VAR}` resolved against that script's `export`.

## Reaching a running tab worker

`send_agent_message(agent_id, message)` sends a follow-up to a running worker, over one of four transports picked from the worker's own record (`server/message-channel.mjs`). For a **tab** worker it types into the pane and submits — focus is stolen, and a failure is reported rather than passed off as a success. For a **cluster** worker there is no pane: it posts one more turn on that worker's **existing session id** through the claude-interactive service, blocks until the pod answers, and returns the answer. For an **A2A** worker (`docker` / `k8s`) it opens one more turn on the worker's own endpoint. For a **headless** worker it does not deliver at all — it resolves the worker's **session-registry name** and returns it with `sent: false`, and the CALLER sends it with `SendMessage(to: <name>)`, because the server is not a Claude session and cannot call that tool. This is the `send_to_agent` the server never had: a worker that has gone wrong can be corrected, and one that has stalled can be nudged, without a human at the tab.

⚠️ **The headless hand-back is a send the caller must finish, and `sent: false` says so rather than reporting a failure.** Measured 2026-11-08: session `6d16758d` was live and registered as `claude-1f`, while the old refusal told the caller to *"reach it mid-task with SendMessage"* — which, given only the id the caller held, answers `No agent named '6d16758d…' is reachable`. The join is `namesForSessionId` (`liveness.mjs`, the inverse of `sessionIdsNamed`) over the session registry Claude Code writes per session; a name another live session also answers to is refused rather than guessed (so is a second name for one id), and a worker with no entry at all (the session exited, or it runs in-process under the SDK, which registers no name) is refused too — the fix widens the success path, never the failure one.

⚠️ **A 2xx from a cluster follow-up is not evidence the SAME conversation ran.** The service takes the session id from the caller and echoes none back, so a turn against an id the pod no longer holds is indistinguishable, from this side, from a real continuation — and it would look like a successful send. The proof is the pod's own log (`turn start id=<id>` / `turn end id=<id>`, from `interactive/session-cache.go`), which is the caller's to read. The same discipline `cluster-spawn.mjs` states for a spawn applies to every later turn.

Three things about the **pane** transport are measured rather than assumed, and each one is a trap:

- **It reaches a tab worker only.** The PANE transport is scoped to a tab worker: a headless worker has no pane, so the pane branch is never taken for one — typing into a pane that does not exist is how a channel reports success while delivering nothing. A cluster or A2A worker has no pane either, and a headless worker is handed back to the caller's `SendMessage` (above) — so read this bullet as scoped to the pane transport, never as "the tool is tab-only".
- **It does not activate the worker's tab.** `send-text --pane-id` reaches a pane in a background tab, so a send never takes the operator's focus. This bullet previously claimed the opposite — that activation was unavoidable and stole focus — and that claim did not reproduce (measured 2026-11-09: 6/6 bytes delivered with `focused_pane_id` unchanged). The earlier 0-byte readings were a canonical-mode artefact, not a delivery failure.
- **It waits for the input prompt, it does not sleep.** A fixed delay is a race that fails on a loaded machine. Readiness is the `❯` glyph in the pane, which appears about a second after spawn.

**The colour is set the same way, and that is a bug fix.** `SUPERVISOR_WORKER_COLOR` (default `/color pink`) used to be seeded as the first line of the worker's prompt, and it never worked: Claude Code parses one submitted message as one command, and `/color` takes the *entire* trimmed argument, so the colour swallowed the blank line and the whole task — `Invalid color "pink\n\n<task>"`. It is now sent as its own message once the pane is up, and the spawn response reports `color: {applied: true}` only when it actually landed.

⚠️ **`transcript_dir` on a tab worker is unreliable, and known to be.** It is derived from the `cwd` you passed, but the `cc-*` launcher does `cd` into its own vault — so a worker spawned with `cwd: "/tmp"` runs in `~/Documents/Obsidian/my-vault` and writes its transcript there, while the spawn response still says `/tmp`. Measured 2026-09-14.

**Resolved 2026-09-19 for reading a worker's state.** `agent_status` resolves a worker's transcript by **session id** — `<projectsDir>/*/<session-id>.jsonl` — and never from a cwd, so the derivation above is not on that path and is not trusted there. It still describes where the spawn response *claims* the transcript is, and is still wrong for a launcher-`cd` worker: nothing should read it as a location.

### Reading a worker

`agent_status(agent_id)` reports four fields that answer "what is it doing", none of which the write half could tell you:

| field | source | means |
|---|---|---|
| `last_message` | the worker's transcript JSONL | the last thing it said. Not "the last record" — most assistant records carry a `tool_use` block and no text (measured on a live session: 127 assistant records, 27 with text), so this is the last one carrying text. |
| `current_tool_call` | the worker's transcript JSONL | the call it is inside **right now** — `{name, input_summary, started_at, held_seconds}` — or `null` when nothing is in flight. This is the field that separates a worker executing a long tool call from one parked on a gate: `session_status` says both are not-idle, and this says *which* call and *for how long*. `input_summary` is capped at 200 chars and whitespace-flattened; the full input is `pending_permissions`' job for a parked call. `started_at`/`held_seconds` are `null` together when the record carries no timestamp — `name` is still reported, because "which tool" and "for how long" fail independently. |
| `session_status` | the session registry | the raw registry status: `idle`, `busy`, `waiting` or `shell`. |
| `awaiting_input` | derived from the status above | `true` only when the status is `waiting`. **`null`, not `false`, when the registry does not list the session** — unlisted is a different fact from not-waiting, and reporting it as `false` would be a guess wearing a measurement's clothes. |

⚠️ **`awaiting_input` means "blocked on input", not specifically "a permission gate is open".** It is the superset; whether a permission prompt specifically produces `waiting` is what the live A/B pins.

⚠️ **`current_tool_call` names the pending call; it does not say whether the worker is parked.** A pending `tool_use` with no matching `tool_result` reads identically whether the worker is executing a tool or parked on a prompt — measured 2026-09-19 across 25 live sessions, 3 `busy` sessions carried exactly that pending call, indistinguishable by transcript from the 2 `waiting` ones. The two fields are complements: the transcript answers *which call*, the registry answers *is it moving*.

**In flight means the LAST `tool_use` with no `tool_result` carrying its id — not "any unmatched `tool_use`".** An interrupted call leaves an unmatched `tool_use` behind and the conversation carries on, so a scan that reports any of them keeps naming a call the worker abandoned; measured 2026-09-20 over 1 667 live transcripts, 58 carried an unmatched `tool_use` and only 36 had it as their last one. The record's own `timestamp` is the start, so `held_seconds` is measured from the transcript and does not reset when a manager reconnects or polls again.

The read is bounded: a transcript is read from its **tail** (256 KB). The last message is at the end by definition, and `list_agents` renders every worker — reading each file whole would turn one status call into a read of the fleet's entire history. A window that finds nothing reports `null` rather than falling back to a full read.

**That bound costs the tool-call field a blind spot the message field does not have, and it is named rather than hidden.** A `tool_result` always follows its `tool_use`, so a call found inside the window cannot have its result outside it — but a call **older than the whole window** (256 KB of records written after it while it runs) is invisible, and reports as `null`, i.e. as "no call in flight". The message read falls back to a full read on a miss; this one deliberately does not, because a miss there is common (an idle worker has no call to find) and an unbounded read on the status path is the cost the window exists to avoid.

⚠️ **Two scope limits are unchanged by this read, and are named here rather than left to be discovered.** `agent_status` resolves by `agent_id` only — a label returns `unknown agent <label>` — and it reads an in-memory Map inside the caller's own server process, so it only knows workers **that session** spawned. Peers spawned elsewhere are invisible to it entirely, and an empty result is never evidence of absence. The new fields inherit both limits; they do not widen them.

## The spawn ledger

Every worker this server spawns gets a record at `~/.local/state/claude-supervisor/sessions/<uuid>.json`, keyed by its session uuid and written at spawn.

Two stores already exist and neither answers the question this one does:

| store | keyed by | lifetime | says |
|---|---|---|---|
| `~/.claude/sessions/<pid>.json` | pid | **deleted when the session exits** | who is running *right now* |
| `~/.claude/projects/<cwd>/<uuid>.jsonl` | uuid | permanent | the conversation |
| **the ledger** | uuid | permanent | **who started it, in what mode, from which manager, and how it ended** |

Measured 2026-09-14: the live registry held 13 entries against 13 live processes with **zero stale** — it tracks liveness, not history, so it forgets a session exactly when a record would first be useful.

```json
{
  "session_id": "b26cb46e-…", "agent_id": "agent_1", "label": "ledger-drill-tab",
  "mode": "interactive", "launcher": "…/cc-private-deepseek", "pane_id": "1714",
  "resumed_from": null, "policy": null, "parent_session": "0096a027-…",
  "role": "agent",
  "spawned_at": "2026-09-14T06:10:31.153Z", "ended_at": null,
  "status": "running", "result": null
}
```

`role` is the role the spawn declared, as resolved by `spawn_agent` from the role map (`manager` / `agent` / `human`; `agent` is the default). It is recorded because **the spawn edge alone cannot tell a manager from a worker**: `/supervisor:open` starts a topic manager in a plain wezterm tab, which writes no record at all, while `spawn_agent(role="manager")` writes one — and `gate-owner-filter.py`'s `is_manager()` reads a record as proof of "worker". Without this field a manager opened through the worker path was invisible to every peer manager, which then re-escalated gates it had already handled (measured 2026-10-06). `null` means the record was written before the field existed, and it reads as a worker — not because no pre-field manager has a record (the manager this fixes had one), but because the field is absent on pre-field workers and managers alike, so defaulting the other way would flip every worker spawned before the change into a manager and drop the fleet's gates. The residue is a known gap: a pre-change `spawn_agent(role="manager")` manager still reads as a worker until it is re-spawned or its record gains the field. `human` is the operator's own workspace, opened into the Direct window rather than as a worker (`commands/open.md` § Step 0.6), so `gate-owner-filter.py`'s `is_human()` excludes such a row as `role-human` instead of emitting it — measured 2026-10-27, a human row the Fleet Manager itself had spawned fired its feed three times in one hour. `is_human()` matches only an explicit `human`, the opposite default from `is_manager()`: a pre-field record and a `null` role are never human, because excluding on an absence is the fleet-wide silence every hop there fails open to avoid.

`parent_session` is the **spawn edge** — the manager session that called `spawn_agent`, resolved once by walking up from this server's own pid to the **nearest ancestor the live registry knows**. It is *not* the direct parent pid: `.mcp.json` starts this server through a `bun run` wrapper, so the direct parent is that wrapper and a bare `process.ppid` lookup named nothing — which is why this field was `null` in every record written before the walk existed. When no ancestor is registered — an exited manager, or a chain that never passed through a session — the field stays `null` rather than carrying a guess. Nothing else records it. A worker whose session id never resolved gets no record rather than one filed under a key nothing would look up, and the server logs that rather than staying quiet.

**Why the id may never resolve.** For a tab worker the id is found *by name* — `findRegisteredByName("⚙ " + label)`, an exact match, polled for up to 8s — so the name is the join back to the session. A name is not unique, and `find` returns the first holder, so a label reused while an earlier worker still answered to it resolved the new spawn to **that** worker and filed the old session's id as the new one's. The spawn therefore derives a free name before starting the process (`⚙ <label> (2)`, … until nothing holds it) and refuses poll matches against holders that existed before the spawn, so the join can only resolve to the session it created. A suffixed tab title is that guard working. See `docs/fleet-surface.md` § *The tab name is the join*.

⚠️ **This is not a liveness source.** An entry here must never be read as proof a session is alive — `liveness.mjs` owns that question, and it answers from the session registry plus the server's in-process record of workers it spawned. The ledger is deliberately the durable half.

`SUPERVISOR_LEDGER_DIR` overrides the location. It is deliberately *not* `SUPERVISOR_SESSIONS_DIR`, which already means the live registry — one variable meaning two stores is how a reader ends up pointing this one at Claude Code's directory.

⚠️ **A headless worker is invisible to `ListAgents` and `/fleet-status`.** A roster-only sweep will declare its task unowned and may spawn a duplicate onto it. Check `list_agents` before concluding a task has no owner.

⚠️ **`permissionMode: 'auto'` bypasses supervision entirely** — the hook never fires and `canUseTool` is never called. Measured: the same `kubectl create secret` that parked for approval under `'default'` ran with **zero** permission requests under `'auto'`. Leave it unset.

## The incident ledger

Every worker blocker — a permission prompt, a question, a stall — is a record at `~/.claude/state/incidents/ledger.json`, written only by `scripts/incident-ledger.py` (`open | close | escalate | share | list | prune`). Override with `SUPERVISOR_INCIDENTS_LEDGER`.

**An incident closes on a verified resume, never on a card close.** That is the whole point of the store, and it is why `close` requires `--resume-evidence`. A close is not a resolution: the attention store records `open` → `closed` as CLEARED with `answered_at` and `decision` unset and no answer routed (`attention-controller` `pkg/attention-store-attempt.go`), and `manager-attention-watch.py` emits `CLEARED` on a settle at `idle`, which is a transcript shape rather than proof the worker acted.

| store | keyed by | lifetime | says |
|---|---|---|---|
| `scripts/asked-ledger.py` | blocked session | pruned by age + liveness | who was **asked**, and who resolved the ask |
| `scripts/drive-ledger.py` | session | per round | nudged / reaped / unverifiable **this round** |
| **the incident ledger** | `incident:<session>:<kind>` | permanent | **which blocker, whose, since when, its deadline, and how it ended** |

`asked-ledger.py` does carry `resolved_by`, but only when the resolver differs from the asker, and neither it nor `drive-ledger.py` carries an outcome or a deadline — so neither can say whether the worker ever resumed. That gap, not a missing field, is why this is a third store.

⚠️ **A stall is a duration, not an event, and it issues no attention card.** Its `detected_by` is the drive verdict that named it (`drive:<verdict>@<round>`), its `card_id` is empty at detection, and `SUPERVISOR_INCIDENT_STALL_ROUNDS` (default 2) is the threshold. Do not fabricate a card id to fill the field — a fabricated id is a join that never resolves and it reads as provenance.

⚠️ **`escalated_by` is the discriminator, not decoration.** `escalate --round-id <id>` stamps `round:<id>`; a hand-run without it stamps `manual:<session>`. That is what makes "escalated by the periodic caller" an observation instead of an assertion, so a periodic caller must pass its own round id rather than letting the default apply.

`SUPERVISOR_INCIDENT_DEADLINE_SECONDS` (default 900) pins the deadline so `escalate` has one reproducible clock.

## Layout

```
.claude-plugin/marketplace.json          marketplace manifest
.claude-plugin/plugin.json               plugin manifest
(no .mcp.json)                           deliberately absent — the plugin declares no MCP server; sessions reach `supervisor` over the daemon's HTTP endpoint (`make check` fails if the file reappears)
skills/supervising-workers/SKILL.md
skills/open-items/SKILL.md               the operator-asks ledger — rules + /supervisor:open-items (script stays at scripts/open-items.py)
commands/{goal-spawn,fleet-workers,goal-answer,goal-drain}.md
commands/{jump,who-needs-me}.md                  find and reach a session
commands/attention-next.md + scripts/attention-answer.py  answer an attention item, route it to the asker
commands/{fleet-loop,fleet-status}.md            fleet surface — many sessions; the loop's arm and its read-only snapshot
commands/fleet-drive.md                          fleet surface — the loop's round, dispatched on every wakeup and runnable by hand
commands/fleet-triage.md + commands/steps/fleet-triage-*.md  one pass over every live session — classify, close, drive, card; the deliverable is all five green conditions: zero stuck, zero uncarded, zero finished-but-open, zero no-loop, and every live session accounted for
commands/{topic-loop,topic-status}.md    one TOPIC — the loop's arm and its read-only goal snapshot
commands/topic-drive.md                  one TOPIC — the loop's round: keep goal managers alive, start only goals the operator flagged
commands/topic-spawn.md                  one TOPIC — start ONE goal manager; holds no spawn tool, delegates to /supervisor:open --goal
commands/topic-verify.md                 topic manager — verify one topic, suggest fixes
commands/{goal-loop,goal-status}.md      one GOAL — the loop's arm and its read-only task snapshot
commands/goal-drive.md                   one GOAL — the loop's round, and runnable by hand
commands/goal-spawn.md                   one GOAL — spawn ONE worker (holds the spawn tool)
commands/fleet-verify.md                         fleet manager — verify the fleet layer's contract, suggest fixes
commands/goal-verify.md                          goal manager — verify one goal, suggest fixes
commands/worker-drive.md                         one worker session — drive its anchored task to done
scripts/progress-window.py                       its measured window — verdict, A/B/C observations and the claim result, from disk only (spec: docs/session-progress.md)
commands/restart.md                              restart ONE named session, worker or manager — kill, resume, re-orient
commands/worker-restart.md                       DEPRECATED alias for commands/restart.md
commands/close.md                                close ONE finished session — eight guards, never a resume
scripts/restart-precheck.py                      its read-only pre-kill probe: worktree, transcript, cause of death
scripts/restart-worker.py                        the kill+resume leg — eight refusals, never a broad `kill`
scripts/resolve-launcher.py                      the resume's launcher + chip, resolved by code — a thin CLI over `restart-worker.py`'s `resolve_launcher`, called by `commands/open.md` § 3.1
scripts/close-worker.py                          the close leg — reuses restart-worker/restart-precheck/jump, adds the task gate
commands/open.md                                 resolve a name → jump / resume / spawn (manager-only)
commands/ready.md                                ready ONE named task to the bar, then open it (cross-tier)
commands/stop.md                                 stand that loop down
commands/reset.md + scripts/reset.py             re-discover its state; never deletes the ledger
docs/fleet-surface.md                            spawn shape + fleet table render spec (canonical)
docs/closer-classes.md                           the CLOSERS class vocabulary, the routine-continue table and the digest join rule (canonical)
docs/sweep-output.md                             the manager sweep's output frame — columns, widths, icons (canonical)
docs/fleet-drive-rationale.md                    the evidence behind the drive leg's rules — the agent body states them and points here
docs/session-tiers.md                            session tiers + command → tier table (canonical)
docs/subject-resolution.md                      the four-source subject chain the eight manager commands share
scripts/topic-spawn.py                           the topic tier's spawn gate — goal-not-in-scope / goal-not-startable / manager-cap, the § 1 ranking, and the worker refusal
scripts/topic-keepalive.py                       the topic tier's keep-alive — resume-not-respawn, 3 resumes per goal per 30 min, crash-loop line
scripts/topicless-goal-keepalive.py              the fleet tier's keep-alive for goal managers no topic declares — selects in_progress+execution goals, asks topic-keepalive.py per goal, never acts
scripts/owner.py                                 THE ownership definition (`docs/session-tiers.md` § Keep-alive) as code — owner / not-owner / cannot-tell from session liveness + loop state; every keeper and filter resolves through it
docs/voice-asks.md                               a voiced question is also a board card — rule + detector (canonical)
docs/orphan-recovery.md                          stop + recreate a remote worker whose container or pod is gone (canonical)
scripts/voice-ask-pairing.py                     flags question `say`s with no `attention-ask.py post` within 60 s
scripts/manager-predispatch.py                   the pre-dispatch gate both manager commands run first (state: ~/.claude/state/manager-predispatch/)
scripts/plugin-version-census.py                 the fleet reading of the plugin-install probe — who is stale, and `--reload` the lever that moves them
scripts/approved-not-started.py                  the approved-but-unstarted reading both loops print (tick = 30 min constant; `unknown`, never `0`, on an unreadable registry)
scripts/adopt-orphans.py                         adopts an exited manager's live workers into the ownership-claim store (/fleet-loop Step 3a) — successor-by-subject, else the tick session
scripts/gate-owner-filter.py                     the ownership-scoped attention arm — drops gates a live peer manager owns, excludes gates the vault says belong to another goal, and excludes a `role: human` row (the operator's own workspace, never a manager's to route) (state: ~/.claude/state/gate-feed/<self-session-id>.json)
scripts/session-scope.py                         that arm's durable ownership input — which goal a session belongs to, from the vault's page stamps (not a live store)
commands/read-guides.md                          load every guide in docs/
scripts/{jump,who-needs-me}.py                   their helpers
scripts/service-guard.py                         the fleet round's standing resource guard (fifth reading, router row 15) — `phys_footprint` + CPU per launchd service against `~/.claude/state/service-guard.json`; `--apply` reopens the owning row on a breach, never restarts
scripts/keeper-census.py                         the sixth reading (router row 15) — every live session's keeper tier and every page's, against `docs/session-tiers.md` § Keep-alive; names each unkept / double-kept row and exits 1, posts one card per red episode, never acts
scripts/declared-wait.py                         the shared `⏰ Ends:` reader — one home for the slot `fleet-board.py`, `manager-predispatch.py` and the fleet sweep all key on (the "fourth definition of idle" guard)
scripts/observable-reprobe.py                    the `⏰ Ends:` observable probe — parses every PR a declaration names, asks `gh` whether each cleared, and prints `resumed` / `still-blocked` / `unprobeable`; clears only when EVERY named PR has, and never on an unreadable input
scripts/wait-reprobe.py                          the round's join of declared waits to that probe — one row per declaring session, rendered as a `REPROBED` block; renders only, never resumes
scripts/round-detectors.py                       the round's SC1/SC2 detectors in ONE call — `NO-CARD` (a parked ask with no live store card) and `ROUTING` (an ownerless `🔴 BLOCKED` closer → the subject's manager); builds the manager chain once, and only when a row needs routing
agents/goal-wrangler.md                routine approval loop over headless workers
agents/topic-sweep-reader.md            the TOPIC sweep's read half — goal rows and their managers (called by topic-loop/topic-status)
agents/topic-drive.md                   the TOPIC sweep's act leg — keep-alive and the ranked start hand-over
agents/topic-verify.md                  the topic tier's checks + report shape (dispatched by /topic-verify)
agents/goal-sweep-reader.md            the GOAL sweep's read half — task rows and their buckets (called by goal-loop/goal-status)
agents/fleet-sweep-reader.md             the fleet sweep's read half, Steps 0b–3 (called by /fleet-loop, read-only by /fleet-drive)
agents/goal-drive.md                    the GOAL sweep's act leg (composed by goal-loop, runnable by hand)
agents/goal-verify.md                  the goal tier's checks + report shape (dispatched by /goal-verify)
agents/fleet-drive.md                    the fleet drive leg — revive/blocked split, re-nudge ledger (called by /fleet-drive and by /fleet-loop every round)
agents/gate-relay-read.md                the manager's gate relay, read leg — pane id in, ≤15-line per-pane summary out; declares no write tool at all
agents/gate-relay-send.md                the manager's gate relay, send leg — the operator's answer into a tab worker's pane; never drives a selection modal
server/supervisor.mjs                    the DEPRECATED MCP server — drill scripts only; the live tool is the Go claude-wrapper daemon
server/policy.json                       bundled approval rules (see README § The approval policy)
launchd/com.bborbe.fleet-keeper.plist    the fleet keeper — one tick a minute, no Claude session as its parent
launchd/com.bborbe.idle-reaper.plist     the idle reaper
launchd/com.bborbe.rebase-agent.plist    the rebase agent
launchd/com.bborbe.session-exit-daily.plist  the daily session exit
launchd/com.bborbe.fleet-pusher.plist    the CHEAP wake cadence — StartInterval 900, no model turn (docs/fleet-pusher.md)
launchd/com.bborbe.fleet-pusher-watch.plist  its watchdog — StartInterval 300, reports a dead cadence
scripts/fleet-pusher-tick.sh             one pusher pass, exits; resolves the NEWEST installed supervisor, never a pinned version
scripts/fleet-pusher-watch.sh            reads the pusher's heartbeat, posts one `ack` card once it ages past 1800 s
docs/fleet-pusher.md                     the pusher's two layers, its install, and the five things that look healthy and are not
```

## Changelog fold guard

The fold is guarded twice. `.github/workflows/changelog-unreleased-fold.yml` runs on every pull request and **predicts** it: it computes the merge with `git merge-tree --write-tree` and fails when a pending `## Unreleased` bullet would land under an already-released heading. `.github/workflows/changelog-fold.yml` runs on every push to master and **detects** it in the real merged tree: it fails when a bullet sits under a released section whose tag does not contain its merge.

The precheck is the one that saves the mail. Between 2026-10-30 and 10-31 the post-merge guard was red for 91 consecutive master runs — 91 detections, not 91 prevented, one failure mail each. Run the precheck locally before pushing: `CHANGELOG_ROOT=$PWD CHANGELOG_BASE_REF=origin/master bash <path-to-bborbe-coding>/scripts/check-changelog-unreleased-fold.sh`.

**A red run on master blocks the next merge.** Owner: the maintainer. Trigger: a `changelog-fold` workflow run concluding `failure` on master. Repair it in the next PR, or in a PR of its own, before merging anything else — an always-red guard cannot distinguish a new fold from a standing backlog, which is how three folds in a row (#398, #405, #406) were each found by hand while the guard sat red and unread.

The repair direction is decided by the TAG, never by the guard's placement advice alone: `git tag --contains <merge-sha>` names the earliest release carrying the work, and the bullet moves into that `## vX.Y.Z` section. Run the guard locally before pushing: `CHANGELOG_ROOT=$PWD bash <path-to-bborbe-coding>/scripts/check-changelog-fold.sh`.

## Status

Extracted from a working prototype proven end-to-end on 2026-09-13: one manager session spawned two workers concurrently, answered both `Write` prompts, and both workers completed (`permission_denials: 0`).

Known gaps, tracked rather than hidden:

- **A headless worker cannot be *steered* mid-task by the supervisor tool — but it is not unreachable.** `send_agent_message` reaches a *pane* or a *cluster session*, and a headless worker has neither, so it does not apply here, and an SDK string-prompt session is single-shot; multi-turn needs streaming input (`AsyncIterable<SDKUserMessage>`). ⚠️ **Read that as a limit on the *tool*, never on the worker:** ordinary cross-session `SendMessage` reaches a headless worker mid-task, in both directions (measured 2026-09-20), and a worker whose question is parked is answered over the permission channel. **A headless worker that has *exited*** — turn end, or a ~11 min question timeout — is **continued, not restarted**; see § Spawn a worker in [`docs/fleet-surface.md`](docs/fleet-surface.md) for the shape and its two traps. A tab worker can be steered with `send_agent_message` or stopped by closing its tab.
- **The policy layer is inert under `auto` / `bypassPermissions`** — both answer tool calls without consulting the `PermissionRequest` hook, so no rule can take effect. The server warns at startup and refuses a per-spawn policy under either. Measured 2026-09-15 on a machine whose `defaultMode` resolved to `auto` from `~/.claude/settings.json`. See README § The approval policy.
- **Status lags after an allow** — `agent_status` can still read `running` for a few seconds, because the server marks a worker running the moment it *answers* the prompt, not when the SDK confirms it resumed. That is the most the server can honestly know at that point, so the fix is to stop treating one post-allow check as a verdict: `answer_permission` already returns the outcome, and a worker that was about to finish will read `done` a moment later.
- **Unanswered prompts auto-deny** after 15 minutes — headless workers only; a tab worker's prompt waits for its tab.
