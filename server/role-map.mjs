// Which window and which chip a spawn gets, resolved from the task's ROLE rather than
// from a window id carried across the tool boundary.
//
// Split out of supervisor.mjs for the usual reason (importing supervisor.mjs starts the
// MCP server), and it exists because passing a window id as a tool argument is not
// reliable. Measured 2026-09-20 across two runs, `window_id: 0` reached the server four
// times out of six; the two failures silently inherited the caller's window instead.
//
// Nothing else in the chain is at fault, and each link was checked rather than assumed:
//
//   wezterm cli spawn --window-id 0   -> window 0, six times out of six
//   window_id: 1 (a number)           -> routed correctly
//   interactive: false                -> survived the same boundary intact
//   supervisor.mjs:930                -> `request.params.arguments ?? {}`, no filtering
//
// Both failures were the first spawn of their run. That is a LEAD, not a mechanism — n=2,
// and no reproducible trigger was found, so nothing here claims to explain it. This module
// does not try to fix that boundary either. It removes the crossing: a role is a WORD
// ("manager", "agent", "human"), never a number, so nothing can be dropped as falsy or
// mistyped on the way in, and the window id is looked up in-process at the moment of spawn.
//
// Nothing here reads the filesystem, the environment, or the clock: the caller reads the
// map and passes it in, the same way spawn-mode.mjs takes the config file rather than
// opening it.

export const ROLES = ['manager', 'agent', 'human']

// `role:` absent means agent, and that is the documented default — it preserves the
// behaviour of every task that has not declared a role, which is all but `Start Day`.
export const DEFAULT_ROLE = 'agent'

// Role names are the task frontmatter's vocabulary; purposes are the window names in
// `window_specs`. They are deliberately different vocabularies — a window named "Direct"
// serves the role "human" — so this is the one place that translates between them, rather
// than each consumer re-deriving the pairing.
export const ROLE_PURPOSES = { manager: 'Managers', agent: 'Agents', human: 'Direct' }

// A HUMAN session's chip follows the VAULT FAMILY, not the role alone. The operator's
// convention is green = Seibert, cyan = private, and colour is the only at-a-glance cue for
// which employer a tab belongs to — so a Seibert tab that comes up cyan reads as a private one.
//
// This is where the SERVER path derives it. It cannot live in the WezTerm config: `window_specs`
// publishes `purpose -> {chip, window_id}`, a purpose is 1:1 with a window, and WezTerm knows
// windows rather than vaults — the vault is known only at spawn time. So the map stays the
// source for `window_id` and for the fallback chip, and this module owns the family split.
//
// ⚠️ `commands/open.md` § 3.0 carries the same table for the shell path — the resume branch and
// the no-supervisor-MCP fallback — because a bash caller cannot import this module. Those two
// are a hand-kept pair, not one source: change one, change the other.
export const FAMILY_CHIPS = { seibert: 'green', private: 'cyan' }

// The family is the prefix before the first hyphen: `seibert-brogrammers` -> `seibert`.
// A vault with no hyphen is its own family, which simply misses the table and falls back.
export function vaultFamily(vault) {
  if (typeof vault !== 'string') return null
  const name = vault.trim()
  if (name === '') return null
  const at = name.indexOf('-')
  return at === -1 ? name : name.slice(0, at)
}

// The chip a session carries in this vault, or `fallback` when the family is not named above.
// The fallback is deliberate and load-bearing: `assistant-*` vaults exist, the operator's
// convention covers only seibert and private, and an unlisted family must keep the colour the
// map already gives it rather than silently picking one of the two.
export function familyChip({ vault, fallback = null } = {}) {
  // `Object.hasOwn`, not a bare lookup: a family named `constructor` or `toString` would
  // otherwise return an inherited member instead of the documented fallback, and that value
  // reaches the pane as a `/color …` argument.
  const family = vaultFamily(vault)
  return Object.hasOwn(FAMILY_CHIPS, family) ? FAMILY_CHIPS[family] : fallback
}

// The chip a spawn actually applies: the role's own chip, refined by the vault family for a
// human session. The composition lives here rather than at the call site so the test exercises
// the same function the spawn path does — a test that re-derives the composition proves only
// that its own copy works.
//
// A degraded resolution (`resolved: false`, `chip: null`) is passed through untouched: the
// family must not manufacture a chip for a role the map could not resolve at all.
export function chipForRole({ resolution, vault } = {}) {
  if (!resolution) return null
  if (resolution.role !== 'human' || !resolution.resolved) return resolution.chip ?? null
  return familyChip({ vault, fallback: resolution.chip })
}

// Resolve a role against an already-parsed role map.
//
// Returns `{ role, purpose, chip, windowId, resolved, warning }` on success, or
// `{ error }` when the role itself is not one we know.
//
// The two failure modes are deliberately NOT the same kind of failure:
//
//   - an unknown ROLE is refused. It is a caller mistake, and a worker opened in the wrong
//     window with the wrong colour is discovered only by noticing it — the repo's
//     convention is to refuse rather than guess (see spawn-mode.mjs).
//   - an unusable MAP is degraded, not refused. The map publishes on WezTerm's reconcile
//     tick, and a headless worker has no tab at all, so an absent map must never block a
//     spawn that never needed a window. It returns `resolved: false` plus a warning so the
//     caller can fall back and SAY SO — a spawn that looks routed and is not is worse than
//     one that plainly is not.
export function resolveRole({ role, map } = {}) {
  const wanted = role === undefined || role === null || role === '' ? DEFAULT_ROLE : role

  if (!ROLES.includes(wanted)) {
    return {
      error:
        `${JSON.stringify(role)} is not a role — refusing to spawn rather than guessing, since a worker ` +
        `opened in the wrong window with the wrong colour is discovered only by noticing it. ` +
        `Valid values: ${ROLES.join(', ')} (or omit the argument for "${DEFAULT_ROLE}").`,
    }
  }

  const purpose = ROLE_PURPOSES[wanted]
  const entry = map && typeof map === 'object' ? map[purpose] : undefined

  if (!entry || typeof entry !== 'object' || entry.window_id === undefined || entry.window_id === null) {
    return {
      role: wanted,
      purpose,
      chip: null,
      windowId: null,
      resolved: false,
      warning:
        `role "${wanted}" did not resolve: the role map has no usable "${purpose}" entry. ` +
        `Falling back to the caller's explicit window and colour.`,
    }
  }

  // `entry.window_id` is passed through UNCHANGED, including its type. The map writes ids
  // as JSON numbers and that is exactly what spawnInteractiveAgent wants — a value that
  // never left this process. Coercing it to a string here would reintroduce the round-trip
  // this module exists to delete.
  return {
    role: wanted,
    purpose,
    chip: entry.chip ?? null,
    windowId: entry.window_id,
    resolved: true,
    warning: null,
  }
}
