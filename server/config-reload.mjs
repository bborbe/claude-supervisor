// The config file's LIVE contents — the value a spawn decides against.
//
// ⚠️ `config.mjs` reads the file once at module load and freezes the result. That is correct for
// every key that cannot change under a running server, and wrong for the spawn limits, which the
// operator edits *precisely because* they want the next spawn to differ from the last. Observed
// 2026-10-14: `spawn.maxManagers` was raised 5 → 10 in `~/.config/claude-supervisor/config.json`
// (the file parsed fine) and two `role: manager` spawns were still refused against 5, until the
// server was reconnected via `/mcp`. The operator had no way to tell that from a bad value.
//
// The file is re-read when its mtime moves, so the hot path costs one `stat` and a parse happens
// only when something actually changed. Read per spawn, never cached across spawns: a spawn
// decided against a limit read minutes earlier is a decision against a config that no longer
// exists — the same reason `manager-sessions.mjs` refuses to cache its count.
//
// ⚠️ A MISSING FILE KEEPS THE LAST GOOD VALUE, and that is not defensive padding. Editors write
// via rename, so the path is briefly absent mid-write; reading that absence as "no config" would
// silently drop the operator's limits to the built-in defaults at exactly the moment they are
// editing them — the one instant they are watching for the opposite.
//
// ⚠️ Scope: this reader is for the SPAWN LIMITS. Keys resolved once at boot for reasons of their
// own (`clusterUrl`/`clusterAuthToken`, `a2a.captureDir`, the unknown-key warnings) still read
// the boot snapshot, and moving them here is a separate decision, not a free consequence of this
// one. A malformed file yields `file: null`, so the limits fall back to their defaults exactly as
// they do at boot; the parse error itself is surfaced by the boot path's logger.

import { statSync } from 'node:fs'

import { readConfigFile } from './config.mjs'

export function mtimeReloadedConfig({ path, read = readConfigFile, stat = statSync }) {
  let mtimeMs = null
  let cached = null

  const mtimeOf = () => {
    try {
      return stat(path).mtimeMs
    } catch {
      // Absent, or unstattable. Both mean "no news", never "empty".
      return null
    }
  }

  return {
    current() {
      const now = mtimeOf()
      if (cached !== null && (now === mtimeMs || now === null)) return cached
      mtimeMs = now
      cached = read(path)
      return cached
    },
  }
}
