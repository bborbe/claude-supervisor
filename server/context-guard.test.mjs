// Tests for the resume context guard.
//
// The guard's whole claim is that it selects the population that actually died, so the
// fixtures here are the four recorded failures' real numbers rather than invented ones — a
// threshold test written against made-up sizes proves the arithmetic and nothing about the
// incident. `21c0b4ff` is the load-bearing case: it is the session that died on its FIRST
// turn, and a bound that lets it through is a bound that does not work.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { DEFAULT_RESERVE_TOKENS, contextStatsFrom, readContextStats, resumeAdmission, resumeContextRefusal } from './context-guard.mjs'

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
// All four ended on the same model they compacted under, which is why the per-model ceiling
// still binds them.
const FAILURES = {
  fc025fc5: { boundaries: 35, ceiling: 261_593, restored: 257_924, dropped: 4_990_548 },
  ab98608f: { boundaries: 3, ceiling: 187_295, restored: 399_143, dropped: 489_029 },
  '42e1db23': { boundaries: 24, ceiling: 193_834, restored: 395_456, dropped: 3_344_874 },
  '21c0b4ff': { boundaries: 10, ceiling: 180_136, restored: 157_727, dropped: 1_359_397 },
}

// A transcript reduced to what the guard reads: a turn that names the model, every boundary
// recorded under it, then a final turn at the recorded payload. Enough to exercise the decision
// without 10 000 unrelated records.
//
// ⚠️ The leading `assistant` is load-bearing, not decoration: a boundary is attributed to the
// model in force when it fired, so a fixture that opens with a boundary attributes it to no
// model and the ceiling it produces can never bind. A real transcript always has turns before a
// compaction.
const transcript = ({ boundaries, ceiling, restored, dropped, model = 'deepseek-v4-flash-max' }) => [
  assistant({ input_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }, model),
  ...Array.from({ length: boundaries }, () => boundary(ceiling, { cumulativeDroppedTokens: dropped })),
  assistant({ input_tokens: 0, cache_read_input_tokens: restored, cache_creation_input_tokens: 0 }, model),
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
  const at = contextStatsFrom(transcript({ boundaries: 3, ceiling: 200_000, restored: 200_000 - DEFAULT_RESERVE_TOKENS }))
  assert.equal(resumeContextRefusal({ stats: at }), null)

  const under = contextStatsFrom(transcript({ boundaries: 3, ceiling: 200_000, restored: 200_000 - DEFAULT_RESERVE_TOKENS + 1 }))
  assert.notEqual(resumeContextRefusal({ stats: under }), null)
})

test('a model switch is not a context emergency — the ceiling binds only under the model the session ended on', () => {
  // The real `7231d34d`: 274 k under deepseek, then 315 turns on opus at 690 k. A single global
  // ceiling called that −416 k of room and refused a healthy resume. Measured 2026-10-29.
  const stats = contextStatsFrom([
    assistant({ input_tokens: 1, cache_read_input_tokens: 0 }, 'deepseek-v4-flash-max'),
    boundary(274_111),
    boundary(268_000),
    assistant({ input_tokens: 0, cache_read_input_tokens: 689_784 }, 'claude-opus-5-5'),
  ])
  assert.equal(stats.model, 'claude-opus-5-5')
  assert.equal(stats.ceiling, null, 'no boundary was recorded under opus, so opus has no measured ceiling')
  assert.equal(stats.compactCount, 2, 'both deepseek boundaries are still counted')
  assert.equal(resumeContextRefusal({ stats }), null, 'and the resume is allowed')
})

test('the same session judged under its own model is still refused', () => {
  // Same numbers, but it ended on the model it compacted under — the binding case.
  const stats = contextStatsFrom([
    assistant({ input_tokens: 1, cache_read_input_tokens: 0 }, 'deepseek-v4-flash-max'),
    boundary(274_111),
    boundary(270_000),
    boundary(268_000),
    assistant({ input_tokens: 0, cache_read_input_tokens: 270_000 }, 'deepseek-v4-flash-max'),
  ])
  assert.equal(stats.ceiling, 274_111)
  assert.notEqual(resumeContextRefusal({ stats }), null)
})

test('a session that compacted under both models binds on the one it ended on', () => {
  const stats = contextStatsFrom([
    assistant({ input_tokens: 1, cache_read_input_tokens: 0 }, 'deepseek-v4-flash-max'),
    boundary(180_000),
    assistant({ input_tokens: 0, cache_read_input_tokens: 300_000 }, 'claude-opus-5-5'),
    boundary(700_000),
    boundary(690_000),
    boundary(680_000),
    assistant({ input_tokens: 0, cache_read_input_tokens: 690_000 }, 'claude-opus-5-5'),
  ])
  assert.equal(stats.ceiling, 700_000, 'the opus boundary, not the deepseek one')
  assert.equal(stats.ceilingCount, 3)
  // A big window is not a free pass: 10 k below a 700 k ceiling is as stuck as 10 k below 180 k.
  assert.notEqual(resumeContextRefusal({ stats }), null)
})

test('the thrash fallback refuses a model-switcher that compacted 3+ times, even with no ceiling for its last model', () => {
  // The real `ab98608f`: it compacted three times under opus, switched to deepseek, and died
  // with "Autocompact is thrashing" at 399 k. Its final model has no boundary of its own, so the
  // per-model ceiling alone would have let it through — the count is what catches it.
  const stats = contextStatsFrom([
    assistant({ input_tokens: 1, cache_read_input_tokens: 0 }, 'claude-opus-5-5'),
    boundary(187_295),
    boundary(180_000),
    boundary(177_853),
    assistant({ input_tokens: 0, cache_read_input_tokens: 399_143 }, 'deepseek-v4-flash-max'),
  ])
  assert.equal(stats.ceiling, null, 'no deepseek boundary')
  assert.equal(stats.compactCount, 3)
  const refusal = resumeContextRefusal({ stats })
  assert.ok(refusal, 'refused on the count, not on a measured window')
  assert.match(refusal, /under another model/, 'and the message says the ceiling came from another model')
})

test('two compactions is below the thrash threshold, so a model-switch with that history is allowed', () => {
  // `7231d34d` again, stated as the boundary condition of the fitted constant.
  const stats = contextStatsFrom([
    assistant({ input_tokens: 1, cache_read_input_tokens: 0 }, 'deepseek-v4-flash-max'),
    boundary(274_111),
    boundary(268_000),
    assistant({ input_tokens: 0, cache_read_input_tokens: 689_784 }, 'claude-opus-5-5'),
  ])
  assert.equal(stats.compactCount, 2)
  assert.equal(resumeContextRefusal({ stats }), null)
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
    assistant({ input_tokens: 1, cache_read_input_tokens: 0 }, 'deepseek-v4-flash-max'),
    boundary(180_136),
    boundary(178_000),
    boundary(176_000),
    assistant({ input_tokens: 123, cache_read_input_tokens: 157_604, cache_creation_input_tokens: 0 }, 'deepseek-v4-flash-max'),
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

test('the compaction count in the message is the count under the ceiling model, and reads as a plural', () => {
  const stats = contextStatsFrom(transcript({ boundaries: 3, ceiling: 180_000, restored: 179_000, dropped: 0 }))
  assert.equal(stats.ceilingCount, 3)
  assert.match(resumeContextRefusal({ stats }), /\(3 compactions on that model,/)
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

// --- the admission gate the spawn path actually calls -------------------------------
//
// These drive `resumeAdmission`, which is the single call `spawnAgent`'s resume arm makes: it
// resolves the path, reads it, and decides. A test against `resumeContextRefusal` alone proves
// the arithmetic; these prove the branch, because a refusal here is what the caller turns into
// `return { error }` before any spawn work.

const fixture = (shape) => transcript(shape).map((r) => JSON.stringify(r)).join('\n')

test('the resume gate REFUSES an above-bound session and hands back the reason to return', () => {
  const admission = resumeAdmission({
    transcriptPath: '/t/21c0b4ff.jsonl',
    readFile: () => fixture(FAILURES['21c0b4ff']),
  })
  assert.equal(admission.admit, false, 'the spawn path must not proceed')
  assert.ok(admission.error, 'and it must carry a reason the caller can return')
  assert.match(admission.error, /Open a FRESH worker/)
  assert.equal(admission.stats.restored, 157_727)
})

test('the resume gate ADMITS a below-bound session, so the spawn proceeds', () => {
  const admission = resumeAdmission({
    transcriptPath: '/t/healthy.jsonl',
    readFile: () => fixture({ boundaries: 3, ceiling: 200_000, restored: 80_000, dropped: 100_000 }),
  })
  assert.equal(admission.admit, true)
  assert.equal(admission.error, null, 'nothing for the caller to return')
})

test('the resume gate ADMITS when the transcript cannot be read — unmeasured is not refused', () => {
  const admission = resumeAdmission({
    transcriptPath: '/t/missing.jsonl',
    readFile: () => { throw new Error('ENOENT') },
  })
  assert.equal(admission.admit, true)
  assert.equal(admission.error, null)
  assert.equal(admission.stats, null)
})

test('the resume gate is total: admit and error can never disagree', () => {
  // The caller's branch is `if (!admission.admit) return { error: admission.error }`, so an
  // `admit: false` with no error would return `{ error: undefined }` — a refusal with no reason.
  const shapes = [
    FAILURES['21c0b4ff'],
    FAILURES['ab98608f'],
    { boundaries: 3, ceiling: 200_000, restored: 80_000, dropped: 0 },
    { boundaries: 0, ceiling: null, restored: 5_000, dropped: 0 },
  ]
  for (const shape of shapes) {
    const a = resumeAdmission({ transcriptPath: '/t/x.jsonl', readFile: () => fixture(shape) })
    assert.equal(a.admit, a.error === null, `disagreement on ${JSON.stringify(shape).slice(0, 60)}`)
    if (!a.admit) assert.equal(typeof a.error, 'string')
  }
})
