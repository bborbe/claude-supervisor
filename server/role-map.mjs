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
