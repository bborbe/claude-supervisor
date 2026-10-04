// Tests for the cluster spawn call.
//
// The things worth pinning are the ones a working-looking implementation gets wrong: that an
// unconfigured service URL REFUSES rather than reaching for a default, that an illegal session
// id is refused BEFORE the call, and that an unconfigured token refuses too — the service
// answers a bad id with a 400 and a missing credential with a 401, both naming nothing the
// caller can act on, so a late failure is indistinguishable from a wrong prompt.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  CLUSTER_SESSION_ID_PATTERN,
  DEFAULT_TIMEOUT_MS,
  PROMPT_PATH,
  SESSION_HEADER,
  newSessionId,
  resolveAuthToken,
  resolveClusterBaseUrl,
  resolveClusterTarget,
  startClusterSession,
} from './cluster-spawn.mjs'

const UUID = '3f2a91c4-5b6d-4e7f-8a90-1b2c3d4e5f60'
const TOKEN = 'not-a-real-token-2f9c41'

// ⚠️ Deliberately NOT imported from cluster-spawn.mjs.
//
// The counterparty's contract, restated from its own document — `bborbe/agent`
// `docs/interactive-service.md` § Authentication, at merge `2379f1c6`: "Every gated route
// requires the request header `Authorization: Bearer <token>`. The scheme is matched exactly
// as `Bearer `".
//
// Importing these would make the assertion agree with the implementation by construction: a
// typo in `AUTH_HEADER`, or a dropped trailing space in `AUTH_SCHEME`, would move both sides
// of the comparison together and the test would still pass. That is the goal's recurring "a
// probe that cannot fail" class, and the only fix is for the expected value to come from the
// counterparty's head rather than the sender's.
const CONTRACT_AUTH_HEADER = 'Authorization'
const CONTRACT_AUTH_SCHEME = 'Bearer '

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

test('an unset auth token refuses rather than sending an unauthenticated request', () => {
  // The failure this prevents: with no header the service answers 401, which is the same
  // observable as a wrong token — so the operator goes looking at the secret instead of at
  // the variable that was never set.
  for (const raw of [undefined, null, '']) {
    const { error } = resolveAuthToken(raw)
    assert.ok(error, `expected ${JSON.stringify(raw)} to be refused`)
    assert.match(error, /INTERACTIVE_AUTH_TOKEN/)
    assert.match(error, /401/)
  }
  assert.match(resolveAuthToken(42).error, /not a token string/)
  assert.deepEqual(resolveAuthToken('abc'), { token: 'abc' })
})

test('a missing token is refused before any request is made', async () => {
  let called = false
  const { error } = await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: 'do the thing',
    sessionId: UUID,
    fetchImpl: async () => {
      called = true
      return ok()()
    },
  })
  assert.match(error, /INTERACTIVE_AUTH_TOKEN/)
  assert.equal(called, false, 'an unauthenticated request must not be attempted')
})

test('a token with leading or trailing whitespace is refused, not trimmed', () => {
  // A trailing newline is the case this catches, and the common one: a secret read whole
  // rather than its value. It is not a legal header value, so the request would die in the
  // generic `could not reach` branch naming neither the header nor the variable. Trimming
  // instead would be worse — it repairs a value the SERVICE did not repair, so the two ends
  // would disagree about the token while both looked configured.
  for (const bad of [' ', '  ', '\n', `${TOKEN}\n`, ` ${TOKEN}`, `${TOKEN} `]) {
    const { error } = resolveAuthToken(bad)
    assert.ok(error, `expected ${JSON.stringify(bad)} to be refused`)
    assert.match(error, /INTERACTIVE_AUTH_TOKEN/)
  }
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
    authToken: TOKEN,
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

test('the request carries the bearer token the counterparty requires', async () => {
  let seen
  await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: 'do the thing',
    sessionId: UUID,
    authToken: TOKEN,
    fetchImpl: async (url, init) => {
      seen = { url, init }
      return { ok: true, status: 200, text: async () => 'done' }
    },
  })

  // Expected value from the CONTRACT constants above, never from cluster-spawn.mjs — see the
  // note there. Delete the header from cluster-spawn.mjs and this fails with
  // `undefined !== 'Bearer not-a-real-token-2f9c41'`, which is the check that it can fail.
  const header = seen.init.headers[CONTRACT_AUTH_HEADER]
  assert.equal(header, `${CONTRACT_AUTH_SCHEME}${TOKEN}`)

  // Pinned by SHAPE, not by inequality. `notEqual(header, TOKEN)` passes for any prefixed
  // value — `'X' + TOKEN` satisfies it — so it documents the two near-misses without checking
  // either. Both are refused by the counterparty with 401 *before* its route handler runs,
  // which is why the scheme's exact spelling and the value's exact bytes are what is asserted.
  assert.ok(header.startsWith(CONTRACT_AUTH_SCHEME), 'the header must open with the contract scheme, verbatim')
  assert.equal(header.slice(CONTRACT_AUTH_SCHEME.length), TOKEN, 'the value after the scheme must be the token, unaltered')
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
    authToken: TOKEN,
    fetchImpl: async () => ({ ok: false, status: 400, text: async () => 'invalid session id\n' }),
  })
  assert.match(error, /HTTP 400/)
  assert.match(error, /invalid session id/)
})

test('a 401 names the credential, so a wrong token is not read as a network fault', async () => {
  // The half resolveAuthToken's refusal cannot reach: a token that IS set and does not match
  // the one the service was started with. The service answers an absent, malformed and wrong
  // credential identically, so the bare status sends the reader looking at their network.
  const { error } = await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: 'x',
    sessionId: UUID,
    authToken: TOKEN,
    fetchImpl: async () => ({ ok: false, status: 401, text: async () => 'unauthorized\n' }),
  })
  assert.match(error, /HTTP 401/)
  assert.match(error, /INTERACTIVE_AUTH_TOKEN/)
})

test('an unreachable service and a timeout are reported as different failures', async () => {
  const refused = await startClusterSession({
    baseUrl: 'http://host:30090',
    prompt: 'x',
    sessionId: UUID,
    authToken: TOKEN,
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
    authToken: TOKEN,
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

// --- resolveClusterTarget: which source each cluster value comes from ----------------------

test('the env var wins over the config file for both cluster values', () => {
  const r = resolveClusterTarget({
    envUrl: 'https://from-env.example',
    envToken: 'env-token',
    file: { cluster: { url: 'https://from-file.example', token: 'file-token' } },
  })
  assert.equal(r.url, 'https://from-env.example')
  assert.equal(r.token, 'env-token')
})

test('the config file supplies a value the env does not', () => {
  const r = resolveClusterTarget({
    envUrl: null,
    envToken: null,
    file: { cluster: { url: 'https://from-file.example', token: 'file-token' } },
  })
  assert.equal(r.url, 'https://from-file.example')
  assert.equal(r.token, 'file-token')
})

test('an empty env string falls through to the file rather than counting as set', () => {
  // resolveClusterBaseUrl and resolveAuthToken both treat '' as unset, so counting it as set
  // here would hand them a value they immediately refuse — a server that reads as configured
  // and refuses every spawn.
  const r = resolveClusterTarget({
    envUrl: '',
    envToken: '',
    file: { cluster: { url: 'https://from-file.example', token: 'file-token' } },
  })
  assert.equal(r.url, 'https://from-file.example')
  assert.equal(r.token, 'file-token')
})

test('neither source set yields nulls, which the resolvers below then refuse', () => {
  const r = resolveClusterTarget({ envUrl: null, envToken: null, file: null })
  assert.equal(r.url, null)
  assert.equal(r.token, null)
  assert.match(resolveClusterBaseUrl(r.url).error, /not configured/)
  assert.match(resolveAuthToken(r.token).error, /no token/)
})

test('a file with no cluster key is not an error — absent is the normal case', () => {
  const r = resolveClusterTarget({ envUrl: 'https://x.example', envToken: 't', file: { spawn: {} } })
  assert.equal(r.url, 'https://x.example')
  assert.equal(r.token, 't')
})

test('a non-object cluster key refuses rather than reading half of it', () => {
  const r = resolveClusterTarget({ envUrl: null, envToken: null, file: { cluster: 'https://x.example' } })
  assert.match(r.error, /which is not an object/)
  assert.match(r.error, /refusing rather than/)
})
