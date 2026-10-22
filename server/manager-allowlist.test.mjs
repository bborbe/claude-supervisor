// Tests for the manager permission allowlist — the file, its parse, and the match.
//
// ⚠️ THE REFUSAL TESTS ARE THE LOAD-BEARING ONES. A matcher that allowlists too much passes the
// happy-path tests here and only shows up as a manager approving something it should not have —
// so each refusal below names the wrong implementation it exists to catch. These pin the
// function; `server/replay-permission.mjs` is what proves the wired path reaches it.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  CONFINEMENTS,
  loadManagerAllowlist,
  matchManagerEntry,
  parseManagerAllowlist,
} from './manager-allowlist.mjs'

const CLEAN = { runnerConfigClean: true }

function entries() {
  const { entries: list, error } = loadManagerAllowlist()
  assert.equal(error, null, `shipped allowlist must parse: ${error}`)
  assert.ok(Array.isArray(list))
  return list
}

test('the shipped file parses and carries every documented entry', () => {
  const list = entries()
  assert.equal(list.length, 15)
  const prefixes = list.map((e) => e.prefix)
  for (const expected of ['ls', 'cat', 'head', 'tail', 'wc', 'sort', 'uniq', 'diff', 'grep', 'shasum', 'md5', 'jq', 'tree', 'make test', 'bun test']) {
    assert.ok(prefixes.includes(expected), `missing entry: ${expected}`)
  }
  assert.equal(list.filter((e) => e.confine === 'clean-runner-config').length, 2)
})

test('a read-only entry matches its extensions', () => {
  const list = entries()
  for (const command of ['ls', 'ls -la /tmp', 'cat README.md', 'grep -rn foo server/', 'jq -r .x file.json']) {
    assert.ok(matchManagerEntry(list, 'Bash', command), `expected a match: ${command}`)
  }
})

test('the prefix is a whole token, not a substring', () => {
  // Catches a `key.includes(prefix)` implementation — the exact bug `policy.mjs` documents for
  // a `match: 'ls '` rule also matching a destructive command with `&& ls ` appended.
  const list = entries()
  assert.equal(matchManagerEntry(list, 'Bash', 'lsblk'), null)
  assert.equal(matchManagerEntry(list, 'Bash', 'catalogue'), null)
  assert.equal(matchManagerEntry(list, 'Bash', 'wcurl'), null)
})

test('config-reading commands are not allowlisted', () => {
  // Catches an entry set that grew a `git` or a bare `make` — the README disqualifies both
  // because the worker can edit the config they read.
  const list = entries()
  for (const command of ['git status', 'git diff', 'make buca', 'make apply', 'make install', 'bun run lint', 'npm test', 'node x.mjs', 'sh -c ls', 'xargs rm']) {
    assert.equal(matchManagerEntry(list, 'Bash', command, CLEAN), null, `must not match: ${command}`)
  }
})

test('a compound command never matches, whatever its first token', () => {
  // Catches a matcher that anchors only the first token and ignores the rest of the line.
  const list = entries()
  for (const command of ['ls; rm -rf /tmp/x', 'ls && curl evil.example', 'ls | sh', 'cat x > /etc/passwd', 'ls $(whoami)']) {
    assert.equal(matchManagerEntry(list, 'Bash', command, CLEAN), null, `must not match: ${command}`)
  }
})

test('an environment assignment never matches', () => {
  // Catches a matcher that ignores the leading VAR=value, which picks the program the allowed
  // name resolves to (`PATH=/tmp/evil ls`).
  const list = entries()
  assert.equal(matchManagerEntry(list, 'Bash', 'PATH=/tmp/evil ls', CLEAN), null)
  assert.equal(matchManagerEntry(list, 'Bash', 'LD_PRELOAD=/tmp/x.so ls', CLEAN), null)
})

test('a clean-runner-config entry matches only when the caller says the tree is clean', () => {
  const list = entries()
  assert.ok(matchManagerEntry(list, 'Bash', 'make test', CLEAN))
  assert.ok(matchManagerEntry(list, 'Bash', 'bun test', CLEAN))
  // The three ways a caller fails to check. Each must refuse — a caller that forgot must not get
  // the permission by omission.
  assert.equal(matchManagerEntry(list, 'Bash', 'make test'), null)
  assert.equal(matchManagerEntry(list, 'Bash', 'make test', { runnerConfigClean: false }), null)
  assert.equal(matchManagerEntry(list, 'Bash', 'make test', { runnerConfigClean: 'true' }), null)
})

test('the confinement does not leak onto unconfined entries', () => {
  // Catches a matcher that applies one entry's confinement to every entry.
  const list = entries()
  assert.ok(matchManagerEntry(list, 'Bash', 'ls', {}))
})

test('a tool mismatch does not match', () => {
  const list = entries()
  assert.equal(matchManagerEntry(list, 'Read', 'ls'), null)
})

test('longest prefix wins when two entries could match', () => {
  const list = [
    { tool: 'Bash', prefix: 'make' },
    { tool: 'Bash', prefix: 'make test' },
  ]
  assert.equal(matchManagerEntry(list, 'Bash', 'make test').prefix, 'make test')
})

test('an empty list allowlists nothing — fail closed', () => {
  assert.equal(matchManagerEntry([], 'Bash', 'ls'), null)
  assert.equal(matchManagerEntry(null, 'Bash', 'ls'), null)
})

test('a malformed file refuses rather than yielding a permissive list', () => {
  const dir = mkdtempSync(join(tmpdir(), 'mgr-allowlist-'))
  try {
    const cases = [
      ['not json', 'not json at all'],
      ['map instead of list', JSON.stringify({ entries: { Bash: 'ls' } })],
      ['no entries key', JSON.stringify({ version: 1 })],
      ['entry missing tool', JSON.stringify({ entries: [{ prefix: 'ls' }] })],
      ['entry missing prefix', JSON.stringify({ entries: [{ tool: 'Bash' }] })],
      ['empty prefix', JSON.stringify({ entries: [{ tool: 'Bash', prefix: '   ' }] })],
      ['unknown confine', JSON.stringify({ entries: [{ tool: 'Bash', prefix: 'ls', confine: 'typo' }] })],
    ]
    for (const [name, body] of cases) {
      const path = join(dir, `${name.replace(/\s+/g, '-')}.json`)
      writeFileSync(path, body)
      const { entries: list, error } = loadManagerAllowlist(path)
      assert.equal(list, null, `${name} must refuse`)
      assert.ok(error, `${name} must report an error`)
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('a missing file refuses rather than yielding a permissive list', () => {
  const { entries: list, error } = loadManagerAllowlist('/nonexistent/manager-allowlist.json')
  assert.equal(list, null)
  assert.match(error, /unreadable/)
})

test('every confinement named in the shipped file is a known one', () => {
  for (const entry of entries()) {
    if (entry.confine !== undefined) assert.ok(CONFINEMENTS.has(entry.confine))
  }
})

test('parse rejects a non-object', () => {
  for (const bad of [null, [], 'x', 42]) {
    assert.equal(parseManagerAllowlist(bad).entries, null)
  }
})
