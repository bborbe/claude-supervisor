// Tests for the resume context guard.
//
// The guard's whole claim is that it selects the population that actually died, so the
// fixtures here are the four recorded failures' real numbers rather than invented ones — a
// threshold test written against made-up sizes proves the arithmetic and nothing about the
// incident. `21c0b4ff` is the load-bearing case: it is the session that died on its FIRST
// turn, and a bound that lets it through is a bound that does not work.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { DEFAULT_RESERVE_TOKENS, contextStatsFrom, readContextStats, resumeContextRefusal } from './context-guard.mjs'

// An assistant record carrying a usage block, in the shape the transcript actually writes.
const assistant = (usage, model = 'claude-opus-5-5') => ({ type: 'assistant', message: { model, usage } })

// A compaction boundary, in the shape the transcript actually writes.
const boundary = (preTokens, { postTokens = 25_000, cumulativeDroppedTokens = 0 } = {}) => ({
  type: 'system',
  subtype: 'compact_boundary',
  compactMetadata: { trigger: 'auto', preTokens, postTokens, cumulativeDroppedTokens },
})

// The four sessions from 2026-10-27, with the numbers measured from their transcripts on
// 2026-10-29. `restored` is the last assistant usage total; `ceiling` is the largest preTokens.
const FAILURES = {
  fc025fc5: { boundaries: 35, ceiling: 261_593, restored: 257_924, dropped: 4_990_548 },
  ab98608f: { boundaries: 3, ceiling: 187_295, restored: 399_143, dropped: 489_029 },
  '42e1db23': { boundaries: 24, ceiling: 193_834, restored: 395_456, dropped: 3_344_874 },
  '21c0b4ff': { boundaries: 10, ceiling: 180_136, restored: 157_727, dropped: 1_359_397 },
}

// A transcript reduced to what the guard reads: every boundary, then a final turn at the
// recorded payload. Enough to exercise the decision without 10 000 unrelated records.
const transcript = ({ boundaries, ceiling, restored, dropped }) => [
  ...Array.from({ length: boundaries }, () => boundary(ceiling, { cumulativeDroppedTokens: dropped })),
  assistant({ input_tokens: 0, cache_read_input_tokens: restored, cache_creation_input_tokens: 0 }),
]

test('every recorded failure is refused — including the one that died on its first turn', () => {
  for (const [session, shape] of Object.entries(FAILURES)) {
    const stats = contextStatsFrom(transcript(shape))
    const refusal = resumeContextRefusal({ stats })
    assert.ok(refusal, `${session} must be refused`)
    // The refusal has to be actionable, not just a "no": it names the fallback.
    assert.match(refusal, /Open a FRESH worker/)
    assert.match(refusal, /Prompt is too long/)
  }
})

test('21c0b4ff sits under the bound by 22 409 tokens, so the margin is real and not luck', () => {
  const stats = contextStatsFrom(transcript(FAILURES['21c0b4ff']))
  assert.equal(stats.ceiling - stats.restored, 22_409)
  assert.ok(stats.ceiling - stats.restored < DEFAULT_RESERVE_TOKENS)
})

test('a session with room to work is allowed', () => {
  const stats = contextStatsFrom(transcript({ boundaries: 1, ceiling: 180_000, restored: 60_000, dropped: 90_000 }))
  assert.equal(resumeContextRefusal({ stats }), null)
})

test('the reserve is the boundary: exactly one working set of room passes, one token less does not', () => {
  const at = contextStatsFrom(transcript({ boundaries: 1, ceiling: 200_000, restored: 200_000 - DEFAULT_RESERVE_TOKENS }))
  assert.equal(resumeContextRefusal({ stats: at }), null)

  const under = contextStatsFrom(transcript({ boundaries: 1, ceiling: 200_000, restored: 200_000 - DEFAULT_RESERVE_TOKENS + 1 }))
  assert.notEqual(resumeContextRefusal({ stats: under }), null)
})

test('a session that has never compacted is allowed — no boundary means no measured ceiling', () => {
  const stats = contextStatsFrom([assistant({ input_tokens: 10, cache_read_input_tokens: 190_000 })])
  assert.equal(stats.compactCount, 0)
  assert.equal(stats.ceiling, null)
  assert.equal(resumeContextRefusal({ stats }), null)
})

test('a transcript with no measurable context at all is null, not a zero-sized session', () => {
  assert.equal(contextStatsFrom([]), null)
  assert.equal(contextStatsFrom([{ type: 'user', message: { content: 'hi' } }]), null)
  assert.equal(resumeContextRefusal({ stats: null }), null)
})

test('the payload is the SUM of the three usage fields, not input_tokens alone', () => {
  // The bug this guards: a real 158 k turn reports input_tokens: 0 and the bulk in cache_read.
  // Reading only input_tokens would make every session look empty and disable the guard.
  const stats = contextStatsFrom([
    boundary(180_136),
    assistant({ input_tokens: 123, cache_read_input_tokens: 157_604, cache_creation_input_tokens: 0 }),
  ])
  assert.equal(stats.restored, 157_727)
  assert.notEqual(resumeContextRefusal({ stats }), null)
})

test('the last turn wins, and a zero-usage synthetic record does not erase it', () => {
  const stats = contextStatsFrom([
    boundary(180_000),
    assistant({ input_tokens: 0, cache_read_input_tokens: 150_000 }),
    assistant({ input_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }),
  ])
  assert.equal(stats.restored, 150_000)
})

test('the ceiling is the LARGEST boundary — a session that compacted early and late is judged on its late size', () => {
  const stats = contextStatsFrom([boundary(167_000), boundary(261_593), boundary(180_000)])
  assert.equal(stats.ceiling, 261_593)
  assert.equal(stats.compactCount, 3)
})

test('cumulativeDroppedTokens is the maximum reported, and the count is every boundary', () => {
  const stats = contextStatsFrom([
    boundary(167_000, { cumulativeDroppedTokens: 143_399 }),
    boundary(168_000, { cumulativeDroppedTokens: 847_268 }),
  ])
  assert.equal(stats.compactCount, 2)
  assert.equal(stats.droppedTokens, 847_268)
})

test('the refusal quotes the measurement, so a reader can re-derive it', () => {
  const stats = contextStatsFrom(transcript(FAILURES['21c0b4ff']))
  const refusal = resumeContextRefusal({ stats })
  assert.match(refusal, /22,409 tokens of room/)
  assert.match(refusal, /157,727 tokens/)
  assert.match(refusal, /180,136/)
  assert.match(refusal, /10 compactions/)
})

test('a single compaction is not pluralised wrongly', () => {
  const stats = contextStatsFrom(transcript({ boundaries: 1, ceiling: 180_000, restored: 179_000, dropped: 0 }))
  assert.match(resumeContextRefusal({ stats }), /1 compaction,/)
})

test('the reader parses the JSONL and hands the same stats the decision uses', () => {
  const shape = FAILURES['21c0b4ff']
  const lines = transcript(shape).map((r) => JSON.stringify(r))
  const readFile = (path, encoding) => {
    assert.equal(path, '/t/21c0b4ff.jsonl')
    assert.equal(encoding, 'utf8')
    return lines.join('\n')
  }
  const stats = readContextStats('/t/21c0b4ff.jsonl', { readFile })
  assert.deepEqual(stats, contextStatsFrom(transcript(shape)))
  assert.notEqual(resumeContextRefusal({ stats }), null)
})

test('the reader skips a torn final line instead of losing the whole transcript', () => {
  const lines = transcript(FAILURES['21c0b4ff']).map((r) => JSON.stringify(r))
  // A session appends while it runs, so the last line is routinely half-written.
  const stats = readContextStats('/t/x.jsonl', { readFile: () => `${lines.join('\n')}\n{"type":"assis` })
  assert.equal(stats.restored, 157_727)
})

test('an unreadable or absent transcript is null — not measured, not zero', () => {
  assert.equal(readContextStats('/t/missing.jsonl', { readFile: () => { throw new Error('ENOENT') } }), null)
  assert.equal(readContextStats(null, { readFile: () => '{}' }), null)
  // No injected reader means no read: the default is deliberate, so an unwired caller
  // reports "not measured" rather than reaching for the filesystem behind the caller's back.
  assert.equal(readContextStats('/t/x.jsonl'), null)
})

test('the model is recorded for the caller, and a synthetic record never becomes it', () => {
  const stats = contextStatsFrom([
    assistant({ input_tokens: 1, cache_read_input_tokens: 1 }, 'deepseek-v4-flash-max'),
    assistant({ input_tokens: 2, cache_read_input_tokens: 2 }, '<synthetic>'),
  ])
  assert.equal(stats.model, 'deepseek-v4-flash-max')
})
