// Tests for the configuration boundary.
//
// The first test is the point of this file: RULE node/config/env-read-at-boundary is
// a MUST, and a rule that is only obeyed once is not enforced. This fails the build if
// any server module reads `process.env` again — including a new one added later, which
// is exactly when a scattered read would otherwise creep back in.
//
// Test files are excluded from that check, matching the rule's own exclusions, and the
// environment-mutation tests below are why: proving `off` is honoured means running
// the module under a different environment.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { config } from './config.mjs'

const SERVER_DIR = dirname(fileURLToPath(import.meta.url))

// Comments discuss this rule by name, so a naive substring scan flags the very files
// that document it — a check that cannot tell code from prose is wrong in both
// directions. Strip comments first, then look.
const ENV_READ = /process\.env\b/
const readsEnv = (source) =>
  source
    .split('\n')
    .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line)) // whole-line comments and block-continuation lines
    .some((line) => ENV_READ.test(line.replace(/\/\/.*$/, ''))) // trailing comments

test('no module but config.mjs reads process.env', () => {
  const offenders = readdirSync(SERVER_DIR)
    .filter((name) => name.endsWith('.mjs') && !name.endsWith('.test.mjs') && name !== 'config.mjs')
    .filter((name) => readsEnv(readFileSync(join(SERVER_DIR, name), 'utf8')))

  assert.deepEqual(
    offenders,
    [],
    'these modules read process.env directly — move the read into config.mjs so the configuration surface stays enumerable',
  )
})

test('the env-read scan tells code from comments', () => {
  // Guards the guard: if comment-stripping breaks, the check above silently starts
  // passing or failing for the wrong reason.
  assert.equal(readsEnv('// process.env.FOO in a comment\n'), false)
  assert.equal(readsEnv('/* process.env.FOO\n * more prose\n */\n'), false)
  assert.equal(readsEnv('const x = 1 // see process.env.FOO\n'), false)
  assert.equal(readsEnv('const x = process.env.FOO\n'), true)
  assert.equal(readsEnv('if (process.env.FOO) { doIt() }\n'), true)
})

test('the config object is a frozen, complete surface', () => {
  assert.equal(Object.isFrozen(config), true, 'a caller must not be able to mutate resolved config')
  for (const key of [
    'logFile',
    'configDir',
    'stateDir',
    'claudeHome',
    'userPolicy',
    'permissionLog',
    'anthropicBaseUrl',
    'costFiguresMeaningful',
    'sessionsDir',
    'permissionMode',
    'configFile',
    'configFileContents',
    'configFileError',
    'spawnMode',
    'claudeCmd',
    'mcpConfig',
    'workerColor',
    'heartbeatDir',
    'heartbeatStateDir',
    'sessionId',
  ]) {
    assert.ok(key in config, `config.${key} is missing — the surface must be enumerable`)
  }
})

test('the heartbeat state directory is a SIBLING of the store, never inside it', () => {
  // ⚠️ Everything in `heartbeatDir` is a stamp: read for its age and listed as a session. A
  // state file living there would be listed as a session that does not exist, so the two must
  // not be the same directory — and a wrong default here is uncaught by the readState tests,
  // which all pass an explicit `dir`.
  assert.notEqual(config.heartbeatStateDir, config.heartbeatDir)
  assert.equal(
    config.heartbeatStateDir,
    join(config.stateDir, 'heartbeat-state'),
    'the state directory must hang off the same state home the store does',
  )
})

test('the resolved paths hang off the XDG homes rather than the checkout', () => {
  assert.match(config.configDir, /claude-supervisor$/)
  assert.match(config.stateDir, /claude-supervisor$/)
  assert.equal(config.userPolicy, join(config.configDir, 'policy.json'))
  assert.equal(config.configFile, join(config.configDir, 'config.json'))
  assert.equal(config.permissionLog, join(config.stateDir, 'permissions.jsonl'))
  assert.equal(config.sessionsDir, join(config.claudeHome, 'sessions'))
})

test('SUPERVISOR_PERMISSION_LOG=off means no log, not the default path', async () => {
  // The subtle case: "off" is a value, not an absence. Read as one thing, unsetting the
  // var and disabling the log produce opposite results.
  const before = process.env.SUPERVISOR_PERMISSION_LOG
  process.env.SUPERVISOR_PERMISSION_LOG = 'off'
  try {
    const { config: fresh } = await import('./config.mjs?permission-log=off')
    assert.equal(fresh.permissionLog, null, '"off" must disable the log file')
  } finally {
    if (before === undefined) delete process.env.SUPERVISOR_PERMISSION_LOG
    else process.env.SUPERVISOR_PERMISSION_LOG = before
  }
})

test('SUPERVISOR_SESSIONS_DIR overrides the registry location', async () => {
  const before = process.env.SUPERVISOR_SESSIONS_DIR
  process.env.SUPERVISOR_SESSIONS_DIR = '/tmp/supervisor-test-sessions'
  try {
    const { config: fresh } = await import('./config.mjs?sessions-dir=override')
    assert.equal(fresh.sessionsDir, '/tmp/supervisor-test-sessions')
  } finally {
    if (before === undefined) delete process.env.SUPERVISOR_SESSIONS_DIR
    else process.env.SUPERVISOR_SESSIONS_DIR = before
  }
})

test('sessionId is the env var, and null when it is absent', async () => {
  // ⚠️ The load-bearing half is the `null`, not the value. A server started outside a session
  // must stamp NOTHING rather than stamping under an invented key — a row under a made-up id
  // would be a permanent phantom reading Live until its TTL, with no session able to clear it.
  // The enumerable-surface test only proves the property EXISTS, which is the weaker claim.
  const before = process.env.CLAUDE_CODE_SESSION_ID
  try {
    process.env.CLAUDE_CODE_SESSION_ID = 'fa942d7a-c190-46dd-93f3-8dfe3280046b'
    assert.equal(
      (await import('./config.mjs?session-id=set')).config.sessionId,
      'fa942d7a-c190-46dd-93f3-8dfe3280046b',
    )

    delete process.env.CLAUDE_CODE_SESSION_ID
    assert.equal(
      (await import('./config.mjs?session-id=absent')).config.sessionId,
      null,
      'an absent variable must be null, never an empty string that would key a phantom row',
    )
  } finally {
    if (before === undefined) delete process.env.CLAUDE_CODE_SESSION_ID
    else process.env.CLAUDE_CODE_SESSION_ID = before
  }
})

test('a cost figure is only meaningful when the traffic reaches Anthropic', async () => {
  // The SDK prices from Anthropic's list, so the figure is real only when Anthropic
  // answered. Unset means the SDK reaches Anthropic itself; any other base URL means
  // something else did, and the number describes a billing model that never ran.
  const before = process.env.ANTHROPIC_BASE_URL
  try {
    delete process.env.ANTHROPIC_BASE_URL
    assert.equal(
      (await import('./config.mjs?cost=unset')).config.costFiguresMeaningful,
      true,
      'unset means the SDK reaches Anthropic, so the figure is real',
    )

    process.env.ANTHROPIC_BASE_URL = 'https://api.anthropic.com'
    assert.equal((await import('./config.mjs?cost=anthropic')).config.costFiguresMeaningful, true)

    process.env.ANTHROPIC_BASE_URL = 'http://127.0.0.1:8788'
    const viaRouter = (await import('./config.mjs?cost=router')).config
    assert.equal(viaRouter.costFiguresMeaningful, false, 'routed traffic is not billed from Anthropic’s list')
    assert.equal(viaRouter.anthropicBaseUrl, 'http://127.0.0.1:8788', 'and the URL that made it untrue is kept')
  } finally {
    if (before === undefined) delete process.env.ANTHROPIC_BASE_URL
    else process.env.ANTHROPIC_BASE_URL = before
  }
})

// --- the config file ---------------------------------------------------------
//
// Read at load and frozen, like everything else here, so these tests write a file, point
// SUPERVISOR_CONFIG at it, and re-import. That the read happens once is the intended
// behaviour and the reason the install runbook says to restart the MCP server after
// editing the file.

const withConfigFile = async (contents, tag) => {
  const dir = mkdtempSync(join(tmpdir(), 'supervisor-config-'))
  const path = join(dir, 'config.json')
  if (contents !== null) writeFileSync(path, contents)
  const before = process.env.SUPERVISOR_CONFIG
  process.env.SUPERVISOR_CONFIG = path
  try {
    return { config: (await import(`./config.mjs?${tag}`)).config, path }
  } finally {
    if (before === undefined) delete process.env.SUPERVISOR_CONFIG
    else process.env.SUPERVISOR_CONFIG = before
  }
}

test('SUPERVISOR_CONFIG overrides the config file location', async () => {
  const { config: fresh, path } = await withConfigFile('{"spawn":{"mode":"headless"}}', 'config-path')
  assert.equal(fresh.configFile, path)
})

test('the config file is parsed and exposed raw', async () => {
  // Raw, not interpreted: what the values MEAN belongs to spawn-mode.mjs, which can then
  // be tested without a filesystem at all.
  const { config: fresh } = await withConfigFile('{"spawn":{"mode":"headless"}}', 'config-parsed')
  assert.deepEqual(fresh.configFileContents, { spawn: { mode: 'headless' } })
  assert.equal(fresh.configFileError, null)
})

test('an absent config file is silence, not an error', async () => {
  // The normal case — most users have none and take the built-in default. Reporting it
  // would train the operator to ignore the channel that reports real problems.
  const { config: fresh } = await withConfigFile(null, 'config-absent')
  assert.equal(fresh.configFileContents, null)
  assert.equal(fresh.configFileError, null, 'a missing optional file must not be reported')
})

test('a config file that exists but does not parse IS reported', async () => {
  // The operator wrote this one and believes it is in effect. Silence here is how a file
  // gets accepted, documented, and never read.
  const { config: fresh, path } = await withConfigFile('{"spawn":{', 'config-broken')
  assert.equal(fresh.configFileContents, null)
  assert.match(fresh.configFileError, /not valid JSON/)
  assert.ok(fresh.configFileError.includes(path), 'the message must name the file to edit')
})

test('SUPERVISOR_SPAWN_MODE is carried raw for spawn-mode.mjs to validate', async () => {
  const before = process.env.SUPERVISOR_SPAWN_MODE
  process.env.SUPERVISOR_SPAWN_MODE = 'headless'
  try {
    assert.equal((await import('./config.mjs?spawn-mode=set')).config.spawnMode, 'headless')
  } finally {
    if (before === undefined) delete process.env.SUPERVISOR_SPAWN_MODE
    else process.env.SUPERVISOR_SPAWN_MODE = before
  }
})
