// Tests for the supervisor's push registry.
//
// ⚠️ THE IDENTITY RULE IS RE-IMPLEMENTED IN THE FAKE, DELIBERATELY. `deliver` below resolves the
// worker from the TOKEN by scanning `workerTokens()`, exactly as the real receiver does — so a
// test asserting "an unknown token surfaces nothing" is asserting the registry's own rule and not
// a convenience of the harness. A fake that took the worker id as an argument would pass that
// test no matter what the registry did with an unrecognised token.
//
// The last test drives the REAL receiver over a real loopback socket, because every other test
// here stubs the transport and a stub cannot show that the token the registry mints is the token
// the receiver actually reads off the wire.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createA2APushRegistry } from './a2a-push-registry.mjs'

function statusEntry(overrides = {}) {
  return { worker_id: 'w-7f3a', state: 'working', text: 'reading server/supervisor.mjs', ts: new Date().toISOString(), ...overrides }
}

function harness({ tokenFactory, now } = {}) {
  let opts = null
  const registry = createA2APushRegistry({
    receiverFactory: (o) => {
      opts = o
      return { listen: async () => 41234, close: async () => {}, messagesFor: () => [] }
    },
    ...(tokenFactory ? { tokenFactory } : {}),
    ...(now ? { now } : {}),
  })
  return {
    registry,
    // The real receiver's identity resolution: the token names the worker, never the body.
    deliver: (token, entry) => {
      for (const [identity, held] of opts.workerTokens().entries()) {
        if (held === token) {
          opts.onMessage(identity, entry)
          return identity
        }
      }
      return null
    },
  }
}

test('beginCreate mints a token and the callback URL together', async () => {
  const { registry } = harness({ tokenFactory: () => 'tok-alpha' })
  const begun = await registry.beginCreate()
  assert.equal(begun.token, 'tok-alpha')
  assert.deepEqual(begun.push, { url: 'http://host.docker.internal:41234/a2a/push', token: 'tok-alpha' })
})

test('a push that lands before the create answers is buffered, then re-keyed to the worker', async () => {
  const { registry, deliver } = harness({ tokenFactory: () => 'tok-alpha' })
  const begun = await registry.beginCreate()

  // The window between create-send and create-reply. The token is live, so the receiver accepts
  // the POST; the worker id does not exist yet, so the entry lands under the token.
  const provisional = deliver('tok-alpha', statusEntry({ text: 'early' }))
  assert.equal(provisional, 'tok-alpha')
  assert.equal(registry.statusMessagesFor('w-7f3a').length, 0, 'not yet under the worker id')

  assert.equal(registry.bind({ token: begun.token, workerId: 'w-7f3a' }), null)
  const moved = registry.statusMessagesFor('w-7f3a')
  assert.equal(moved.length, 1, 'the buffered entry survived the bind rather than being dropped')
  assert.equal(moved[0].text, 'early')
  assert.equal(registry.statusMessagesFor('tok-alpha').length, 0, 'the provisional key is gone')
})

test('a token the registry never minted resolves to no worker', async () => {
  const { registry, deliver } = harness({ tokenFactory: () => 'tok-alpha' })
  await registry.beginCreate()
  // This is the receiver's 401 path: no identity, so nothing is stored anywhere.
  assert.equal(deliver('tok-nobody', statusEntry()), null)
  assert.equal(registry.statusMessagesFor('w-7f3a').length, 0)
  assert.equal(registry.statusMessagesFor('tok-nobody').length, 0)

  // Positive control in the same run, per this task's own rule that an all-refused run proves
  // nothing: the minted token still works beside the refusal above.
  assert.equal(deliver('tok-alpha', statusEntry()), 'tok-alpha')
})

test('bind refuses an unknown token and an empty worker id', async () => {
  const { registry } = harness({ tokenFactory: () => 'tok-alpha' })
  const begun = await registry.beginCreate()
  assert.match(registry.bind({ token: 'tok-nobody', workerId: 'w-1' }).error, /never minted/)
  assert.match(registry.bind({ token: begun.token, workerId: '' }).error, /non-empty/)
  // Neither refusal bound anything, so the token is still pending and still releaseable.
  assert.equal(registry.release({ token: begun.token }), true)
})

test('lastStatusFor is null before anything arrives, then carries the worker ts and an age', async () => {
  const ts = '2026-10-17T14:09:03.000Z'
  const { registry, deliver } = harness({
    tokenFactory: () => 'tok-alpha',
    now: () => Date.parse(ts) + 12_000,
  })
  const begun = await registry.beginCreate()
  assert.equal(registry.lastStatusFor('w-7f3a'), null, 'null, not an empty object')

  registry.bind({ token: begun.token, workerId: 'w-7f3a' })
  deliver('tok-alpha', statusEntry({ ts, state: 'completed', text: 'docker arm green' }))

  assert.deepEqual(registry.lastStatusFor('w-7f3a'), {
    worker_id: 'w-7f3a',
    state: 'completed',
    text: 'docker arm green',
    ts,
    age_s: 12,
  })
})

test('an unparseable ts yields a null age rather than a NaN', async () => {
  const { registry, deliver } = harness({ tokenFactory: () => 'tok-alpha' })
  const begun = await registry.beginCreate()
  registry.bind({ token: begun.token, workerId: 'w-7f3a' })
  deliver('tok-alpha', statusEntry({ ts: 'not-a-timestamp' }))
  assert.equal(registry.lastStatusFor('w-7f3a').age_s, null)
})

test('the store is bounded per worker, keeping the newest', async () => {
  const { registry, deliver } = harness({ tokenFactory: () => 'tok-alpha' })
  const begun = await registry.beginCreate()
  registry.bind({ token: begun.token, workerId: 'w-7f3a' })
  for (let i = 0; i < 60; i++) deliver('tok-alpha', statusEntry({ text: `m${i}` }))
  const kept = registry.statusMessagesFor('w-7f3a')
  assert.equal(kept.length, 50)
  assert.equal(kept[kept.length - 1].text, 'm59')
})

test('release drops an unbound token and its buffered entries, and no-ops on a bound one', async () => {
  const { registry, deliver } = harness({ tokenFactory: () => 'tok-alpha' })
  const begun = await registry.beginCreate()
  deliver('tok-alpha', statusEntry({ text: 'orphan' }))

  assert.equal(registry.release({ token: begun.token }), true)
  assert.equal(registry.statusMessagesFor('tok-alpha').length, 0, 'the buffer went with the token')
  assert.equal(deliver('tok-alpha', statusEntry()), null, 'and the token no longer resolves')

  // A second, bound token: releasing it must not erase a live worker's record.
  const second = await registry.beginCreate()
  registry.bind({ token: second.token, workerId: 'w-9c11' })
  deliver(second.token, statusEntry({ worker_id: 'w-9c11' }))
  assert.equal(registry.release({ token: second.token }), false)
  assert.equal(registry.statusMessagesFor('w-9c11').length, 1)
})

test('a bind failure is cached, not retried per spawn', async () => {
  let calls = 0
  const registry = createA2APushRegistry({
    receiverFactory: () => ({
      listen: async () => {
        calls += 1
        throw new Error('EADDRINUSE')
      },
      close: async () => {},
      messagesFor: () => [],
    }),
  })
  const first = await registry.beginCreate()
  const second = await registry.beginCreate()
  assert.match(first.error, /could not bind/)
  assert.match(second.error, /could not bind/)
  assert.equal(calls, 1, 'one bind attempt for the life of the process, not one per spawn')
})

test('the real receiver accepts the token the registry minted, over a real socket', async () => {
  const registry = createA2APushRegistry({ tokenFactory: () => 'tok-alpha' })
  try {
    const begun = await registry.beginCreate()
    assert.ok(!begun.error, `the receiver must bind: ${begun.error}`)
    const port = new URL(begun.push.url).port

    // The refusal first, so the acceptance below is a control and not a receiver that takes all.
    const refused = await fetch(`http://127.0.0.1:${port}/a2a/push`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'A2A-Notification-Token': 'tok-nobody' },
      body: JSON.stringify({ statusUpdate: { taskId: 't-1', status: { state: 'working' } } }),
    })
    assert.equal(refused.status, 401)

    const accepted = await fetch(`http://127.0.0.1:${port}/a2a/push`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'A2A-Notification-Token': 'tok-alpha' },
      body: JSON.stringify({
        statusUpdate: { taskId: 't-1', status: { state: 'working', message: { role: 'agent', parts: [{ text: 'live' }] } } },
      }),
    })
    assert.equal(accepted.status, 200)

    // Delivered under the provisional key, because no create has answered yet — which is the
    // buffering path exercised here against the real transport rather than a stub.
    assert.equal(registry.statusMessagesFor('tok-alpha').length, 1)
    registry.bind({ token: begun.token, workerId: 'w-7f3a' })
    assert.equal(registry.statusMessagesFor('w-7f3a')[0].text, 'live')
  } finally {
    await registry.close()
  }
})
