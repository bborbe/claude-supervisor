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
 * @param {object}   [args.heartbeat] `{start, stop}` — publishes this worker as live to a process
 *                                    that did not spawn it; the no-op when a caller has none.
 * @param {Function} [args.onTurnComplete] `async (agent) => void` — fired AFTER each `result`
 *                                    payload is captured, and awaited. ⚠️ It must not assume the
 *                                    turn is over: a `result` can arrive mid-turn, so the hook
 *                                    re-reads the wrapper and decides for itself. A hook that
 *                                    throws is logged and swallowed — the turn's outcome is
 *                                    already captured, and turning a hook bug into an `error`
 *                                    status would recreate the defect this loop was fixed for.
 * @returns {Promise<void>}
 */
// The no-op a caller that does not publish liveness gets, so the loop stays drivable from a
// test with a synthetic stream and no filesystem.
const NO_HEARTBEAT = { start() {}, stop() {} }

export async function runAgentLoop({ q, agent, writeLedger, log, heartbeat = NO_HEARTBEAT, onTurnComplete = null }) {
  try {
    for await (const message of q) {
      agent.transcript.push(message)
      // The session id arrives with init, which is the first moment a headless
      // worker's ledger record can be filed — unlike a tab worker it has no registry
      // name to be found by, so this is the only place the key exists.
      if (message.type === 'system' && message.subtype === 'init') {
        agent.sessionId = message.session_id
        // The first moment this worker can be published as live to a process that did not
        // spawn it. The stamp is keyed by session id and the id does not exist until here,
        // so this cannot happen at spawn. See heartbeat.mjs.
        heartbeat.start(agent.sessionId)
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
        // Fires AFTER the payload is captured, so a hook that ends the session cannot swallow the
        // turn it is ending. ⚠️ It must NOT assume the turn is over: a `result` can arrive
        // mid-turn (see the header), so the hook re-reads the wrapper and decides for itself.
        // A hook that throws must not settle the worker — the turn's outcome is already captured,
        // and turning a hook bug into an `error` status would recreate the defect this fixes.
        if (onTurnComplete) {
          try {
            await onTurnComplete(agent)
          } catch (error) {
            log(`agent ${agent.id} turn-complete hook threw: ${error}`)
          }
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
    // ⚠️ **A throw AFTER the turn's `result` was captured is not a worker error.** The stream can
    // die for reasons that say nothing about the work — the wrapper restarted, a proxy dropped the
    // connection, the subscriber was torn down — while the turn it was reporting genuinely
    // completed. Settling that as `error` records a finished worker as a dead one, which is what
    // every session in the wrapper-termination defect has in common: `result.subtype: success`
    // alongside a stream failure.
    //
    // ⚠️ **Both branches now write a terminal row, and the old one wrote NONE.** A throw used to
    // leave the durable row asserting `running` forever — a record that outlives the server which
    // wrote it, that no later reader can correct, and that `server/ledger.mjs` explicitly warns
    // must never be read as proof of life. That leak was independent of the classification above:
    // any other throw reached it too.
    // ⚠️ **A `StreamLaggedError` is never a completed turn, whatever was captured.** It is raised
    // precisely when the stream stopped while the child was still generating — a subscriber lag, a
    // wrapper restart, a dropped connection — and `wrapper-stream.mjs` exists to keep that
    // distinguishable from an exit. A `result` can arrive MID-turn (see the header), so its
    // presence does not prove the turn closed; the absence of this error is what does. Settling it
    // `done` would resurrect the exact "a lag looks like a finished worker" defect the adapter was
    // built to prevent, which is a worse failure than the `error` it replaces.
    const terminal = { ended_at: new Date().toISOString() }
    const turnClosed = agent.result && !agent.result.is_error && error?.name !== 'StreamLaggedError'
    if (turnClosed) {
      agent.status = 'done'
      writeLedger(agent, { ...terminal, status: 'done', result: agent.result })
      log(`agent ${agent.id} stream ended after a completed turn: ${error}`)
    } else {
      agent.status = 'error'
      agent.error = String(error)
      writeLedger(agent, { ...terminal, status: 'error', error: agent.error })
      log(`agent ${agent.id} threw: ${error}`)
    }
  } finally {
    // Covers BOTH endings — the settle above and the throw here — so a worker that errors
    // does not keep a live stamp behind it. This is the graceful half ONLY: a server killed
    // with `kill -9` never reaches this line, and the stamp it leaves is read as stale by
    // age. That asymmetry is the design, not an omission — see heartbeat.mjs.
    if (agent.sessionId) heartbeat.stop(agent.sessionId)
  }
}
