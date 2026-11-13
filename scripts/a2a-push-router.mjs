#!/usr/bin/env node
// Start the A2A push router — the one process that owns the address a cluster relay forwards to.
//
// Run it once per machine, under launchd or a terminal that outlives the sessions. Every
// supervisor server that sets SUPERVISOR_A2A_PUSH_ROUTER_URL then keeps its own receiver on
// loopback and registers its tokens here, so N sessions no longer contend for one port.
//
// ⚠️ THE DEFAULT BIND IS ALL INTERFACES, AND THAT IS DELIBERATE. The in-cluster relay's
// Endpoints point at this machine, so 127.0.0.1 alone is not reachable from a pod — but binding
// the LAN address *only* makes loopback unreachable, and a same-machine supervisor registering
// at `http://<lan>:<port>` is seen by the kernel with the LAN source address, so the
// loopback-only registration gate refuses its own machine and every cluster spawn loses its
// channel. All interfaces keeps both reachable: the relay dials the LAN address, sessions
// register at `http://127.0.0.1:<port>`, and the gate stays strict.
//
// Env:
//   A2A_PUSH_ROUTER_HOST  bind address   (default 0.0.0.0)
//   A2A_PUSH_ROUTER_PORT  bind port      (default 18083 — must equal the relay's targetPort)
//
//   A2A_PUSH_ROUTER_RELAY_URL       set to switch this router into TUNNEL MODE: instead of
//                                   receiving pushes it long-polls that relay and dispatches
//                                   what it pulls. Required for a `k8s` worker, whose push can
//                                   never reach this machine — its firewall drops every port
//                                   except two something else owns, so the Mac must dial out.
//   A2A_PUSH_ROUTER_TUNNEL_SECRET   the secret that relay's pull endpoint authenticates with.
//                                   Required whenever RELAY_URL is set; the router refuses to
//                                   start without it rather than retrying 401s forever.
//
// ⚠️ SETTING ONLY ONE OF THE PAIR IS A STARTUP ERROR, BY DESIGN. A relay URL with no secret
// produces a loop that authenticates nothing and delivers nothing, and the failure is silent
// from every angle except a request log — the same deployed-but-inert shape this file's own
// comments keep warning about.
//
// ⚠️ THE RELAY'S ENDPOINTS CARRY THIS MACHINE'S DHCP LEASE, not this bind address. A lease
// change silently repoints them at an address that now belongs to something else; re-derive it
// with `route -n get 192.168.178.30 | grep interface` and `ifconfig <iface> | grep 'inet '`.

import { createA2APushRouter } from '../server/a2a-push-router.mjs'

const host = process.env.A2A_PUSH_ROUTER_HOST || '0.0.0.0'
const port = Number(process.env.A2A_PUSH_ROUTER_PORT) || 18083

// Tunnel mode, off unless a relay URL names one. The secret is required alongside it — the
// router itself refuses the half-configured pair, and doing the check here too means the
// operator sees a sentence naming the variable rather than a constructor stack trace.
const relayUrl = process.env.A2A_PUSH_ROUTER_RELAY_URL || null
const tunnelSecret = process.env.A2A_PUSH_ROUTER_TUNNEL_SECRET || ''

if (relayUrl && !tunnelSecret) {
  console.error(
    'a2a-push-router: A2A_PUSH_ROUTER_RELAY_URL is set but A2A_PUSH_ROUTER_TUNNEL_SECRET is empty — ' +
      'the tunnel would authenticate nothing. Set both, or neither.',
  )
  process.exit(1)
}

const router = createA2APushRouter({
  host,
  port,
  ...(relayUrl ? { relayUrl, tunnelSecret } : {}),
})

try {
  const bound = await router.listen()
  // Named on the same line as the bind: a router that is listening but pulling is a different
  // animal from one receiving pushes, and the operator should not have to read env to tell.
  console.log(
    relayUrl
      ? `a2a-push-router: listening on ${host}:${bound} (tunnel mode, pulling ${relayUrl})`
      : `a2a-push-router: listening on ${host}:${bound}`,
  )
} catch (cause) {
  // ⚠️ A BIND FAILURE IS FATAL HERE, and that is the difference from the server side. There,
  // an unbindable receiver is additive and degrades to a quiet channel; here the whole point is
  // to own the address, so exiting loudly is what stops a second copy from silently doing
  // nothing while the first one's routes go stale.
  console.error(`a2a-push-router: could not bind ${host}:${port} — ${cause?.message ?? cause}`)
  process.exit(1)
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, async () => {
    await router.close()
    process.exit(0)
  })
}
