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
  currentToolCallFrom,
  lastAssistantTextFrom,
  sessionStatusFor,
  tabWorkerRead,
  transcriptPathFor,
} from './tab-read.mjs'

const SESSION = '70aa97fc-38c0-4e2c-b531-aa6e6e39d361'
const OTHER = 'c57f50ab-1111-2222-3333-444455556666'

// The launcher's vault, not the cwd the caller passed — the whole reason the path is
// resolved by session id.
const VAULT_PROJECT = '-Users-user-Documents-Obsidian-my-vault'
const TMP_PROJECT = '-private-tmp'

const assistant = (content, timestamp) =>
  JSON.stringify({ type: 'assistant', timestamp, message: { role: 'assistant', content } })

const user = (content, timestamp) =>
  JSON.stringify({ type: 'user', timestamp, message: { role: 'user', content } })

const textBlock = (text) => ({ type: 'text', text })
const toolBlock = () => ({ type: 'tool_use', id: 'toolu_1', name: 'Bash', input: {} })

// A call, and the result that closes it. Every in-flight case below is built from these,
// because "unmatched" is the only thing that makes a call current.
const call = (id, name, input, timestamp) => assistant([{ type: 'tool_use', id, name, input }], timestamp)
const result = (id, timestamp = '2026-09-20T11:10:00.000Z') =>
  user([{ type: 'tool_result', tool_use_id: id, content: 'ok' }], timestamp)

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
    current_tool_call: null,
    session_status: 'waiting',
    awaiting_input: true,
  })
})

// --- the current tool call -------------------------------------------------------------
//
// The regression these exist for: the transcript ALREADY carried the call and the read
// path threw it away — `scanForLastAssistantText` keeps only `type === 'text'` blocks, so
// "what is it doing" was answerable only from a pane. The second case below is the one a
// naive implementation fails: an interrupted call leaves an unmatched `tool_use` behind
// forever, so "any unmatched tool_use" reports a call the worker abandoned while a real
// one is in flight.

const CALL_AT = '2026-09-20T11:09:54.738Z'
const at = (iso) => Date.parse(iso)

test('reports the call in flight, with the duration it has been held', () => {
  const root = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: [
      assistant([textBlock('I will look')], CALL_AT),
      call('toolu_1', 'Bash', { command: 'ls /tmp' }, CALL_AT),
    ].join('\n'),
  })
  assert.deepEqual(
    currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at('2026-09-20T11:25:00.000Z') }),
    {
      name: 'Bash',
      input_summary: 'ls /tmp',
      started_at: CALL_AT,
      held_seconds: 905,
    },
  )
})

test('a call whose result came back is not in flight, however recently it ran', () => {
  const root = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: [
      call('toolu_1', 'Bash', { command: 'ls /tmp' }, CALL_AT),
      result('toolu_1'),
      assistant([textBlock('done')], '2026-09-20T11:10:01.000Z'),
    ].join('\n'),
  })
  assert.equal(currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at('2026-09-20T11:25:00.000Z') }), null)
})

test('an interrupted call does not stay "current" once the worker moves on', () => {
  // The measured shape: the user interrupts mid-call, no `tool_result` is ever written for
  // it, and the conversation continues. Scanning for ANY unmatched `tool_use` names the
  // abandoned call for the rest of the session; the in-flight one is the LAST call, and it
  // is the one with a result — so nothing is in flight.
  const root = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: [
      call('toolu_abandoned', 'Bash', { command: 'sleep 600' }, CALL_AT),
      assistant([textBlock('let me try something else')], '2026-09-20T11:20:00.000Z'),
      call('toolu_2', 'Bash', { command: 'ls' }, '2026-09-20T11:20:01.000Z'),
      result('toolu_2', '2026-09-20T11:20:02.000Z'),
    ].join('\n'),
  })
  assert.equal(currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at('2026-09-20T11:25:00.000Z') }), null)
})

test('with an abandoned call AND a live one, the live one is reported', () => {
  const root = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: [
      call('toolu_abandoned', 'Bash', { command: 'sleep 600' }, CALL_AT),
      assistant([textBlock('trying again')], '2026-09-20T11:20:00.000Z'),
      call('toolu_2', 'Bash', { command: 'git fetch' }, '2026-09-20T11:24:00.000Z'),
    ].join('\n'),
  })
  const out = currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at('2026-09-20T11:38:00.000Z') })
  assert.equal(out.name, 'Bash')
  assert.equal(out.input_summary, 'git fetch')
  assert.equal(out.held_seconds, 840)
})

test('a parked call is in flight too — the transcript names it either way', () => {
  // The parked and the executing case are identical here ON PURPOSE: the transcript cannot
  // tell them apart, and must not pretend to. `session_status` is what separates them; this
  // field only says which call is waiting on something.
  const root = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: [
      call('toolu_1', 'Bash', { command: 'ls /tmp' }, CALL_AT),
      JSON.stringify({ type: 'attachment', timestamp: CALL_AT, attachment: { type: 'hook_success', toolUseID: 'toolu_1' } }),
    ].join('\n'),
  })
  const out = currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at('2026-09-20T11:11:00.000Z') })
  assert.equal(out.name, 'Bash')
  assert.equal(out.input_summary, 'ls /tmp')
  assert.equal(out.held_seconds, 65)
})

test('a call with no timestamp still names the tool, and reports the duration as unknown', () => {
  // "Which tool" and "for how long" fail independently. Dropping the whole field because one
  // half is missing would throw away the half that answered.
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: call('toolu_1', 'Bash', { command: 'ls' }) })
  assert.deepEqual(currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at(CALL_AT) }), {
    name: 'Bash',
    input_summary: 'ls',
    started_at: null,
    held_seconds: null,
  })
})

test('a timestamp ahead of this process clock reads as 0, never as negative', () => {
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: call('toolu_1', 'Bash', { command: 'ls' }, CALL_AT) })
  assert.equal(currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at('2026-09-20T11:00:00.000Z') }).held_seconds, 0)
})

test('a tool_result line is not read as a call — the id key shares the call type prefix', () => {
  // `{"tool_use_id": …}` contains the substring `"tool_use` but not the type value, and a
  // scan that matches the prefix counts every result as a call.
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: result('toolu_1') })
  assert.equal(currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at(CALL_AT) }), null)
})

test('the Bash command is the summary; any other input falls back to compact JSON', () => {
  const root = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: call('toolu_1', 'TaskOutput', { block: true, timeout: 200000 }, CALL_AT),
  })
  assert.equal(
    currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at(CALL_AT) }).input_summary,
    '{"block":true,"timeout":200000}',
  )
})

test('a multi-line command is flattened and capped, so it survives a table cell', () => {
  const root = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: call('toolu_1', 'Bash', { command: 'line one\n  line two' }, CALL_AT),
  })
  assert.equal(currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at(CALL_AT) }).input_summary, 'line one line two')

  const long = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: call('toolu_1', 'Bash', { command: 'x'.repeat(900) }, CALL_AT),
  })
  assert.equal(currentToolCallFrom(transcriptPathFor(SESSION, { dir: long }), { now: at(CALL_AT) }).input_summary.length, 200)
})

test('an empty input reads as null, not as the string "{}"', () => {
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: call('toolu_1', 'Bash', {}, CALL_AT) })
  assert.equal(currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at(CALL_AT) }).input_summary, null)
})

test('a call at the end of a transcript longer than the tail window is still found', () => {
  const filler = JSON.stringify({ type: 'user', timestamp: CALL_AT, message: { content: 'x'.repeat(400) } })
  const padding = Array.from({ length: 900 }, () => filler).join('\n')
  const body = [padding, call('toolu_1', 'Bash', { command: 'ls /tmp' }, CALL_AT)].join('\n')
  assert.ok(body.length > 256 * 1024, 'fixture must exceed the tail window')
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: body })
  const out = currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at('2026-09-20T11:10:54.738Z') })
  assert.equal(out.input_summary, 'ls /tmp')
  assert.equal(out.held_seconds, 60)
})

test('a malformed line does not take the whole read down', () => {
  const body = ['{not json at all', call('toolu_1', 'Bash', { command: 'survives' }, CALL_AT)].join('\n')
  const root = fixture({ [`${VAULT_PROJECT}/${SESSION}.jsonl`]: body })
  assert.equal(currentToolCallFrom(transcriptPathFor(SESSION, { dir: root }), { now: at(CALL_AT) }).input_summary, 'survives')
})

test('no transcript and no session id both read as no call, not as an error', () => {
  assert.equal(currentToolCallFrom(join(tmpdir(), 'no-such-transcript-supervisor.jsonl'), { now: at(CALL_AT) }), null)
  assert.equal(currentToolCallFrom(transcriptPathFor(null, { dir: tmpdir() }), { now: at(CALL_AT) }), null)
})

test('tabWorkerRead carries the in-flight call alongside the message and the registry state', () => {
  const root = fixture({
    [`${VAULT_PROJECT}/${SESSION}.jsonl`]: [
      assistant([textBlock('asking first')], CALL_AT),
      call('toolu_1', 'Bash', { command: 'ls /tmp' }, CALL_AT),
    ].join('\n'),
  })
  const out = tabWorkerRead(SESSION, {
    dir: root,
    now: at('2026-09-20T11:14:54.738Z'),
    registry: () => [{ sessionId: SESSION, pid: 1, status: 'waiting' }],
  })
  assert.deepEqual(out, {
    last_message: 'asking first',
    current_tool_call: { name: 'Bash', input_summary: 'ls /tmp', started_at: CALL_AT, held_seconds: 300 },
    session_status: 'waiting',
    awaiting_input: true,
  })
})
