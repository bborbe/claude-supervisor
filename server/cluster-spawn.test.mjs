// Tests for the cluster spawn call.
//
// The two things worth pinning are the ones a working-looking implementation gets wrong:
// that an unconfigured service URL REFUSES rather than reaching for a default, and that an
// illegal session id is refused BEFORE the call — the service answers both with a 400 that
// names nothing the caller can act on, so a late failure is indistinguishable from a wrong
// prompt.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  CLUSTER_SESSION_ID_PATTERN,
  DEFAULT_TIMEOUT_MS,
  PROMPT_PATH,
  SESSION_HEADER,
  newSessionId,
  resolveClusterBaseUrl,
  startClusterSession,
} from './cluster-spawn.mjs'

const UUID = '3f2a91c4-5b6d-4e7f-8a90-1b2c3d4e5f60'

const ok = (body = 'the answer', status = 200) => async () => ({
  ok: status >= 200 && status < 300,
  status,
  text: async () => body,
})

test('a configured URL is normalized, and its trailing slashes removed', () => {
  assert.deepEqual(resolveClusterBaseUrl('http://192.168.178.30:30090'), {
    baseUrl: 'http://192.168.178.30:30090',
  })
  // Without this the path becomes `//prompt`, which the service's mux does not register —
  // a 404 that reads like a missing endpoint rather than a doubled separator.
  assert.deepEqual(resolveClusterBaseUrl('http://host:30090///'), { baseUrl: 'http://host:30090' })
  assert.deepEqual(resolveClusterBaseUrl('https://host/base/'), { baseUrl: 'https://host/base' })
})

test('an unset service URL refuses rather than defaulting', () => {
  // The failure this prevents: a default like `localhost:9090` is reachable-looking, so a
  // cluster spawn would fail with a connection error against the operator's own machine
  // instead of saying the target was never configured.
  for (const raw of [undefined, null, '']) {
    const { error } = resolveClusterBaseUrl(raw)
    assert.ok(error, `expected ${JSON.stringify(raw)} to be refused`)
    assert.match(error, /not configured/)
    assert.match(error, /SUPERVISOR_CLUSTER_URL/)
  }
})

test('a malformed or non-http service URL refuses', () => {
  assert.match(resolveClusterBaseUrl('not a url').error, /not a valid URL/)
  assert.match(resolveClusterBaseUrl('ftp://host').error, /not http or https/)
  assert.match(resolveClusterBaseUrl(9090).error, /not a URL/)
})

test('a minted session id satisfies the service pattern', () => {
  const { sessionId } = newSessionId(() => UUID)
  assert.equal(sessionId, UUID)
  assert.ok(CLUSTER_SESSION_ID_PATTERN.test(sessionId))
})

test('a generator that cannot produce a legal id is refused', () => {
  // The service rejects a leading '-' because the id reaches a backend CLI as an argument.
  // Refusing here means the caller hears "the id is wrong", not "HTTP 400 invalid session id".
  for (const bad of ['-leading-dash', 'has/slash', 'x'.repeat(65), '', 42]) {
    const { error } = newSessionId(() => bad)
    assert.ok(error, `expected ${JSON.stringify(bad)} to be refused`)
    assert.match(error, /does not match the service's pattern/)
  }
})

test('the prompt is POSTed to /prompt with the session header', async () => {
  let seen
  const result = await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: 'do the thing',
    sessionId: UUID,
    fetchImpl: async (url, init) => {
      seen = { url, init }
      return { ok: true, status: 200, text: async () => 'done' }
    },
  })

  assert.deepEqual(result, { sessionId: UUID, status: 200, answer: 'done' })
  assert.equal(seen.url, `http://host:30090${PROMPT_PATH}`)
  assert.equal(seen.init.method, 'POST')
  // The header spelling is the contract: an absent one silently resolves every caller to the
  // service's shared `identity` conversation, so a typo puts all workers in one conversation
  // and nothing errors.
  assert.equal(seen.init.headers[SESSION_HEADER], UUID)
  assert.equal(seen.init.body, 'do the thing')
})

test('an illegal session id is refused before any request is made', async () => {
  let called = false
  const { error } = await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: 'do the thing',
    sessionId: '-not-allowed',
    fetchImpl: async () => {
      called = true
      return ok()()
    },
  })
  assert.match(error, /does not match the service's pattern/)
  assert.equal(called, false, 'the request must not be attempted with an id the service will reject')
})

test('an empty prompt is refused before any request is made', async () => {
  let called = false
  const { error } = await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: '   ',
    sessionId: UUID,
    fetchImpl: async () => {
      called = true
      return ok()()
    },
  })
  assert.match(error, /prompt is required/)
  assert.equal(called, false)
})

test("the service's own refusal is surfaced, not flattened to the status", async () => {
  // "HTTP 400" alone sends the caller looking at their network; the service's body says
  // which of its refusals fired.
  const { error } = await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: 'x',
    sessionId: UUID,
    fetchImpl: async () => ({ ok: false, status: 400, text: async () => 'invalid session id\n' }),
  })
  assert.match(error, /HTTP 400/)
  assert.match(error, /invalid session id/)
})

test('an unreachable service and a timeout are reported as different failures', async () => {
  const refused = await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: 'x',
    sessionId: UUID,
    fetchImpl: async () => {
      throw new Error('connect ECONNREFUSED 192.168.178.30:30090')
    },
  })
  assert.match(refused.error, /could not reach the cluster service/)
  assert.match(refused.error, /ECONNREFUSED/)

  const aborted = await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: 'x',
    sessionId: UUID,
    timeoutMs: 5,
    fetchImpl: async () => {
      const error = new Error('aborted')
      error.name = 'AbortError'
      throw error
    },
  })
  assert.match(aborted.error, /no answer within 5ms/)
})

test('the default timeout is bounded', () => {
  // Unbounded would let a wedged pod hold a manager session inside a tool call indefinitely;
  // the service answers in one response, so there is no partial progress to wait for.
  assert.ok(Number.isFinite(DEFAULT_TIMEOUT_MS) && DEFAULT_TIMEOUT_MS > 0)
})
