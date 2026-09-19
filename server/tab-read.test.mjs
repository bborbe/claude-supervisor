// Unit tests for the tab-worker disk read.
//
// The regression these exist for: `agent_status.last_message` was `lastAssistantText`
// over the in-memory `transcript` array, which only the headless SDK loop ever pushes to,
// so every tab worker reported null. The "finds the transcript by session id" and
// "returns the last TEXT record, not the last record" cases below both fail against that
// implementation — the first because there was no disk read at all, the second because a
// naive "take the final assistant record" returns a `tool_use` block and no text.
//
// They do NOT replace the end-to-end run: that a live worker's gate actually surfaces as
// `waiting` is an integration fact these tests cannot see, and the module says so.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  lastAssistantTextFrom,
  sessionStatusFor,
  tabWorkerRead,
  transcriptPathFor,
} from './tab-read.mjs'

const SESSION = '70aa97fc-38c0-4e2c-b531-aa6e6e39d361'
const OTHER = 'c57f50ab-1111-2222-3333-444455556666'

// The launcher's vault, not the cwd the caller passed — the whole reason the path is
// resolved by session id.
const VAULT_PROJECT = '-Users-bborbe-Documents-Obsidian-Personal'
const TMP_PROJECT = '-private-tmp'

const assistant = (content) =>
  JSON.stringify({ type: 'assistant', message: { role: 'assistant', content } })

const textBlock = (text) => ({ type: 'text', text })
const toolBlock = () => ({ type: 'tool_use', id: 'toolu_1', name: 'Bash', input: {} })

function fixture(files) {
  const root = mkdtempSync(join(tmpdir(), 'supervisor-tab-read-'))
  for (const [rel, body] of Object.entries(files)) {
    const path = join(root, rel)
    mkdirSync(join(path, '..'), { recursive: true })
    writeFileSync(path, body)
  }
  return root
}

test('resolves a transcript by session id, in whichever project directory holds it', () => {
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: assistant([textBlock('hello')]) })
  assert.equal(
    transcriptPathFor(SESSION, { dir: root }),
    join(root, VAULT_PROJECT, `${SESSION}.jsonl`),
  )
})

test('a session id in no project directory resolves to null', () => {
  const root = fixture({ [`${VAULT_PROJECT}/${OTHER}.jsonl`]: assistant([textBlock('other')]) })
  assert.equal(transcriptPathFor(SESSION, { dir: root }), null)
})

test('an unreadable projects root reads as no transcript, not as an error', () => {
  assert.equal(transcriptPathFor(SESSION, { dir: join(tmpdir(), 'does-not-exist-supervisor') }), null)
})

test('no session id resolves to null rather than scanning', () => {
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: assistant([textBlock('hello')]) })
  assert.equal(transcriptPathFor(null, { dir: root }), null)
})

test('returns the last TEXT record, not the last record — which is usually a tool call', () => {
  const body = [
    assistant([textBlock('first thing said')]),
    assistant([toolBlock()]),
    assistant([textBlock('last thing said')]),
    // The common shape: the worker narrates, then calls a tool. Taking the final record
    // yields no text at all, which is exactly how the bug read from the outside.
    assistant([toolBlock()]),
  ].join('\n')
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: body })
  assert.equal(lastAssistantTextFrom(transcriptPathFor(SESSION, { dir: root })), 'last thing said')
})

test('joins multiple text blocks in one record, as the in-memory path does', () => {
  const root = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: assistant([textBlock('a'), toolBlock(), textBlock('b')]),
  })
  assert.equal(lastAssistantTextFrom(transcriptPathFor(SESSION, { dir: root })), 'a\nb')
})

test('a transcript with no assistant text reads as null, not as empty string', () => {
  const body = [assistant([toolBlock()]), JSON.stringify({ type: 'user', message: {} })].join('\n')
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: body })
  assert.equal(lastAssistantTextFrom(transcriptPathFor(SESSION, { dir: root })), null)
})

test('a record with only whitespace text does not count as said', () => {
  const body = [assistant([textBlock('real')]), assistant([textBlock('   \n ')])].join('\n')
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: body })
  assert.equal(lastAssistantTextFrom(transcriptPathFor(SESSION, { dir: root })), 'real')
})

test('a malformed line is skipped rather than taking the whole read down', () => {
  const body = [
    assistant([textBlock('survives')]),
    '{not json at all',
    JSON.stringify({ type: 'assistant', message: { content: 'not a list' } }),
  ].join('\n')
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: body })
  assert.equal(lastAssistantTextFrom(transcriptPathFor(SESSION, { dir: root })), 'survives')
})

test('a transcript longer than the tail window still yields the last message', () => {
  // Padding well past TAIL_BYTES, so the read takes the bounded window and starts
  // mid-line — the case where a naive window would parse a truncated record.
  const filler = JSON.stringify({ type: 'user', message: { content: 'x'.repeat(400) } })
  const padding = Array.from({ length: 900 }, () => filler).join('\n')
  const body = [padding, assistant([textBlock('the one that matters')])].join('\n')
  assert.ok(body.length > 256 * 1024, 'fixture must exceed the tail window')
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: body })
  assert.equal(lastAssistantTextFrom(transcriptPathFor(SESSION, { dir: root })), 'the one that matters')
})

test('a large TRAILING record does not hide the last message — the live 700 KB shape', () => {
  // Reproduces the failure the live A/B caught on 2026-09-19, which this suite had missed:
  // the last assistant-with-text record sat at byte 402 426 while a 256 KB window began at
  // byte 438 158, because the records AFTER it were large attachments. The previous test
  // pads with few-hundred-byte lines, so the answer always lands inside the window — the
  // window has to be pushed PAST the answer by records that come after it, and that is the
  // only shape that fails.
  const small = JSON.stringify({ type: 'user', message: { content: 'x'.repeat(200) } })
  const head = Array.from({ length: 100 }, () => small).join('\n')
  const answer = assistant([textBlock('said before the big attachment')])
  const huge = JSON.stringify({ type: 'attachment', blob: 'z'.repeat(400 * 1024) })
  const body = [head, answer, huge].join('\n')
  assert.ok(body.length > 256 * 1024, 'fixture must exceed the tail window')
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: body })
  assert.equal(
    lastAssistantTextFrom(transcriptPathFor(SESSION, { dir: root })),
    'said before the big attachment',
  )
})

test('text is capped at the same length the in-memory path caps it', () => {
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: assistant([textBlock('y'.repeat(5000))]) })
  assert.equal(lastAssistantTextFrom(transcriptPathFor(SESSION, { dir: root })).length, 2000)
})

test('a missing transcript file reads as null', () => {
  assert.equal(lastAssistantTextFrom(join(tmpdir(), 'no-such-transcript-supervisor.jsonl')), null)
})

test('session status comes from the registry, and absence is null rather than a value', () => {
  const registry = () => [
    { sessionId: SESSION, pid: 1, status: 'waiting' },
    { sessionId: OTHER, pid: 2, status: 'busy' },
  ]
  assert.equal(sessionStatusFor(SESSION, { registry }), 'waiting')
  assert.equal(sessionStatusFor(OTHER, { registry }), 'busy')
  assert.equal(sessionStatusFor('unknown-session', { registry }), null)
})

test('awaiting_input is three-way: true, false, and null for unlisted', () => {
  const read = (status) =>
    tabWorkerRead(SESSION, {
      dir: fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: assistant([textBlock('hi')]) }),
      registry: () => (status === null ? [] : [{ sessionId: SESSION, pid: 1, status }]),
    })

  assert.equal(read('waiting').awaiting_input, true)
  assert.equal(read('busy').awaiting_input, false)
  assert.equal(read('idle').awaiting_input, false)
  // The distinction the module exists to preserve: not listed is not the same as not
  // waiting, and reporting it as `false` would be a guess wearing a measurement's clothes.
  assert.equal(read(null).awaiting_input, null)
  assert.equal(read(null).session_status, null)
})

test('tabWorkerRead reports both fields together from their two different sources', () => {
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: assistant([textBlock('parked on a prompt')]) })
  const out = tabWorkerRead(SESSION, {
    dir: root,
    registry: () => [{ sessionId: SESSION, pid: 1, status: 'waiting' }],
  })
  assert.deepEqual(out, {
    last_message: 'parked on a prompt',
    session_status: 'waiting',
    awaiting_input: true,
  })
})
