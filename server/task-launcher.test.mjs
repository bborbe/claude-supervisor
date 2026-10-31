// Tests for reading a task's `launcher:` frontmatter through vault-cli.
//
// The fake `run` answers the shapes the real CLI produces, measured 2026-10-09: an unset key
// is `{"value": ""}` (exit 0); a missing task on `get` is an error envelope with EXIT 0. The
// exit-0 envelope is the case a status-only check misreads as "field unset", which is why it
// is pinned here.
//
// ⚠️ GOALS ARE NOT READ, and the case named for it pins that. A goal's `launcher:` is a
// manager statement — managers resolve through `manager_launcher:` and never this field — so
// inheriting it here handed every worker under a goal its manager's Opus launcher (measured
// 2026-10-22: 12 private-personal goals, every worker beneath them on Opus). A regression that
// reintroduced the goal read would issue a `task show` / `goal get` call, which these fakes
// reject as unexpected.

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

test('a task naming its own launcher reports it', () => {
  const { run, calls } = fake({ 'task get T': ok({ value: 'cc-private-claude' }) })
  assert.deepEqual(readTaskLaunchers({ task: 'T', vault: 'v', run }), { taskLauncher: 'cc-private-claude' })
  assert.deepEqual(calls, ['task get T'])
})

test('a task with no launcher reads as empty and reads no goal', () => {
  const { run, calls } = fake({ 'task get T': ok({ value: '' }) })
  assert.deepEqual(readTaskLaunchers({ task: 'T', vault: 'v', run }), { taskLauncher: '' })
  assert.deepEqual(calls, ['task get T'])
})

test('a whitespace-padded value is trimmed', () => {
  const { run } = fake({ 'task get T': ok({ value: '  cc-private-glm  ' }) })
  assert.equal(readTaskLaunchers({ task: 'T', vault: 'v', run }).taskLauncher, 'cc-private-glm')
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
