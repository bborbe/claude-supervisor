# Changelog

All notable changes to this project will be documented in this file.

Please choose versions by [Semantic Versioning](http://semver.org/).

* MAJOR version when you make incompatible API changes,
* MINOR version when you add functionality in a backwards-compatible manner, and
* PATCH version when you make backwards-compatible bug fixes.

## Unreleased

- feat: the sweep table carries member goals as rows, with tasks grouped under them

## v0.21.0

- feat: ship the worker sweep's reader as a plugin agent, so `/supervisor:worker-status` stops dispatching to a file the plugin does not own. `worker-sweep-reader` computed the sweep's read-only half — tracked-set read, canonical seven-bucket classification, unanchored id-set extraction, collision count, table render — but lived only in the **global** `~/.claude/agents/`, while the command that invoked it ships here. That is a plugin→vault reference: any install without this user's global agents directory got a command dispatching to an agent type that does not exist. The agent now ships in `agents/` beside `worker-wrangler.md`, converted from its `##`-heading shape to this repo's reference-pair XML shape (`<role>`/`<constraints>`/`<process>`/`<error_handling>`/`<output_format>`/`<success_criteria>`). **Behaviour is unchanged** — the relocation is deliberately neutral so it can be reviewed and reverted on its own; the topic-level necessity check is a separate change and is not in this one. `/supervisor:worker-manager` is not yet wired to the agent — it still carries the analysis inline, and that rewiring lands separately.

## v0.20.0

- feat: `agent_status` and `list_agents` now report **`current_tool_call`** — `{name,
  input_summary, started_at, held_seconds}`, the call a worker is inside right now and how long
  it has been held, or `null` when nothing is in flight. This is the operator's stated
  debugging need, and it is the only signal that separates a worker executing a long tool call
  from one parked on a gate without a pane: `session_status` says both are not-idle, and it
  names neither. **The transcript already carried it and the read path threw it away** —
  `tab-read.mjs`'s `scanForLastAssistantText` parses every `tool_use` block and keeps only
  `type === 'text'`, so the call, its input and its timestamp were all present and discarded.
  In flight means the **LAST** `tool_use` record with no `tool_result` carrying its id; "any
  unmatched `tool_use`" is the plausible wrong answer, because an interrupted call leaves an
  unmatched `tool_use` behind and the conversation carries on — measured 2026-09-20 over
  1 667 live transcripts, 58 carried an unmatched `tool_use` and only 36 had it as their last
  one, so that scan names calls the worker abandoned. `held_seconds` is measured from the
  record's own `timestamp`, not from when the caller first observed the worker, so it does not
  reset across polls. `input_summary` is capped at 200 chars and whitespace-flattened (the full
  input remains `pending_permissions`' job for a parked call); `started_at`/`held_seconds` are
  `null` together when the record carries no timestamp, and `name` is still reported. Read from
  the transcript for **both** worker kinds — the in-memory array carries the call but no
  timestamp with it, so a duration cannot come from memory. ⚠️ **One named blind spot:** the
  read is the same 256 KB tail window as the message read and has **no** full-file fallback, so
  a call older than the whole window (256 KB of records written after it while it runs) reports
  as `null`. The fallback is deliberately omitted here — unlike a missed message, "no call
  found" is the common case for an idle worker, and an unbounded read on the status path is
  what the window exists to avoid.

## v0.19.2

- fix: route a spawn to window id 0, which a `typeof === 'string'` gate was silently dropping. `spawn_agent` accepted `window_id` only when it arrived as a string, so the number `0` failed the test, the flag was dropped entirely, and the tab inherited the caller's window — the partial spawn (right colour, wrong window) that reads as success and is worse than no signal. Measured 2026-09-20 on the manager role, whose window is 0: `window_id="0"` inherited the caller's window, `"00"` routed correctly to window 0, and `"99"` was refused by wezterm with `window_id 99 not found` — the third being the conclusive one, since it proves wezterm was handed the flag and parsed it, leaving the loss upstream of wezterm and specific to the canonical-number spelling. Resolution moves to `window-id.mjs` and is widened to accept any value while keeping `undefined` and `null` absent: a naive widening would turn a dropped flag into one that silently targets window 0 whenever the caller passed nothing, which is why the absent case now carries its own test. Emptiness is still decided in exactly one place — `spawnInteractiveAgent`'s `String(windowId).trim() !== ''` — so the new module does not also resolve it.

## v0.19.1

- fix: stop counting a rendered closer panel as a block, and record which hook event wrote each row. `attention-log.py` writes a genuinely parked gate and a rendered `👤 You:` closer panel under the same `kind: question` with an identical `cleared_by` — four write sites, two colliding pairs — so no consumer could tell "waiting on a human" from "rendered a line describing what waiting looks like". Measured 2026-09-20: 13 rows reported as needing the operator, 4 genuinely blocked. Every record now carries `event` (the `hook_event_name` that wrote it), and `who-needs-me.py` lists panels in their own group rather than counting them as blocks — grouped, never dropped, since the soft signal is what the command is for. A record with no `event` predates the marker and is still counted as a gate, so nothing is silently reclassified.

## v0.19.0

- feat: route a spawned session to its **role's window**, and stop a manager dispatching a human-only task. Three changes, all consumers of the same `role → {chip, window_id}` mapping the wezterm config publishes to `~/.cache/wezterm-role-map.json` on its reconcile tick.
  - **`spawn_agent` gains an optional `window_id`**, threaded through `spawnAgent` to `spawnInteractiveAgent` and onto the `wezterm cli spawn` call as `--window-id`. Without it a role-routed spawn was a **partial spawn**: right chip, wrong window, because the tab inherited `WEZTERM_PANE` from the caller — which is how a human-only task came up in the Agents window (measured 2026-09-20 on `Start Day`). Omitted or empty, the flag is dropped entirely rather than passed empty, so the old behaviour is preserved exactly for callers that do not resolve a role. Tab path only; a headless worker has no tab.
  - **`worker-manager.md` refuses to dispatch a `role: human` task**, rendering it `👤 YOURS` instead of `🚀 READY TO START`. A human-only task is not un-converted work waiting to become autonomous — it is work that *cannot* be delegated, and the two are indistinguishable on the chip (both cyan). Dispatching one would not merely waste a worker: it would put a session on work the operator intended to do themselves, looking like every other agent. Absent `role:`, behaviour is unchanged — the gate only ever subtracts. `role: manager` is likewise not a spawn target.
  - **`sendToPane` activates by pane id, not tab id** (`server/tab.mjs`). A tab id is renumbered when its tab moves windows (2026-09-18: tabs 158/159/160 → 163/164/165, `activate-tab --tab-id 159` failed while `activate-pane --pane-id 239` worked), so the colour-send path broke exactly when this feature started moving tabs between windows. The function already held the pane id; the tab lookup is now used only for the return value. The test moved with it, and asserts the absence of `activate-tab` rather than only the presence of the new call.

## v0.18.4

- fix: verify the worker colour against the pane instead of trusting the send. `spawn_agent` reported
  `color: {applied: true}` whenever `send-text` exited 0 — true whether or not the message actually
  submitted. Measured 2026-09-20: **3 of 6 spawns asserted success for a colour that never applied**,
  confirmed against the transcript's own `agent-color` entry rather than the pane. Two causes, both
  fixed. (1) `isReady` accepted the prompt glyph while the TUI was still painting the composer's
  placeholder suggestion (`Try "fix lint errors"`); a message sent into that phase has its Enter
  swallowed and the text stranded — two spawns failed 81ms and 137ms after a placeholder sample, while
  the one that saw a plain `❯ ` submitted. Readiness is now an **empty** composer, not a drawn one, and
  a composer already holding text is refused rather than typed into. (2) `sendToPane` takes an opt-in
  `confirm` marker, polls the pane's own `Session color set to` output with a bounded bare-Enter retry,
  and returns an error when it never appears — so an unconfirmed colour can no longer be reported as
  applied. Confirmation is opt-in because `send_agent_message` shares the function and never produces
  that marker. A cleared composer is deliberately not treated as delivery: a stranded message can also
  be discarded without ever submitting (observed: text sat unsubmitted 14 minutes, then vanished, with
  the colour never applied).

- docs: correct the roster and channel claims across the manager command surface, and document the
  headless answer path as primary. **Refuted claim (four sites):** `commands/fleet-manager.md`
  (two), `commands/worker-manager.md` (step 8 of the sweep procedure) and `llms.txt` asserted that
  a headless supervisor worker has no unix socket and cannot appear in `ListAgents`. Measured 2026-09-20 — a headless worker read its own roster row back
  (`personal-80 [fd6bbe] · interactive · busy`) and holds `/tmp/cc-socks/<pid>.sock`, confirmed
  independently from a manager session — refutes every clause. The duplicate-spawn warning built on
  it is re-founded on the session registry. Two real limits replace the false one: the roster's mode
  column reports `interactive` for headless workers too (so it cannot tell the two apart), and the
  roster is volatile (12 rows → 8 within 17 minutes as workers exited at turn end).
  **Harmful omission:** `commands/answer.md` presented `allow`/`deny` as a classifier-mode choice and
  never said that for an `AskUserQuestion` the answer is `deny` + a `message` — `allow` runs the tool
  in a tty-less session, waits ~11 minutes, and the worker **exits with the question unanswered**.
  `commands/answer.md` and `llms.txt` now state the rule. **Narrowed claim:** `llms.txt` carried
  `send_agent_message`'s tab-only limit in a form that read as "a headless worker cannot be reached";
  corrected to the narrow truth — the tool is tab-only, but ordinary cross-session `SendMessage`
  reaches a headless worker mid-task in both directions.
- docs: document the headless continuation mechanic and the spawn-scope constraint.
  `docs/fleet-surface.md` gains § "A headless worker exits at turn end" — a headless worker ends
  its turn on a READY panel or exits when a parked question times out (~11 min), neither of which
  is completion; the continuation is `spawn_agent(prompt=…, resume=<id>, interactive=false,
  cwd=<explicit>)` as a **plain user turn**, not a relay. It records that **`cwd` is not inherited
  on resume** (the session id names a conversation, not a directory) and that a worker which
  exited on a timeout **still holds its unanswered question**, so a "continue" prompt parks it at
  the same gate again. `commands/worker-manager.md` and `commands/fleet-manager.md` state the
  scope boundary: a headless worker's prompts park only with the server process of the session
  that spawned it, so a worker manager answers its own workers and a fleet manager has no channel
  to answer a topic manager's — route to the owning manager instead.
- docs: state the headless channel as the primary path and demote the tab relay to a fallback.
  `commands/fleet-manager.md` gains an explicit two-entry channel table (headless-parked →
  `answer_permission`; headless-exited → `spawn_agent(resume=…)`; tab worker → its pane), so a
  reader meets the headless path as a peer of the tab path rather than as an afterthought.
  `README.md` narrows "a headless worker cannot be corrected or stopped once running" — which
  read as a statement about the worker — to a limit on `send_agent_message` specifically, noting
  that cross-session `SendMessage` reaches a headless worker mid-task and that an exited one is
  continued via `spawn_agent(resume=…)`.
- docs: name the one-manager-N-workers bottleneck and repeat the tab-relay demotion at the
  worker-manager relay section. Measured 2026-09-20: two workers lost a turn to supervisor
  timeouts in one night while their manager was busy elsewhere — the failure mode is a
  **silently stalled worker**, with nothing reported. Because an unanswered headless prompt
  auto-denies after 15 minutes, a request left parked past that window resumes the worker with
  a denial it did not earn; the command now says to answer promptly or not at all, and to
  prefer fewer longer-lived workers over many short ones.

## v0.18.3

- fix: stamp every worker a supervisor owned as `unknown` when the server exits, instead of
  leaving its ledger record asserting `running` forever. A record whose server died, was
  restarted, or never saw the exit kept claiming `running` indefinitely — worse than absence,
  because it reads as an affirmative claim about a worker nobody is watching. `SIGTERM` and
  `SIGINT` now write `status: "unknown"` with `supervisor_exited_at` and a per-kind
  `unknown_reason`, reusing `liveness.mjs`'s `{ live: null }` convention rather than coining a
  new vocabulary. Never `done`/`error`: those assert an outcome nobody observed. The handler is
  deliberately write-only — it does not probe liveness, so shutdown cannot hang on an unbounded
  wait. **Consumers reading the ledger must handle the new `unknown` status**; a reader that
  trusts `status` should resolve `unknown` against `checkLiveness` rather than reading it as
  either alive or dead.

## v0.18.2

- fix: retract the claim that `answer_permission(allow)` is blocked by a structural, one-directional
  classifier gate. A controlled re-measurement on 2026-09-19 refuted it: the refusal is the
  **manager session's own** `auto`-mode classifier gating its outgoing call before the fleet is
  involved, and under `accept edits` the identical call succeeds. The old reading pointed operators
  at the wrong fix — changing the worker's mode or `defaultMode`, which `spawn_agent` cannot even
  accept. `commands/fleet-manager.md`, `commands/worker-manager.md`, `commands/answer.md`,
  `agents/worker-wrangler.md`, `skills/supervising-workers/SKILL.md`, `README.md` and `llms.txt` now
  state the real precondition (the manager's own mode) and the one-keystroke fix (Shift+Tab →
  `accept edits`, then retry).

## v0.18.1

- fix: `agent_status.last_message` now finds a tab worker's last message even when the
  transcript's **trailing** records are large. The disk read treated a 256 KB tail window as a
  bound on correctness rather than as a fast path, so a file whose records *after* the answer
  are big attachments could push the answer outside the window and report `null` — a worker
  that had spoken reading as one that said nothing, which is the exact failure the feature
  exists to fix. Measured on the first real transcript: 700 302 bytes, last
  assistant-with-text record at bytes 402 426–403 570, window starting at byte 438 158. A miss
  now falls back to scanning the whole file. ⚠️ **The unit test that shipped with the feature
  did not catch this** — its filler lines were 400 chars, so the answer always landed inside
  the window; only records that come *after* the answer and are themselves large reproduce it.
  That case is now covered, and the fallback was verified against the real transcript that
  failed rather than against a fixture. The live A/B caught what the suite could not, which is
  why the feature was specified to require one.

## v0.18.0

- feat: `agent_status` reads a tab worker's state from disk, so a manager no longer needs
  `wezterm cli get-text` to learn what a worker said or whether it is waiting. `last_message`
  was `null` for **every** interactive worker: the in-memory transcript is pushed by the
  headless SDK loop alone, so the tab path had no source at all and `lastAssistantText` walked
  an empty array. It is now read from the worker's own transcript JSONL, resolved by session
  id rather than from a cwd — the `transcriptDirFor(cwd)` derivation the README calls
  unreliable is not consulted, because the `cc-*` launcher `cd`s into its own vault and the
  passed cwd is not where the transcript lands. Two fields are added: `session_status`, the
  session registry's raw status, and `awaiting_input`, `true` only when that status is
  `waiting`. `awaiting_input` is `null` — not `false` — when the registry does not list the
  session, because unlisted is a different fact from not-waiting, and reporting it as `false`
  would be a guess wearing a measurement's clothes. ⚠️ Gate state comes from the registry and
  **not** from the transcript: measured across 25 live sessions, a pending `tool_use` with no
  matching `tool_result` reads identically for a worker executing a tool and one parked on a
  permission prompt. The dead `agentView.lastText?.()` call — `agentView` has no `lastText`,
  so it always fell through — is removed. `SUPERVISOR_PROJECTS_DIR` overrides the transcript
  root.

## v0.17.2

- fix: `fleet-sessions.py` and `fleet-colours.py` no longer take a project scope. Both
  defaulted to the *current working directory*'s project, so a fleet sweep run from a vault
  silently narrowed the roster and read a live session in another project as *gone* — the
  exact hazard `fleet-manager`'s scope guard existed to paper over. The fleet is now always
  every project, newest first, with no time filter, so there is no scope left to disagree
  about. The retired flags (`--all`, `--minutes N`, `--vault NAME`) are accepted and ignored
  rather than rejected, so callers that still pass them — the Fleet Manager Session runbook
  passes `--all` — keep working. `fleet-manager`'s "⚠️ Scope guard" section, the snapshot's
  `scope` field, and `fleet-status`'s scoping paragraph are deleted along with the mechanism
  they guarded, and `fleet-status`'s `argument-hint` no longer advertises the flags.

## v0.17.1

- fix: the gate notification puts the gate's own text on a line of its own, so the command inside it survives a
  copy. A phone wraps a long line mid-argument, and an operator copying the recommended command out of a delivered
  notification got `/vault-cli:complete-goal "The Manager Ranks` — truncated exactly at the wrap, leaving an
  unbalanced quote and a goal name that does not exist. The gate text is where a manager puts that command, and it
  was previously rendered inline after a `Manager gate open -- <owner>: ` prefix that pushed it ~24 columns into the
  wrap zone; it now starts a line. The trailing line also stops advising and states the wall instead —
  *"Replies here are not read"* — because *"Answer it in the owning session, not here"* was advice the operator
  reasonably ignored: they replied in Telegram, the predictable response to a message that reads like a conversation.

## v0.17.0

- fix: the manager commands now hand over `/supervisor:jump <pane-id>` instead of the bare
  `/jump <pane-id>`, which resolves to nothing. The plugin installs as
  `supervisor@claude-supervisor`, so its commands are namespaced by the install id — the bare
  form answers `Unknown command` at exactly the moment it is needed, because the handover is
  the manager's last resort for a worker already blocked on a gate a relay cannot release.
  32 bare references across the five command files (`jump`, `fleet-status`, `worker-status`,
  `fleet-manager`, `worker-manager`) now print the namespaced form, including `jump.md`'s own
  contract sentence — the line that defined the defect. `docs/fleet-surface.md` § How commands
  are addressed states the rule once, so a plugin or marketplace rename is a one-place edit.
  Measured 2026-09-19: the operator followed a printed handover, typed `/jump 271`, and got
  `Unknown command: /jump`.

- feat: publish manager ACTION gates to the notification core, so a gate raised while the operator is away reaches
  their phone instead of waiting unbounded on a TTS line nobody is in the room to hear. New `scripts/notify-gate.py`
  reads the round's gates on stdin, publishes through the configured endpoint, and owns the delivery cadence plus
  the per-layer ledger under `~/.claude/state/gate-notifications-<layer>.json` (the bound itself lives in the
  script). Config sits in `~/.config/claude-supervisor/config.json` beside `spawn.mode`; an absent or incomplete
  `notify` block exits non-zero rather than skipping silently, because a silently-skipped gate is indistinguishable
  from a clean sweep. Wired at the existing ACTION-gate moments in `commands/worker-manager.md` and
  `commands/fleet-manager.md`; only Gate-triage classes C, D and E publish — A and B are the manager's own to clear
  and stay silent.

## v0.16.3

- fix: a topic's `## Goals` list may now declare a **task** directly, alongside its goals.
  The tracked set was goal-anchored — *"every entry is a member goal. Tracked set = those
  goals + every task whose `goals:` frontmatter names one of them"* — so live work in a
  domain that named no member goal rendered as **no row at all**: no error, no
  smaller-looking table, just a complete-looking one. Measured 2026-09-19 on
  `23 Topics/Notification System.md`, two `in_progress` tasks with running sessions were
  absent from a 19-row table and had to be reported in prose beside it. Both
  `worker-manager.md` and `worker-status.md` § Resolution step 2 now state that an entry
  may be a goal or a task. **The declaration is unchanged in kind** — membership is still
  read from the page and never re-derived: no glob, no `goals:` scan, no theme match and no
  content grep was added. `worker-status.md` additionally named the heading `# Goals`, which
  matches nothing on a topic page; corrected to `## Goals`, a sub-heading under `# Scope`.

## v0.16.2

- fix: the reapability check now reads the session's *live* closer and accepts both
  sanctioned close-gate forms, so `Reapable` stops reporting `0` while finished sessions
  sit in `Needs you`. Two independent defects, both inside `is_reapable()`. It matched
  `rec["detail"]` — the hook-written field that is never refreshed — so a session that had
  cleared one gate and raised another was judged on the old text; `reclassify_idle()`
  already re-derives the closer from the transcript, but it returns early on
  `kind != "idle"`, so every hook-written gate kept the stale detail. And it matched the
  close gate with a literal `startswith("approve: /vault-cli:session-close")`, blind to the
  `pick` form the operator's global DONE rule prescribes alongside it. Measured 2026-09-19:
  four reapable sessions in roughly forty minutes and three were missed — 312 caught
  (`approve:`, fresh), 338 and 254 missed (stale `detail`), 17 missed (`pick` form) — while
  the same output correctly suppressed parked `later (on <trigger>):` waits, so an operator
  who had learned to trust the new feed had every reason to trust a `Reapable (0)`. The
  `pick` match requires session-close to be the first/recommended disposition, and
  `is_parked_verb()` is checked first, so widening a prefix test into a containment test
  cannot turn a `later (on …)` deferral into a close gate. `last_assistant_text()` is now
  memoized per session, holding the transcript read at one per candidate.

## v0.16.1

- fix: `open-items.py` now resolves an entry's `--task` against **every** vault in vault-cli's
  config, and marks an open entry whose task target backs no file as `⚠️ UNRESOLVABLE`. The ledger
  previously resolved nothing at all — `list` rendered `task` and `resolves on` as free text — so an
  entry claiming `resolves on: task file status: completed` could not be checked, and a lookup that
  missed read exactly like a task that was never filed. Measured 2026-09-19 while auditing a live
  35-entry ledger: a target read as absent was sitting in a **sibling vault**, `status: completed`,
  while the entry stayed open over a day — a vault-blind lookup, which is the same shape as the
  defect it was mistaken for. `add` now stores the resolved path and warns (never refuses: the ledger
  exists to record an ask *before* its task exists); `list` re-resolves on every read, so filing a
  late task clears the marker without re-adding the entry, and flags **open** entries only — a
  correctly closed entry with a dead target is history, not a problem, and re-flagging it would make
  the very entries this explains look broken after they were closed. `--tasks-dir` scopes resolution
  for a caller that already knows its vault. A path-bearing title (`~/.claude/commands/open.md`) is
  also matched against its on-disk sanitised form, since `/` cannot appear in a filename and an
  exact-title lookup would otherwise report "no such file" for a task sitting right there. When no
  task dir is searchable at all (vault-cli absent, its config unreadable, no `--tasks-dir`), an
  entry renders `⚠️ UNCHECKED`, not `UNRESOLVABLE` — a check that could not run must not assert a
  negative, which would flag every entry on such a host and is the same failed-lookup-as-claim
  shape this change removes.

## v0.16.0

- feat: the worker-manager now authors tasks before spawning, and names the split explicitly —
  authoring (sections, subtasks, DoD, SC evidence shapes) moves to the manager; execution planning
  (which file, which mechanism, what the system permits) stays with the worker. A hand-written task
  file ships without `# Tasks`/`# Definition of Done`, so the worker's own `plan-task` gate stops and
  asks the operator for the decomposition inside the worker's pane — a multi-question wizard that
  cannot safely be relayed. Measured 2026-09-19: three hand-written task files produced three
  3-question wizards, nine operator decisions, none of which needed the repo open. The command now
  requires `/vault-cli:create-task` (which dispatches the `task-creator` agent) plus
  `vault-cli:task-auditor` before any spawn, with a grep check that the three sections landed. Both
  Worker Manager Session runbooks (Personal + Brogrammers) mirrored, and their § Self-improvement
  source-of-truth path corrected — `~/.claude/commands/worker-manager.md` does not exist; the real
  home is this repo's `commands/worker-manager.md`.

## v0.15.3

- fix: stop `who-needs-me.py` listing parked waits, entity-padded closers, peer-gate restatements
  and completed-task close gates under `Needs you`. All four entered through one
  `startswith("nothing")` test in `reclassify_idle()`, so a correctly-parked session was
  reclassified `idle` → `question` and counted as an open gate. The feed is triaged oldest-first,
  so the false positives are exactly what a manager reaches first. Measured 2026-09-19: 19 listed,
  roughly 7 real — the two oldest were `later (on <trigger>):` waits misreported as neglect for
  over five hours. `/supervisor:jump` shares the parser but re-derived the predicate inline, so it
  kept offering answered records, parked waits and finished-work close gates that the feed had
  dropped — the two surfaces disagreed about who needs you. It now classifies through the same
  function, and the two are verified to agree on the live fleet.

## v0.15.2

- fix: `worker-manager` starts a worker by spawning it with the work command as the `prompt`
  argument, so the worker creates its own session in its own pane. Minting the session first
  ran a full planning turn inside the manager's own session — blocking it for minutes and
  making the manager do the worker's work. `docs/fleet-surface.md` § Spawn a worker now
  carries the fresh-start path, and resolves `interactive` from the fleet's config file
  (`SUPERVISOR_SPAWN_MODE` → `spawn.mode` → built-in) instead of presenting the built-in code
  default as the answer.

- fix: `/fleet-manager` reports the green/cyan colour backlog in its sweep, and its Step 3b no
  longer claims colour is unreadable. That line read *"**no colour is machine-readable**:
  `wezterm cli list --format json` exposes 19 pane fields and none is a colour, and the session
  registry carries none either"* — an unmeasured negative: the two stores checked are exactly the
  two that lack a colour key, and the transcript, which carries one, was never checked. The guard
  the line exists for still holds and is restated: a `purple` chip means the operator *marked* a
  session finished, which is a claim, not the fact the done-check needs. The pane-field count is
  corrected to 18. The new sweep line is the roster's only **quantitative** measure — every other
  line in that output is a per-session judgement — and it is read `--all`, because a narrower
  scope silently undercounts the fleet.

## v0.15.1

- fix: stop the forked-ledger warning firing on a ledger that has already been reconciled. The
  shared-id scan counted **all** entries rather than open ones, so closing the duplicate copies —
  the documented way to resolve a fork — left the warning in place forever, still claiming the
  two "will diverge" after one side was already closed. Measured 2026-09-19: a fork reconciled
  with `close --id … --evidence "reconciled: owned by session <A>"` read `A open 2 · B open 0`
  and still warned on every read. The scan now compares open entries on both sides; an
  unresolved fork still warns, verified against the real pair on this machine. The
  `origin_session_id` line is deliberately unchanged — "this ledger was copied" is a provenance
  fact that stays true after reconciliation, and gating it on shared entries would lose the case
  where the parent's ledger no longer exists at all.

## v0.15.0

- feat: adds `scripts/fleet-colours.py`, a census of what colour each live session is. Colour is
  the operator's attention-cost ranking — green/blue/cyan cost a keystroke per step, pink is an
  agent, orange a manager — and until now nothing but the operator's eye could read it, so the
  unconverted backlog was unmeasurable across forty panes. The colour turns out to be persisted
  already: every session transcript re-emits `{"type":"agent-color","agentColor":…}` each turn
  carrying the *current* value, so the last record is live. Neither `wezterm cli list` (18 fields)
  nor `~/.claude/sessions/*.json` carries a colour key — the census reads transcripts. Reports
  green/blue/cyan as the backlog and keeps `default` (transcript present, colour never set) and
  `purple` (finished) separate rather than folding either in; a session with no transcript reads
  `unknown` rather than a guessed colour. Panes join on the registry's `pid` → tty, never on cwd,
  which every session in one vault shares.

## v0.14.0

- docs: two manager-contract rules earned this session. **The PR is the boundary, whatever the
  file extension** — editing a doc may be management, opening a PR never is, because it obligates
  a review loop, a merge, an auto-release and a deploy verification; and the Build Drift Gate is a
  *crossing* trigger, so a one-shot that grows a branch and commits needs its anchor at the
  crossing, not retroactively. **Never forward a claim you did not measure** — a peer's or a
  sub-agent's observation is testimony, and relaying it onward launders provenance because the
  receiver reads it as your measurement. Both measured 2026-09-19, each against a specific
  failure in the same session.

- fix: name `~/.claude/sessions/*.json` as the **authoritative liveness store** in `/fleet-manager`,
  and forbid writing a session stamp from a spawn-ledger reading. The ledger never closes when a
  worker's pane dies, so it does not go quiet when wrong — it asserts the opposite. Measured
  2026-09-19: a manager read a dead id as `running` from the ledger and **repointed a task's
  `claude_session_id` to it**, pointing a live task at a dead session — the exact orphaning the
  repoint was meant to prevent, inverted. The registry deletes an entry on exit, which is the
  property no other channel has (`pgrep -f` is argv-only, `ListAgents` omits headless workers, a
  pane can outlive its session). Also records the `/branch` case that defeats id-keyed probes
  outright: a branch holds a new id while sharing the parent's task file, so every probe keyed on
  the original id calls a working session dead.

- feat: managers now **reap finished sessions** instead of leaving them parked. `/fleet-manager`'s
  Step 3 classified `idle` + all-boxes-ticked as `done → nothing`, so a session whose work was
  complete sat waiting on an operator whose only legal move was the obvious one — measured
  2026-09-19, **four sessions** parked simultaneously on `approve: /vault-cli:session-close`,
  every one over a task reading `status: completed`, `phase: done`, zero open boxes. New
  `/fleet-manager` § Step 3b and a matching `/worker-manager` sweep bullet: verify the three
  facts against disk, message the worker the evidence non-authorisingly, and report the set to
  the operator as **self-closeable** — one line, never N approvals.
  - Encodes what a manager provably **cannot** do here, so the rule is not written against a
    capability that does not exist: `sync-progress` and `session-close` read the parent
    conversation, and every route into a worker's pane is refused — `[Remote Shell Writes]`,
    `[Auto-Mode Bypass]`, classifier-blocked `answer_permission(..., allow)`, and
    `[Self-Modification]`. (`answer_permission(..., deny)` passes; the gate is one-directional.)
  - Guards the two ways the check misfires: **deliberately-open Self-Review boxes** mean the task
    is not complete and the worker is right to park, and **session colour is not machine-readable**
    (`wezterm cli list --format json` exposes 19 pane fields, none a colour) so it can never be the
    detection signal.

- fix: restore the attention-feed staleness rule to `/fleet-manager`, lost when the migration
  deleted the pre-plugin `commands/fleet-manager.md`. **The feed answers "was a gate raised",
  never "is a gate open"** — a record is overwritten only by that session's next tool call, so a
  cleared gate lingers and a fresh one is absent. Cost when unrecorded: one stale permission
  prompt reported **four times in one day**. Extended to cover the doorbell `Monitor` over the
  same feed, which inherits the limit exactly: a `CLEARED` event means the entry left the feed,
  which a session going *busy* produces just as readily as a gate being answered.

## v0.13.3

- fix: the fleet-manager orphan check seeds from the ownership **declaration** instead of a flag.
  `claude_session_started` had decayed to **0 of 3845** tasks (down from 496 when the check was
  written), so the candidate set was empty and the check reported a confident clean bill while
  real orphans went undetected — two were found by accident on 2026-09-18, neither carrying the
  flag. Seeding now enumerates `claude_session_id` + `status: in_progress`.
- fix: adds a **park filter** ahead of the liveness probe — a future `defer_date` **or**
  `created_by: recurring-task-creator` means scheduled, not abandoned. Neither signal alone is
  sufficient: the `Start Day` family carries no `defer_date`, and `Repair Bike Switch` carries no
  `created_by`. Both are dead on every liveness axis, so no process or transcript evidence can
  separate a parked routine from an orphan; the park signal is the only discriminator.
- fix: **removes the `≥4h` lower age bound**, which excluded the recently-died orphans the check
  exists to find — `32d5e57c` died ~35 min before detection, leaving its file only 3h stale.
  Replayed against recorded state: **1 of 2** orphans detected with the bound, **2 of 2** without.
  The park filter covers the routine class the bound was originally added for; measured 21
  candidates with and without it, and 0 recurring tasks in the unbounded set. A 7-day upper bound
  remains, so the fleet-wide set stays actionable at ~21.
- fix: the check reads **frontmatter only**. Task bodies quote these keys in prose, so a
  whole-file `grep` reads a task as parked or owned on the strength of a sentence *about*
  parking — it reported the very task documenting the defect as a routine.
- refactor: the candidate enumeration moves out of the command into
  **`scripts/orphan-candidates.py`**, alongside the other manager helpers (`fleet-sessions.py`,
  `who-needs-me.py`, `fleet-snapshot.py`). The command now calls it and no longer carries the
  filter logic inline, and the operator's runbook calls the same script — one source rather
  than two that drift. Verified by running both implementations against the live fleet: the
  script and the inline block return an **identical 21-task set**.
- fix: **the check now fails loud instead of quiet.** `scripts/orphan-candidates.py` resolves
  its own liveness probe (the sibling `fleet-sessions.py`) and prints
  `⚠️ ORPHAN CHECK FAILED — the result is not clean, it is UNKNOWN.` on **stdout** when it
  cannot run. This was not hypothetical: `~/.claude/scripts/fleet-sessions.py` — the path both
  the command and the runbook called — was deleted on 2026-09-18 when the manager scripts
  moved into the plugin, and the old invocation returned zero candidates with exit 0. The
  runbook's two references are repointed.
- fix: **the call site checks the exit code.** `commands/fleet-manager.md` § Step 2b now runs
  `orphan-candidates.py … || { echo "⚠️ ORPHAN CHECK FAILED — the orphan section of this sweep
  is UNKNOWN, not clean."; exit 1; }`. The script already prints its own warning, but a manager
  reading only stdout rows can still take an empty result for a clean one; propagating the
  status to the sweep's own exit code is what makes "unknown" unable to masquerade as "clean".
- test: adds `scripts/tests/test_orphan_candidates.py` (stdlib `unittest`, 21 cases) covering
  frontmatter-only parsing, the park union, and the absence of a lower age bound — each case
  guards a defect the check has actually shipped. Wired into `make test` alongside the Node
  suite. **Verified by mutation:** re-introducing the removed `≥4h` lower bound fails 2 cases,
  so the tests bite rather than decorate.

## v0.13.2

- docs: let a proven-dead resume take path B where headless is not permitted, and split resume
  into two decisions. `docs/fleet-surface.md` § Spawn a worker sent every proven-dead session
  to the headless `spawn_agent` path — which any phase forbidding headless cannot execute, so
  the worker-manager auto-resume gate was unreachable for that phase's duration even though
  the command authorises it and the runbooks restate it. The section now records that the
  no-headless constraint is phase-scoped rather than permanent, routes the proven-dead row to
  path B under it, scopes the `resume`/`interactive` refusal to `mcp__supervisor__spawn_agent`'s
  tab path rather than to the platform, and separates the *path* decision (liveness) from the
  *drive* decision (mid-work needs the work delivered; a gate-death must be left idle).
  `commands/worker-manager.md` gap-6 and gap-7 no longer restate the spawn shape — each points
  at that section, as the section's own single-home rule already required.

## v0.13.1

- fix: make the forked-ledger warning actionable. It said *"N entries also live in session X's
  ledger — the two have forked and will diverge"* without naming **which** entries or how to
  resolve them, so the operator had to reconstruct both from the JSON by hand. It now lists the
  shared ids and prints the exact command — `close --id <id> --evidence "reconciled: owned by
  session <owner-id>"` — to record which side owns each entry. Still warn-only: the script never
  picks a winner, because a silent pick is the same divergence bug wearing a different hat.

## v0.13.0

- feat: add the **print-the-artifact** guardrail to `worker-manager` — a manager must print the
  artifact in the same turn as any **negative** claim ("X does not exist", "the delta is in
  neither file") or any **attribution** ("you said X", "the script reported Y"). The rule names
  the glob trap (a shell glob is a search, and a search that fails to match proves nothing about
  absence — `ls /tmp/*x*` cannot descend into `/tmp/subdir/`) and treats pane text as
  multi-author (a WezTerm pane mixes session output with harness-generated lines, so quoting it
  requires knowing who wrote the line). Justified by the asymmetry: the check is one call, the
  failure is silent.

## v0.12.2

- fix: read the session id from `CLAUDE_CODE_SESSION_ID`, not `CLAUDE_SESSION_ID`. The
  exported name is the former, so the old fallback could never fire and every caller had to
  hand-pass `--session` — while the script's own error text told them to guess the id from
  `/status` or a transcript path, the exact route its own warning calls out as silently
  splitting a ledger in two. `CLAUDE_SESSION_ID` is kept as a fallback for older callers.
- fix: join the fleet sweep on the session id instead of the session name. The old join
  (`ListAgents` name == `fleet-sessions.py` `WORKING ON`) held only because `/rename <task
  title>` happened to make the two strings equal, so renaming a session silently dropped it
  from the sweep while it stayed alive and possibly blocked on an unanswered gate. The
  session id is stable across `/rename` and is bridged to the roster by the session registry
  at `~/.claude/sessions/<pid>.json`.
- fix: key the fleet snapshot on the session id, not `[ref]`. `[ref]` is computed per roster
  read and is not stable across time, so an unchanged session read as vanished-and-new
  between sweeps — the one thing this file exists to detect correctly.

## v0.12.1

- fix: resolve helper scripts from the plugin's own marketplace clone when
  `CLAUDE_PLUGIN_ROOT` is unset, instead of falling back to `~/.claude/scripts/`.
  `CLAUDE_PLUGIN_ROOT` is not set in the shell a command's Bash runs in, so the fallback
  branch is the one that actually executes — and the old fallback pointed at the very
  directory the migration is about to empty, which would have broken all six commands the
  moment the originals were removed.
- fix: port two helper-script changes that landed in `~/.claude/scripts/` after the migration
  PRs were cut — `who-needs-me.py` gains `is_open_gate()`/`answered()`, so an unanswered gate
  is distinguished from one the operator has acted on rather than inferred from file
  existence; `open-items.py` documents that `--answer` on an `asked-of-you` forges an
  operator attribution a later reader cannot tell from a real one.

## v0.12.0

- feat: ship `/supervisor:worker-manager` and `/supervisor:worker-status` with the
  `open-items.py` ledger helper, completing the move of the fleet control surface into the
  plugin. `worker-status` was previously duplicated across two Obsidian vaults and had
  diverged behaviourally; the agent-delegating form is the one shipped.
- feat: `worker-manager` now points at `docs/fleet-surface.md` for the spawn shape instead of
  an Obsidian runbook referenced by absolute path, so the command is readable from any vault.

## v0.11.0

- feat: ship `/supervisor:fleet-manager` and `/supervisor:fleet-status` with their renderer and
  discovery helpers (`fleet-sessions.py`, `box-table.py`, `fleet-snapshot.py`,
  `context-usage.py`), previously maintained by hand in `~/.claude/commands/`.
- feat: add `docs/fleet-surface.md` as the canonical home for the spawn shape and the fleet
  table render spec, so the plugin no longer defers its own mechanics to an Obsidian runbook
  referenced by absolute path.
- docs: README gains a prerequisites table for the machine-local state the fleet commands read
  but this plugin does not ship, and the rule for how commands reference vault notes.

## v0.10.0

- feat: ship `/supervisor:jump` and `/supervisor:who-needs-me` as plugin commands, with their
  `jump.py` and `who-needs-me.py` helpers. Both files were previously untracked in `~/.claude`
  — named by the handover rule in five command files while living in no repository.
- feat: `jump.py` resolves its import of `who-needs-me.py` via `${CLAUDE_PLUGIN_ROOT}` with a
  `~/.claude` fallback, so the pair travels together as an installed plugin and still runs
  standalone.

## v0.9.0

- feat: read the default spawn mode from `~/.config/claude-supervisor/config.json` (`{"spawn":{"mode":"interactive"|"headless"}}`, path override `SUPERVISOR_CONFIG`), so the fleet-wide interactive-vs-headless decision is one file edit instead of one edit per manager command file. Precedence, highest first: the per-call `interactive` argument, `SUPERVISOR_SPAWN_MODE`, the config file, then the built-in `interactive`.
- feat: report `mode_source` (`argument`/`env`/`config`/`default`) on `spawn_agent`, `agent_status`, `list_agents` and each ledger record, so a worker that opened the wrong way says which of the four sources decided it.
- fix: stop coercing an omitted `interactive` argument to `true` in the `spawn_agent` handler — the server received an explicit mode on every call, which made the env var and the config file unreachable.
- feat: refuse every spawn when `spawn.mode` or `SUPERVISOR_SPAWN_MODE` holds an unknown value, naming the file and the valid values, rather than silently falling back. An unknown *key* only warns, so a config written for a newer version stays usable.

## v0.8.4

- chore: **Carry `homepage` and `repository` in the plugin manifest.** `plugin.json` had neither, while both sibling plugins (`vault-cli`, `dark-factory`) carry both pointing at their own repo — so a listing of this plugin had no link home. Checked against the siblings rather than assumed: `marketplace.json` carries neither in any of the three, so the gap is `plugin.json` alone and the change is two fields

## v0.8.3

- fix: **Resolve a worker's permission mode per settings tier, not from the merged value.** The guard read `filterEscalatingDefaultMode(resolveSettings(...)).permissions.defaultMode` — the merge, then the trust filter — and that pair can report a policy as *reachable* while the worker runs under one that makes the hook unreachable. `project` outranks `user`, so a project-tier `defaultMode: default` displaces a trusted tier's `auto` in `effective`; the trust filter drops an escalating mode only when its provenance says `project`, and provenance is key-level, so the displaced `default` passes through untouched. Measured 2026-09-16: with `.claude/settings.json` holding `{"permissions":{"defaultMode":"default"}}` in the worker's cwd, a live worker was auto-approved with no hook call, no `canUseTool` call, and no permission-log line — while the guard reported the mode reachable. The mode now comes from scanning `resolveSettings(...).sources` per tier, where any trusted tier (`user`/`local`/`managed`/`flag` — the tiers the filter does not strip) holding `auto` or `bypassPermissions` is decisive. Deliberately unranked by precedence among trusted tiers: ranking them would rebuild the merge reasoning the measurement just disproved, and over-reporting costs a loud refusal against a policy accepted and silently inert
- fix: **Make the policy drill resolve the mode through the server's own resolver.** `scripts/policy-drill.py` re-derived the rule in its own Node snippet and had drifted into the same merged-value read, so the instrument supplying the policy criterion's evidence was a second implementation of the thing under test. It now imports `resolveEffectiveMode` and `POLICY_UNREACHABLE_MODES` from `server/mode.mjs`. It also stops rather than picking a branch when the resolution fails — its old fallback returned stderr text as if it were a mode, which is truthy and so skipped the caller's own "could not resolve" guard
- test: Cover the tier scan in `server/mode.test.mjs`, including the configuration that fail-opened. The test asserts both halves together — that the merged value reads `default` *and* that the resolver reports `auto` — so an edit to either side is visible rather than silently agreeing

## v0.8.2

- docs: **Correct where the escalating permission mode comes from.** The `auto` that makes the policy layer inert was attributed to "the **managed** settings tier" — in the README (twice), in a `supervisor.mjs` comment, and in the v0.8.0 entry below. It comes from `~/.claude/settings.json`, the **user** tier. The tier was inferred from key-level provenance, which names the highest-precedence contributor for the whole `permissions` object: the managed drop-in contributes an `allow` entry, so `provenance.permissions` reported `managed` while `defaultMode` itself came from `user`. A per-source dump settles it — `user -> auto /Users/bborbe/.claude/settings.json`, and the managed drop-in carries no `defaultMode` at all. This earns its own release because it changes the *remedy*, not just the wording: "managed" points at a root-owned file nobody reading the README can edit, which reads as unfixable, when the setting is the operator's own and one line to change. The v0.8.0 section is left exactly as released; this entry is the correction

## v0.8.1

- fix: **Report a worker's cost only when the figure is real.** The SDK prices a turn from Anthropic's list, so under claude-code-router — where the traffic goes to whatever backend the router points at — the number describes a billing model that never ran. Measured 2026-09-13 at $0.40–$0.79 per worker against vLLM. It was previously returned with a README caveat, and a caveat does not travel: a manager reading the response sees a confident number with nothing beside it saying otherwise. `result.total_cost_usd` is now present only when the worker reached Anthropic itself — no `ANTHROPIC_BASE_URL`, or one pointing at `api.anthropic.com` — and absent otherwise, because absence travels where a caveat does not. The § Status gap is deleted rather than restated, and the rule now sits on the `agent_status` row where a reader actually looks
- docs: **Explain the post-allow status lag instead of only warning about it.** The prototype recorded a "race" — `agent_status` reading `running` right after an allow, with the session closing seconds later. Reading the state machine found no wrong state: the server marks a worker running when it *answers* the prompt, not when the SDK confirms it resumed, which is the most it can honestly know at that moment. So the § Status entry now names that cause and points at the answer already in hand (`answer_permission` returns the outcome), rather than leaving "never treat one check as final" as an unexplained rule
- test: Cover the predicate at the config boundary in all three states — unset (real), an Anthropic URL (real), a router URL (not) — and assert the URL that made it untrue is kept, so the reason is diagnosable rather than merely absent

## v0.8.0

- feat: Add a **per-spawn approval policy**. `spawn_agent({ policy: "<path>" })` gives one worker its own rules, evaluated ahead of the user and bundled files — first match wins, so a rule there beats both, while the bundled set still covers what it does not name. Overlay rather than replace, deliberately: a permissive override must not silently drop the `rm -rf` deny along with it. Full replacement stays reachable by ending the file with a `*:*` escalate catch-all, which then matches before the bundled rules get a turn. An absolute path is used as-is, a relative one resolves against the worker's cwd, and the policy is recorded on the ledger record, in `agent_status`, and on every permission-log line — so a decision mined out of the log can be traced to the file that produced it, not just to the rule text
- fix: **Refuse a policy that could never be consulted**, rather than accepting it and reporting it applied. Two cases, each an argument the server would otherwise drop silently: a tab worker answers its own prompts in its tab, so `interactive: true` + `policy` is refused — the same shape as the resume refusal in v0.7.1 — and a policy file that cannot be read is refused rather than falling back to the server default, since running under rules the caller did not choose is worse than not starting
- fix: **Report when the policy layer cannot take effect at all.** `auto` and `bypassPermissions` answer tool calls without consulting the `PermissionRequest` hook, so no rule — bundled, user, or per-spawn — is reachable. The server now resolves the worker's *effective* permission mode — an escalating `permissions.defaultMode` from a trusted settings tier wins over the `permissionMode` query option, and otherwise the option governs, so both sources are folded in via `resolveSettings` + `filterEscalatingDefaultMode` rather than a settings file read by hand — and warns at startup, and refuses a per-spawn policy under either mode. Measured 2026-09-15: `permissions.defaultMode` resolved to `auto` from the **managed** tier, and a live worker ran a non-allowlisted command, reported `success`, and produced no hook call, no `canUseTool` call and no permission-log line — the policy code was correct, unit-tested, and doing nothing. An escalating mode from a trusted tier wins over the `permissionMode` query option, so this is not fixable from the spawn
- test: Unit-test the overlay (an override rule beats a bundled one that also matches, the bundled safety net survives, a trailing catch-all reaches full replacement), the ledger field, and both refusals. Plus `scripts/policy-drill.py`, which resolves the effective mode first and asserts the behaviour that mode implies — the A/B under a reachable mode, the refusal under an unreachable one — so it stays honest on a machine where no policy can run instead of failing for a reason that has nothing to do with policy
- docs: Document the approval policy, the per-spawn override, and the hook-reachability boundary in the README, and correct the Layout block, which still labelled `server/policy.json` "NOT wired yet" four releases after it was wired — the same stale-claim class v0.7.2 swept, at the one instance that sweep did not reach

## v0.7.2

- docs: **Describe both spawn modes in the skill, the four commands, the wrangler agent and `llms.txt`**, all of which still documented the headless path as though it were the only one. `spawn_agent` has defaulted to a wezterm tab since v0.6.0, so the skill's documented loop — `spawn_agent` → `await_permission` → `answer_permission` → `list_agents` — described a sequence that **silently does nothing** for a default spawn: a tab worker answers its own prompts, never calls `canUseTool`, and so never parks a request for `await_permission` to return. The skill now leads with the mode choice and states the trap outright; `/supervisor:spawn` says which mode it opened and why the default is not the supervised one; `/supervisor:workers` states that an empty pending list says nothing about a blocked tab worker, and that a headless worker is invisible to the session roster even though `list_agents` shows it; `/supervisor:answer` and `/supervisor:drain` state that they serve headless workers only. The `worker-wrangler` agent had the same gap — its loop keyed on `running`, while a tab worker reports `interactive`. Three stale claims went with it: the 15-minute auto-deny, "a worker session is single-shot", and the README's own Status list, which named `send_to_agent` as missing twice, one bullet after documenting `send_agent_message`

## v0.7.1

- fix: **Refuse a resume a tab worker cannot honour**, instead of accepting it and dropping it. `spawn_agent({ interactive: true, resume })` ran the two-writer guard — doing real work, refusing a live session and failing closed on an unreadable registry — and then discarded the id it had just guarded, because `resume` only reaches the SDK query on the headless branch and the tab path launches the `cc-*` launcher, which is never handed the flag. The caller got a **fresh** conversation while believing it was continuing one. The refusal happens before the liveness probe, since there is no point guarding an argument the tab path would drop anyway, and it names the alternative (`interactive:false`) so the caller can act rather than guess
- test: Assert the refusal exists at all — the failure being prevented is a silent drop, so the test is that something refuses, not merely that the message reads well — plus `scripts/resume-tab-drill.py`, which exercises **both** resume refusals through the real tool and shows they are distinct: a tab worker is refused because it cannot honour the flag, a headless worker because the session is still running. A plain tab worker with no resume is unaffected
- fix: Resolve the repo root in **all four drill scripts** from the script's own location instead of a hardcoded home path. A hardcoded path works on exactly one machine and breaks silently everywhere else — the same class as the `/tmp` log path this repo already fixed once. Caught by review of #9, and it applied to three scripts that had already merged, so the whole class is fixed rather than the one instance the reviewer could see

## v0.7.0

- feat: Record every spawned worker to a **durable ledger** at `~/.local/state/claude-supervisor/sessions/<uuid>.json`, keyed by the session uuid and written at spawn so a long-running worker is recorded while it is still running. It closes the gap the other two stores leave: Claude Code's live registry is keyed by pid and **deleted when the session exits** (measured: 13 entries against 13 live processes, zero stale), so it forgets a session exactly when a record would first be useful, and the transcript holds only the conversation — neither says who started the session, in what mode, from which manager, or how it ended. `SUPERVISOR_LEDGER_DIR` overrides the location; deliberately not `SUPERVISOR_SESSIONS_DIR`, which already means the live registry
- feat: Resolve a **tab worker's session id** from the registry by the tab name, since a tab worker is a separate process whose id is never reported to the SDK caller the way a headless worker's is. Polled rather than assumed — the session registers about a second after the pane opens — and now surfaced as `session_id` on the spawn response, which previously could not tell you which conversation the worker you just started was in
- feat: Record the **spawn edge**. The server's own parent pid is the MCP client — the manager session that called `spawn_agent` — and the live registry maps that pid to a session id, so the answer is stamped into a record that outlives the registry entry it came from. A record that cannot be written is logged rather than swallowed, and a worker whose session id never resolved gets no record at all rather than one filed under a key nothing would look up
- test: Unit-test the ledger, including the merge (a completion patch must not erase the spawn fields — they arrive minutes apart from different call sites) and the atomic write (a reader must never catch a half-written record and read it as a session with no fields). Plus `scripts/ledger-drill.py`, which spawns both modes in one run and checks the records **on disk**: keyed by uuid, outcome written, edge resolved against the real registry, and a record still readable after its session's registry entry is gone

## v0.6.0

- fix: Send the worker colour as **its own message** instead of seeding it into the prompt, where it never worked. Claude Code parses one submitted message as one command, and `/color` takes the *entire trimmed argument* — so `/color pink\n\n<task>` validated as `Invalid color "pink\n\n<task>"`, the colour command swallowing the blank line and the whole task. Verified against a live worker and against the CLI's own implementation, which trims the whole argument. The spawn response now reports `color: {applied: true}` only when the colour actually landed, rather than that we asked for it
- feat: Add `send_agent_message(agent_id, message)` — the `send_to_agent` this server never had. It types a follow-up into a running **tab** worker and submits it, so a worker that has gone wrong can be corrected and one that has stalled can be nudged. Measured rather than assumed: `send-text` reaches a pane only once its tab is **activated** (two attempts against a live pane silently did nothing until then), readiness is the `❯` input glyph rather than a fixed sleep, and a terminal's Enter is `\r` not `\n`. A headless worker is refused rather than attempted — it has no pane, and typing into one that does not exist is how a channel reports success while delivering nothing
- test: Unit-test the tab channel, including that activation precedes the send — asserted on call order, because an unactivated send appears to succeed and delivers nothing. Plus `scripts/channel-drill.py`, which proves both halves end to end against a real worker: colour applied and follow-up acted on, both read from the worker's own transcript rather than the server's self-report
- docs: Document the tab channel and its traps, and correct the tool list, which still described `spawn_agent` as "one `query()` session" after tab mode became the default. Record `transcript_dir` as unreliable for a tab worker — it is derived from the `cwd` you passed, but the `cc-*` launcher `cd`s into its own vault, so a `/tmp` worker writes its transcript in the Personal vault

## v0.5.2

- test: Commit `server/fork-probe.mjs`, the measurement of what `forkSession` and `resumeSessionAt` actually do to a session id — kept in the repo so it can be re-run rather than believed. `resume` alone returns **the same id** (continues), `resume` + `forkSession: true` returns **a new one** (forks), and `resumeSessionAt` returns **the same id** (continues). So forking is the only one of the three that does, and the only way to produce the `continued: false` that `agent_status` reports. The first run reported nothing for `resumeSessionAt` and read as "unsupported by the SDK"; the cause was this probe resolving the transcript path from the *unresolved* cwd while `/tmp` symlinks to `/private/tmp` — the trap `transcriptDirFor()` already documents. Worth recording because the wrong conclusion was available and plausible: the measurement was broken, not the SDK. Manual by design — it makes real model calls through claude-code-router, so it is not part of `make test`

## v0.5.1

- refactor: Move every `process.env` read into `server/config.mjs`, so the configuration surface is enumerable in one place. RULE node/config/env-read-at-boundary is a MUST, and review of the liveness guard caught `liveness.mjs` — a library module, not a bootstrap — reading two vars at module level. Fixing only those two would have missed the rule's point: eleven more reads sat in the entrypoint, which the rule exempts but which was hiding the same surface. Values resolve once at load and are frozen, so callers inject instead of depending on ambient process state
- test: Fail the build if any server module but `config.mjs` reads `process.env`, including one added later — a rule obeyed once is not enforced. The scan strips comments first, because the files documenting the rule mention `process.env` by name and a naive substring check flags them; a second test guards the scan itself, so a broken comment-stripper cannot make the check pass or fail for the wrong reason
- test: Commit the refusal drill as `scripts/refusal-drill.py`, so the end-to-end evidence is reproducible instead of a one-off. It drives the built server over stdio, spawns a throwaway worker, and tries to resume it while it is live. **Measured on the same session at the same instant:** `pgrep -fl` alone found nothing and would have allowed the resume, the registry probe reported it live, and the tool refused with a clear error — which is the guard's whole purpose demonstrated against the case that used to slip through

## v0.5.0

- fix: Detect a running session by the **session registry** (`~/.claude/sessions/<pid>.json`), not by `pgrep` alone. `pgrep -fl <id>` matches a process's command line, so it only ever found sessions launched as `claude --resume <id>` — a session started *fresh* carries its id nowhere in argv and read as closed, which is exactly the resume the guard exists to refuse. The registry is keyed by pid and names the session it belongs to, so it sees a fresh session; `pgrep` is kept as the second probe for processes the registry does not list, and every registry hit is confirmed against the pid so a file left by a crashed session cannot read as live forever. Verified both directions against a live session the old probe could not see
- fix: Fail **closed** when neither probe can be read, rather than open. "Could not confirm the session is closed" and "confirmed closed" are different answers and only one is safe to resume; the refusal names the reason and points at `SUPERVISOR_SESSIONS_DIR`. The precedent was this guard shipping as a silent fail-open, which is not a guard
- feat: Report `resumed_from` and `continued` in `agent_status` and `list_agents`, so an adoption is never mistaken for a fresh start. `resumed_from` names the conversation being continued, and `continued` says whether the id came back the same (continued) or different (forked) — `session_id` alone cannot express this, since a resume that continues reports the original id
- docs: Correct the README, which called the two-writer guard "a warning in the tool description, not an enforced guard" in the very commit that made it enforced, and document both probes plus how to read which conversation an adoption landed in
- test: Unit-test the liveness probes, including the regression that pgrep alone misses a fresh session, the stale-registry case, and "could not tell" being distinct from "closed"
- chore: Syntax-check every server module rather than `supervisor.mjs` alone, so a new file cannot land unchecked

## v0.4.0

- feat: Spawn workers as real sessions in a wezterm tab **by default**, so they come up with the same tooling a normal session has — launcher env, plugin skills, MCP servers, settings.json permissions — and can be watched and driven by hand. Pass `interactive:false` for the headless path the manager supervises
- feat: Resolve the `cc-*` launcher from `vault-cli config` instead of invoking the bare `claude` binary, which routes around the router, the MCP config and the model selection
- feat: Load `user`/`project`/`local` settings in headless workers, which previously started from nothing
- feat: Refuse to resume a session that is **still running**, rather than putting two writers on one conversation. Uses the same `pgrep -fl` probe `/open` uses, which catches both a tab session and a headless `--print` run no pane would show. If the probe cannot run it fails open **loudly**, since an unguarded resume can corrupt a conversation. Verified both directions: a live id is refused, a closed one allowed. Documented: adopting an existing session (README)
- feat: Accept a `resume` session id, so the manager can adopt a **closed** session and supervise it. The session must be closed — resuming a live one puts two writers on one conversation. This reaches what the permission channel exists for, without a worker-side plugin: a session you did not create cannot be supervised, but one you resume you *do* create. **Verified**: a session killed hours earlier was resumed by the manager, recalled its own prior task unprompted, parked a `Write` prompt for the manager (`path is outside allowed working directories`), and completed on approval — same session id, not forked
- feat: Hand the launcher's `--mcp-config` servers to the SDK query, closing the last gap between the two modes — those servers arrive as a CLI flag, not as settings, so `settingSources` could not reach them. **Verified**: a headless worker now reports 12 MCP prefixes, matching the tab worker's set of enabled servers, where it previously reported 0 then 7
- feat: Prefix managed worker tab titles with a marker so they are identifiable in the tab bar and the fleet roster, where they are otherwise indistinguishable from a human session
- feat: Run a colour command before each worker's task, configurable via `SUPERVISOR_WORKER_COLOR` (`off` disables)

## v0.3.0

- feat: Wire `policy.json` through a `PermissionRequest` hook — `allow`/`deny` answer the worker directly, and anything unmatched defers to `canUseTool` so it still parks for the manager
- feat: Record every permission request and the manager's verdict as JSONL (`SUPERVISOR_PERMISSION_LOG`), so the policy can be grown from decisions actually made instead of guessed
- feat: Read config from XDG paths — `~/.config/claude-supervisor/policy.json` overlays the bundled rules, `~/.local/state/claude-supervisor/permissions.jsonl` holds the log — so editing your own policy never dirties the checkout
- fix: Read the server version from `plugin.json` instead of a hardcoded literal, which drifted a release behind and was invisible to the version check
- fix: Compare the `cwd` rule on a path boundary rather than a string prefix — `/work/repo-2/x` started with `/work/repo` and was wrongly allowed as inside it
- fix: Report unreadable or malformed policy files and failing log writes instead of swallowing them, so ignored rules and unrecorded decisions are visible
- fix: Validate `SUPERVISOR_PERMISSION_MODE` once at load and reject unknown values, rather than silently falling through to the SDK default
- test: Unit-test the policy decision path (allow / deny / escalate, `cwd`, first-match ordering); `make test` now runs them
- chore: Extract the pure policy evaluation into `server/policy.mjs` so it can be tested without importing the server
- chore: Commit the bun lockfile so the server's dependencies install reproducibly

## v0.2.0

- fix: Stop writing the log to a hardcoded prototype path; the file log is now opt-in via SUPERVISOR_LOG and stderr is the default
- docs: Correct the README layout block to the shipped flat plugin layout
- docs: Drop the stale "not yet audited" gap and name the marketplace owner in the install snippet

## v0.1.0

- feat: Add supervisor MCP server that spawns worker sessions and parks their permission prompts for the manager to answer
- feat: Add spawn, workers, answer and drain slash commands for the operator surface
- feat: Add worker-wrangler agent running the routine approval loop on a cheap model
- feat: Add supervising-workers skill documenting when to spawn a worker instead of working in the manager session
- docs: Document the escalation chain, the tool list and the known gaps in the README
