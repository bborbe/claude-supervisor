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
 * @returns {{end: boolean, reason: string}}
 */
export function shouldEndIdleWorker({ session, cards } = {}) {
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
  return { end: true, reason: 'idle-with-nothing-pending' }
}
