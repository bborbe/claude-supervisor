// The settings a SHIPPING headless worker is spawned with — and nothing else.
//
// Why this exists. A headless worker's edits and commits travel the supervisor's
// permission channel: `permissionMode: 'default'` sends every approval-requiring
// tool to the `PermissionRequest` hook and then to `canUseTool`. When that channel
// dies — measured 2026-10-05, a worker whose every Bash/Edit returned
// `Tool permission request failed: AbortError: Stream closed` — the worker cannot
// edit or commit at all, and it dies silently. A worker whose job IS to ship
// should not be able to fail that way.
//
// ⚠️ `acceptEdits` is the mode precisely BECAUSE it is not one of
// `POLICY_UNREACHABLE_MODES` (see `mode.mjs`): it auto-accepts edits and still
// prompts for everything else, so every other rule in `policy.json` stays
// reachable. It buys autonomy on edits and the three git verbs, not on the fleet.
// `bypassPermissions` would have been simpler and would have switched supervision
// off for the workers most able to do damage.
//
// ⚠️ `Bash(git push:*)` is in the allowlist, so a shipping worker can push without
// the channel. That is the point of the class, and it is also its blast radius:
// this list is the whole of what a shipping worker may do unattended, so widen it
// only with that sentence in mind.
//
// ⚠️ The tier this lands in is NOT arbitrary. `mode.mjs` trusts
// `['user', 'local', 'managed', 'flag']` and the SDK's `filterEscalatingDefaultMode`
// DROPS an escalating mode whose provenance is `project`. A repo-committed
// `.claude/settings.json` would therefore be filtered out and the worker would run
// on `default` while every reader believed otherwise — a fail-open in the guard
// whose whole job is to catch exactly that. They are delivered through the SDK's
// `settings` query option, which loads into the `flag` tier (sdk.d.ts: "loaded into
// the 'flag settings' layer") — a trusted tier, and no file written anywhere.
//
// Pure by design, like `spawn-cwd.mjs`: no filesystem, no environment, no clock, so
// the negative control (a non-shipping spawn carries none of this) is testable
// without starting a server.

export const SHIPPING_PERMISSION_MODE = 'acceptEdits'

// Exactly the verbs a shipping worker needs when the channel is down: stage, commit,
// push. Nothing broader — see the blast-radius note above.
export const SHIPPING_ALLOW = Object.freeze([
  'Bash(git add:*)',
  'Bash(git commit:*)',
  'Bash(git push:*)',
])

// The settings object for a spawn, or `null` when the spawn is not shipping.
//
// ⚠️ `null` rather than an empty object. An empty `permissions` block would still
// be a present field, and `mode.mjs` reads tiers individually precisely because a
// present-but-inert value is indistinguishable from a decisive one downstream.
// Absent is the honest shape for "this worker gets nothing extra".
export function shippingSettings(isShipping) {
  if (isShipping !== true) return null
  return Object.freeze({
    permissions: Object.freeze({
      defaultMode: SHIPPING_PERMISSION_MODE,
      allow: Object.freeze([...SHIPPING_ALLOW]),
    }),
  })
}

// The refusal for a spawn that cannot honour `shipping`, or `null`.
//
// Same reasoning and the same class of bug as `policySupportError` in `tab.mjs`: an
// argument the spawn path cannot apply is REFUSED, never accepted and quietly
// ignored. A tab worker answers its own prompts in its tab and loads its own
// settings, and a cluster worker is started by another service — in both, a
// `shipping: true` would read as granted while the worker still parked on edits.
export function shippingSupportError({ shipping, interactive, cluster }) {
  if (shipping !== true) return null
  if (cluster) return 'shipping reaches a local headless worker only — the cluster path cannot apply its settings. Omit shipping, or spawn locally with interactive:false.'
  if (interactive) return 'shipping reaches a headless worker only — a tab worker loads its own settings and answers its own prompts, so the shipping settings would never apply. Pass interactive:false, or omit shipping.'
  return null
}
