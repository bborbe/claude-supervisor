# Changelog

All notable changes to this project will be documented in this file.

Please choose versions by [Semantic Versioning](http://semver.org/).

* MAJOR version when you make incompatible API changes,
* MINOR version when you add functionality in a backwards-compatible manner, and
* PATCH version when you make backwards-compatible bug fixes.

## Unreleased

- feat: Spawn workers as real sessions in a wezterm tab **by default**, so they come up with the same tooling a normal session has — launcher env, plugin skills, MCP servers, settings.json permissions — and can be watched and driven by hand. Pass `interactive:false` for the headless path the manager supervises
- feat: Resolve the `cc-*` launcher from `vault-cli config` instead of invoking the bare `claude` binary, which routes around the router, the MCP config and the model selection
- feat: Load `user`/`project`/`local` settings in headless workers, which previously started from nothing
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
