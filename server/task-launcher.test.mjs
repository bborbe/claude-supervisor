// Tests for reading task / goal `launcher:` frontmatter through vault-cli.
//
// The fake `run` answers the shapes the real CLI produces, measured 2026-10-09: an unset key
// is `{"value": ""}` (exit 0); a missing task or goal on `get` is an error envelope with
// EXIT 0; `task show` omits `goals` when there are none. The exit-0 envelope is the case a
// status-only check misreads as "field unset", which is why it is pinned here.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readTaskLaunchers } from './task-launcher.mjs'

const ok = (body) => ({ status: 0, stdout: JSON.stringify(body), stderr: '' })
const notFound = (what) => ok({ error: `find ${what}: file not found`, success: false })

const fake = (answers) => {
  const calls = []
  const run = (args) => {
    calls.push(args.slice(0, 3).join(' '))
    const key = args.slice(0, 3).join(' ')
    if (!(key in answers)) throw new Error(`unexpected call: ${key}`)
    return answers[key]
  }
  return { run, calls }
}

test('task naming its own launcher: goals are never read', () => {
  const { run, calls } = fake({ 'task get T': ok({ value: 'cc-private-claude' }) })
  assert.deepEqual(readTaskLaunchers({ task: 'T', vault: 'v', run }), {
    taskLauncher: 'cc-private-claude',
    goalLaunchers: [],
    warnings: [],
  })
  assert.deepEqual(calls, ['task get T'])
})

test('task with no field inherits its goals’ values', () => {
  const { run } = fake({
    'task get T': ok({ value: '' }),
    'task show T': ok({ goals: ['[[]]', '[[G]]'] }),
    'goal get G': ok({ value: 'cc-private-claude' }),
  })
  assert.deepEqual(readTaskLaunchers({ task: 'T', vault: 'v', run }).goalLaunchers, ['cc-private-claude'])
})

test('task with no goals at all reads as nothing set', () => {
  const { run } = fake({ 'task get T': ok({ value: '' }), 'task show T': ok({ name: 'T' }) })
  assert.deepEqual(readTaskLaunchers({ task: 'T', vault: 'v', run }), { taskLauncher: '', goalLaunchers: [], warnings: [] })
})

test('a missing task answered with exit 0 is an error, never "field unset"', () => {
  const { run } = fake({ 'task get T': notFound('task') })
  const r = readTaskLaunchers({ task: 'T', vault: 'v', run })
  assert.ok(r.error)
  assert.match(r.error, /file not found/)
})

test('a non-zero exit is an error', () => {
  const { run } = fake({ 'task get T': { status: 1, stdout: '', stderr: 'boom' } })
  assert.match(readTaskLaunchers({ task: 'T', vault: 'v', run }).error, /boom/)
})

test('unparseable output is an error', () => {
  const { run } = fake({ 'task get T': { status: 0, stdout: 'not json', stderr: '' } })
  assert.match(readTaskLaunchers({ task: 'T', vault: 'v', run }).error, /unparseable/)
})

test('a stale goal link is skipped with a warning, the others still count', () => {
  const { run } = fake({
    'task get T': ok({ value: '' }),
    'task show T': ok({ goals: ['[[Gone]]', '[[G]]'] }),
    'goal get Gone': notFound('goal'),
    'goal get G': ok({ value: 'cc-private-claude' }),
  })
  const r = readTaskLaunchers({ task: 'T', vault: 'v', run })
  assert.equal(r.error, undefined)
  assert.deepEqual(r.goalLaunchers, ['cc-private-claude'])
  assert.equal(r.warnings.length, 1)
  assert.match(r.warnings[0], /Gone/)
})
