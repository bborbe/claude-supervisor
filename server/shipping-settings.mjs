// The settings a SHIPPING worker is spawned with — and nothing else.
//
// Two carriers, one mode. A HEADLESS worker gets `shippingSettings` through the SDK's
// `settings` query option. A TAB worker cannot: its path builds a shell command line
// and never reaches that option, so the same mode travels as an exported
// `PERMISSION_MODE` the launcher already reads (`shippingTabEnv` below).
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
// only with that sentence in mind. It is also wider than it reads: `git push` takes
// a remote helper, so `git push "ext::sh -c '<cmd>'"` or `--receive-pack=<cmd>`
// matches the prefix and runs arbitrary shell. Treat the entry as shell access.
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

// The tab path's carrier for the same mode, or `null` when the spawn is not shipping.
//
// ⚠️ A TAB worker cannot use `shippingSettings` above. The tab path builds a shell
// command line (`spawnInteractiveAgent`) and never reaches the SDK's `settings` query
// option, so an object has nowhere to land. The launchers already expose the seam —
// they pass `--permission-mode "${PERMISSION_MODE}"` — so the mode travels as an
// exported variable that `shellEnvExports` writes onto the tab's command line.
//
// ⚠️ This works only against a launcher that DEFAULTS the variable rather than
// assigning it. Every launcher used to `export PERMISSION_MODE="auto"` unconditionally,
// which overwrites whatever the spawn set — accepted and quietly ignored, the exact
// failure class this module's refusals exist to prevent. `launcherHonoursModeEnv`
// checks for the defaulting form, and `shippingSupportError` refuses a launcher that
// lacks it rather than trusting one.
export const SHIPPING_MODE_ENV = 'PERMISSION_MODE'

export function shippingTabEnv(isShipping) {
  if (isShipping !== true) return null
  return Object.freeze({ [SHIPPING_MODE_ENV]: SHIPPING_PERMISSION_MODE })
}

// Whether a launcher script PRESERVES an exported `PERMISSION_MODE` instead of
// overwriting it. Takes the script's text and reads no file, so the positive and the
// negative control are both testable without a filesystem.
//
// The shape it looks for is the shell's default-parameter expansion. A plain
// `export PERMISSION_MODE="auto"` — the form every launcher carried before this
// change — assigns unconditionally and outranks the spawn's value.
export function launcherHonoursModeEnv(scriptText) {
  return typeof scriptText === 'string' && /\$\{PERMISSION_MODE:-/.test(scriptText)
}

// The refusal for a spawn that cannot honour `shipping`, or `null`.
//
// Same reasoning and the same class of bug as `policySupportError` in `tab.mjs`: an
// argument the spawn path cannot apply is REFUSED, never accepted and quietly
// ignored. A cluster worker is started by another service and has no local
// environment to set, so there `shipping: true` would read as granted while the
// worker still parked on edits.
//
// ⚠️ The TAB path IS supported now, but only through a launcher that honours the env
// var. A launcher assigning `PERMISSION_MODE` unconditionally would overwrite the
// spawn's value and the worker would park on the very gate this argument exists to
// remove — so that case is refused here, at spawn time, rather than discovered later
// in a pane. Pass `launcherHonoursModeEnv: true` only after reading the launcher.
export function shippingSupportError({ shipping, interactive, cluster, launcherHonoursModeEnv }) {
  if (shipping !== true) return null
  if (cluster) return 'shipping reaches a local worker only — the cluster path cannot apply its settings. Omit shipping, or spawn locally.'
  if (interactive && launcherHonoursModeEnv !== true) {
    return (
      `shipping on a tab worker needs a launcher that preserves an exported ${SHIPPING_MODE_ENV} ` +
      `(the \`\${${SHIPPING_MODE_ENV}:-auto}\` form). This launcher assigns it unconditionally, so the ` +
      `mode would be silently overwritten and the worker would still park on edits. Update the ` +
      `launcher, or spawn with interactive:false.`
    )
  }
  return null
}
