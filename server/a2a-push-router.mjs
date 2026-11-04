// The one listener a cluster worker's status callback can reach, fanning each push out to the
// supervisor session that spawned the worker.
//
// ⚠️ WHY THIS EXISTS, AND WHY IT IS NOT PART OF THE RECEIVER. A `k8s` worker reaches this
// machine through an in-cluster relay, and a relay's upstream is deployed configuration — a
// fixed host and port. A fixed port admits exactly ONE process, so the first supervisor server
// to bind it holds it for the life of the machine and every other session's spawns get
// `push_registration: not-attempted` with `listen EADDRINUSE` — silently, because the channel
// is additive by design. Measured 2026-11-03: nineteen servers carried the config and eighteen
// were in that state. An ephemeral per-session port cannot be the relay's upstream, so the
// stable endpoint and the per-session isolation have to be separated: one process owns the
// stable address, and each session keeps its own receiver on loopback.
//
// ⚠️ THE ROUTING KEY IS THE TOKEN, NOT THE BODY. `a2a-receive.mjs` resolves the worker from
// the `a2a-notification-token` header and never from the body, because a body-supplied
// `worker_id` would let one worker claim another's identity. The router inherits that rule
// exactly: it forwards by the header it received, and it never parses the payload — a router
// that read the body would reintroduce the identity confusion the receiver exists to prevent.
//
// ⚠️ REGISTRATION IS LOOPBACK-ONLY, AND THAT IS WHY THE DEFAULT BIND IS ALL INTERFACES RATHER
// THAN THE LAN ADDRESS. A registration is a routing claim — "send this token's pushes to my
// port" — so accepting one from off-machine would let a remote party intercept another worker's
// status; only a loopback peer may register, while a push may arrive from anywhere the network
// allows. But binding a *specific* LAN address makes loopback unreachable, and a same-machine
// supervisor registering at `http://<lan>:<port>` is then seen by the kernel with the LAN
// source address, so the gate refuses its own machine's registration and every cluster spawn
// loses its channel. Binding all interfaces keeps both reachable and lets the gate stay strict:
// the relay dials the LAN address, sessions register at `http://127.0.0.1:<port>`.

import { createServer, request as httpRequest } from 'node:http'

/** The header the SDK's push sender sets. Same name `a2a-receive.mjs` reads. */
export const TOKEN_HEADER = 'a2a-notification-token'

export const REGISTER_PATH = '/register'
export const PUSH_PATH = '/a2a/push'

/** How long a forwarding call may take before it is abandoned. */
const FORWARD_TIMEOUT_MS = 10_000

/**
 * Is this socket address the local machine?
 *
 * `::ffff:127.0.0.1` is what a dual-stack listener reports for an IPv4 loopback peer, and it
 * is NOT matched by a plain `127.0.0.1` comparison — a check that missed it would refuse every
 * legitimate registration on such a listener.
 */
export function isLoopback(address) {
  if (typeof address !== 'string') return false
  const bare = address.startsWith('::ffff:') ? address.slice('::ffff:'.length) : address
  return bare === '127.0.0.1' || bare === '::1' || bare === 'localhost'
}

function readBody(req, limit = 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > limit) {
        reject(new Error(`body exceeded ${limit} bytes`))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

/**
 * Create the push router.
 *
 * @param {object} [options]
 * @param {string} [options.host] Where to bind. A LAN address, so the relay can reach it.
 * @param {number} [options.port] The stable port the relay's upstream names.
 * @param {(target: {port: number, body: Buffer, headers: object}) => Promise<{status: number, body: string}>}
 *   [options.forward] Injected so the routing rules are testable without a second socket.
 */
/** Hop-by-hop headers must not be replayed (RFC 7230 §6.1) — see `forwardOverLoopback`. */
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
])

export function createA2APushRouter({
  host = '0.0.0.0',
  port = 0,
  forward = forwardOverLoopback,
  now = () => Date.now(),
  routeTtlMs = 24 * 60 * 60 * 1000,
} = {}) {
  // token -> { port, at }. Re-registration overwrites: a session that restarts reuses nothing,
  // and a stale port would forward into a dead socket rather than reporting the miss.
  //
  // ⚠️ ROUTES EXPIRE, AND A FAILED FORWARD DROPS ONE IMMEDIATELY. This is the one long-lived
  // process on the machine, so an eviction-free map grows one entry per spawn forever. Worse
  // than the growth: a stale token→port route outlives its session, and once the OS reuses that
  // ephemeral port for an unrelated listener, a push carrying the old token is delivered to an
  // unrelated local process — which answers 200, so it reads as delivered.
  const routes = new Map()
  const live = (token) => {
    const route = routes.get(token)
    if (!route) return undefined
    if (now() - route.at > routeTtlMs) {
      routes.delete(token)
      return undefined
    }
    return route
  }

  const server = createServer(async (req, res) => {
    const respond = (code, payload) => {
      res.writeHead(code, { 'content-type': 'application/json' })
      res.end(JSON.stringify(payload))
    }

    if (req.method !== 'POST') {
      respond(405, { error: 'method not allowed' })
      return
    }

    if (req.url === REGISTER_PATH) {
      // ⚠️ A LAN caller must not be able to claim a token. This is the one branch where the
      // remote address is load-bearing; the push branch deliberately does not check it.
      if (!isLoopback(req.socket.remoteAddress)) {
        respond(403, { error: 'registration is accepted from loopback only' })
        return
      }
      let parsed
      try {
        parsed = JSON.parse((await readBody(req)).toString('utf8') || '{}')
      } catch {
        respond(400, { error: 'registration body must be JSON' })
        return
      }
      const { token, port: sessionPort } = parsed
      // ⚠️ `<= 65535` IS NOT DECORATION. A larger port is accepted here and then throws
      // ERR_SOCKET_BAD_PORT inside the forward, which surfaces as a 502 "receiver did not
      // answer" — a misleading verdict for a registration the router should have refused.
      if (
        typeof token !== 'string' ||
        token === '' ||
        !Number.isInteger(sessionPort) ||
        sessionPort <= 0 ||
        sessionPort > 65535
      ) {
        respond(400, { error: 'registration requires { token, port } with port in 1..65535' })
        return
      }
      routes.set(token, { port: sessionPort, at: now() })
      respond(200, { registered: true, port: sessionPort })
      return
    }

    if (req.url === PUSH_PATH) {
      const token = req.headers[TOKEN_HEADER]
      const target = typeof token === 'string' ? live(token) : undefined
      // ⚠️ AN UNKNOWN TOKEN IS REFUSED HERE RATHER THAN FORWARDED AND REFUSED THERE. Both end
      // in a 4xx, but forwarding every unknown token would let an unauthenticated caller make
      // this process open arbitrary loopback connections, which is the SSRF shape the push
      // sender's own guard exists to avoid.
      if (target === undefined) {
        respond(403, { error: 'unknown or unregistered notification token' })
        return
      }
      let result
      try {
        result = await forward({ port: target.port, body: await readBody(req), headers: req.headers })
      } catch (cause) {
        // A receiver that refuses the connection is gone; keeping its route would let a later
        // push land on whatever reuses that port.
        if (cause?.code === 'ECONNREFUSED') routes.delete(token)
        respond(502, { error: `the owning session's receiver did not answer: ${cause?.message ?? cause}` })
        return
      }
      res.writeHead(result.status, { 'content-type': 'application/json' })
      res.end(result.body)
      return
    }

    respond(404, { error: 'not found' })
  })

  return {
    server,
    /** The token -> session-port map, for assertions. Read-only by convention. */
    routesFor: () => new Map(routes),
    listen: () =>
      new Promise((resolve, reject) => {
        server.once('error', reject)
        server.listen(port, host, () => resolve(server.address().port))
      }),
    close: () => new Promise((resolve) => server.close(resolve)),
  }
}

/**
 * The real forward: replay the push, verbatim, at the owning session's loopback receiver.
 *
 * The body and the token header travel unchanged — the receiver re-validates the token itself,
 * so the router is a transport hop and never an authority. A receiver that has since exited
 * surfaces as a 502 rather than a silent 200, because a dropped push must not read as
 * delivered.
 */
export function forwardOverLoopback({ port, body, headers }) {
  // ⚠️ HOP-BY-HOP HEADERS ARE STRIPPED (RFC 7230 §6.1), and `transfer-encoding` is the one that
  // bites: replaying it beside the recomputed `content-length` sends a request claiming both,
  // with a length that no longer describes the buffered body.
  const replayable = {}
  for (const [name, value] of Object.entries(headers)) {
    if (!HOP_BY_HOP.has(name.toLowerCase())) replayable[name] = value
  }
  return new Promise((resolve, reject) => {
    const outbound = httpRequest(
      {
        host: '127.0.0.1',
        port,
        path: PUSH_PATH,
        method: 'POST',
        headers: { ...replayable, host: `127.0.0.1:${port}`, 'content-length': body.length },
        timeout: FORWARD_TIMEOUT_MS,
      },
      (res) => {
        const chunks = []
        res.on('data', (c) => chunks.push(c))
        res.on('end', () => resolve({ status: res.statusCode ?? 502, body: Buffer.concat(chunks).toString('utf8') }))
      },
    )
    outbound.on('timeout', () => outbound.destroy(new Error(`no answer within ${FORWARD_TIMEOUT_MS}ms`)))
    outbound.on('error', reject)
    outbound.end(body)
  })
}
