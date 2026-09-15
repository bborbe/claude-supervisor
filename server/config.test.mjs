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
import { readdirSync, readFileSync } from 'node:fs'
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
    'claudeCmd',
    'mcpConfig',
    'workerColor',
  ]) {
    assert.ok(key in config, `config.${key} is missing — the surface must be enumerable`)
  }
})

test('the resolved paths hang off the XDG homes rather than the checkout', () => {
  assert.match(config.configDir, /claude-supervisor$/)
  assert.match(config.stateDir, /claude-supervisor$/)
  assert.equal(config.userPolicy, join(config.configDir, 'policy.json'))
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
