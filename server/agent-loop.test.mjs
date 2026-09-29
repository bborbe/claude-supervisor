// Unit tests for the headless worker's message loop.
//
// The regression these exist for: the loop treated ANY SDK `result` message as the
// end of the turn, so a `result` arriving MID-turn settled the worker as `done` and
// filed its ledger record as terminal (`ended_at` set) while its transcript was
// still growing. Measured 2026-09-22 — a resumed worker read `done` with
// `num_turns: 0` two minutes after spawn while its transcript gained 113 records.
//
// It matters because the failure runs in the dangerous direction: a manager that
// reads `done` right after a spawn concludes the spawn failed, and the retry it
// invites is a SECOND WRITER on one conversation — the exact condition the server's
// resume guard exists to prevent, on a path where the guard is not consulted.
//
// These do NOT replace a live run — they pin the behaviour the server owes without
// needing the CLI to misbehave again. The trigger (a degenerate mid-turn `result`)
// stopped recurring on its own at CLI 2.1.280, so a fix proven only by "it stopped
// happening" would be proven by absence.
//
// The stream carries TWO `result` messages on purpose: with only one, a loop that
// kept the first result's payload would be indistinguishable from one that kept the
// last, and the ledger's content assertion below could not fail.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { runAgentLoop } from './agent-loop.mjs'

// Builds a synthetic message stream and a driver that records the agent's `status`
// after each message has been consumed by the loop — which is the only place the
// timing guarantee is observable. Yielding from an async generator gives the loop a
// suspension point between messages, so the status can be read mid-stream rather
// than only once the stream has ended.
function syntheticStream({ isError = false } = {}) {
  return [
    { type: 'system', subtype: 'init', session_id: 'sess-1' },
    { type: 'assistant', message: { content: 'working' } },
    { type: 'result', subtype: 'success', is_error: isError, result: 'first', num_turns: 1, permission_denials: [] },
    { type: 'assistant', message: { content: 'still working' } },
    { type: 'result', subtype: 'success', is_error: isError, result: 'last', num_turns: 2, permission_denials: [] },
  ]
}

function makeAgent() {
  return { id: 'agent-1', sessionId: null, status: 'running', transcript: [] }
}

// Runs the loop and records `agent.status` after each message the stream delivered.
// Returns the observations plus the ledger calls the loop made.
async function drive({ isError = false } = {}) {
  const agent = makeAgent()
  const messages = syntheticStream({ isError })
  const ledgerCalls = []
  const writeLedger = (...args) => {
    ledgerCalls.push(args)
    return {}
  }

  let delivered = 0
  const observed = []
  async function* observedStream() {
    for (const message of messages) {
      delivered += 1
      yield message
      // Runs once the loop has consumed this message and come back for the next.
      observed.push({ after: delivered, status: agent.status })
    }
  }

  await runAgentLoop({ q: observedStream(), agent, writeLedger, log: () => {} })
  return { agent, observed, ledgerCalls }
}

test('a mid-turn result does not settle the worker as done', async () => {
  const { agent, observed } = await drive()

  // The first `result` is consumed at message 3, with two messages still to come.
  // Against the pre-fix loop this reads 'done' — the defect.
  assert.equal(observed[2].status, 'running', 'status must stay running while the stream is still delivering')
  assert.equal(observed[3].status, 'running', 'status must stay running after a later non-result message')
  assert.equal(agent.status, 'done', 'status must reach done once the stream has ended')
})

test('the ledger terminal patch is written once, after the loop ends', async () => {
  const { ledgerCalls } = await drive()

  // One non-terminal write at init (the session-id record), then exactly one
  // terminal patch — and no terminal patch while the turn was still live.
  const terminal = ledgerCalls.filter(([, patch]) => patch !== undefined)
  assert.equal(terminal.length, 1, 'the terminal patch must be written exactly once, not mid-turn and again at the end')
})

test('the deferred ledger patch carries the last result, not the first', async () => {
  const { ledgerCalls } = await drive()

  const terminal = ledgerCalls.filter(([, patch]) => patch !== undefined)
  assert.equal(terminal.length, 1)
  assert.equal(terminal[0][1].result.result, 'last', 'a mid-turn result must not be filed as the worker outcome')
})

test('a clean stream ending with no result still settles as done', async () => {
  const agent = makeAgent()
  async function* stream() {
    yield { type: 'system', subtype: 'init', session_id: 'sess-1' }
    yield { type: 'assistant', message: { content: 'no result here' } }
  }

  await runAgentLoop({ q: stream(), agent, writeLedger: () => ({}), log: () => {} })
  assert.equal(agent.status, 'done', 'the post-loop no-result fallback must remain the genuine no-result path')
})

test('an is_error result settles the turn as error', async () => {
  const { agent } = await drive({ isError: true })
  assert.equal(agent.status, 'error', 'a result carrying is_error must end the turn as error, not done')
})
