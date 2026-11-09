// Unit tests for `answer-reach.mjs` — the reach record the answer-path probe reads.
//
// The regression these exist for: the probe certified spawners by an armed loop alone, and on
// 2026-11-09 spawner `0110c4a3` read exit 0 while its 27 workers' parks were released 56 times
// by the store and never once by it. A reach is the missing fact. These tests pin the two
// properties that make it evidence rather than decoration: it writes nothing it cannot key,
// and a broken record is distinguishable from an absent one.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { LEGS, reachPath, recordReach } from './answer-reach.mjs'

const fixedNow = () => new Date('2026-11-09T06:00:00.000Z')

function fakeFs({ failWrite = false } = {}) {
  const files = new Map()
  return {
    files,
    write: async (path, body) => {
      if (failWrite) throw new Error('EACCES: permission denied')
      files.set(path, body)
    },
    mkdirp: async () => {},
    read: async (path) => {
      if (!files.has(path)) {
        const err = new Error('ENOENT: no such file or directory')
        err.code = 'ENOENT'
        throw err
      }
      return files.get(path)
    },
  }
}

test('a reach writes one file per (session, leg), so the two writers cannot erase each other', async () => {
  const fs = fakeFs()
  const dir = '/reach'
  const mcp = await recordReach({ dir, sessionId: 'sid-1', leg: 'mcp', outcome: 'foreign-park', now: fixedNow, write: fs.write, mkdirp: fs.mkdirp })
  const wrapper = await recordReach({ dir, sessionId: 'sid-1', leg: 'wrapper', outcome: 'http-409', now: fixedNow, write: fs.write, mkdirp: fs.mkdirp })

  assert.equal(mcp.written, true)
  assert.equal(wrapper.written, true)
  // ⚠️ The point of the assertion: a single `<session>.json` would leave ONE entry here, and
  // whichever leg wrote second would have erased the first.
  assert.equal(fs.files.size, 2)
  assert.equal(reachPath(dir, 'sid-1', 'mcp'), `${dir}/sid-1.mcp.json`)
  assert.equal(reachPath(dir, 'sid-1', 'wrapper'), `${dir}/sid-1.wrapper.json`)
  assert.deepEqual(JSON.parse(fs.files.get(`${dir}/sid-1.mcp.json`)), {
    session_id: 'sid-1',
    leg: 'mcp',
    ts: '2026-11-09T06:00:00.000Z',
    outcome: 'foreign-park',
  })
})

test('a null session id writes nothing — the spawn gate has already refused it', async () => {
  const fs = fakeFs()
  const out = await recordReach({ dir: '/reach', sessionId: null, leg: 'mcp', outcome: 'foreign-park', write: fs.write, mkdirp: fs.mkdirp })
  assert.equal(out.written, false)
  assert.match(out.reason, /no session id/)
  assert.equal(fs.files.size, 0)
})

test('an unknown leg writes nothing', async () => {
  const fs = fakeFs()
  const out = await recordReach({ dir: '/reach', sessionId: 'sid-1', leg: 'carrier-pigeon', write: fs.write, mkdirp: fs.mkdirp })
  assert.equal(out.written, false)
  assert.match(out.reason, /unknown leg/)
  assert.equal(fs.files.size, 0)
})

test('a failed write is reported, never thrown — the caller owes a quotable refusal', async () => {
  const fs = fakeFs({ failWrite: true })
  const out = await recordReach({ dir: '/reach', sessionId: 'sid-1', leg: 'mcp', write: fs.write, mkdirp: fs.mkdirp })
  assert.equal(out.written, false)
  assert.match(out.reason, /could not write mcp reach/)
})

test('LEGS is the two routes the probe reports, in report order', () => {
  assert.deepEqual(LEGS, ['mcp', 'wrapper'])
})
