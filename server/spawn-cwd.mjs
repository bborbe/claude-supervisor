// Where a spawned worker runs, and which launcher it inherits.
//
// Split out of supervisor.mjs for the usual reason (importing supervisor.mjs starts the
// MCP server), but it exists at all because of a measured failure. On 2026-10-03 a manager
// session called `spawn_agent` 27 times with no `cwd`: every worker started in this
// server's own working directory — `~/Documents/workspaces/claude-supervisor/server` — and
// every one ran Opus instead of the vault's `cc-private-deepseek`, because the launcher
// lookup fell back to a vault named `personal` that no longer exists (renamed
// `private-personal`) and then to the bare `claude` binary the cc-* scripts exist to
// replace. Nothing in the spawn response named the launcher, the vault or the model, so the
// caller had no way to see any of it.
//
// Two silent fallbacks, one silent default, and no way to observe the result. The rule here
// is the repo's standing one, the same one `spawn-mode.mjs` applies to the mode and the
// target: refuse rather than guess, because a worker started in the wrong place under the
// wrong launcher is discovered only by noticing it.
//
// Nothing here reads the filesystem, the environment, or the clock. `vaults` is handed in
// already parsed, so every resolution is a pure function of its arguments.

// A path is "under" another when it is that path or a proper descendant of it.
//
// The trailing slash is load-bearing: a bare prefix test reads
// `/vaults/private-personal-archive` as being inside `/vaults/private-personal`, which would
// resolve a worker in an unrelated directory to this vault's launcher — the same class of
// silent wrong answer this module exists to remove.
const isUnder = (child, parent) => {
  if (typeof child !== 'string' || typeof parent !== 'string') return false
  const root = parent.endsWith('/') ? parent.slice(0, -1) : parent
  if (root === '') return false
  return child === root || child.startsWith(`${root}/`)
}

const knownNames = (vaults) =>
  vaults
    .map((v) => v.name)
    .filter(Boolean)
    .join(', ')

// Every refusal names the thing that has no launcher rather than saying "no launcher", so a
// caller can tell an unknown vault from a vault that exists but is not configured to run
// one — two different repairs.
const noLauncher = (where) =>
  `${where} has no \`claude_script\` in vault-cli config, so there is no launcher to start a worker ` +
  'with — refusing rather than falling back to the bare `claude` binary, which routes around the router ' +
  'env, the MCP config and the model selection that every cc-* script carries.'

// The directory a worker runs in, the vault it belongs to, and the launcher it inherits —
// or `{ error }`.
//
// Exactly one of `cwd` / `vault` is required, and requiring one is the whole fix: the
// default that used to apply here was this server's own working directory, which is a
// directory no caller ever chose and no vault ever owns.
//
// `vault` is the reliable form and `cwd` the explicit one. Naming a vault resolves both the
// directory and the launcher from one value, so they cannot disagree; naming a `cwd` leaves
// the vault to be found by containment. Passing both is allowed only when they agree.
export function resolveWorkerTarget({ cwd, vault, vaults, serverCwd } = {}) {
  if (!Array.isArray(vaults) || vaults.length === 0) {
    return {
      error:
        'the vault list could not be read from `vault-cli config list --output json`, so neither `cwd` nor ' +
        '`vault` can be resolved to a launcher — refusing to spawn rather than guessing, since a worker ' +
        'started under the wrong launcher is discovered only by noticing it.',
    }
  }

  const hasCwd = typeof cwd === 'string' && cwd.trim() !== ''
  const hasVault = typeof vault === 'string' && vault.trim() !== ''

  if (!hasCwd && !hasVault) {
    const where = serverCwd ? `this server's own working directory (${serverCwd})` : "this server's own working directory"
    return {
      error:
        'spawn_agent requires `cwd` or `vault`. Omitting both used to default the worker to ' +
        `${where}, with whatever launcher happened to match — which is how 27 workers started in the ` +
        'supervisor repo on the wrong model in one batch (2026-10-03). Pass `vault` (the vault name, which ' +
        'resolves both the launcher and the directory) or an explicit `cwd`.',
    }
  }

  if (hasVault) {
    const match = vaults.find((v) => v.name === vault)
    if (!match) {
      return {
        error:
          `vault ${JSON.stringify(vault)} is not configured — refusing to spawn rather than falling back to ` +
          `another vault's launcher. Known vaults: ${knownNames(vaults)}.`,
      }
    }
    if (!match.path) {
      return {
        error:
          `vault ${JSON.stringify(vault)} has no \`path\` in vault-cli config, so there is no directory to ` +
          'run a worker in.',
      }
    }
    if (!match.claude_script) return { error: noLauncher(`vault ${JSON.stringify(vault)}`) }
    if (hasCwd && !isUnder(cwd, match.path)) {
      return {
        error:
          `\`cwd\` ${JSON.stringify(cwd)} is not inside vault ${JSON.stringify(vault)} (${match.path}) — pass ` +
          'one or the other, not both, since a worker cannot run in a directory its launcher does not belong to.',
      }
    }
    return { cwd: match.path, vault: match.name, launcher: match.claude_script }
  }

  // `cwd` only. The LONGEST matching path wins, so a vault nested inside another resolves
  // to the inner one — the outer vault's launcher would otherwise shadow it purely by
  // having been configured first.
  const match = vaults
    .filter((v) => v.path && isUnder(cwd, v.path))
    .sort((a, b) => b.path.length - a.path.length)[0]

  if (!match) {
    return {
      error:
        `cwd ${JSON.stringify(cwd)} is outside every configured vault, so no launcher could be resolved for ` +
        `it — refusing to spawn rather than falling back to the bare \`claude\` binary. Known vaults: ` +
        `${knownNames(vaults)}. Pass \`vault\` instead when the worker belongs to a vault rather than to a ` +
        'specific directory.',
    }
  }
  if (!match.claude_script) return { error: noLauncher(`cwd ${JSON.stringify(cwd)} (vault ${JSON.stringify(match.name)})`) }

  return { cwd, vault: match.name, launcher: match.claude_script }
}

// The model a launcher script starts its session with, or `null` when it cannot be read.
//
// Read from the script rather than from vault-cli config, because the model is not in that
// config at all: `cc-private-deepseek` sets it as an argument it passes to `claude`
// (`--model "${ANTHROPIC_DEFAULT_OPUS_MODEL}"`), with the value itself in an `export` above.
// A caller asking "did my worker come up on deepseek or on Opus" cannot answer that from the
// vault name, and the vault name is all the config holds.
//
// `null` rather than a guess when either half is missing. A model reported from a failed
// parse would be worse than no model at all: the field exists precisely so a wrong spawn is
// visible, and a plausible wrong value defeats that.
export function parseLauncherModel(scriptText) {
  if (typeof scriptText !== 'string' || scriptText === '') return null

  // `--model` is anchored on whitespace rather than on a line start, so both shapes a
  // launcher script can take are read: the multi-line form every cc-* script uses, and the
  // one-line form. The trailing whitespace requirement is what keeps a hypothetical
  // `--model-name` flag out of the match.
  const flag = scriptText.match(/(?:^|\s)--model[ \t]+(?:"([^"]*)"|'([^']*)'|(\S+))/m)
  if (!flag) return null
  const raw = (flag[1] ?? flag[2] ?? flag[3] ?? '').trim()
  if (raw === '') return null

  // A literal model name is the answer.
  const ref = raw.match(/^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/) ?? raw.match(/^\$([A-Za-z_][A-Za-z0-9_]*)$/)
  if (!ref) return raw

  // An expansion is resolved against this script's own `export`, which is where the cc-*
  // launchers keep it. The name is escaped because it comes from the file being parsed.
  const name = ref[1].replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const assign = scriptText.match(
    new RegExp(`^[ \\t]*export[ \\t]+${name}=(?:"([^"]*)"|'([^']*)'|(\\S+))`, 'm'),
  )
  if (!assign) return null
  const value = (assign[1] ?? assign[2] ?? assign[3] ?? '').trim()
  return value === '' ? null : value
}
