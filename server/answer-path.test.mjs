// Unit tests for the headless-spawn refusal in `answer-path.mjs`.
//
// The regression these exist for: a headless worker's prompts park with the session that
// spawned it, and a spawner with no answer path stranded two workers on 2026-09-20 with
// nothing at the spawn site asking. They do NOT replace the live probe: that a real manager
// reads `OK` and a real unregistered session is refused is a fact about the installed fleet,
// established by running `scripts/answer-path-check.py`, not by this file.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ANSWER_PATH_REMEDY, answerPathRefusal, runProbe } from './answer-path.mjs'

const refused = (detail = 'no manager registration') => ({ ok: false, detail })
const allowed = { ok: true, detail: "goal 'X' loop is OK" }

test('an interactive spawn is never checked — the park belongs to the tab', () => {
  let called = false
  const out = answerPathRefusal({
    opensInteractive: true,
    sessionId: 'sid',
    probe: () => {
      called = true
      return refused()
    },
  })
  assert.equal(out, null)
  assert.equal(called, false)
})

test('a headless spawn with a proven answer path proceeds', () => {
  assert.equal(answerPathRefusal({ opensInteractive: false, sessionId: 'sid', probe: () => allowed }), null)
})

test('a headless spawn without one is refused, naming the probe and the remedy', () => {
  const out = answerPathRefusal({
    opensInteractive: false,
    sessionId: 'sid',
    probe: () => refused("this session's manager loop on goal 'X' reads STALE, not OK"),
  })
  assert.match(out, /^refusing a headless spawn:/)
  assert.match(out, /reads STALE, not OK/)
  assert.match(out, /scripts\/answer-path-check\.py/)
  assert.match(out, /15 minutes/)
  assert.ok(out.includes(ANSWER_PATH_REMEDY))
})

test('a session id the server cannot name refuses without consulting the probe', () => {
  let called = false
  const out = answerPathRefusal({
    opensInteractive: false,
    sessionId: '',
    probe: () => {
      called = true
      return allowed
    },
  })
  assert.equal(called, false)
  assert.match(out, /cannot name the session/)
})

// --- the probe runner -------------------------------------------------------
//
// `runProbe` is where a broken probe could fail OPEN, so each way it can go wrong is pinned
// to a refusal. The fake stands in for `spawnSync`.

const exec = (result) => () => result

test('a probe that cannot run refuses', () => {
  const out = runProbe('sid', { exec: exec({ error: new Error('spawn python3 ENOENT') }) })
  assert.equal(out.ok, false)
  assert.match(out.detail, /could not run/)
})

test('a probe that prints nothing parseable refuses, quoting what it said', () => {
  const out = runProbe('sid', { exec: exec({ stdout: '', stderr: 'Traceback: boom' }) })
  assert.equal(out.ok, false)
  assert.match(out.detail, /nothing parseable/)
  assert.match(out.detail, /Traceback: boom/)
})

test('a truthy-but-not-true ok refuses — a changed contract must not fail open', () => {
  const out = runProbe('sid', { exec: exec({ stdout: '{"ok":"yes","detail":"x"}' }) })
  assert.equal(out.ok, false)
})

test('a non-zero exit that still reports ok refuses — the two halves disagree', () => {
  const out = runProbe('sid', { exec: exec({ status: 1, stdout: '{"ok":true,"detail":"loop is OK"}' }) })
  assert.equal(out.ok, false)
  assert.match(out.detail, /exited 1 while reporting ok/)
})

test('a non-zero exit carrying a structured refusal keeps its reason', () => {
  // The probe exits 1 on every refusal AND prints the body, so a blanket
  // `status !== 0 → generic message` would throw away the sentence the caller quotes.
  const out = runProbe('sid', { exec: exec({ status: 1, stdout: '{"ok":false,"detail":"reads STALE, not OK"}' }) })
  assert.equal(out.ok, false)
  assert.equal(out.detail, 'reads STALE, not OK')
})

test('the real probe shape parses', () => {
  // `status: 0` is part of the shape, not decoration: a real `spawnSync` always carries it,
  // and the disagreement branch reads it, so a fake without it tests a call that cannot happen.
  const out = runProbe('sid', { exec: exec({ status: 0, stdout: '{"ok":true,"detail":"loop is OK"}' }) })
  assert.deepEqual(out, { ok: true, detail: 'loop is OK' })
})
