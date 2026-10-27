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
import { execSync } from 'node:child_process'
import { chmodSync, existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { shellQuote } from './spawn-mode.mjs'
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

// ⚠️ An ORDERING assertion passes on the broken form. The first version of this file
// asserted only that `cd` came before `_sx()`, and its name claimed the failed-`cd` property
// while the shape did the opposite: `cd X && _sx() {...}; marker; observe; trap; launcher`
// skips only the function DEFINITION when `cd` fails and runs the launcher anyway. The PR
// review caught it as critical. So this drives a REAL shell — the same round trip
// `spawnInteractiveAgent` makes — and asserts on what actually happened, with a positive
// control in the same test so a probe that can never fire cannot read as a pass.
function fixture() {
  const dir = mkdtempSync(join(tmpdir(), 'tab-spawn-'))
  const record = join(dir, 'record.log')
  const launched = join(dir, 'launched')
  const writer = join(dir, 'writer.sh')
  writeFileSync(writer, `#!/bin/sh\nprintf '%s\\n' "$*" >> ${shellQuote(record)}\n`)
  chmodSync(writer, 0o755)
  const launcher = join(dir, 'launcher.sh')
  writeFileSync(launcher, `#!/bin/sh\ntouch ${shellQuote(launched)}\n`)
  chmodSync(launcher, 0o755)
  return { dir, record, launched, writer, launcher }
}

function run({ dir, writer, launcher }, cwd) {
  const script = tabSpawnInner({ cwd, launcher, tabName: 'tab', prompt: 'prompt', env: {} })
  try {
    // ⚠️ `shellQuote`, not JSON: the script carries `$?`, `$$` and `$SESSION_EXIT_RECORD`,
    // and a double-quoted wrapper would expand them in the OUTER shell before bash -lc ever
    // saw them — the positive control then fails for a reason that has nothing to do with
    // the shape under test.
    execSync(`SESSION_EXIT_RECORD=${shellQuote(writer)} bash -lc ${shellQuote(script)}`, {
      encoding: 'utf8',
      stdio: 'pipe',
      cwd: dir,
    })
  } catch {
    // A failed `cd` exits non-zero by design; the assertions below are the verdict.
  }
}

test('a failed cd aborts the spawn instead of launching in the wrong directory', () => {
  const fx = fixture()
  run(fx, join(fx.dir, 'does-not-exist'))
  assert.equal(existsSync(fx.launched), false, 'the launcher must not run after a failed cd')
  assert.equal(existsSync(fx.record), false, 'no record may be written after a failed cd')
})

test('a good cd launches, and writes the spawn marker first — the positive control', () => {
  const fx = fixture()
  run(fx, fx.dir)
  assert.equal(existsSync(fx.launched), true, 'the launcher must run when the cd succeeds')
  assert.match(readFileSync(fx.record, 'utf8'), /--kind spawn/)
})

test('per-call env is exported before the cd so it survives profile files', () => {
  const s = inner({ env: { FOO: 'bar' } })
  assert.ok(s.indexOf('FOO') < s.indexOf('cd '))
})

test('the caller supplies the writer path, so the shape never resolves it itself', () => {
  assert.equal(inner().includes('SESSION_EXIT_RECORD='), false)
})
