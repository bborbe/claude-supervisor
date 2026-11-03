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
// ⚠️ THE RELAY'S ENDPOINTS CARRY THIS MACHINE'S DHCP LEASE, not this bind address. A lease
// change silently repoints them at an address that now belongs to something else; re-derive it
// with `route -n get 192.168.178.30 | grep interface` and `ifconfig <iface> | grep 'inet '`.

import { createA2APushRouter } from '../server/a2a-push-router.mjs'

const host = process.env.A2A_PUSH_ROUTER_HOST || '0.0.0.0'
const port = Number(process.env.A2A_PUSH_ROUTER_PORT) || 18083

const router = createA2APushRouter({ host, port })

try {
  const bound = await router.listen()
  console.log(`a2a-push-router: listening on ${host}:${bound}`)
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
