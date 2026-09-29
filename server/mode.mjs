// Where a worker's effective permission mode comes from, split out of supervisor.mjs so
// it can be tested without importing the server (importing supervisor.mjs starts the MCP
// server, which makes every function in it untestable in-process).
//
// Nothing here reads the filesystem, the environment, or the clock.

import { filterEscalatingDefaultMode } from '@anthropic-ai/claude-agent-sdk'

// Modes under which the PermissionRequest hook is never consulted, so every rule in
// policy.json — and any per-spawn override — is unreachable. `auto` is the case the
// README has warned about since v0.3.0; `bypassPermissions` is the SDK's own wording
// ("auto-approves every tool call … before the callback is consulted"). `acceptEdits` is
// deliberately NOT here: it auto-accepts edits and still prompts for everything else, so
// the policy stays reachable for non-edit tools.
export const POLICY_UNREACHABLE_MODES = ['auto', 'bypassPermissions']

// The tiers the SDK's trust filter leaves alone. It drops an escalating defaultMode set
// by `project` — a repo-committed file nobody vetted — and keeps every other tier's. A
// mode from one of these is therefore decisive; a mode from `project` is advisory.
export const TRUSTED_SETTING_TIERS = ['user', 'local', 'managed', 'flag']

// The escalating mode a worker will actually run under, read PER TIER rather than from
// the merged result. That distinction is the whole point of this function.
//
// The merged result is not a safe answer here, for two compounding reasons:
//
//   1. Precedence is not what decides. `project` outranks `user`, so a project-tier
//      `defaultMode: default` overwrites a user-tier `auto` in `effective` — while the
//      live worker still ran under `auto`. Measured 2026-09-16: with
//      `.claude/settings.json` holding `{"permissions":{"defaultMode":"default"}}` in the
//      worker's cwd, the command was auto-approved and neither the hook nor canUseTool
//      was called. The project tier IS read — a `permissions.deny: ["Bash"]` there
//      blocked Bash outright — it just does not win for `defaultMode`.
//   2. The trust filter cannot rescue it. `filterEscalatingDefaultMode` drops an
//      escalating mode only when provenance says `project`, and provenance is key-level:
//      `provenance.permissions` names the highest-precedence contributor to the whole
//      `permissions` object, not to `defaultMode` within it. So a project-tier `default`
//      that displaced a user-tier `auto` leaves `effective` reading `default` — not
//      escalating — and the filter passes it through untouched.
//
// Together those let the merged value report a REACHABLE policy while the worker runs
// under one that makes the hook unreachable: a fail-open in the guard whose whole job is
// to catch exactly that. Scanning the tiers cannot be fooled that way.
//
// Deliberately not ordered by precedence among the trusted tiers either — ranking them
// would rebuild the merge reasoning that (1) just disproved. Any trusted tier holding an
// unreachable mode reports it, which can over-report when a higher-precedence trusted
// tier disagrees. That is the safe direction: a loud refusal on a worker that might have
// been fine, against a policy accepted and then silently inert.
export function escalatingModeFromTrustedTier(resolved) {
  for (const entry of resolved?.sources ?? []) {
    if (!TRUSTED_SETTING_TIERS.includes(entry?.source)) continue
    const mode = entry?.settings?.permissions?.defaultMode
    if (POLICY_UNREACHABLE_MODES.includes(mode)) return mode
  }
  return null
}

// The permission mode a worker will ACTUALLY run under. Two sources, and which one wins
// is not obvious: an escalating `permissions.defaultMode` from a trusted settings tier
// beats the `permissionMode` query option, and otherwise the option is what governs.
// Reporting only the settings value would miss `SUPERVISOR_PERMISSION_MODE=auto`, which
// makes the policy exactly as unreachable as a settings `auto` does.
//
// `resolved` is null when `resolveSettings` threw. That path keeps the old answer — the
// option we send — because the caller has already logged the uncertainty, and inventing
// an unreachable mode from a failed lookup would refuse every spawn on an @alpha hiccup.
export function resolveEffectiveMode({ resolved, optionMode }) {
  if (!resolved) return optionMode
  const fromTrustedTier = escalatingModeFromTrustedTier(resolved)
  if (fromTrustedTier) return fromTrustedTier
  const fromSettings = filterEscalatingDefaultMode(resolved).permissions?.defaultMode ?? null
  return POLICY_UNREACHABLE_MODES.includes(fromSettings) ? fromSettings : optionMode
}
