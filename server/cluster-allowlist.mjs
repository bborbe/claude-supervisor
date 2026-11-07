// Which clusters each vault's workers may run on — the file, and the live read of it.
//
// The rule this serves (operator, 2026-10-20): "seibert worker should not run on nuke-dev|prod
// and trading / personal worker should not run on octopus-dev|staging|prod". A worker on the
// wrong side of that boundary is a data boundary breach, not a mistake that can be retried, so
// every branch here refuses rather than defaulting — the same standing rule `spawn-cwd.mjs`
// states for the launcher and `spawn-mode.mjs` for the mode and target.
//
// ⚠️ THIS IS NOT `config-reload.mjs`, AND THE DIFFERENCE IS DELIBERATE. That module is scoped by
// its own header to the SPAWN LIMITS, and it says moving another key there "is a separate
// decision, not a free consequence of this one". It also keeps the last good value when the file
// vanishes, which is right for a limit the operator is mid-edit and wrong for an allowlist whose
// absence must never read as "no restriction". So this is a sibling reader with its own
// fail-closed semantics, not a reuse.
//
// ⚠️ A MISSING FILE AT BOOT REFUSES; A MISSING FILE MID-RUN KEEPS THE LAST GOOD VALUE. The two
// halves look contradictory and are not. Editors write via rename, so the path is briefly absent
// mid-write — reading that absence as "no allowlist" would turn a write race into a fail-open
// guard at exactly the moment the operator is editing it. But a reader that has never had a good
// value has nothing to keep, and inventing one ("no vaults are allowed", "all are") is a guess.
// So the rule is: never had a value + file absent = refuse; had one + file briefly absent = keep.
//
// ⚠️ A MALFORMED FILE REFUSES RATHER THAN FALLING BACK to the last good value. A limit that fails
// to parse can safely keep yesterday's number; an allowlist that fails to parse must not keep
// yesterday's permissions, because the operator's edit may have been a NARROWING and keeping the
// old value would silently widen it.

import { readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { config } from './config.mjs'

// The file the operator edits.
//
// ⚠️ Resolved from `config.configDir` and NOT from `process.env` here: `config.test.mjs` pins that
// no module but `config.mjs` reads the environment, so the whole configuration surface stays
// enumerable in one place. The path is an argument on every reader below, which is what the tests
// use to point at a temp file — an env override would add a second, less visible way to redirect
// a data boundary.
export const ALLOWLIST_FILE = join(config.configDir, 'vaults.json')

// The documented shape is `{ "vaults": { "<vault>": ["<cluster>", …] } }`.
//
// Validated here rather than trusted, because a shape error read leniently is a permission
// change: `{"vaults": ["nukedev"]}` (a list where a map belongs) would otherwise parse to
// "no vault declares anything" and refuse everything, while a missing `vaults` key would be
// indistinguishable from an empty allowlist. Both are reported by name instead.
export function parseAllowlist(parsed, path = ALLOWLIST_FILE) {
  if (parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {
      allowlist: null,
      error:
        `${path} must hold a JSON object with a "vaults" map, e.g. ` +
        '{"vaults": {"private-personal": ["nukedev"]}}.',
    }
  }
  const vaults = parsed.vaults
  if (vaults == null || typeof vaults !== 'object' || Array.isArray(vaults)) {
    return {
      allowlist: null,
      error:
        `${path} has no "vaults" map, so no vault declares which clusters it may use — ` +
        'refusing rather than reading that as "no vault may use any".',
    }
  }
  const allowlist = {}
  for (const [vault, clusters] of Object.entries(vaults)) {
    if (!Array.isArray(clusters) || clusters.some((c) => typeof c !== 'string' || c.trim() === '')) {
      return {
        allowlist: null,
        error:
          `${path} maps vault ${JSON.stringify(vault)} to ${JSON.stringify(clusters)}, which is not ` +
          'a list of cluster names — refusing rather than guessing what was meant.',
      }
    }
    allowlist[vault] = clusters.map((c) => c.trim())
  }
  return { allowlist, error: null }
}

// The file's contents, or an error. Absence is an ERROR here, not silence — see the header.
export const readAllowlistFile = (path = ALLOWLIST_FILE) => {
  let raw
  try {
    raw = readFileSync(path, 'utf8')
  } catch (error) {
    return {
      allowlist: null,
      error:
        error.code === 'ENOENT'
          ? `${path} does not exist, so no vault declares which clusters it may use — ` +
            'refusing to spawn rather than running without the boundary.'
          : `cannot read ${path}: ${error.message}`,
    }
  }
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch (error) {
    return { allowlist: null, error: `${path} is not valid JSON: ${error.message}` }
  }
  return parseAllowlist(parsed, path)
}

// The live allowlist — the value a spawn decides against.
//
// Re-read when the mtime moves, so an edit reaches the next spawn without a `/mcp` Reconnect;
// cached when it has not, so the hot path costs one `stat`. Read per spawn rather than captured
// at boot for the reason `config-reload.mjs` gives: a spawn decided against permissions read
// minutes earlier is a decision against a file that no longer exists.
export function mtimeReloadedAllowlist({ path = ALLOWLIST_FILE, read = readAllowlistFile, stat = statSync } = {}) {
  let mtimeMs = null
  let cached = null

  const mtimeOf = () => {
    try {
      return stat(path).mtimeMs
    } catch {
      return null
    }
  }

  return {
    current() {
      const now = mtimeOf()
      if (cached !== null && (now === mtimeMs || now === null)) return cached
      // Never had a good value and the file is absent: `read` reports the absence as an error,
      // which is the refuse case. Reaching it through `read` keeps one message, not two.
      mtimeMs = now
      cached = read(path)
      return cached
    },
  }
}
