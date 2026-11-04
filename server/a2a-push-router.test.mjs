// Tests for the push router.
//
// ⚠️ THE FORWARD IS INJECTED, AND THAT IS THE POINT. The routing rules — which token reaches
// which port, what an unknown token gets, who may register — are the router's own state
// machine. Stubbing the hop asserts those rules without a second socket; the real hop is
// exercised once, at the end, over a real loopback listener.
//
// Every negative sits beside its positive in the same process, because a run in which nothing
// is forwarded proves nothing on its own.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import os from 'node:os'
import { createA2APushRouter, isLoopback, TOKEN_HEADER, REGISTER_PATH, PUSH_PATH } from './a2a-push-router.mjs'

async function startRouter({ forward } = {}) {
  const calls = []
  const router = createA2APushRouter({
    port: 0,
    forward: async (args) => {
      calls.push(args)
      return { status: 200, body: '{"accepted":true}' }
    },
    ...(forward ? { forward } : {}),
  })
  const port = await router.listen()
  return { router, port, calls, base: `http://127.0.0.1:${port}` }
}

const register = (base, token, port) =>
  fetch(`${base}${REGISTER_PATH}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ token, port }),
  })

const push = (base, token, body = '{"kind":"statusUpdate"}') =>
  fetch(`${base}${PUSH_PATH}`, { method: 'POST', headers: { [TOKEN_HEADER]: token }, body })

test('a registered token is forwarded to the port it registered, with its body intact', async () => {
  const { router, calls, base } = await startRouter()
  try {
    assert.equal((await register(base, 'tok-a', 41234)).status, 200)
    const res = await push(base, 'tok-a', '{"kind":"statusUpdate","n":1}')
    assert.equal(res.status, 200)
    assert.equal(await res.text(), '{"accepted":true}')
    assert.equal(calls.length, 1)
    assert.equal(calls[0].port, 41234)
    assert.equal(calls[0].body.toString('utf8'), '{"kind":"statusUpdate","n":1}')
  } finally {
    await router.close()
  }
})

test('an unregistered token is refused and never forwarded', async () => {
  const { router, calls, base } = await startRouter()
  try {
    // The positive control in the same process: the same call with a registered token is
    // forwarded, so the refusal below is the token check and not a broken harness.
    assert.equal((await register(base, 'tok-known', 41000)).status, 200)
    assert.equal((await push(base, 'tok-known')).status, 200)
    assert.equal(calls.length, 1)

    const refused = await push(base, 'tok-stranger')
    assert.equal(refused.status, 403)
    assert.match(await refused.text(), /unknown or unregistered/)
    assert.equal(calls.length, 1, 'a refused token must not open a loopback connection')
  } finally {
    await router.close()
  }
})

test('re-registering a token moves its route rather than adding a second one', async () => {
  const { router, calls, base } = await startRouter()
  try {
    await register(base, 'tok-a', 41000)
    await register(base, 'tok-a', 42000)
    await push(base, 'tok-a')
    assert.equal(calls.length, 1)
    assert.equal(calls[0].port, 42000, 'the newest registration wins — a stale port would forward into a dead socket')
    assert.equal(router.routesFor().size, 1)
  } finally {
    await router.close()
  }
})

test('a malformed registration is refused rather than stored', async () => {
  const { router, base } = await startRouter()
  try {
    assert.equal((await register(base, '', 41000)).status, 400)
    assert.equal((await register(base, 'tok-a', 0)).status, 400)
    assert.equal((await register(base, 'tok-a', 'not-a-port')).status, 400)
    assert.equal(router.routesFor().size, 0)
    // Control: the well-formed shape is accepted, so the three refusals above are the
    // validation and not a listener that refuses everything.
    assert.equal((await register(base, 'tok-a', 41000)).status, 200)
    assert.equal(router.routesFor().size, 1)
  } finally {
    await router.close()
  }
})

test('the loopback test accepts the forms a dual-stack listener reports and nothing else', () => {
  assert.equal(isLoopback('127.0.0.1'), true)
  assert.equal(isLoopback('::1'), true)
  assert.equal(isLoopback('::ffff:127.0.0.1'), true, 'a dual-stack listener reports IPv4 loopback this way')
  assert.equal(isLoopback('192.168.20.5'), false)
  assert.equal(isLoopback('::ffff:192.168.20.5'), false)
  assert.equal(isLoopback(undefined), false)
})

test('a forwarding failure surfaces as 502, never as a delivered push', async () => {
  const router = createA2APushRouter({
    port: 0,
    forward: async () => {
      throw new Error('ECONNREFUSED 127.0.0.1:41000')
    },
  })
  const port = await router.listen()
  const base = `http://127.0.0.1:${port}`
  try {
    await register(base, 'tok-a', 41000)
    const res = await push(base, 'tok-a')
    assert.equal(res.status, 502, 'a dead receiver must not read as a delivered push')
    assert.match(await res.text(), /did not answer/)
  } finally {
    await router.close()
  }
})

test('registration over loopback succeeds on an all-interfaces listener, and a LAN source is refused', async () => {
  // ⚠️ THE REGRESSION THIS PINS. The router binds every interface so a relay can reach it, and
  // a session registers at `http://127.0.0.1:<port>`. A listener bound to a *specific* LAN
  // address instead makes that registration arrive with the LAN source address, the
  // loopback-only gate refuses its own machine, and every cluster spawn silently loses its
  // channel. Both directions are asserted here: the loopback registration works, and the same
  // request sourced from a LAN address is refused.
  const router = createA2APushRouter({ host: '0.0.0.0', port: 0 })
  const port = await router.listen()
  try {
    const viaLoopback = await register(`http://127.0.0.1:${port}`, 'tok-loop', 41000)
    assert.equal(viaLoopback.status, 200, 'a same-machine session must be able to register')

    const lan = Object.values(os.networkInterfaces())
      .flat()
      .find((i) => i && i.family === 'IPv4' && !i.internal)
    if (lan) {
      const viaLan = await register(`http://${lan.address}:${port}`, 'tok-lan', 41001)
      assert.equal(viaLan.status, 403, 'a routing claim from off-loopback must be refused')
      assert.equal(router.routesFor().has('tok-lan'), false)
    }
  } finally {
    await router.close()
  }
})

test('an expired route is refused and evicted, so a stale token cannot reach a reused port', async () => {
  let clock = 1_000_000
  const { router, calls, base } = await startRouter()
  const expiring = createA2APushRouter({ port: 0, now: () => clock, routeTtlMs: 1000, forward: async () => ({ status: 200, body: '{}' }) })
  const p = await expiring.listen()
  try {
    await register(`http://127.0.0.1:${p}`, 'tok-old', 41000)
    clock += 2000
    const res = await push(`http://127.0.0.1:${p}`, 'tok-old')
    assert.equal(res.status, 403, 'a route past its TTL must not forward')
    assert.equal(expiring.routesFor().size, 0, 'and it must be evicted, not merely ignored')
  } finally {
    await expiring.close()
  }
  await router.close()
  assert.equal(calls.length, 0)
})

test('the real hop reaches a loopback receiver and replays the token header', async () => {
  const seen = []
  const receiver = createServer((req, res) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', () => {
      seen.push({ url: req.url, token: req.headers[TOKEN_HEADER], body: Buffer.concat(chunks).toString('utf8') })
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end('{"accepted":true,"stored":true}')
    })
  })
  const receiverPort = await new Promise((resolve) => receiver.listen(0, '127.0.0.1', () => resolve(receiver.address().port)))

  const router = createA2APushRouter({ port: 0 })
  const routerPort = await router.listen()
  const base = `http://127.0.0.1:${routerPort}`
  try {
    await register(base, 'tok-real', receiverPort)
    const res = await push(base, 'tok-real', '{"kind":"statusUpdate","x":9}')
    assert.equal(res.status, 200)
    assert.equal(await res.text(), '{"accepted":true,"stored":true}')
    assert.equal(seen.length, 1)
    assert.equal(seen[0].url, PUSH_PATH)
    assert.equal(seen[0].token, 'tok-real', 'the token must reach the receiver unchanged — the router is a transport hop, never an authority')
    assert.equal(seen[0].body, '{"kind":"statusUpdate","x":9}')
  } finally {
    await router.close()
    await new Promise((resolve) => receiver.close(resolve))
  }
})

// ⚠️ TUNNEL MODE IS ASSERTED WITH A STUBBED RELAY AND A STUBBED FORWARD, for the same reason
// the push tests stub the hop: the rules under test are the router's own — which token reaches
// which port, and what an unknown token does — not the transport. The loop runs concurrently
// with the assertions, so these assert on membership, never on a call count.
// ⚠️ A bare `fetch` here leaves undici's pool holding sockets, which keeps the test process
// alive after every server is closed — the run then hangs with all tests green. `agent: false`
// plus `connection: close` makes each request own and drop its socket.
import { request as httpRequest } from 'node:http'
function postJson(port, path, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      {
        host: '127.0.0.1',
        port,
        path,
        method: 'POST',
        agent: false,
        headers: { 'content-type': 'application/json', connection: 'close', ...headers },
      },
      (res) => {
        res.resume()
        res.on('end', () => resolve(res.statusCode))
      },
    )
    req.on('error', reject)
    req.end(typeof body === 'string' ? body : JSON.stringify(body))
  })
}

function tunnelRouter({ items, calls, ready = () => true, served = { count: 0 } }) {
  // The stub hands its batch over once and then reports empty, which is what a real relay does
  // after a drain. A stub that repeats its batch forever would spin the loop with no idle turn.
  let pending = [...items]
  // The first pull answers empty on purpose: the loop starts with the listener, before a
  // session has registered, and delivering into that window would test a case that cannot
  // happen in production (the router is up long before a worker pushes).
  let firstPull = true
  return createA2APushRouter({
    port: 0,
    relayUrl: 'http://relay.test',
    tunnelSecret: 's3cret',
    retryDelayMs: 5,
    forward: async (args) => {
      calls.push(args)
      return { status: 200, body: '{"accepted":true}' }
    },
    fetchImpl: async (url, init) => ({
      ok: true,
      status: 200,
      url,
      init,
      json: async () => {
        if (firstPull) {
          firstPull = false
          return { items: [] }
        }
        // ⚠️ THE BATCH IS GATED ON THE ROUTE EXISTING, NOT ON A SLEEP. The token only becomes
        // routable after a real loopback registration round-trip, so a stub that hands the
        // batch over on its own schedule can deliver into a window where nothing is registered
        // — which fails the positive test on a loaded box and, worse, makes the negative test
        // pass for the wrong reason: the item would be dropped because no route existed at
        // pull time, not because its token was unknown.
        if (!ready() || pending.length === 0) return { items: [] }
        const batch = pending
        pending = []
        served.count++
        return { items: batch }
      },
    }),
  })
}

// waitFor polls a predicate to a deadline. A fixed sleep is a bet on machine speed, and this
// suite loses that bet on a loaded CI box.
async function waitFor(predicate, timeoutMs = 5000, stepMs = 10, diagnostic = () => '') {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise((r) => setTimeout(r, stepMs))
  }
  throw new Error(`condition not met within ${timeoutMs}ms ${diagnostic()}`)
}

test('tunnel mode dispatches a pulled item to the session that registered its token', async () => {
  const calls = []
  let router
  router = tunnelRouter({
    items: [{ token: 'tok-a', body: { kind: 'statusUpdate' } }],
    calls,
    ready: () => router.routesFor().has('tok-a'),
  })
  const port = await router.listen()
  try {
    await postJson(port, REGISTER_PATH, { token: 'tok-a', port: 5555 })

    await waitFor(() => calls.length > 0)

    const hit = calls.find((c) => c.port === 5555)
    assert.ok(hit, 'the item must reach the registered port')
    assert.equal(hit.headers[TOKEN_HEADER], 'tok-a')
    // The body travels verbatim: the receiver re-validates it, so the router is a transport hop
    // and never an authority over its content.
    assert.equal(hit.body.toString('utf8'), JSON.stringify({ kind: 'statusUpdate' }))
  } finally {
    await router.close()
  }
})

test('tunnel mode drops an item whose token nothing registered', async () => {
  const calls = []
  const served = { count: 0 }
  let router
  router = tunnelRouter({
    items: [{ token: 'nobody', body: { kind: 'statusUpdate' } }],
    calls,
    served,
    // The route for a DIFFERENT token exists, so the batch is genuinely served — the drop is
    // then about the item's own unknown token, which is the thing under test.
    ready: () => router.routesFor().has('tok-a'),
  })
  const port = await router.listen()
  try {
    await postJson(port, REGISTER_PATH, { token: 'tok-a', port: 5555 })

    await waitFor(
      () => served.count === 1,
      5000,
      10,
      () => `(served=${served.count} routes=${[...router.routesFor().keys()].join(',')})`,
    )

    assert.equal(calls.length, 0, 'an unknown token must not open a loopback connection')
  } finally {
    await router.close()
  }
})

test('tunnel mode refuses a direct push, because the relay is the push endpoint', async () => {
  const calls = []
  const router = tunnelRouter({ items: [], calls })
  const port = await router.listen()
  try {
    const status = await postJson(port, PUSH_PATH, '{}', { [TOKEN_HEADER]: 'tok-a' })

    assert.equal(status, 503)
  } finally {
    await router.close()
  }
})

test('a failed pull rejects rather than reporting an empty success', async () => {
  const router = createA2APushRouter({
    port: 0,
    relayUrl: 'http://relay.test',
    tunnelSecret: 's3cret',
    fetchImpl: async () => ({ ok: false, status: 401, json: async () => ({}) }),
  })
  await assert.rejects(() => router.pullOnce(), /401/)
})

// ⚠️ A PULL THAT NEVER ANSWERS MUST NOT OUTLIVE `close()`. Without an abort, `close()` resolves
// while a request is still open and the loop can dispatch a batch after shutdown — bounded in
// practice only because the launcher exits the process immediately afterwards, which is not a
// property this module should depend on.
test('close aborts a pull that is still in flight', async () => {
  let pullStarted = false
  let aborted = false
  const router = createA2APushRouter({
    port: 0,
    relayUrl: 'http://relay.test',
    tunnelSecret: 's3cret',
    retryDelayMs: 5,
    fetchImpl: (url, init) =>
      new Promise((_resolve, reject) => {
        pullStarted = true
        init?.signal?.addEventListener('abort', () => {
          aborted = true
          reject(new Error('aborted'))
        })
      }),
  })
  await router.listen()
  await waitFor(() => pullStarted)

  const startedAt = Date.now()
  await router.close()

  assert.equal(aborted, true, 'the in-flight pull must be aborted, not merely abandoned')
  assert.ok(
    Date.now() - startedAt < 2000,
    `close() must not wait on a pull that never answers (took ${Date.now() - startedAt}ms)`,
  )
})

test('a pull that never answers times out instead of parking the loop', async () => {
  const router = createA2APushRouter({
    port: 0,
    relayUrl: 'http://relay.test',
    tunnelSecret: 's3cret',
    pullTimeoutMs: 50,
    fetchImpl: (url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal.reason))
      }),
  })
  await assert.rejects(() => router.pullOnce(), /exceeded 50ms/)
})

test('a tunnel with no secret is refused at construction rather than started inert', () => {
  assert.throws(
    () => createA2APushRouter({ port: 0, relayUrl: 'http://relay.test', tunnelSecret: '' }),
    /tunnelSecret/,
  )
})
