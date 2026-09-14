# Changelog

All notable changes to this project will be documented in this file.

Please choose versions by [Semantic Versioning](http://semver.org/).

* MAJOR version when you make incompatible API changes,
* MINOR version when you add functionality in a backwards-compatible manner, and
* PATCH version when you make backwards-compatible bug fixes.

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
