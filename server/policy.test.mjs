// Unit tests for the pure policy layer.
//
// These cover the decision path including the two branches the PR description
// flagged as untested — 'deny' and the 'cwd' match. They do NOT replace an
// end-to-end run: that the hook's deny actually blocks a live worker, and that a
// deferred request still parks, are integration facts these tests cannot see.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { inputKey, ruleMatches, decide } from './policy.mjs'

const BUNDLED = [
  { tool: 'Read', match: '*', action: 'allow' },
  { tool: 'Glob', match: '*', action: 'allow' },
  { tool: 'Write', match: 'cwd', action: 'allow' },
  { tool: 'Bash', match: 'rm -rf', action: 'deny' },
  { tool: '*', match: '*', action: 'escalate' },
]

test('inputKey picks the field a rule could name', () => {
  assert.equal(inputKey({ file_path: '/a/b.txt' }), '/a/b.txt')
  assert.equal(inputKey({ command: 'ls -la' }), 'ls -la')
  assert.equal(inputKey({ pattern: '**/*.md' }), '**/*.md')
  assert.equal(inputKey({ url: 'https://example.com' }), 'https://example.com')
})

test('inputKey is empty rather than throwing on shapes it does not know', () => {
  assert.equal(inputKey(null), '')
  assert.equal(inputKey('a string'), '')
  assert.equal(inputKey({ unexpected: 1 }), '')
})

test('ruleMatches honours tool wildcard, match wildcard and substring', () => {
  assert.equal(ruleMatches({ tool: '*', match: '*' }, 'Anything', 'k', '/cwd'), true)
  assert.equal(ruleMatches({ tool: 'Read', match: '*' }, 'Read', 'k', '/cwd'), true)
  assert.equal(ruleMatches({ tool: 'Read', match: '*' }, 'Write', 'k', '/cwd'), false)
  assert.equal(ruleMatches({ tool: 'Bash', match: 'rm -rf' }, 'Bash', 'rm -rf /tmp', '/cwd'), true)
  assert.equal(ruleMatches({ tool: 'Bash', match: 'rm -rf' }, 'Bash', 'ls', '/cwd'), false)
})

test('the cwd match admits paths under the agent cwd and nothing else', () => {
  const rule = { tool: 'Write', match: 'cwd', action: 'allow' }
  assert.equal(ruleMatches(rule, 'Write', '/work/repo/a.txt', '/work/repo'), true)
  assert.equal(ruleMatches(rule, 'Write', '/work/other/a.txt', '/work/repo'), false)
  // A sibling directory sharing a prefix must NOT match — '/work/repo-2' starts
  // with '/work/repo' as a string but is not inside it.
  assert.equal(ruleMatches(rule, 'Write', '/work/repo-2/a.txt', '/work/repo'), false)
  assert.equal(ruleMatches(rule, 'Write', '', '/work/repo'), false)
})

test('decide returns allow, deny and escalate across the bundled set', () => {
  assert.equal(decide(BUNDLED, 'Read', { file_path: '/x' }, '/cwd').action, 'allow')
  assert.equal(decide(BUNDLED, 'Bash', { command: 'rm -rf /' }, '/cwd').action, 'deny')
  assert.equal(decide(BUNDLED, 'WebFetch', { url: 'https://x' }, '/cwd').action, 'escalate')
})

test('decide applies the cwd rule to a write inside cwd', () => {
  const write = decide(BUNDLED, 'Write', { file_path: '/work/repo/a.txt' }, '/work/repo')
  assert.equal(write.action, 'allow')
  assert.equal(write.rule.match, 'cwd')
})

test('decide falls through to escalate when nothing matches, and reports no rule', () => {
  const result = decide([{ tool: 'Read', match: '*', action: 'allow' }], 'Bash', { command: 'ls' }, '/cwd')
  assert.equal(result.action, 'escalate')
  assert.equal(result.rule, null)
  assert.equal(result.key, 'ls')
})

test('first match wins, which is what makes the user overlay work', () => {
  // Same shape as the real overlay: user rules are passed first, bundled after.
  const user = [{ tool: 'Bash', match: 'ls', action: 'deny' }]
  const merged = [...user, ...BUNDLED]
  assert.equal(decide(merged, 'Bash', { command: 'ls -la' }, '/cwd').action, 'deny')

  // Reversed, the bundled catch-all wins and the user's rule is unreachable —
  // this is the ordering the overlay must never accidentally produce.
  assert.equal(decide([...BUNDLED, ...user], 'Bash', { command: 'ls -la' }, '/cwd').action, 'escalate')
})

test('decide tolerates a missing or malformed rule list', () => {
  assert.equal(decide(undefined, 'Read', {}, '/cwd').action, 'escalate')
  assert.equal(decide([], 'Read', {}, '/cwd').action, 'escalate')
})
