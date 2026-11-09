// Tests for the answer marker — the record that says WHO answered a worker's question.
//
// Three things are load-bearing, and each is tested against its negative:
//
//   * the SHAPE the hook reads. `answerer_kind` outside the hook's vocabulary is a write
//     that can never be read, so it is refused here rather than discovered as a silent gap.
//   * `ts` in SECONDS. Milliseconds read as future-dated to the hook, which rejects them —
//     every marker would be ignored and nothing on either side would report it.
//   * the empty-session operator answer. `""` must survive as `""`, never be dropped: it is
//     what keeps "the operator answered, unnamed" distinct from "nothing is known".

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, existsSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  ANSWERER_KINDS,
  answerMarker,
  answerMarkerPath,
  writeAnswerMarker,
} from './answer-marker.mjs'
import { config } from './config.mjs'

const SESSION = '11111111-1111-1111-1111-111111111111'

test('answerMarker carries the hook vocabulary verbatim', () => {
  for (const kind of ANSWERER_KINDS) {
    const rec = answerMarker({ answererSession: 'x', answererKind: kind })
    assert.equal(rec.answerer_kind, kind)
  }
})

test('answerMarker refuses a kind the hook would ignore', () => {
  // The hook accepts only manager / operator / other. Writing anything else produces a file
  // that exists and is never read, which is worse than not writing at all.
  assert.equal(answerMarker({ answererSession: 'x', answererKind: 'robot' }), null)
  assert.equal(answerMarker({ answererSession: 'x', answererKind: '' }), null)
  assert.equal(answerMarker({ answererSession: 'x', answererKind: undefined }), null)
})

test('ts is epoch seconds, not milliseconds', () => {
  const now = new Date('2026-11-09T12:00:00Z')
  const rec = answerMarker({ answererKind: 'manager', now })
  assert.equal(rec.ts, now.getTime() / 1000)
  // A millisecond value would be ~1000x larger. Assert the magnitude directly, because the
  // hook's failure mode (future-dated => ignored) is silent on both sides.
  assert.ok(rec.ts < 1e11, `expected seconds, got ${rec.ts}`)
})

test('the operator answer keeps an empty session rather than dropping it', () => {
  const rec = answerMarker({ answererSession: '', answererKind: 'operator' })
  assert.equal(rec.answerer_session, '')
  assert.equal(rec.answerer_kind, 'operator')
})

test('a non-string session is normalised to empty, never to a number', () => {
  const rec = answerMarker({ answererSession: 42, answererKind: 'manager' })
  assert.equal(rec.answerer_session, '')
})

test('answerMarkerPath refuses an empty session id', () => {
  // An empty id would write `<state>/.answer.json` — one shared file every session collides
  // on, which would attribute one worker's answer to every other.
  assert.equal(answerMarkerPath({ stateDir: '/tmp/x', sessionId: '' }), null)
  assert.equal(answerMarkerPath({ stateDir: '/tmp/x', sessionId: undefined }), null)
  assert.equal(answerMarkerPath({ stateDir: '/tmp/x', sessionId: SESSION }),
    join('/tmp/x', `${SESSION}.answer.json`))
})

test('writeAnswerMarker lands a record the hook can read', () => {
  const dir = mkdtempSync(join(tmpdir(), 'answer-marker-'))
  assert.equal(writeAnswerMarker({
    stateDir: dir, sessionId: SESSION, answererSession: 'mgr', answererKind: 'manager',
  }), true)
  const rec = JSON.parse(readFileSync(join(dir, `${SESSION}.answer.json`), 'utf8'))
  assert.deepEqual(Object.keys(rec).sort(), ['answerer_kind', 'answerer_session', 'ts'])
  assert.equal(rec.answerer_session, 'mgr')
  assert.equal(rec.answerer_kind, 'manager')
})

test('writeAnswerMarker returns false and writes nothing on an unknown kind', () => {
  const dir = mkdtempSync(join(tmpdir(), 'answer-marker-'))
  assert.equal(writeAnswerMarker({
    stateDir: dir, sessionId: SESSION, answererKind: 'robot',
  }), false)
  assert.equal(existsSync(join(dir, `${SESSION}.answer.json`)), false)
})

test('writeAnswerMarker never throws when the directory cannot be created', () => {
  // A file where the directory should be: mkdirSync fails, and the caller is mid-transition.
  // Throwing here would strand a worker to record a measurement.
  const dir = mkdtempSync(join(tmpdir(), 'answer-marker-'))
  const blocker = join(dir, 'blocked')
  writeFileSync(blocker, 'not a directory')
  assert.doesNotThrow(() => writeAnswerMarker({
    stateDir: blocker, sessionId: SESSION, answererKind: 'manager',
  }))
  assert.equal(writeAnswerMarker({
    stateDir: blocker, sessionId: SESSION, answererKind: 'manager',
  }), false)
})

test('attentionStateDir mirrors the hook resolution', () => {
  // Resolved in config.mjs, not here: this module reads no environment variable, so the
  // assertion belongs on the config surface that owns the default.
  assert.match(config.attentionStateDir, /(\.claude\/state\/attention|ATTENTION_STATE_DIR)/)
})
