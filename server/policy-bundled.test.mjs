// Tests for the SHIPPED server/policy.json — the file the supervisor and the wrapper
// poller actually evaluate, not a stub.
//
// ⚠️ THE REFUSAL TESTS ARE THE LOAD-BEARING ONES. An allowlist that is too wide passes
// every "this read-only command is allowed" case and shows up only as a worker running
// something the policy said it never would. Each refusal below names the wrong
// implementation it exists to catch, and every one of them was reachable in the first
// version of this file's ruleset.
//
// A prefix rule cannot exclude a flag (server/policy.mjs § ruleMatches), so a command is
// only listable when EVERY extension of its prefix is read-only. server/manager-allowlist
// .test.mjs pins that rule for the manager allowlist; this file pins it for the bundled
// policy, so the two cannot drift apart.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { decide } from './policy.mjs'

const RULES = JSON.parse(readFileSync(new URL('./policy.json', import.meta.url), 'utf8')).rules
const CWD = '/work/repo'

const verdict = (command) => decide(RULES, 'Bash', { command }, CWD).action

test('the shipped file parses and carries rules', () => {
  assert.ok(Array.isArray(RULES))
  assert.ok(RULES.length > 0)
})

test('every Bash allow rule is anchored — a substring allow is a universal bypass', () => {
  // A plain `match` is a substring test, so `{"match": "ls"}` also allows
  // `rm -rf /etc && ls`. Catches anyone adding a Bash allow without matchType.
  const bashAllows = RULES.filter((r) => r.tool === 'Bash' && r.action === 'allow')
  assert.ok(bashAllows.length > 0)
  for (const rule of bashAllows) {
    assert.equal(rule.matchType, 'command', `Bash allow must be anchored: ${JSON.stringify(rule)}`)
  }
})

test('read-only commands are allowed', () => {
  for (const command of ['ls -la /tmp', 'cat README.md', 'grep -rn foo server/', 'head -20 f', 'jq -r .x f.json', 'wc -l f']) {
    assert.equal(verdict(command), 'allow', `expected allow: ${command}`)
  }
})

test('sort and tree are NOT allowed — their extensions write files or run a program', () => {
  // `sort -o FILE`, `sort --output=FILE`, `sort --compress-program=PROG` and `tree -o FILE`
  // all match a bare prefix. Catches anyone re-adding them.
  for (const command of ['sort x', 'sort -o /tmp/out x', 'sort --output=/tmp/out x', 'sort --compress-program=sh x', 'tree', 'tree -o /tmp/out .']) {
    assert.notEqual(verdict(command), 'allow', `must not be allowed: ${command}`)
  }
})

test('rg is NOT allowed — --pre runs a program of the worker\'s choosing', () => {
  // `rg --pre=sh pat f` executes a command per input file. Catches anyone re-adding `rg`.
  for (const command of ['rg pat f', 'rg --pre=sh pat f', 'rg --pre "bash -c id" pat f']) {
    assert.notEqual(verdict(command), 'allow', `must not be allowed: ${command}`)
  }
})

test('file and diff are NOT allowed — --compile writes and --paginate runs a program', () => {
  // `file -C -m PATH` / `--compile` compiles a magic file and writes PATH.mgc — an
  // arbitrary-path write that bypasses Write(cwd); `diff -l` / `--paginate` runs pr(1).
  // Both match a bare prefix. Catches anyone re-adding them.
  for (const command of ['file README.md', 'file -C -m /tmp/magic', 'file --compile -m magic', 'diff a b', 'diff -l a b', 'diff --paginate a b']) {
    assert.notEqual(verdict(command), 'allow', `must not be allowed: ${command}`)
  }
})

test('uniq is NOT allowed — its second positional is an output file', () => {
  // `uniq IN OUT` writes OUT — an arbitrary-path write that bypasses Write(cwd). A prefix
  // rule cannot exclude a positional, so the command is not listed. Catches a re-add.
  for (const command of ['uniq f', 'uniq -c f', 'uniq /dev/null /etc/authorized_keys', 'uniq -c /dev/null /tmp/out']) {
    assert.notEqual(verdict(command), 'allow', `must not be allowed: ${command}`)
  }
})

test('the operator-decided refusals stay refused', () => {
  // These are the classes the change deliberately left out. Pinned so a later widening
  // has to argue with this test rather than slip through review.
  for (const command of ['sed -n -i p f', 'find . -delete', 'git status', 'git push origin master', 'python3 -c 1', 'xargs rm', 'awk 1 f', 'env ls', 'for i in 1 2; do echo $i; done']) {
    assert.notEqual(verdict(command), 'allow', `must not be allowed: ${command}`)
  }
})

test('the rm -rf deny still beats the allow rules', () => {
  assert.equal(verdict('rm -rf /tmp/scratch'), 'deny')
})

test('cd is allowed alone but never carries a refused command past the policy', () => {
  // A chain is allowed only when EVERY segment is allowed on its own, which is the whole
  // reason `cd` is safe to list.
  assert.equal(verdict('cd /tmp'), 'allow')
  assert.equal(verdict('cd /tmp && git status'), 'escalate')
  assert.equal(verdict('cd /tmp && rm -rf /'), 'deny')
})
