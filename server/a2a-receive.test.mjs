// Tests for the A2A push receiver.
//
// The refusals are asserted as hard as the acceptance, and every negative case runs beside a
// positive control in the SAME process: an all-refused run proves nothing, because a receiver
// that refuses everything looks identical to one that refuses correctly. That is the
// acceptance rule the parent task states, and it is the reason `control` appears in each
// refusal test rather than in a separate one.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createA2AStatusReceiver, callbackUrlFor } from './a2a-receive.mjs'

const TOKENS = new Map([
  ['w-7f3a', 'tok-alpha'],
  ['w-9c11', 'tok-beta'],
])

function statusBody({
  taskId = 't-1',
  state = 'working',
  text = 'reading server/supervisor.mjs',
  messageId,
} = {}) {
  // `messageId` is omitted entirely when not given, rather than sent as null: the SDK omits
  // the field on an event built without a message, and a test that always sent it would
  // never exercise the absent case the completion currently produces.
  const message = { role: 'agent', parts: [{ text }] }
  if (messageId !== undefined) message.messageId = messageId
  return JSON.stringify({
    statusUpdate: { taskId, status: { state, message } },
  })
}

async function withReceiver(fn) {
  const seen = []
  const receiver = createA2AStatusReceiver({
    workerTokens: () => TOKENS,
    onMessage: (workerId, entry) => seen.push({ workerId, entry }),
  })
  const port = await receiver.listen()
  try {
    await fn({ port, seen, receiver })
  } finally {
    await receiver.close()
  }
}

const post = (port, { token, body, headers = {} } = {}) =>
  fetch(`http://127.0.0.1:${port}/a2a/push`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...(token ? { 'A2A-Notification-Token': token } : {}), ...headers },
    body,
  })

test('a valid token stores the status, and the two refusals beside it do not', async () => {
  await withReceiver(async ({ port, seen }) => {
    // Negative (a): a token belonging to no worker.
    const unknown = await post(port, { token: 'tok-nobody', body: statusBody() })
    assert.equal(unknown.status, 401)
    assert.deepEqual(await unknown.json(), { error: 'unknown worker or bad token' })

    // Negative (b): no token at all.
    const missing = await post(port, { body: statusBody() })
    assert.equal(missing.status, 401)

    // Positive control, same run: an accepted status, so the two refusals above are refusals
    // and not a receiver that never accepts anything.
    const ok = await post(port, { token: 'tok-alpha', body: statusBody() })
    assert.equal(ok.status, 200)
    assert.deepEqual(await ok.json(), { accepted: true, stored: true })

    assert.equal(seen.length, 1)
    assert.equal(seen[0].workerId, 'w-7f3a')
    assert.equal(seen[0].entry.state, 'working')
    assert.equal(seen[0].entry.text, 'reading server/supervisor.mjs')
    assert.equal(seen[0].entry.worker_id, 'w-7f3a')
    assert.ok(Date.parse(seen[0].entry.ts) > 0, 'ts must be a real instant')
    assert.equal(
      seen[0].entry.source,
      'push',
      'the receive path marks its own entries; a2aTurnPatch never sets this',
    )
  })
})

test('the worker message id is carried through, and is null rather than absent when the event has no message', async () => {
  await withReceiver(async ({ port, seen }) => {
    // Present: the mid-turn event carries a message, so its id must survive to the record.
    const withId = await post(port, {
      token: 'tok-alpha',
      body: statusBody({ messageId: 'msg-abc123' }),
    })
    assert.equal(withId.status, 200)
    assert.equal(seen[0].entry.messageId, 'msg-abc123')

    // Absent: the terminal completion is yielded as `NewStatusUpdateEvent(ctx,
    // TaskStateCompleted, nil)` — no message, so no id exists to read. The field must be
    // `null` rather than `undefined`, so a reader can tell "this event had no message" from
    // "the receive path dropped it", which is the same distinction `last_status` draws
    // between `null` and an empty object.
    const withoutId = await post(port, {
      token: 'tok-alpha',
      body: statusBody({ state: 'completed', text: 'done' }),
    })
    assert.equal(withoutId.status, 200)
    assert.equal(seen[1].entry.messageId, null)
    assert.ok(
      'messageId' in seen[1].entry,
      'the key must be present even when its value is null',
    )
  })
})

test('a token cannot be forged by naming another worker in the body', async () => {
  await withReceiver(async ({ port, seen }) => {
    // The body is worker-supplied. If identity came from it, any worker holding any valid
    // token could write into another worker's record.
    const body = JSON.stringify({
      statusUpdate: {
        taskId: 't-1',
        worker_id: 'w-9c11',
        status: { state: 'working', message: { role: 'agent', parts: [{ text: 'spoofed' }] } },
      },
    })
    const res = await post(port, { token: 'tok-alpha', body })
    assert.equal(res.status, 200)
    assert.equal(seen[0].workerId, 'w-7f3a', 'identity comes from the token, never the body')
  })
})

test('a bespoke body keyed on `event` is refused', async () => {
  await withReceiver(async ({ port, seen }) => {
    const bespoke = JSON.stringify({ event: { state: 'working', text: 'hi' } })
    const res = await post(port, { token: 'tok-alpha', body: bespoke })
    assert.equal(res.status, 400)
    assert.match((await res.json()).error, /unknown envelope key|exactly one envelope key/)

    // Control: the SDK's own envelope shape is accepted, so the refusal above is about the
    // shape and not about rejection in general.
    const ok = await post(port, { token: 'tok-alpha', body: statusBody() })
    assert.equal(ok.status, 200)
    assert.equal(seen.length, 1)
  })
})

test('a two-key envelope is refused — the union carries exactly one discriminator', async () => {
  await withReceiver(async ({ port }) => {
    const twoKeys = JSON.stringify({ statusUpdate: { status: { state: 'working' } }, task: { id: 't-1' } })
    const res = await post(port, { token: 'tok-alpha', body: twoKeys })
    assert.equal(res.status, 400)
  })
})

test('a non-status event is accepted but not stored', async () => {
  await withReceiver(async ({ port, seen }) => {
    const artifact = JSON.stringify({ artifactUpdate: { taskId: 't-1', artifact: { parts: [] } } })
    const res = await post(port, { token: 'tok-alpha', body: artifact })
    assert.equal(res.status, 200)
    assert.deepEqual(await res.json(), { accepted: true, stored: false, kind: 'artifactUpdate' })
    assert.equal(seen.length, 0)
  })
})

test('a status with no text is stored with text null, not an empty string', async () => {
  await withReceiver(async ({ port, seen }) => {
    const noText = JSON.stringify({ statusUpdate: { taskId: 't-1', status: { state: 'working' } } })
    const res = await post(port, { token: 'tok-alpha', body: noText })
    assert.equal(res.status, 200)
    assert.equal(seen[0].entry.text, null)
  })
})

test('the store is bounded per worker', async () => {
  await withReceiver(async ({ port, receiver }) => {
    for (let i = 0; i < 60; i++) {
      await post(port, { token: 'tok-alpha', body: statusBody({ text: `m${i}` }) })
    }
    const kept = receiver.messagesFor('w-7f3a')
    assert.equal(kept.length, 50)
    assert.equal(kept[kept.length - 1].text, 'm59', 'the newest is kept')
  })
})

test('the callback URL names the docker gateway and the receiver port', () => {
  assert.equal(callbackUrlFor({ port: 41234 }), 'http://host.docker.internal:41234/a2a/push')
})
