// Unit tests for the session-liveness probe.
//
// Two regressions these exist for, both found live rather than reasoned about:
//
// 1. `sessionIsLive` was `pgrep -fl <id>` alone, and a session started fresh carries
//    its id nowhere in argv, so the guard read a live session as closed and would have
//    allowed the resume it exists to refuse.
// 2. The probe added to fix (1) matched the FULL COMMAND LINE of any process, so a
//    finished worker whose id was merely MENTIONED — by a shell, a watcher, a grep —
//    read as live and became unresumable. The bystander test below fails against that
//    implementation, and it is the reason no argv probe survives in liveness.mjs.
//
// They do NOT replace the end-to-end run: that a refused resume leaves no second
// writer on the conversation is an integration fact these tests cannot see.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { checkLiveness, pidIsAlive, readRegistry, registeredAsLive } from './liveness.mjs'

const SESSION = '63c613e0-7fd8-4dc3-b05e-c43bbf89be58'
const OTHER = '4ff785a3-c2b9-45cc-98d9-83720ff1883d'

// A pid that cannot be running: above macOS's maximum, so `kill -0` is ESRCH.
const DEAD_PID = 999999

function fixture(files) {
  const dir = mkdtempSync(join(tmpdir(), 'supervisor-liveness-'))
  for (const [name, body] of Object.entries(files)) {
    writeFileSync(join(dir, name), typeof body === 'string' ? body : JSON.stringify(body))
  }
  return dir
}

// The registry fixture in which the id is registered nowhere — the state a finished
// worker is in, and the state a bystander used to be able to override.
function registryWithout(sessionId) {
  return fixture({ '1.json': { pid: process.pid, sessionId } })
}

test('pidIsAlive confirms this process and rejects a pid that is gone', () => {
  assert.equal(pidIsAlive(process.pid), true)
  assert.equal(pidIsAlive(DEAD_PID), false)
  assert.equal(pidIsAlive(0), false)
  assert.equal(pidIsAlive('1234'), false)
  assert.equal(pidIsAlive(undefined), false)
})

test('readRegistry reports "no information" for a missing directory, not emptiness', () => {
  assert.equal(readRegistry('/nonexistent/supervisor/sessions'), null)
})

test('readRegistry skips files that are unparsable or not registry entries', () => {
  const dir = fixture({
    '1.json': { pid: 1, sessionId: SESSION },
    'broken.json': '{ not json',
    'foreign.json': { hello: 'world' },
    'notes.txt': 'ignored entirely',
  })
  try {
    const entries = readRegistry(dir)
    assert.equal(entries.length, 1)
    assert.equal(entries[0].sessionId, SESSION)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('registeredAsLive matches the session id against a running pid', () => {
  const dir = fixture({ '1.json': { pid: process.pid, sessionId: SESSION } })
  try {
    assert.equal(registeredAsLive(SESSION, { dir }), true)
    assert.equal(registeredAsLive(OTHER, { dir }), false)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('registeredAsLive ignores a stale entry whose pid is gone', () => {
  const dir = fixture({ '1.json': { pid: DEAD_PID, sessionId: SESSION } })
  try {
    assert.equal(registeredAsLive(SESSION, { dir }), false, 'a crashed session must not read as live forever')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('checkLiveness finds a live session the registry knows and no process probe could see', () => {
  const dir = fixture({ '1.json': { pid: process.pid, sessionId: SESSION } })
  try {
    const verdict = checkLiveness(SESSION, { dir })
    assert.equal(verdict.live, true, 'this is the fresh-session case a pgrep-alone probe missed')
    assert.deepEqual(verdict.probes, ['registry'])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('a session id mentioned in some other process command line does not read as live', () => {
  // The regression this file exists for, stated as the guard meets it: the registry
  // does not know this id, and something on the machine merely MENTIONS it.
  //
  // Against the `pgrep -fl <id>` probe this replaces, this test fails — the bystander
  // matches, the verdict is `live: true`, and a finished worker becomes unresumable.
  // Measured live 2026-09-20: a manager's own completion watcher held the id in its
  // argv, so the session that armed the watcher could not resume the worker it had
  // just watched finish.
  const dir = registryWithout(OTHER)
  try {
    const verdict = checkLiveness(SESSION, { dir })
    assert.equal(verdict.live, false, 'a bystander mention must not forbid a resume')
    assert.notEqual(verdict.live, true, 'the two-writers guard must not fire on a process that is not the session')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('checkLiveness reports "could not tell" when the registry is unreadable', () => {
  const verdict = checkLiveness(SESSION, { dir: '/nonexistent/supervisor/sessions' })
  assert.equal(verdict.live, null, 'unreadable must never be folded into "closed"')
  assert.deepEqual(verdict.probes, [], 'no probe ran, so the caller cannot read this as "confirmed closed"')
})

test('checkLiveness reports closed only when a probe ran and found nothing', () => {
  const dir = registryWithout(OTHER)
  try {
    const verdict = checkLiveness(SESSION, { dir })
    assert.equal(verdict.live, false)
    assert.deepEqual(verdict.probes, ['registry'], 'a probe ran and found nothing — this is a real "closed"')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
