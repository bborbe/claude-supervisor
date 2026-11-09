// Whether a headless worker whose turn has closed should now be stopped.
//
// A wrapper child never exits on its own: it is spawned with `--input-format stream-json` and
// holds its stdin pipe, so after a turn it waits for the next message forever. Measured
// 2026-11-01, 21 of 59 wrapper sessions were non-`exited` and `idle`, the oldest 22 hours —
// because the only thing that ever ended one was undici's five-minute body timeout, which ended
// it wrongly. Removing that timeout makes this decision the ONLY end-of-life a worker has.
//
// It lives here, pure, because the caller cannot be tested: `supervisor.mjs` connects a stdio
// server at load, so the predicate that decides whether a worker dies would otherwise be
// unreachable from any test — which is how the behaviour a change exists to deliver goes
// unasserted while the change looks covered.
//
// ⚠️ **Every keep-alive condition fails CLOSED, and each names its own reason** so a no-op is
// observable in the log rather than silent. A wrong stop orphans a card a person is about to
// answer; a wrong keep leaves a process alive for one more tick. Those are not symmetric.

/**
 * @param {object}  args
 * @param {object|null|undefined} args.session the wrapper's session record, or null when unreadable
 * @param {Array|null|undefined}  args.cards   the session's open attention cards, or null when the store was unreadable
 * @param {object|null}           [args.lastResult] the worker's most recent captured `result`, if any.
 *   A zero-turn one keeps the session (reason `zero-turn-result`) because a resumed CLI can settle
 *   its own synthetic `<task-notification>` before the queued prompt runs.
 * @param {boolean}               [args.zeroTurnGraceExpired] the caller's grace window for that
 *   keep has elapsed; a still-idle, unparked, cardless session is then ended like any other.
 * @returns {{end: boolean, reason: string}}
 */
export function shouldEndIdleWorker({ session, cards, lastResult = null, zeroTurnGraceExpired = false } = {}) {
  if (!session || typeof session !== 'object') return { end: false, reason: 'unreadable-wrapper' }
  // ⚠️ `undefined` is not "no turn in flight" — it is a record this build does not understand.
  // Without this branch an older wrapper that omits either field would make every worker a
  // permanent keep, so the idle-worker leak would quietly survive the fix that exists to remove
  // it. The reason is logged by the caller for exactly that reason.
  if (session.state === undefined || session.pending_turns === undefined) {
    return { end: false, reason: 'wrapper-record-incomplete' }
  }
  if (session.state !== 'idle') return { end: false, reason: 'not-idle' }
  if (session.pending_turns !== 0) return { end: false, reason: 'turn-in-flight' }
  if (session.question) return { end: false, reason: 'parked-question' }
  if (cards === null || cards === undefined) return { end: false, reason: 'unreadable-cards' }
  if (cards.length > 0) return { end: false, reason: 'open-card' }
  // ⚠️ Checked LAST, so every more specific keep reason above still wins. A `result` carrying
  // `num_turns: 0` closed NO turn this server sent: on resume the CLI injects its own
  // `<task-notification>` for background agents the previous process orphaned, ahead of the
  // caller's prompt; that synthetic input settles with a zero-turn `result`, which the wrapper
  // counts against `pending_turns` (claude-wrapper `pkg/supervisor.go` `case "result"`), so the
  // record reads idle with nothing pending while the real prompt still sits in the CLI's queue.
  // Ending here killed that prompt — measured 2026-11-05, agent_12 / agent_13. The keep is
  // bounded: once the caller's grace window has expired and the session is STILL idle with
  // nothing pending, it really did nothing and is ended like any other idle worker.
  if (isZeroTurnResult(lastResult) && !zeroTurnGraceExpired) return { end: false, reason: 'zero-turn-result' }
  return { end: true, reason: 'idle-with-nothing-pending' }
}

const isZeroTurnResult = (result) => Boolean(result) && !result.is_error && result.num_turns === 0

// How long a session whose last result ran zero turns is kept for a queued prompt to start.
export const ZERO_TURN_GRACE_MS = 120_000

/**
 * Arm the bounded keep for a zero-turn result: after `graceMs`, re-run the FULL idle decision
 * (`recheck`, which re-reads the wrapper and the attention store) with the grace marked expired.
 * The timer never stops a session by itself — a turn in flight, a parked question or an open
 * card found by the re-check still keeps the worker. It is a no-op when a newer result has
 * replaced the one it was armed for (that result ran its own decision) or the worker settled.
 *
 * @returns the timer handle
 */
export function armZeroTurnGrace({ agent, recheck, graceMs = ZERO_TURN_GRACE_MS, setTimer = setTimeout }) {
  const armedFor = agent.result
  const handle = setTimer(() => {
    // Only a REPLACED result cancels the keep: a newer result ran its own decision. A settled
    // status must not cancel it — a worker that settled `error: zero-turn` still holds a wrapper
    // child nobody will ever stream again, and this timer is the only reaper it has.
    if (agent.result !== armedFor) return
    return recheck(agent, { zeroTurnGraceExpired: true })
  }, graceMs)
  handle?.unref?.()
  return handle
}

