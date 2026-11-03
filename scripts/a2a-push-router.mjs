#!/usr/bin/env node
// Start the A2A push router — the one process that owns the address a cluster relay forwards to.
//
// Run it once per machine, under launchd or a terminal that outlives the sessions. Every
// supervisor server that sets SUPERVISOR_A2A_PUSH_ROUTER_URL then keeps its own receiver on
// loopback and registers its tokens here, so N sessions no longer contend for one port.
//
// ⚠️ THE HOST IS A LAN ADDRESS ON PURPOSE. The in-cluster relay's Endpoints point at this
// machine, and 127.0.0.1 is not reachable from a pod. That widens the push surface to the LAN —
// which is why `/register` accepts loopback peers only: a push may arrive from anywhere the
// network allows, but a routing claim may not.
//
// Env:
//   A2A_PUSH_ROUTER_HOST  bind address   (default 192.168.20.100 — the Mac's cluster-facing
//                                         lease on en7, the interface that routes to nuke)
//   A2A_PUSH_ROUTER_PORT  bind port      (default 18082 — must equal the relay's upstream port)
//
// ⚠️ THE DEFAULT HOST IS A DHCP LEASE. A lease change silently repoints the relay's Endpoints
// at an address that now belongs to something else. Re-derive it with
// `route -n get 192.168.178.30 | grep interface` and `ifconfig <iface> | grep 'inet '`.

import { createA2APushRouter } from '../server/a2a-push-router.mjs'

const host = process.env.A2A_PUSH_ROUTER_HOST || '192.168.20.100'
const port = Number(process.env.A2A_PUSH_ROUTER_PORT) || 18082

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
