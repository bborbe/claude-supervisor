// Tests for the config file's LIVE contents — the value a spawn decides against.
//
// The defect these exist for, observed 2026-10-14: `config.mjs` reads the file once at module
// load and freezes the result, so raising `spawn.maxManagers` in `~/.config/claude-supervisor/
// config.json` changed nothing until the operator reconnected the server via `/mcp`. Two manager
// spawns were refused against the OLD limit while the file on disk already held the new one.
//
// The boot snapshot is not the bug — it is correct for every key that cannot change under a
// running server. It is wrong for the spawn limits, which the operator edits precisely because
// they want the next spawn to differ from the last. These tests pin the seam that fixes it.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { mtimeReloadedConfig } from './config-reload.mjs'

test('SC1 — an edit is in effect on the next read, once the file mtime has moved', () => {
  let body = { spawn: { maxManagers: 5 } }
  let mtime = 1_000
  const live = mtimeReloadedConfig({
    path: '/cfg.json',
    read: () => ({ file: body, error: null }),
    // Reads `mtime` live, never captured: a helper that froze it would pass this test against a
    // reader that ignores the mtime entirely — the exact defect under test.
    stat: () => ({ mtimeMs: mtime }),
  })

  assert.equal(live.current().file.spawn.maxManagers, 5, 'the boot value is served first')

  // The operator edits the file. Nothing restarts; only the mtime moves.
  body = { spawn: { maxManagers: 10 } }
  mtime = 2_000

  assert.equal(
    live.current().file.spawn.maxManagers,
    10,
    'an edit must reach the next spawn without a reconnect — this is the whole defect',
  )
})

test('an unchanged mtime serves the cached parse rather than re-reading the file', () => {
  let reads = 0
  const live = mtimeReloadedConfig({
    path: '/cfg.json',
    read: () => {
      reads += 1
      return { file: { spawn: { maxManagers: 5 } }, error: null }
    },
    stat: () => ({ mtimeMs: 1_000 }),
  })

  live.current()
  live.current()
  live.current()

  assert.equal(reads, 1, 'the mtime check is what keeps this off the hot path')
})

test('a vanished file keeps the last good value rather than reverting to defaults', () => {
  let mtime = 1_000
  const live = mtimeReloadedConfig({
    path: '/cfg.json',
    read: () => ({ file: { spawn: { maxManagers: 7 } }, error: null }),
    stat: () => {
      if (mtime === null) {
        const error = new Error('ENOENT')
        error.code = 'ENOENT'
        throw error
      }
      return { mtimeMs: mtime }
    },
  })

  assert.equal(live.current().file.spawn.maxManagers, 7)

  // An editor that writes via rename briefly leaves no file at the path. Reading that as
  // "no config" would silently drop the operator's limits to the built-in defaults.
  mtime = null
  assert.equal(live.current().file.spawn.maxManagers, 7, 'a missing file is not an empty one')
})
