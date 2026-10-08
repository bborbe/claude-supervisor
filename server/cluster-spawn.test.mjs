// Tests for the cluster spawn call.
//
// The things worth pinning are the ones a working-looking implementation gets wrong: that an
// unconfigured service URL REFUSES rather than reaching for a default, that an illegal session
// id is refused BEFORE the call, and that an unconfigured token refuses too — the service
// answers a bad id with a 400 and a missing credential with a 401, both naming nothing the
// caller can act on, so a late failure is indistinguishable from a wrong prompt.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  CLUSTER_ANSWER_MAX,
  CLUSTER_SESSION_ID_PATTERN,
  DEFAULT_TIMEOUT_MS,
  PROMPT_PATH,
  SESSION_HEADER,
  classifyClusterAnswer,
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

  // `answer` AND `outcome` — the two fields the spawn path used to drop. Asserted as a whole
  // object rather than field-by-field so a future edit that stops returning either one fails
  // here rather than only at a live spawn against the pod.
  assert.deepEqual(result, { sessionId: UUID, status: 200, answer: 'done', outcome: 'answered' })
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

test('neither source set refuses HERE, naming the config file the server actually read', () => {
  // Refused inside resolveClusterTarget rather than left to startClusterSession, because only
  // this frame holds the real config path — a refusal naming the default location points a
  // SUPERVISOR_CONFIG operator at a file that does not exist on their machine.
  const r = resolveClusterTarget({ envUrl: null, envToken: null, file: null, path: '/etc/sup.json' })
  assert.match(r.error, /not configured/)
  assert.match(r.error, /\/etc\/sup\.json/, 'must name the file the server read')
  assert.ok(!r.error.includes('~/.config/claude-supervisor'), 'must not name the default location')

  const t = resolveClusterTarget({ envUrl: 'https://x.example', envToken: null, file: null, path: '/etc/sup.json' })
  assert.match(t.error, /no token/)
  assert.match(t.error, /\/etc\/sup\.json/)
})

test('a file with no cluster key is not an error — absent is the normal case', () => {
  const r = resolveClusterTarget({ envUrl: 'https://x.example', envToken: 't', file: { spawn: {} } })
  assert.equal(r.url, 'https://x.example')
  assert.equal(r.token, 't')
})

test('a non-object cluster key refuses rather than reading half of it, and is not echoed', () => {
  // `[]` and a scalar both slip past a bare `typeof === 'object'` check, which is why the
  // guard is `typeof !== 'object' || Array.isArray` — deleting either half keeps a weaker
  // suite green.
  for (const bad of ['https://x.example', 5, [], true]) {
    const r = resolveClusterTarget({ envUrl: null, envToken: null, file: { cluster: bad } })
    assert.match(r.error, /is not an object/, `cluster: ${JSON.stringify(bad)} must refuse`)
  }
  // The scalar case is the one that could BE the token — an operator writing `cluster: "<token>"`
  // by mistake — and this string travels back to the client, so it is not echoed.
  const r = resolveClusterTarget({ envUrl: null, envToken: null, file: { cluster: 'a-secret-value' } })
  assert.ok(!r.error.includes('a-secret-value'), 'the offending value must not be echoed')
})

test('a bad value in the LOSING source refuses too, not only the winner', () => {
  // The sibling precedent's explicit rule (spawn-mode.mjs: "validation is deliberately NOT
  // limited to the winner"). Unchecked, the file's bad value sits dormant until the env var is
  // removed — and then the spawn breaks with an error that was present all along.
  const r = resolveClusterTarget({
    envUrl: 'https://ok.example',
    envToken: 'good',
    file: { cluster: { url: 'not a url' } },
  })
  assert.match(r.error, /not a valid URL/)
})

test('a file-sourced bad value names the file key, not the env var it never came from', () => {
  const r = resolveClusterTarget({ envUrl: null, envToken: null, file: { cluster: { url: 'not a url' } } })
  assert.match(r.error, /"cluster\.url" in the supervisor config/)
  assert.ok(!r.error.includes('SUPERVISOR_CLUSTER_URL'), 'must not name the env var for a file value')
})

test('supervisor.mjs actually resolves through resolveClusterTarget', () => {
  // The WIRING is the feature. Every other test here calls resolveClusterTarget directly, so
  // reverting the call site to `baseUrl: config.clusterUrl` would leave the whole suite green
  // with the feature dead. Pinned by reading supervisor.mjs's text, the remedy this repo
  // already uses for the same reason in attention-poll.test.mjs — it starts an MCP server at
  // import, so it cannot be imported behaviourally.
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(src, /resolveClusterTarget\(\{/)
  assert.match(src, /file: config\.configFileContents/)
  assert.match(src, /baseUrl: target\.url/)
  assert.match(src, /authToken: target\.token/)
})

test('supervisor.mjs actually carries the answer out of the spawn', () => {
  // The same wiring-is-the-feature guard, for the defect this change closes. `startClusterSession`
  // returned `answer` long before anything read it: the call site used `started.sessionId` alone,
  // so every other test here could pass — and did — while three complete answers were read from
  // the outside as three empty turns (2026-10-08). Asserting the read is the only way to pin a
  // value whose absence is silent by construction.
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(src, /started\.answer/)
  // And that it reaches BOTH surfaces a caller can read — the tool result and the agent record.
  assert.match(src, /answer: result\.answer/)
  assert.match(src, /outcome: result\.outcome/)
  assert.match(src, /result,\n\s*error: null,/)
})

test('a structured failure and an empty turn are different verdicts, not one', () => {
  // The load-bearing pair, and the whole point of the classification. If these two ever collapse
  // to the same value the defect is back, whatever else this suite says.
  assert.equal(classifyClusterAnswer('{"status":"failed","message":"vault-cli project not found"}'), 'failed')
  assert.equal(classifyClusterAnswer(''), 'empty')
})

test('the verdicts the deployed pod actually returned are read as themselves', () => {
  // Shapes taken verbatim from the 2026-10-08 replay against `claude-interactive`, so this suite
  // fails if the reading drifts from the payload the pod really sends.
  assert.equal(classifyClusterAnswer('{"status": "success"}'), 'succeeded')
  assert.equal(classifyClusterAnswer('{"status":"failed","message":"…"}'), 'failed')
  assert.equal(classifyClusterAnswer('OK'), 'answered')
})

test('whitespace is emptiness, and a JSON non-object is not a verdict', () => {
  assert.equal(classifyClusterAnswer('   \n  '), 'empty')
  // `JSON.parse` accepts every one of these; none carries a status, so none may read as one.
  for (const body of ['42', '"failed"', '[{"status":"failed"}]', 'null', 'true']) {
    assert.equal(classifyClusterAnswer(body), 'answered', `${body} must not read as a verdict`)
  }
})

test('prose that merely mentions failure is not a failed turn', () => {
  // The narrowness that keeps this honest: only a parsed OBJECT with a `status` field counts.
  // A child writing "the build failed" is answering, not reporting a structured failure, and
  // reinterpreting its words would be inventing a taxonomy over text this module does not own.
  assert.equal(classifyClusterAnswer('the build failed, here is why: …'), 'answered')
  assert.equal(classifyClusterAnswer('{"error":"boom"}'), 'answered')
})

test('a non-string answer is empty rather than a crash', () => {
  // `answer` is typed by the caller, not by this module: a fetch double or a future transport
  // could hand back undefined, and a classifier that threw would take the whole spawn down.
  for (const body of [undefined, null, 42, {}]) {
    assert.equal(classifyClusterAnswer(body), 'empty')
  }
})

test('the answer bound is the number the spawn slices with', () => {
  // Pinned as a value: `spawnClusterWorker` slices with THIS constant, so a silent change here
  // is a silent change to how much of a child's answer a manager is shown.
  assert.equal(CLUSTER_ANSWER_MAX, 4000)
})

test('url and token resolve INDEPENDENTLY — a mixed pair takes each from its own source', () => {
  // The whole point of the feature is per-value resolution, and both precedence tests above set
  // BOTH values from the SAME source — so a future edit routing them through one shared loop
  // would keep them green. These two cases pin the independence in both directions.
  const urlFromFile = resolveClusterTarget({
    envUrl: null,
    envToken: 'env-token',
    file: { cluster: { url: 'https://from-file.example', token: 'file-token' } },
  })
  assert.equal(urlFromFile.url, 'https://from-file.example')
  assert.equal(urlFromFile.token, 'env-token')

  const tokenFromFile = resolveClusterTarget({
    envUrl: 'https://from-env.example',
    envToken: null,
    file: { cluster: { url: 'https://from-file.example', token: 'file-token' } },
  })
  assert.equal(tokenFromFile.url, 'https://from-env.example')
  assert.equal(tokenFromFile.token, 'file-token')
})

test('a file-sourced bad TOKEN names the token key, not the url key', () => {
  // The mirror of the bad-URL case above, and unasserted until now: swapping the two `label`
  // strings in resolveClusterTarget's source arrays kept the suite green.
  const r = resolveClusterTarget({ envUrl: null, envToken: null, file: { cluster: { token: 1234567890 } } })
  assert.match(r.error, /"cluster\.token" in the supervisor config/)
  assert.ok(!r.error.includes('"cluster.url"'), 'must not name the url key for a token value')
  assert.ok(!r.error.includes('1234567890'), 'and must not echo the value')
})

test('a credential in a url userinfo is redacted before the refusal echoes it', () => {
  const r = resolveClusterBaseUrl('ftp://user:secret@host/x')
  assert.match(r.error, /<redacted>@/)
  assert.ok(!r.error.includes('secret'), 'the userinfo must not ride out in the message')
})

test('a whitespace-only env token WINS and is refused, rather than falling through to the file', () => {
  // The asymmetry with '' is deliberate and argued at length in the source: '' is absence, so it
  // falls through; ' ' is a value the operator put there, and trimming it would silently repair
  // a value the service compares exactly. That cost is real — a trailing newline in an exported
  // env value shadows a perfectly good `cluster.token` — so it is pinned rather than left to the
  // comment: a future change making whitespace fall through would otherwise stay green.
  const r = resolveClusterTarget({ envToken: ' ', file: { cluster: { token: 'file-token' } } })
  assert.match(r.error, /INTERACTIVE_AUTH_TOKEN/)
  assert.match(r.error, /whitespace/)
  assert.ok(!r.error.includes('cluster.token'), 'must not fall through to the file value')
})

test('a non-string token reports its TYPE and never echoes the value', () => {
  // The hardening this pins: a nested object under `cluster.token` would be rendered whole by
  // JSON.stringify — secret included — into a string that travels back to the client. Asserting
  // only `/not a token string/` passes identically against the old echoing message, so the
  // assertion has to be about the ABSENCE of the value, which is the whole point.
  const nested = resolveAuthToken({ value: 'a-secret-value' }, '"cluster.token" in /c.json')
  assert.match(nested.error, /not a token string/)
  assert.ok(!nested.error.includes('a-secret-value'), 'the value must not be echoed')
  assert.ok(!nested.error.includes('value'), 'nor any of its nested contents')

  const numeric = resolveAuthToken(1234567890)
  assert.match(numeric.error, /not a token string/)
  assert.ok(!numeric.error.includes('1234567890'), 'the value must not be echoed')
})
