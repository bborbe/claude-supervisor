// The headless worker's message loop, extracted so it can be tested.
//
// Why this is its own module: the loop's whole job is a timing guarantee — when a
// worker's `status` and its ledger's terminal patch become terminal — and timing
// guarantees are exactly what a live reproduction cannot establish reliably. The
// trigger that motivated this split (an SDK `result` arriving MID-turn, reported
// 2026-09-22) stopped recurring on its own when the bundled CLI moved 2.1.270 →
// 2.1.280, so a fix proven only by "it stopped happening" would be proven by
// absence.
//
// It could not be tested in place. `supervisor.mjs` exports nothing and runs
// `await server.connect(...)` at module load, so importing it from a test starts a
// stdio server; and the loop ran as a fire-and-forget IIFE that nothing outside
// held a handle to. Extracting it here lets a test drive the REAL loop with a fake
// message stream and await its completion. `supervisor.mjs` delegates to this
// function, so the tested code and the shipped code are the same code — a test
// asserting on a loop the server does not call would prove nothing.
//
// Dependencies are injected rather than imported wherever the server owns them
// (`writeLedger`, `log`): both are module-local to `supervisor.mjs`, and importing
// them back would be a cycle. Injecting `writeLedger` also makes the ledger's write
// timing directly observable, which is half of what this module owes.

import { config } from './config.mjs'

/**
 * Consume a headless worker's SDK message stream, recording its transcript and
 * settling its terminal state.
 *
 * Resolves once the stream has ended (or the loop has thrown). It does not reject:
 * a stream error settles the worker as `error` and is logged, because a worker whose
 * stream died must still reach a terminal state rather than assert `running` forever.
 *
 * @param {object}   args
 * @param {AsyncIterable} args.q      the SDK query's message stream
 * @param {object}   args.agent       the in-memory agent record (mutated in place)
 * @param {Function} args.writeLedger `(agent, patch?) => record` — files the durable ledger record
 * @param {Function} args.log         `(...parts) => void` — the server's stderr/file log
 * @returns {Promise<void>}
 */
export async function runAgentLoop({ q, agent, writeLedger, log }) {
  try {
    for await (const message of q) {
      agent.transcript.push(message)
      // The session id arrives with init, which is the first moment a headless
      // worker's ledger record can be filed — unlike a tab worker it has no registry
      // name to be found by, so this is the only place the key exists.
      if (message.type === 'system' && message.subtype === 'init') {
        agent.sessionId = message.session_id
        writeLedger(agent)
      }
      // Captured, NOT settled. An SDK `result` message is not the end of the turn:
      // the CLI can emit one MID-turn, and settling here marked a worker `done`
      // while its transcript was still growing (measured 2026-09-22). The payload is
      // overwritten per result, so the value left after the loop is the last one —
      // which is what the ledger's terminal patch must carry.
      if (message.type === 'result') {
        agent.result = {
          subtype: message.subtype,
          is_error: message.is_error ?? false,
          result: typeof message.result === 'string' ? message.result.slice(0, 4000) : undefined,
          num_turns: message.num_turns,
          // Omitted rather than reported-and-disclaimed. A README caveat does not
          // travel with the value: a manager reading this response sees a confident
          // number describing a billing model its traffic never touched. Absence does
          // travel. Present only when the worker reached Anthropic itself — see
          // costFiguresMeaningful in config.mjs.
          ...(config.costFiguresMeaningful ? { total_cost_usd: message.total_cost_usd } : {}),
          permission_denials: message.permission_denials?.length ?? 0,
        }
      }
    }
    // The turn is over only now that the stream has ended. Settle the terminal
    // `status` and file the ledger's terminal patch here — once, never mid-turn. A
    // stream that ended without any `result` at all still settles as `done`; that is
    // the genuine no-result path, not a fallback for a result the loop failed to
    // settle.
    if (agent.status === 'running') agent.status = 'done'
    if (agent.result) {
      agent.status = agent.result.is_error ? 'error' : 'done'
      writeLedger(agent, {
        status: agent.status,
        ended_at: new Date().toISOString(),
        result: agent.result,
      })
    }
  } catch (error) {
    agent.status = 'error'
    agent.error = String(error)
    log(`agent ${agent.id} threw: ${error}`)
  }
}
