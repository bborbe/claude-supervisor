// The supervisor's half of the A2A push channel: the receive endpoint's lifetime, the
// per-worker token it accepts, and the status store `agent_status` / `list_agents` project.
//
// ⚠️ WHY THIS IS ITS OWN MODULE. `supervisor.mjs` connects a stdio server at load and so cannot
// be imported by a test at all — every rule that lives only in that file ships uncovered. The
// registry is therefore the whole state machine of the push channel, and `supervisor.mjs` keeps
// only the three lines that start it, hand it a token at spawn, and read it back.
//
// ⚠️ THE TOKEN IS MINTED HERE, NOT BY THE CONTROLLER. The party that must VERIFY a credential is
// the party that should issue it: the receiver compares the `A2A-Notification-Token` the SDK's
// push sender sets against this registry's own map, so a controller-chosen token would make the
// receiver trust a value it never generated. The token rides out inside the `create` request and
// is never written to the ledger — `a2aWorkerRecord`'s field allowlist is what keeps it out of
// `list_agents`, and this module never hands it to a writer.

import { randomUUID } from 'node:crypto'
import { createA2AStatusReceiver, callbackUrlFor } from './a2a-receive.mjs'

// One bound per worker, matching the receiver's own. A status channel is a rolling window of
// recent facts, not a log: an unbounded array here is a slow leak in a process that lives for
// the whole session.
export const A2A_PUSH_MAX_MESSAGES = 50

/**
 * Create the push registry.
 *
 * @param {object} [options]
 * @param {() => string} [options.tokenFactory] Mints a per-worker token. Injectable so a test
 *   asserts the exact token that travels out rather than pattern-matching a uuid.
 * @param {Function} [options.receiverFactory] Builds the HTTP receiver. Injectable for the same
 *   reason: the registry's own rules are then testable without a socket.
 * @param {string} [options.callbackHost] The host a container uses to reach this machine's
 *   loopback. Measured, not assumed — see `a2a-receive.mjs`.
 * @param {() => number} [options.now] The clock `age_s` is computed against. Injectable so the
 *   age assertion is exact rather than a range.
 */
export function createA2APushRegistry({
  tokenFactory = () => randomUUID(),
  receiverFactory = createA2AStatusReceiver,
  callbackHost = 'host.docker.internal',
  now = () => Date.now(),
} = {}) {
  // token -> workerId, or `null` while the `create` that minted it has not answered yet. The
  // `null` is load-bearing rather than a placeholder: it is what makes a minted-but-unbound
  // token acceptable to the receiver AND still releasable when its create fails.
  const tokens = new Map()
  // workerId -> entries. Keyed by the TOKEN until `bind` re-keys it, so a push that lands while
  // the create is in flight is buffered rather than refused.
  const messages = new Map()

  let listening = null

  const receiver = receiverFactory({
    // ⚠️ THE PROVISIONAL KEY IS THE TOKEN, AND THAT IS NOT A PLACEHOLDER. Until the controller
    // names a worker id, the token IS the only identity this side holds, and it is a sound one:
    // it is unguessable, single-worker and minted here. Reporting the entry under it and
    // re-keying at `bind` loses nothing; refusing it instead would drop a status the worker
    // really sent, during a window that opens at create-send and closes at create-reply.
    workerTokens: () => {
      const map = new Map()
      for (const [token, workerId] of tokens.entries()) map.set(workerId ?? token, token)
      return map
    },
    onMessage: (identity, entry) => {
      const list = messages.get(identity) ?? []
      list.push(entry)
      if (list.length > A2A_PUSH_MAX_MESSAGES) list.splice(0, list.length - A2A_PUSH_MAX_MESSAGES)
      messages.set(identity, list)
    },
  })

  // Idempotent, and its failure is CACHED rather than retried per spawn: a port that could not
  // be bound will not bind on the next call, and a per-spawn retry would turn one bind failure
  // into one failed syscall per worker for the life of the process.
  const ensureListening = () => {
    if (listening === null) {
      listening = receiver
        .listen()
        .then((port) => ({ port, url: callbackUrlFor({ port, host: callbackHost }) }))
        .catch((cause) => ({
          error: `the A2A status receiver could not bind a loopback port: ${cause?.message ?? cause}`,
        }))
    }
    return listening
  }

  return {
    ensureListening,

    /**
     * Mint this spawn's token and the URL it must be registered against — `{ token, push }`, or
     * `{ error }`.
     *
     * ⚠️ ONE CALL, SO A TOKEN CANNOT EXIST WITHOUT A URL. Minting and URL-building are the same
     * step because a token with no URL registers nothing and reads downstream as a configured
     * channel that is merely quiet — the failure shape this whole task exists to remove.
     */
    async beginCreate() {
      const ready = await ensureListening()
      if (ready.error) return { error: ready.error }
      const token = tokenFactory()
      tokens.set(token, null)
      return { token, push: { url: ready.url, token } }
    },

    /**
     * Attach the controller's worker id to a minted token — `null`, or `{ error }`.
     *
     * Called once the `create` reply names the worker, and it is what moves any buffered entries
     * from the token's provisional key onto the worker's real one.
     */
    bind({ token, workerId } = {}) {
      if (!tokens.has(token)) {
        return { error: 'bind was called for a token this registry never minted.' }
      }
      if (typeof workerId !== 'string' || workerId === '') {
        return { error: `bind requires a non-empty \`workerId\` — got ${JSON.stringify(workerId) ?? 'undefined'}.` }
      }
      tokens.set(token, workerId)
      const buffered = messages.get(token)
      if (buffered) {
        messages.delete(token)
        // The buffered entries are older than anything already filed under the worker id, so
        // they go first — an ordering the receive timestamps would show anyway, but one a
        // reader should not have to re-derive from them.
        const existing = messages.get(workerId) ?? []
        messages.set(workerId, [...buffered, ...existing].slice(-A2A_PUSH_MAX_MESSAGES))
      }
      return null
    },

    /**
     * Drop a token whose `create` failed — `true` when one was dropped, `false` otherwise.
     *
     * ⚠️ ONLY AN UNBOUND TOKEN CAN BE RELEASED. Once a worker id is attached the entries are that
     * worker's record, and dropping them would erase a status a live worker already reported —
     * so a release aimed at a bound token is a no-op rather than a deletion.
     */
    release({ token } = {}) {
      if (tokens.get(token) !== null) return false
      tokens.delete(token)
      messages.delete(token)
      return true
    },

    /** Every status received for this worker, oldest first. */
    statusMessagesFor: (workerId) => messages.get(workerId) ?? [],

    /**
     * The most recent status for this worker with its age — `{worker_id, state, text, ts, age_s}`,
     * or `null` when nothing has arrived.
     *
     * ⚠️ `null` RATHER THAN AN EMPTY OBJECT, and the two must not read alike: `null` is "this
     * worker has reported nothing", which is the ordinary state of every non-A2A worker and of an
     * A2A worker whose first push has not landed. An object with null fields would read as a
     * report that carried no values.
     */
    lastStatusFor(workerId) {
      const list = messages.get(workerId)
      if (!list || list.length === 0) return null
      const entry = list[list.length - 1]
      const receivedAt = Date.parse(entry.ts)
      // A `ts` this side could not parse cannot yield an age, and `NaN` would render as an age
      // of nothing while reading as a number. `null` says the clock is unavailable.
      const ageS = Number.isFinite(receivedAt) ? Math.max(0, Math.round((now() - receivedAt) / 1000)) : null
      return { ...entry, age_s: ageS }
    },

    close: () => receiver.close(),
  }
}
