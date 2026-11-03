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
