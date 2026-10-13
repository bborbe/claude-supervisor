// Tests for the fleet-wide MANAGER cap — `resolveMaxManagers`, `managerLimitRefusal` and the
// `managerSessions` producer. Mirrors the worker cap's tests: each test names the reason the
// rule exists, not the mechanic.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  DEFAULT_MAX_MANAGERS,
  DEFAULT_MAX_MANAGERS_HARD,
  MANAGER_FLEET_FULL,
  managerLimitRefusal,
  resolveMaxManagers,
  unknownKeyWarnings,
} from './spawn-mode.mjs'
import { ROOT_NAME, managerSessions } from './manager-sessions.mjs'

const CFG = '/cfg.json'
const limits = (spawn) => resolveMaxManagers({ file: { spawn }, path: CFG })

test('the operator design is reachable with no config edit — soft 3, hard 5', () => {
  assert.equal(DEFAULT_MAX_MANAGERS, 3)
  assert.equal(DEFAULT_MAX_MANAGERS_HARD, 5)
  assert.deepEqual(resolveMaxManagers({ file: {} }), { limit: 3, hardLimit: 5, source: 'default', hardSource: 'default' })
})

test('configured values win, and the source says so', () => {
  assert.deepEqual(limits({ maxManagers: 2, maxManagersHard: 4 }), { limit: 2, hardLimit: 4, source: 'config', hardSource: 'config' })
})

test('the soft off switch disables both — "no limit" must not leave the hard key capping', () => {
  assert.equal(limits({ maxManagers: 0, maxManagersHard: 'garbage' }).hardLimit, null)
})

test('a hard 0 is refused — it would delete the ceiling while the soft cap stays', () => {
  assert.match(limits({ maxManagersHard: 0 }).error, /spawn\.maxManagersHard/)
})

test('a ceiling beneath its floor is refused rather than reordered', () => {
  assert.match(limits({ maxManagers: 5, maxManagersHard: 3 }).error, /below the soft one/)
})

test('a non-integer is refused and named as a manager limit, not a worker one', () => {
  const { error } = limits({ maxManagers: true })
  assert.match(error, /concurrent-manager limit/)
  assert.match(error, /spawn\.maxManagers/)
})

test('both new keys are known — an unregistered key warns "ignored" on every boot', () => {
  assert.deepEqual(unknownKeyWarnings({ spawn: { maxManagers: 3, maxManagersHard: 5 } }, CFG), [])
})

const decide = (liveCount, operatorNamed = false, spawn = {}) =>
  managerLimitRefusal({ limits: limits(spawn), liveCount, operatorNamed, configFile: CFG })

test('below the soft cap a manager opens', () => {
  assert.equal(decide(2), null)
})

test('SC1 — at the hard cap a 6th manager is refused, naming spawn.maxManagersHard and its source', () => {
  const msg = decide(5, true)
  assert.ok(msg.startsWith(MANAGER_FLEET_FULL))
  assert.match(msg, /spawn\.maxManagersHard" \(source: default\)/)
})

test('SC2 — at the soft cap an ordinary open is refused WITHOUT the fleet-full phrase the card keys on', () => {
  const msg = decide(3)
  assert.match(msg, /spawn\.maxManagers"/)
  assert.ok(!msg.includes(MANAGER_FLEET_FULL), 'a soft refusal must not trigger the hard-cap card')
})

test('SC2 — between soft and hard an operator-named manager still opens', () => {
  assert.equal(decide(4, true), null)
})

test('an uncountable fleet refuses rather than reading as empty', () => {
  assert.match(decide(null), /could not be taken/)
})

const board = (sessions, status = 0) => async () => ({ status, stdout: JSON.stringify({ sessions }) })

test('SC3 — the count includes a manager with no ledger record, and excludes the Fleet Manager', async () => {
  const ids = await managerSessions({
    run: board([
      { session_id: 'root', label: ROOT_NAME, role: 'manager' },
      { session_id: 'hand-started', label: 'Some Topic Manager', role: 'manager' },
      { session_id: 'w', label: 'a task', role: 'worker' },
    ]),
  })
  assert.deepEqual(ids, ['hand-started'])
})

test('an unreadable or malformed board is unknown (null), never zero', async () => {
  assert.equal(await managerSessions({ run: board([], 1) }), null)
  assert.equal(await managerSessions({ run: async () => ({ status: 0, stdout: 'not json' }) }), null)
  assert.equal(await managerSessions({ run: async () => ({ status: 0, stdout: '{}' }) }), null)
})

test('ROOT_NAME agrees with the board that owns it', () => {
  const py = readFileSync(new URL('../scripts/fleet-board.py', import.meta.url), 'utf8')
  assert.match(py, new RegExp(`^ROOT_NAME = "${ROOT_NAME}"$`, 'm'))
})

test('the spawn path consults the manager cap for a manager role', () => {
  const source = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(source, /if \(roleResolution\.role === 'manager'\) \{\s*const managerError = await managerLimitError/)
})
