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

// ⚠️ The `catch` used to do two wrong things at once, and both are pinned here because a fix
// for one leaves the other reachable: it classified EVERY throw as a worker error — so a stream
// that died after the turn's `result` had been captured recorded a finished worker as a dead one
// — and it wrote NO ledger row at all, so the durable record kept asserting `running` forever,
// which `ledger.mjs` names as the reading it must never be trusted for.
//
// Measured 2026-11-01 on the deployed build: three runs of three threw `TypeError: terminated`
// 300.85 s / 301.27 s / 301.18 s after a `success` result, with the child still `idle` afterwards.
// The trigger was undici's default five-minute `bodyTimeout` on a legitimately idle SSE body —
// fixed in `wrapper-stream.mjs` — but this classification must not depend on that trigger staying
// away, which is why it is asserted directly rather than through the wrapper.
async function driveThrow({ withResult = true, isError = false } = {}) {
  const agent = makeAgent()
  const ledgerCalls = []
  async function* stream() {
    yield { type: 'system', subtype: 'init', session_id: 'sess-1' }
    if (withResult) {
      yield { type: 'result', subtype: 'success', is_error: isError, result: 'done work', num_turns: 1, permission_denials: [] }
    }
    throw new TypeError('terminated')
  }
  await runAgentLoop({
    q: stream(),
    agent,
    writeLedger: (...args) => {
      ledgerCalls.push(args)
      return {}
    },
    log: () => {},
  })
  return { agent, ledgerCalls }
}

test('a stream that dies after a completed turn settles done, not error', async () => {
  const { agent, ledgerCalls } = await driveThrow()

  assert.equal(agent.status, 'done', 'a throw after a captured success result must not record a finished worker as dead')
  const terminal = ledgerCalls.filter(([, patch]) => patch !== undefined)
  assert.equal(terminal.length, 1, 'the terminal row must be written even when the stream threw')
  assert.equal(terminal[0][1].status, 'done')
  assert.equal(terminal[0][1].result.result, 'done work', 'the row must carry the result the turn actually produced')
  assert.ok(terminal[0][1].ended_at, 'a terminal row without ended_at is not terminal')
  assert.equal(terminal[0][1].error, undefined, 'a completed turn must not file an error field')
})

test('a stream that dies with no result settles error AND writes the terminal row', async () => {
  const { agent, ledgerCalls } = await driveThrow({ withResult: false })

  assert.equal(agent.status, 'error')
  const terminal = ledgerCalls.filter(([, patch]) => patch !== undefined)
  assert.equal(terminal.length, 1, 'the old catch wrote no row at all, which left the durable record at running forever')
  assert.equal(terminal[0][1].status, 'error')
  assert.match(terminal[0][1].error, /terminated/)
})

test('a stream that dies after an is_error result settles error', async () => {
  const { agent } = await driveThrow({ isError: true })
  assert.equal(agent.status, 'error', 'an is_error result stays an error even when the stream dies afterwards')
})

// The turn-complete hook is the deliberate end-of-life: a wrapper child never exits on its own,
// so the supervisor stops a worker whose turn has closed and which has nothing parked. The loop
// owes the hook two things — it fires AFTER the payload is captured, so a hook that ends the
// session cannot swallow the turn it is ending, and it fires per `result`, because only the hook
// can tell a mid-turn `result` from a closed turn (it re-reads the wrapper's `pending_turns`).
test('the turn-complete hook runs after the payload is captured, once per result', async () => {
  const agent = makeAgent()
  const seen = []
  async function* stream() {
    yield { type: 'system', subtype: 'init', session_id: 'sess-1' }
    yield { type: 'result', subtype: 'success', is_error: false, result: 'first', num_turns: 1, permission_denials: [] }
    yield { type: 'assistant', message: { content: 'still working' } }
    yield { type: 'result', subtype: 'success', is_error: false, result: 'last', num_turns: 2, permission_denials: [] }
  }

  await runAgentLoop({
    q: stream(),
    agent,
    writeLedger: () => ({}),
    log: () => {},
    onTurnComplete: async (current) => {
      seen.push(current.result?.result)
    },
  })

  assert.deepEqual(seen, ['first', 'last'], 'the hook must see the captured payload, and must fire per result')
})

test('a throwing turn-complete hook does not settle the worker as error', async () => {
  const agent = makeAgent()
  async function* stream() {
    yield { type: 'system', subtype: 'init', session_id: 'sess-1' }
    yield { type: 'result', subtype: 'success', is_error: false, result: 'done work', num_turns: 1, permission_denials: [] }
  }

  await runAgentLoop({
    q: stream(),
    agent,
    writeLedger: () => ({}),
    log: () => {},
    onTurnComplete: () => {
      throw new Error('hook bug')
    },
  })

  assert.equal(agent.status, 'done', 'a hook bug must not be recorded as a worker error — the turn completed')
})
