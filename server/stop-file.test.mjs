// Unit tests for the machine-wide A2A stop file.
//
// The defect: `writeLedger` refuses a rehydrated agent (the single-writer guarantee), so a stop
// on a worker this server did NOT spawn — which `stop_agent` says is the common case, because it
// reads the request-time roster — reached the controller and left no durable record. The sweep
// then read that worker as an orphan candidate and offered it for stop+recreate.
//
// ⚠️ **The shape is the contract, not an implementation detail.** The reader is a Python script
// (`scripts/a2a-liveness.py`) in a different process and a different language; nothing type-checks
// across that seam. So the field names and the one-line-per-stop layout are asserted here
// literally, and the Python side pins the same shape from its end — a rename on either side fails
// a test rather than silently blinding the resolver.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { appendStop, stopEntry, stopFilePath } from './stop-file.mjs'
import { config } from './config.mjs'

const WORKER = '37ded6f4ca94e293'
const SESSION = 'aaaa1111-0000-0000-0000-000000000000'

function withDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), 'a2a-stops-'))
  try {
    return fn(join(dir, 'a2a-stops.jsonl'))
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

function lines(path) {
  return readFileSync(path, 'utf8').split('\n').filter(Boolean)
}

test('a stop is one JSON line carrying exactly the four fields the reader joins on', () => {
  withDir((path) => {
    assert.equal(
      appendStop({ workerId: WORKER, task: 'Some Task', at: '2026-10-17T09:00:00.000Z', session: SESSION, path }),
      true,
    )
    const rows = lines(path)
    assert.equal(rows.length, 1)
    assert.deepEqual(JSON.parse(rows[0]), {
      worker_id: WORKER,
      task: 'Some Task',
      stopped_at: '2026-10-17T09:00:00.000Z',
      stopping_session: SESSION,
    })
  })
})

test('appends rather than rewrites — a second stop does not erase the first', () => {
  withDir((path) => {
    appendStop({ workerId: WORKER, task: 'a', session: SESSION, path })
    appendStop({ workerId: 'another-worker', task: 'b', session: SESSION, path })
    const rows = lines(path).map((line) => JSON.parse(line).worker_id)
    assert.deepEqual(rows, [WORKER, 'another-worker'])
  })
})

test('the directory is created if it is not there yet', () => {
  const dir = mkdtempSync(join(tmpdir(), 'a2a-stops-'))
  try {
    const nested = join(dir, 'does', 'not', 'exist', 'stops.jsonl')
    assert.equal(appendStop({ workerId: WORKER, session: SESSION, path: nested }), true)
    assert.equal(lines(nested).length, 1)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('a stop with no worker id is refused, because nothing could join it back', () => {
  withDir((path) => {
    for (const workerId of [undefined, null, '', 0, {}]) {
      assert.equal(appendStop({ workerId, task: 'x', session: SESSION, path }), false, String(workerId))
    }
    assert.throws(() => readFileSync(path, 'utf8'))
  })
})

test('it answers false rather than throwing when the path cannot be written', () => {
  // A stop that already reached the controller must not become a failed stop because a file is
  // unwritable — the caller has nothing to catch with.
  const dir = mkdtempSync(join(tmpdir(), 'a2a-stops-'))
  try {
    // A directory where the file should be: the append fails, the call still returns.
    assert.equal(appendStop({ workerId: WORKER, session: SESSION, path: dir }), false)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('stopEntry defaults the stamp and tolerates a missing task or session', () => {
  const entry = stopEntry({ workerId: WORKER })
  assert.equal(entry.worker_id, WORKER)
  assert.equal(entry.task, '')
  assert.equal(entry.stopping_session, '')
  assert.match(entry.stopped_at, /^\d{4}-\d{2}-\d{2}T/)
})

test('the default path is machine-wide under the readers state root, not the supervisor own', () => {
  // ⚠️ The readers are the plugin's Python scripts, and they all resolve under `~/.claude/state/`.
  // Pointing this at the supervisor's own state dir would create a second root only this file
  // uses, and the reader would have to know about both. Asserted through `config`, because the
  // path may not be read from the environment here.
  assert.match(stopFilePath(), /\/\.claude\/state\/a2a-stops\.jsonl$/)
  assert.equal(stopFilePath(), config.stopFile)
})
