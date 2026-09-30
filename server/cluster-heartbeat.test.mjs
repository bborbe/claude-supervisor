// Tests for the cluster → local heartbeat mirror.
//
// The load-bearing property is the one that is invisible when it works: a FAILED poll must not
// refresh the reachability marker. Refreshing it would tell `live-workers.py` that the cluster
// was readable when it was not, and every cluster stamp would then read as a dead worker rather
// than as "cannot tell" — the answer that permits a resume onto a worker that may be alive
// behind a network fault.

import { mkdtempSync, readFileSync, existsSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import assert from 'node:assert/strict'

import { pollCluster, REACHABILITY_FILE } from './cluster-heartbeat.mjs'

const ok = (stamps) => () => ({ status: 0, stdout: JSON.stringify(stamps), stderr: '' })
const fail = (status = 1) => () => ({ status, stdout: '', stderr: 'connection refused' })

function tmp() {
  return mkdtempSync(join(tmpdir(), 'cluster-heartbeat-'))
}

test('a successful poll stamps each session with source=cluster', () => {
  const dir = tmp()
  const result = pollCluster({ dir, run: ok([{ session_id: 'a1b2c3d4-1111', age_seconds: 3, pid: null }]) })

  assert.equal(result.ok, true)
  assert.deepEqual(result.stamped, ['a1b2c3d4-1111'])
  const record = JSON.parse(readFileSync(join(dir, 'a1b2c3d4-1111.json'), 'utf8'))
  assert.equal(record.source, 'cluster', 'the readers key their cluster staleness rule on this field')
  assert.equal(record.mode, 'cluster')
})

test('a successful poll refreshes the reachability marker', () => {
  const dir = tmp()
  pollCluster({ dir, run: ok([{ session_id: 'a1b2c3d4-1111', age_seconds: 3 }]) })
  assert.equal(existsSync(join(dir, REACHABILITY_FILE)), true)
})

test('a failed poll does NOT refresh the marker, so the stamps it wrote go stale', () => {
  const dir = tmp()
  pollCluster({ dir, run: ok([{ session_id: 'a1b2c3d4-1111', age_seconds: 3 }]) })
  const before = statSync(join(dir, REACHABILITY_FILE)).mtimeMs

  const result = pollCluster({ dir, run: fail() })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'cluster store unreadable')
  assert.equal(
    statSync(join(dir, REACHABILITY_FILE)).mtimeMs,
    before,
    'refreshing the marker on a failed poll would launder an outage into a death',
  )
})

test('a failed poll never creates a marker where none existed', () => {
  const dir = tmp()
  pollCluster({ dir, run: fail() })
  assert.equal(existsSync(join(dir, REACHABILITY_FILE)), false)
})

test('unparseable reader output is a failure, not an empty success', () => {
  const dir = tmp()
  const result = pollCluster({ dir, run: () => ({ status: 0, stdout: 'not json', stderr: '' }) })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'unparseable reader output')
  assert.equal(existsSync(join(dir, REACHABILITY_FILE)), false)
})

test('a stamp with no session id is skipped rather than written under an empty key', () => {
  const dir = tmp()
  const result = pollCluster({ dir, run: ok([{ age_seconds: 1 }, { session_id: 'b2c3d4e5-2222', age_seconds: 1 }]) })
  assert.deepEqual(result.stamped, ['b2c3d4e5-2222'])
  assert.equal(existsSync(join(dir, '.json')), false)
})
