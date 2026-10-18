// A2A push-notification receiver for the supervisor.
//
// A worker reports its own progress by POSTing to a callback the controller registered
// against its A2A endpoint. Until now the supervisor had no network surface at all — it
// speaks stdio to its MCP client and holds worker records in memory — so the callback had
// nowhere to land and every status fact was pulled from the worker's reply instead. This
// module is that landing place.
//
// Why the receiver lives in each supervisor server rather than in one shared process: the
// push has to land in the records of the session that SPAWNED the worker, because that is
// the session whose `agent_status` / `list_agents` must show it. The callback URL already
// travels per create, so a per-server listener needs no worker->session routing table; a
// shared receiver would need one that does not exist today.

import { createServer } from 'node:http'
import { timingSafeEqual } from 'node:crypto'

// The header the A2A SDK's push sender sets. Canonicalised, because Node lower-cases
// incoming header names and a raw `req.headers['A2A-Notification-Token']` lookup silently
// returns undefined — the same trap the SDK's own `http.CanonicalHeaderKey` guards on the
// sending side.
const TOKEN_HEADER = 'a2a-notification-token'

// A status message is kept per worker, bounded, oldest-first. Unbounded growth here would
// be a slow leak in a process that lives for the whole session.
const MAX_MESSAGES_PER_WORKER = 50

// Constant-time compare, and length-checked first because timingSafeEqual throws on a
// length mismatch — which would turn a wrong-length token into a 500 instead of a 401.
function tokenMatches(presented, expected) {
  if (typeof presented !== 'string' || typeof expected !== 'string') return false
  const a = Buffer.from(presented)
  const b = Buffer.from(expected)
  if (a.length !== b.length) return false
  return timingSafeEqual(a, b)
}

// The pushed body is the SDK's `StreamResponse` union envelope: exactly one top-level key
// naming the event discriminator (`statusUpdate`, `task`, `artifactUpdate`, `message`).
// There is deliberately no `event` key — an earlier spec asserted one and was wrong; a body
// keyed on `event` is a bespoke shape and is refused rather than guessed at.
function parseStreamResponse(raw) {
  let body
  try {
    body = JSON.parse(raw)
  } catch {
    return { error: 'body is not JSON' }
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    return { error: 'body is not a JSON object' }
  }
  const keys = Object.keys(body)
  if (keys.length !== 1) {
    return { error: `expected exactly one envelope key, got ${keys.length}` }
  }
  const [kind] = keys
  if (!['statusUpdate', 'task', 'artifactUpdate', 'message'].includes(kind)) {
    return { error: `unknown envelope key ${JSON.stringify(kind)}` }
  }
  return { kind, payload: body[kind] }
}

// Text of the mid-turn status. The worker builds the message through the SDK's own message
// constructors, so the text lives under `status.message.parts[].text`. A status with no
// text is still a valid state transition — it just carries no `text` field rather than an
// empty string, so a caller can tell "said nothing" from "said an empty thing".
function statusText(payload) {
  const parts = payload?.status?.message?.parts
  if (!Array.isArray(parts)) return null
  const text = parts
    .map((part) => (typeof part?.text === 'string' ? part.text : ''))
    .join('')
    .trim()
  return text === '' ? null : text
}

/**
 * Create the push receiver.
 *
 * @param {object} options
 * @param {() => Map<string, string>} options.workerTokens
 *   Maps worker id -> the per-worker token the controller registered. A token that is not
 *   in this map is refused: an unknown worker must not be able to write into the records
 *   of a session it was not spawned by.
 * @param {(workerId: string, entry: object) => void} options.onMessage
 *   Called once per accepted status. The caller owns the store.
 * @param {string} [options.host] Loopback by default. The container reaches the host
 *   through the Docker gateway (`host.docker.internal`), which was measured to reach a
 *   listener bound to 127.0.0.1 only — so a loopback bind is sufficient and is the
 *   narrowest surface that works.
 */
export function createA2AStatusReceiver({ workerTokens, onMessage, host = '127.0.0.1' } = {}) {
  if (typeof workerTokens !== 'function') {
    throw new Error('createA2AStatusReceiver: workerTokens must be a function returning worker id -> token')
  }
  if (typeof onMessage !== 'function') {
    throw new Error('createA2AStatusReceiver: onMessage must be a function')
  }

  const messages = new Map()

  const server = createServer((req, res) => {
    const respond = (code, payload) => {
      res.writeHead(code, { 'content-type': 'application/json' })
      res.end(JSON.stringify(payload))
    }

    if (req.method !== 'POST') {
      respond(405, { error: 'method not allowed' })
      return
    }

    const presented = req.headers[TOKEN_HEADER]
    const tokens = workerTokens() ?? new Map()

    // Resolve the worker from the token, never from the body: the body is worker-supplied,
    // so a `worker_id` field in it would let one worker claim another's identity. The token
    // is the credential and the identity together.
    let workerId = null
    for (const [id, token] of tokens.entries()) {
      if (tokenMatches(presented, token)) {
        workerId = id
        break
      }
    }
    if (workerId === null) {
      respond(401, { error: 'unknown worker or bad token' })
      return
    }

    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      // A push body is a task event, not a payload — a megabyte here is a misbehaving
      // caller, and reading it all would let one worker exhaust the supervisor's memory.
      if (size > 256 * 1024) {
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      if (size > 256 * 1024) return
      const parsed = parseStreamResponse(Buffer.concat(chunks).toString('utf8'))
      if (parsed.error) {
        respond(400, { error: parsed.error })
        return
      }
      if (parsed.kind !== 'statusUpdate') {
        // Accepted, not stored: the record is a status channel, and a task/artifact/message
        // event is not a status. 200 rather than 4xx so the SDK does not treat a perfectly
        // valid event as a delivery failure.
        respond(200, { accepted: true, stored: false, kind: parsed.kind })
        return
      }
      const entry = {
        worker_id: workerId,
        state: parsed.payload?.status?.state ?? null,
        text: statusText(parsed.payload),
        // The worker's own send instant is NOT used: the SDK sends no such field, and a
        // body-supplied `ts` would be worker-asserted. This is the receive instant, which
        // is the only timestamp this side can vouch for.
        ts: new Date().toISOString(),
      }
      const list = messages.get(workerId) ?? []
      list.push(entry)
      if (list.length > MAX_MESSAGES_PER_WORKER) list.splice(0, list.length - MAX_MESSAGES_PER_WORKER)
      messages.set(workerId, list)
      onMessage(workerId, entry)
      respond(200, { accepted: true, stored: true })
    })
    req.on('error', () => {
      // A client that vanished mid-body is not the supervisor's failure to report.
    })
  })

  return {
    server,
    messagesFor: (workerId) => messages.get(workerId) ?? [],
    listen: () =>
      new Promise((resolve, reject) => {
        server.once('error', reject)
        server.listen(0, host, () => resolve(server.address().port))
      }),
    close: () => new Promise((resolve) => server.close(resolve)),
  }
}

/**
 * The callback URL a worker is told to POST to. `host.docker.internal` is how a container
 * reaches a listener bound to the host's loopback — measured, not assumed — and the port is
 * the receiver's own ephemeral port.
 */
export function callbackUrlFor({ port, host = 'host.docker.internal' } = {}) {
  return `http://${host}:${port}/a2a/push`
}
