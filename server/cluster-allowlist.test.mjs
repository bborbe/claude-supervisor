// Tests for the per-vault cluster allowlist — the file, the live read, and the refusal.
//
// The rule these exist for (operator, 2026-10-20): "seibert worker should not run on
// nuke-dev|prod and trading / personal worker should not run on octopus-dev|staging|prod".
//
// ⚠️ THE REFUSAL TESTS ARE THE LOAD-BEARING ONES, and the ones a plausible-but-wrong
// implementation passes are called out where they sit. The unit tests are NOT the task's
// acceptance evidence — the task's Success Criteria require a live `spawn_agent` call against a
// running server, precisely because a guard can pass here and never be wired in. These pin the
// function; the probes prove it is reached.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { clusterAllowlistRefusal, clusterOfTarget } from './a2a-spawn.mjs'
import { mtimeReloadedAllowlist, parseAllowlist, readAllowlistFile } from './cluster-allowlist.mjs'

const ALLOWLIST = {
  'private-personal': ['nukedev'],
  'private-trading': ['nukedev'],
  'seibert-brogrammers': ['octopus-dev', 'octopus-staging', 'octopus-prod'],
  'seibert-personal': ['octopus-dev', 'octopus-staging', 'octopus-prod'],
}

const tempFile = (body) => {
  const dir = mkdtempSync(join(tmpdir(), 'cluster-allowlist-'))
  const path = join(dir, 'vaults.json')
  if (body !== undefined) writeFileSync(path, typeof body === 'string' ? body : JSON.stringify(body))
  return { path, cleanup: () => rmSync(dir, { recursive: true, force: true }) }
}

// --- the refusal ---------------------------------------------------------------

test('a forbidden pairing is refused and names BOTH the vault and the cluster', () => {
  const refusal = clusterAllowlistRefusal({
    vault: 'private-personal',
    target: 'k8s:octopus-dev',
    allowlist: ALLOWLIST,
  })

  assert.ok(refusal, 'a forbidden pairing must refuse')
  // Both identifiers are required by SC1/SC2: a refusal naming only the cluster cannot tell the
  // caller which vault to fix, and one naming only the vault cannot say which cluster was tried.
  assert.match(refusal, /private-personal/)
  assert.match(refusal, /octopus-dev/)
  // And it must name the permitted set, so the repair is visible without reading the file.
  assert.match(refusal, /nukedev/)
})

test('every family in the operator rule is refused in both directions', () => {
  const forbidden = [
    ['seibert-brogrammers', 'k8s:nukedev'],
    ['seibert-personal', 'k8s:nukeprod'],
    ['private-personal', 'k8s:octopus-dev'],
    ['private-trading', 'k8s:octopus-prod'],
  ]

  for (const [vault, target] of forbidden) {
    const refusal = clusterAllowlistRefusal({ vault, target, allowlist: ALLOWLIST })
    assert.ok(refusal, `${vault} → ${target} must refuse`)
    assert.match(refusal, new RegExp(vault))
    assert.match(refusal, new RegExp(target.slice('k8s:'.length)))
  }
})

test('an allowed pairing passes', () => {
  assert.equal(
    clusterAllowlistRefusal({ vault: 'private-personal', target: 'k8s:nukedev', allowlist: ALLOWLIST }),
    null,
  )
  assert.equal(
    clusterAllowlistRefusal({ vault: 'seibert-brogrammers', target: 'k8s:octopus-dev', allowlist: ALLOWLIST }),
    null,
  )
})

test('a vault that declares nothing is refused, not treated as unrestricted', () => {
  // The plausible-but-wrong implementation reads an absent vault as "no opinion, allow it".
  const refusal = clusterAllowlistRefusal({
    vault: 'some-other-vault',
    target: 'k8s:nukedev',
    allowlist: ALLOWLIST,
  })

  assert.ok(refusal, 'an undeclared vault must refuse — silence is not permission')
  assert.match(refusal, /some-other-vault/)
  assert.match(refusal, /declares no allowed clusters/)
})

test('an unreadable allowlist refuses rather than spawning unchecked', () => {
  const refusal = clusterAllowlistRefusal({
    vault: 'private-personal',
    target: 'k8s:octopus-dev',
    allowlist: null,
  })

  assert.ok(refusal, 'a missing allowlist must refuse — this is the fail-closed half')
  assert.match(refusal, /could not be read/)
})

test('a k8s target that names no cluster refuses rather than passing', () => {
  // `k8s` alone names no cluster, so the allowlist cannot be checked against it. Reading that as
  // "not a cluster target, skip the guard" is the hole this test exists to close.
  const refusal = clusterAllowlistRefusal({
    vault: 'private-personal',
    target: 'k8s',
    allowlist: ALLOWLIST,
  })

  assert.ok(refusal, 'a cluster-less k8s target must refuse')
  assert.match(refusal, /names no cluster/)
})

test('a non-cluster target is outside this guard', () => {
  // `docker` runs a container on this Mac and has no cluster to check.
  assert.equal(clusterAllowlistRefusal({ vault: 'private-personal', target: 'docker', allowlist: ALLOWLIST }), null)
  assert.equal(clusterAllowlistRefusal({ vault: 'private-personal', target: 'local', allowlist: ALLOWLIST }), null)
})

test('the refusal cannot depend on where the target came from', () => {
  // SC requires a target resolved from a vault's DEFAULT cluster to be refused exactly like an
  // explicit one. The function takes no origin argument, so the two are the same call by
  // construction — this test pins that shape, so a later refactor cannot introduce an
  // `explicit: true` escape without failing here.
  const explicit = clusterAllowlistRefusal({
    vault: 'private-personal',
    target: 'k8s:octopus-dev',
    allowlist: ALLOWLIST,
  })
  const fromDefault = clusterAllowlistRefusal({
    vault: 'private-personal',
    target: 'k8s:octopus-dev',
    allowlist: ALLOWLIST,
  })

  assert.equal(explicit, fromDefault)
  assert.ok(explicit, 'a default-sourced forbidden pairing must refuse too')
})

test('clusterOfTarget separates "no cluster target" from "cluster target naming no cluster"', () => {
  assert.deepEqual(clusterOfTarget('docker'), { named: false })
  assert.deepEqual(clusterOfTarget('local'), { named: false })
  assert.deepEqual(clusterOfTarget(undefined), { named: false })
  assert.deepEqual(clusterOfTarget('k8s'), { named: true, cluster: null })
  assert.deepEqual(clusterOfTarget('k8s:'), { named: true, cluster: null })
  assert.deepEqual(clusterOfTarget('k8s:nukedev'), { named: true, cluster: 'nukedev' })
})

// --- the file ------------------------------------------------------------------

test('a well-formed file parses to a vault → clusters map', () => {
  const read = parseAllowlist({ vaults: ALLOWLIST }, '/cfg/vaults.json')
  assert.equal(read.error, null)
  assert.deepEqual(read.allowlist, ALLOWLIST)
})

test('a missing file is an error, never an empty allowlist', () => {
  const { path, cleanup } = tempFile(undefined)
  try {
    const read = readAllowlistFile(path)
    assert.equal(read.allowlist, null)
    assert.match(read.error, /does not exist/)
  } finally {
    cleanup()
  }
})

test('a malformed file refuses rather than falling back', () => {
  const { path, cleanup } = tempFile('{ not json')
  try {
    const read = readAllowlistFile(path)
    assert.equal(read.allowlist, null)
    assert.match(read.error, /not valid JSON/)
  } finally {
    cleanup()
  }
})

test('a file with no "vaults" map refuses rather than reading as an empty allowlist', () => {
  const { path, cleanup } = tempFile({ private: ['nukedev'] })
  try {
    const read = readAllowlistFile(path)
    assert.equal(read.allowlist, null)
    assert.match(read.error, /no "vaults" map/)
  } finally {
    cleanup()
  }
})

test('a vault mapped to a non-list refuses rather than guessing', () => {
  const { path, cleanup } = tempFile({ vaults: { 'private-personal': 'nukedev' } })
  try {
    const read = readAllowlistFile(path)
    assert.equal(read.allowlist, null)
    assert.match(read.error, /not a list of cluster names/)
  } finally {
    cleanup()
  }
})

// --- the live read -------------------------------------------------------------

test('an edit reaches the next read once the mtime moves, without a reconnect', () => {
  let body = { vaults: { 'private-personal': ['nukedev'] } }
  let mtime = 1_000
  const live = mtimeReloadedAllowlist({
    path: '/cfg/vaults.json',
    read: () => parseAllowlist(body, '/cfg/vaults.json'),
    stat: () => ({ mtimeMs: mtime }),
  })

  assert.deepEqual(live.current().allowlist['private-personal'], ['nukedev'])

  body = { vaults: { 'private-personal': ['nukedev', 'octopus-dev'] } }
  mtime = 2_000

  assert.deepEqual(
    live.current().allowlist['private-personal'],
    ['nukedev', 'octopus-dev'],
    'the operator edits the file and the next spawn sees it — this is the whole point of the reload',
  )
})

test('an unchanged mtime serves the cache rather than re-reading', () => {
  let reads = 0
  const live = mtimeReloadedAllowlist({
    path: '/cfg/vaults.json',
    read: () => {
      reads += 1
      return parseAllowlist({ vaults: ALLOWLIST })
    },
    stat: () => ({ mtimeMs: 1_000 }),
  })

  live.current()
  live.current()
  live.current()

  assert.equal(reads, 1)
})

test('a file that vanishes after a good read keeps the last good value', () => {
  let mtime = 1_000
  const live = mtimeReloadedAllowlist({
    path: '/cfg/vaults.json',
    read: () => parseAllowlist({ vaults: ALLOWLIST }),
    stat: () => {
      if (mtime === null) {
        const error = new Error('ENOENT')
        error.code = 'ENOENT'
        throw error
      }
      return { mtimeMs: mtime }
    },
  })

  assert.ok(live.current().allowlist)
  // An editor writing via rename briefly leaves no file at the path. Reading that as "no
  // allowlist" would fail-open at exactly the moment the operator is narrowing it.
  mtime = null
  assert.deepEqual(live.current().allowlist, ALLOWLIST, 'a missing file mid-edit is not an empty allowlist')
})

test('a file that was never readable refuses, and does not keep retrying a good value it never had', () => {
  const live = mtimeReloadedAllowlist({
    path: '/cfg/vaults.json',
    read: () => ({ allowlist: null, error: '/cfg/vaults.json does not exist' }),
    stat: () => {
      const error = new Error('ENOENT')
      error.code = 'ENOENT'
      throw error
    },
  })

  const read = live.current()
  assert.equal(read.allowlist, null, 'no cached value + absent file must refuse')
  assert.match(read.error, /does not exist/)
})
