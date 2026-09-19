# Changelog

All notable changes to this project will be documented in this file.

Please choose versions by [Semantic Versioning](http://semver.org/).

* MAJOR version when you make incompatible API changes,
* MINOR version when you add functionality in a backwards-compatible manner, and
* PATCH version when you make backwards-compatible bug fixes.

## Unreleased

- fix: `worker-manager` starts a worker by spawning it with the work command as the `prompt`
  argument, so the worker creates its own session in its own pane. Minting the session first
  ran a full planning turn inside the manager's own session — blocking it for minutes and
  making the manager do the worker's work. `docs/fleet-surface.md` § Spawn a worker now
  carries the fresh-start path, and resolves `interactive` from the fleet's config file
  (`SUPERVISOR_SPAWN_MODE` → `spawn.mode` → built-in) instead of presenting the built-in code
  default as the answer.

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
