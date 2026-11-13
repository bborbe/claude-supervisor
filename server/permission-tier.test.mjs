// The server's tier wrapper runs the SAME fixture the Python suite runs, through the subprocess
// path the server actually uses — so a case that passes in `scripts/tests/test_permission_tier.py`
// and fails here means the wrapper, not the classifier, is wrong.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { permissionTier, permissionTierTimeoutMs } from './permission-tier.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const { cases } = JSON.parse(readFileSync(join(HERE, 'permission-tier.fixtures.json'), 'utf8'))

test('every fixture case returns its tier and form through the wrapper', () => {
  assert.ok(cases.length > 40)
  for (const c of cases) {
    const got = permissionTier(c.tool, c.input)
    assert.equal(got.tier, c.tier, `${c.id}: ${got.reason}`)
    if (c.form) assert.equal(got.form, c.form, `${c.id}: ${got.reason}`)
  }
})

test('the confined test entry needs the caller-checked flag', () => {
  assert.equal(permissionTier('Bash', { command: 'make test' }).tier, 'manager')
  assert.equal(permissionTier('Bash', { command: 'make test' }, { runnerConfigClean: true }).tier, 'allowlist')
})

test('a missing classifier fails closed to operator', () => {
  const got = permissionTier('Bash', { command: 'git status' }, { script: '/nonexistent/permission-tier.py' })
  assert.equal(got.tier, 'operator')
  assert.equal(got.form, 'unverifiable')
})

test('a classifier printing an unknown tier fails closed to operator', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ptier-'))
  const script = join(dir, 'fake.py')
  writeFileSync(script, 'import json; print(json.dumps({"tier": "anyone"}))\n')
  assert.equal(permissionTier('Bash', { command: 'ls' }, { script }).tier, 'operator')
})

// supervisor.mjs cannot be imported by a test (it starts the server), so its two call sites are
// pinned by source text — the same convention the timeout-record test uses.
test('supervisor.mjs computes the tier at park time and publishes it', () => {
  const src = readFileSync(join(HERE, 'supervisor.mjs'), 'utf8')
  assert.match(src, /import \{ permissionTier \} from '\.\/permission-tier\.mjs'/)
  assert.match(src, /record\.permissionTier = permissionTier\(toolName, input,/)
  assert.match(src, /permission_tier: p\.permissionTier\?\.tier \?\? 'operator',/)
})

test('no tool name fails closed', () => {
  assert.equal(permissionTier('', {}).tier, 'operator')
})

// ⚠️ A timeout is the failure this suite used to report as a routing difference. A classifier that
// runs past its budget must be legible AS a timeout: the reason names it and the form is
// `unverifiable`, so no reader can take the result for a tier verdict.
test('a classifier that runs past its budget reports the timeout, not a tier', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ptier-slow-'))
  const script = join(dir, 'slow.py')
  writeFileSync(script, 'import time\ntime.sleep(5)\n')
  const got = permissionTier('Bash', { command: 'git status' }, { script, timeoutMs: 200 })
  assert.equal(got.tier, 'operator')
  assert.equal(got.form, 'unverifiable')
  assert.match(got.reason, /did not run: .*(ETIMEDOUT|timed out)/i)
})

test('the classifier budget is a measured multiple, not a fixed five seconds', () => {
  assert.equal(permissionTierTimeoutMs({ measuredMs: 50 }), 10_000)       // below the floor
  assert.equal(permissionTierTimeoutMs({ measuredMs: 500 }), 10_000)      // 20 × 500 is the floor
  assert.equal(permissionTierTimeoutMs({ measuredMs: 2_000 }), 40_000)    // 20 × 2 000
  assert.equal(permissionTierTimeoutMs({ measuredMs: 60_000 }), 120_000)  // above the cap
})
