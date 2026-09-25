// Unit tests for the headless-worker heartbeat store.
//
// The property that matters is not "the functions work" — it is that the WRITE and the READ
// can happen in different processes, because that is the entire reason this module exists.
// The spawning server knows its workers from an in-process Map; the manager that must decide
// whether to resume one has no access to that Map. So a same-process round trip proves
// nothing here: it would pass just as well against an in-memory variable, which is the
// design that already failed. The cross-process test below is therefore the load-bearing one,
// and it spawns a real child rather than mocking the boundary.
//
// The second property is the kill -9 case. A marker written at spawn and unlinked on a
// graceful exit passes every test that only ever ends its worker politely; it leaves a
// permanent "running" behind when the server is SIGKILLed mid-turn, which is the defect the
// task exists to fix. The staleness tests inject `now` rather than sleeping, so the age
// comparison is exercised without a 60-second test.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  HEARTBEAT_INTERVAL_MS,
  HEARTBEAT_TTL_MS,
  clearStamp,
  listLive,
  readLive,
  stampPath,
  stampRecord,
  sweepStale,
} from './heartbeat.mjs'

const SESSION = 'fa942d7a-c190-46dd-93f3-8dfe3280046b'
const OTHER = '11111111-2222-4333-8444-555555555555'

const withDir = (fn) => {
  const dir = mkdtempSync(join(tmpdir(), 'hb-test-'))
  try {
    return fn(dir)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

test('a stamp is readable by the process that wrote it', () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    assert.equal(readLive(SESSION, { dir }).live, true)
  })
})

test('a stamp written by ANOTHER process is readable here — the whole point', () => {
  // Spawned, not mocked. If this module's read path depended on anything process-local —
  // an in-memory Map, a module-level cache — this test fails and the ones above still pass,
  // which is exactly the asymmetry that makes it the one worth having.
  withDir((dir) => {
    const script = `
      import { stampRecord } from ${JSON.stringify(new URL('./heartbeat.mjs', import.meta.url).href)}
      stampRecord(${JSON.stringify(dir)}, { sessionId: ${JSON.stringify(SESSION)}, pid: process.pid, mode: 'headless' })
      console.log('stamped by', process.pid)
    `
    const childPid = Number(
      execFileSync(process.execPath, ['--input-type=module', '-e', script], { encoding: 'utf8' }).trim().replace('stamped by ', ''),
    )

    assert.notEqual(childPid, process.pid, 'the write must have happened in a different process')

    const verdict = readLive(SESSION, { dir })
    assert.equal(verdict.live, true, 'a non-spawning process reads a worker the child stamped')

    const [entry] = listLive({ dir })
    assert.equal(entry.sessionId, SESSION, 'the reader names the worker by session id')
    assert.equal(entry.pid, childPid, 'and reports the pid that stamped it')
  })
})

test('a worker with no stamp reads not-alive, and does so decisively', () => {
  withDir((dir) => {
    const verdict = readLive(OTHER, { dir })
    assert.equal(verdict.live, false)
    assert.notEqual(verdict.live, null, 'an absent stamp is a negative, not an unreadable channel')
  })
})

test('a stamp older than the TTL reads not-alive while the file is still on disk', () => {
  // The SIGKILL case: nothing cleared this file, because the server that would have was
  // killed mid-turn. The verdict must come from the age, not from the absence.
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    assert.equal(readLive(SESSION, { dir, now: Date.now() + HEARTBEAT_TTL_MS + 1 }).live, false)
  })
})

test('the TTL leaves a whole missed tick of slack over the interval', () => {
  // With TTL === INTERVAL a live worker reads dead for up to 2x, because the newest stamp is
  // already one full interval old when the next is due. The bound is only meaningful if the
  // two constants are related, so the relationship is asserted rather than assumed.
  assert.ok(
    HEARTBEAT_TTL_MS >= HEARTBEAT_INTERVAL_MS * 2,
    'the TTL must cover one missed interval, or a live worker reads dead between stamps',
  )
})

test('clearStamp removes the stamp, so the next reader does not wait out a TTL', () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    clearStamp(SESSION, { dir })
    assert.equal(readLive(SESSION, { dir }).live, false)
  })
})

test('listLive reports only the fresh stamps', () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    // A stale sibling, written directly so its mtime is set explicitly.
    writeFileSync(stampPath(dir, OTHER), '{"sessionId":"' + OTHER + '"}\n', { flag: 'w' })
    const past = Date.now() - HEARTBEAT_TTL_MS - 60_000
    execFileSync('touch', ['-t', new Date(past).toISOString().replace(/[-:T]/g, '').slice(0, 12), stampPath(dir, OTHER)])

    const live = listLive({ dir })
    assert.deepEqual(
      live.map((entry) => entry.sessionId),
      [SESSION],
      'the stale stamp is not a live worker, and the fresh one is',
    )
  })
})

test('sweepStale removes exactly the stale stamps', () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    writeFileSync(stampPath(dir, OTHER), '{"sessionId":"' + OTHER + '"}\n')
    const past = Date.now() - HEARTBEAT_TTL_MS - 60_000
    execFileSync('touch', ['-t', new Date(past).toISOString().replace(/[-:T]/g, '').slice(0, 12), stampPath(dir, OTHER)])

    assert.deepEqual(sweepStale({ dir }), [OTHER], 'only the stale one is swept')
    assert.equal(readLive(SESSION, { dir }).live, true, 'the live worker survives the sweep')
  })
})

test('an unreadable directory is "could not tell", never a confident not-alive', () => {
  // The caller acts on `false` by ALLOWING a resume, so an unreadable store must not be
  // folded into a negative — that turns an I/O error into permission to start a second
  // writer on one conversation. A path that exists but is not a directory is the cheap
  // stand-in for a permissions failure, and it is a different error from ENOENT.
  withDir((dir) => {
    const notADir = join(dir, 'a-file')
    mkdirSync(notADir)
    writeFileSync(join(notADir, 'x'), '')
    const verdict = readLive(SESSION, { dir: join(notADir, 'x') })
    assert.equal(verdict.live, null, 'an unreadable store is not evidence of death')
    assert.equal(listLive({ dir: join(notADir, 'x') }), null)
  })
})

test('an absent directory is a real negative, not "could not tell"', () => {
  // ENOENT means no worker has ever been stamped here, which is an answer. Folding it into
  // null would make a fresh machine's first read unreadable rather than empty.
  withDir((dir) => {
    assert.equal(readLive(SESSION, { dir: join(dir, 'nope') }).live, false)
    assert.deepEqual(listLive({ dir: join(dir, 'nope') }), [])
  })
})
