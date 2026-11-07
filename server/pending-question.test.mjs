// Call-site pins for the status surface's `pending_question`.
//
// Why source-text assertions and not a unit test of the handler: `supervisor.mjs` starts an MCP
// server at import and cannot be imported here. Every behaviour this field exists for is
// exercised on the module side in `wrapper-stream.test.mjs` (`pendingQuestionOf`); what can
// silently regress is the WIRING — a revert to `agentView` alone would leave the module suite
// green with the manager-visible half dead. Same remedy as `open-cards.test.mjs` and
// `registry-rehydrate.test.mjs`.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')

// Without this field a manager cannot obtain the request id `answer_permission` takes, so
// Contract B — answer by id, wrapper's live record authoritative — is unusable from outside.
test('list_agents carries pending_question, read once for the whole roster', () => {
  assert.match(
    src,
    /case 'list_agents': \{[\s\S]{0,1600}?const questions = await readWrapperQuestions\(roster\.some\(\(a\) => a\.wrapperSessionId\)\)/,
  )
  assert.match(
    src,
    /pending_question: pendingQuestionOf\(questions, agent\.wrapperSessionId\)/,
  )
})

test('agent_status carries pending_question', () => {
  assert.match(
    src,
    /case 'agent_status': \{[\s\S]{0,1600}?pending_question: pendingQuestionOf\(questions, agent\.wrapperSessionId\)/,
  )
})

// The read is deliberately skipped when no row is wrapper-backed: otherwise every manager
// sweep in a fleet with no wrapper-backed worker pays a round-trip, and with the wrapper down
// its timeout, for a field that cannot apply.
test('the wrapper is not read when no worker is wrapper-backed', () => {
  assert.match(src, /async function readWrapperQuestions\(needed\) \{\n\s+if \(!needed\) return null/)
})

// ⚠️ `pending_question` IS A STATUS FIELD, NEVER A GATE.
// A parked worker already produces exactly one gate — its attention card (`open_cards`) — and
// the fleet's attention poll reads the store, not agent status. A second surface reading the
// same park as a gate would make one park look like two and invite a duplicate answer.
test('pending_question is never read as a gate', () => {
  // ⚠️ Exact-count, not a sampled scan. The old check looked only 80 chars after each hit and
  // only for a few operators, so a ternary, a `switch (pending_question)` or a comparison
  // further out passed while the test still claimed "never a gate". Every occurrence in the
  // server must now be one of the two projection keys, each fed by `pendingQuestionOf`.
  const keys = [...src.matchAll(/pending_question: pendingQuestionOf\(/g)]
  const all = [...src.matchAll(/pending_question/g)]
  assert.equal(keys.length, 2, 'exactly the two status replies project the field')
  // One further mention is the comment on `wrapper_session_id`; anything else is a new reader.
  assert.equal(all.length, 3, 'a new mention of pending_question needs a justification, not a silent read')
  assert.ok(
    !/\bpending_question\b(?!: pendingQuestionOf)[^\n]*\b(if|switch|while|return|===|!==)\b/.test(src),
    'pending_question must not gate a branch or drive a comparison',
  )
  // The gate the poll actually reads is `awaiting_input`, derived from the session registry.
  // Pinning its source keeps this change from quietly re-pointing the gate at a wrapper park.
  assert.match(src, /awaiting_input: awaitingInput\(sessionStatus\)/)
})

// One wrapper read for the whole roster, never one per worker.
test('the status surface never reads the wrapper once per worker', () => {
  assert.ok(
    !/pending_question: pendingQuestionOf\(await readWrappedSession/.test(src),
    'a per-agent wrapper read would be N round-trips in front of every manager sweep',
  )
})

// ── the answer half, which had no wiring pin at all ─────────────────────────────────────────
// The suite above pins the read surface; nothing pinned that `answer_permission` actually
// reaches the wrapper. A revert to the SDK-only path would leave every test here green while
// the whole feature was unreachable, which is the failure this pin exists to catch.
test('supervisor.mjs routes answer_permission through answerWrapperPark', () => {
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(src, /const viaWrapper = await answerWrapperPark\(args\.request_id, args\.behavior\)/)
  // ⚠️ `sources_consulted` must be COMPUTED, never the constant it used to be: a constant made
  // a wrapper outage and a foreign park print the same sentence.
  assert.ok(
    !/sources_consulted: \['pending', 'wrapper'\],\n\s+\}\)/.test(src),
    'sources_consulted must not be a constant on the refusal path',
  )
  assert.match(src, /const consulted = viaWrapper\.consulted === false \? \['pending'\] : \['pending', 'wrapper'\]/)
  // The outage arm must exist and must not be reported as a foreign park.
  assert.match(src, /if \(viaWrapper\.unreadable\) \{/)
})

// The spawn must reap a wrapper child whose opening turn was refused, or the failed spawn
// leaves an idle process holding stdin that looks exactly like a thinking worker.
test('a refused opening turn reaps the wrapper session', () => {
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(src, /await sendWrappedSession\(\{ baseUrl: config\.wrapperUrl, sessionId: wrapperSession\.id, text: prompt \}\)\n\s+\} catch \(error\) \{\n\s+await stopWrappedSession\(/)
})
