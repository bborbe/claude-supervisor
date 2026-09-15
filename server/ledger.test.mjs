// Unit tests for the durable spawn ledger.
//
// The load-bearing ones are the merge (a completion patch must not erase the spawn
// fields — they arrive minutes apart from different call sites) and the atomic write
// (a reader must never catch a half-written record and read it as a session with no
// fields, which is the same "reports success while being wrong" shape as the bugs this
// file's neighbours exist to prevent).
//
// These do NOT replace the end-to-end check: that a record survives its session — with
// the live registry entry gone — is a fact about two stores, which a fixture cannot see.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { buildRecord, listRecords, parentSessionId, readRecord, recordPath, updateRecord, writeRecord } from './ledger.mjs'

const SESSION = 'ea363bb5-123a-4d03-bc89-1087a3114bbd'

function withDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), 'supervisor-ledger-'))
  try {
    return fn(dir)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

const spawnRecord = (over = {}) =>
  buildRecord({
    sessionId: SESSION, agentId: 'agent_1', label: 'drill', mode: 'interactive',
    cwd: '/tmp', launcher: 'cc-personal-deepseek', paneId: '1711', ...over,
  })

test('buildRecord carries the spawn edge and starts with no outcome', () => {
  const record = spawnRecord({ resumedFrom: 'aaaa1111-0000-0000-0000-000000000000', parentSession: 'bbbb2222-0000-0000-0000-000000000000' })
  assert.equal(record.session_id, SESSION)
  assert.equal(record.mode, 'interactive')
  assert.equal(record.pane_id, '1711')
  assert.equal(record.resumed_from, 'aaaa1111-0000-0000-0000-000000000000')
  assert.equal(record.parent_session, 'bbbb2222-0000-0000-0000-000000000000')
  assert.equal(record.status, 'running')
  assert.equal(record.ended_at, null, 'a running worker has no end time yet')
  assert.equal(record.result, null)
})

test('buildRecord records which policy the worker ran under', () => {
  // Absence means the server policy — the same reading agentView and the permission log
  // use, so the three cannot disagree about what "no policy" means.
  assert.equal(spawnRecord().policy, null, 'no per-spawn policy reads as the server policy, not as unknown')
  assert.equal(spawnRecord({ policy: '/etc/strict.json' }).policy, '/etc/strict.json')
})

test('buildRecord refuses a record it could not file', () => {
  assert.throws(() => buildRecord({ mode: 'interactive' }), /needs a sessionId/)
  assert.throws(() => buildRecord({ sessionId: SESSION, mode: 'telepathy' }), /unknown mode/)
})

test('the record is keyed by the session uuid, which is the resume handle', () => {
  assert.equal(recordPath('/state/sessions', SESSION), `/state/sessions/${SESSION}.json`)
})

test('write then read round-trips, creating the directory', () => {
  withDir((dir) => {
    const nested = join(dir, 'sessions')
    writeRecord(nested, spawnRecord())
    assert.equal(readRecord(nested, SESSION).label, 'drill')
  })
})

test('writing leaves no temporary file behind', () => {
  withDir((dir) => {
    writeRecord(dir, spawnRecord())
    assert.deepEqual(readdirSync(dir).filter((f) => f.endsWith('.tmp')), [], 'a stray .tmp is a half-written record waiting to be read')
  })
})

test('a completion patch merges rather than replacing the spawn fields', () => {
  withDir((dir) => {
    writeRecord(dir, spawnRecord())
    const done = updateRecord(dir, SESSION, {
      status: 'done',
      ended_at: '2026-09-14T08:00:00.000Z',
      result: { subtype: 'success', permission_denials: 0 },
    })
    assert.equal(done.status, 'done')
    assert.equal(done.result.subtype, 'success')
    // The spawn edge must survive: it is what nothing else records, and the caller
    // that knows the outcome does not know it.
    assert.equal(done.parent_session, spawnRecord().parent_session)
    assert.equal(done.mode, 'interactive')
    assert.equal(done.label, 'drill')
    assert.equal(done.pane_id, '1711')
    assert.equal(readRecord(dir, SESSION).status, 'done', 'the merge is persisted, not just returned')
  })
})

test('updating an unknown session reports nothing rather than inventing a record', () => {
  withDir((dir) => {
    assert.equal(updateRecord(dir, 'no-such-session', { status: 'done' }), null)
    assert.equal(existsSync(recordPath(dir, 'no-such-session')), false)
  })
})

test('listRecords tolerates a missing directory and skips what it cannot parse', () => {
  assert.deepEqual(listRecords('/nonexistent/supervisor/ledger'), [])
  withDir((dir) => {
    writeRecord(dir, spawnRecord())
    writeRecord(dir, spawnRecord({ sessionId: 'other-session' }))
    assert.equal(listRecords(dir).length, 2)
  })
})

test('parentSessionId resolves the manager through the live registry', () => {
  const registry = () => [{ pid: 58436, sessionId: 'manager-session' }, { pid: 999, sessionId: 'someone-else' }]
  assert.equal(parentSessionId({ ppid: 58436, registry }), 'manager-session')
  assert.equal(parentSessionId({ ppid: 1234, registry }), null, 'an unknown parent is null, never a guess')
  assert.equal(parentSessionId({ ppid: 58436, registry: () => null }), null, 'an unreadable registry is null too')
})
