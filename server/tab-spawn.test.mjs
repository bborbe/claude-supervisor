// Unit tests for the tab spawn's inner command.
//
// The case that matters most is `exec`. It was present in five call sites and the fix for
// those five missed the sixth — this module — so a review caught it rather than a test.
// `exec` is invisible in every downstream signal: the spawn still succeeds, the tab still
// comes up, the session still runs. The only observable difference is a record that is
// never written, and that absence is indistinguishable from a session that is still alive.
//
// So the assertions here are on the ORDER and PRESENCE of the pieces, not on the string as
// a whole: a future edit that keeps the trap but starts the observer after the launcher
// would pass a substring check and fail the one that matters.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { tabSpawnInner } from './tab-spawn.mjs'

const inner = (over = {}) =>
  tabSpawnInner({
    cwd: '/tmp/work',
    launcher: 'cc-worker',
    tabName: '⚙ label',
    prompt: 'do the thing',
    env: {},
    ...over,
  })

test('exec is absent from the whole command', () => {
  assert.equal(/\bexec\b/.test(inner()), false)
})

test('the launcher runs as a child, after the trap is installed', () => {
  const s = inner()
  const trapAt = s.indexOf('trap _sx EXIT')
  const launcherAt = s.indexOf('cc-worker -n')
  assert.notEqual(trapAt, -1)
  assert.notEqual(launcherAt, -1)
  assert.ok(trapAt < launcherAt, 'the trap must be installed before the launcher runs')
})

test('the spawn marker is written before the launcher runs', () => {
  const s = inner()
  assert.ok(s.indexOf('--kind spawn') < s.indexOf('cc-worker -n'))
})

test('the observer starts before the trap and before the launcher', () => {
  const s = inner()
  const observeAt = s.indexOf(' observe ')
  assert.notEqual(observeAt, -1)
  assert.ok(observeAt < s.indexOf('trap _sx EXIT'))
  assert.ok(observeAt < s.indexOf('cc-worker -n'))
})

test('the observer watches this shell, not the launcher', () => {
  assert.match(inner(), /"\$SESSION_EXIT_RECORD" observe --pid \$\$/)
})

test('the pid rides on the marker, the trap and the observer alike', () => {
  const s = inner()
  assert.equal(s.match(/--pid \$\$/g).length, 3)
})

test('the session id is never invented', () => {
  assert.equal(inner().includes('--session'), false)
})

test('the exit status is captured on the function entry, not at definition time', () => {
  assert.match(inner(), /_sx\(\) \{ rc=\$\?;/)
})

test('the cd precedes the trap, so a failed cd never writes a record', () => {
  const s = inner()
  assert.ok(s.indexOf('cd ') < s.indexOf('_sx()'))
})

test('per-call env is exported before the cd so it survives profile files', () => {
  const s = inner({ env: { FOO: 'bar' } })
  assert.ok(s.indexOf('FOO') < s.indexOf('cd '))
})

test('the caller supplies the writer path, so the shape never resolves it itself', () => {
  assert.equal(inner().includes('SESSION_EXIT_RECORD='), false)
})
