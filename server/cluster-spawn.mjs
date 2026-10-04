// Start a worker as a SESSION inside the cluster's `claude-interactive` service.
//
// The service already holds a Claude Code session across requests and already answers over
// HTTP, so a cluster worker is not a new pod — it is a conversation inside the pod that is
// already running. This module is the whole of that call: mint a session id, address the
// service's `POST /prompt` with it, and hand back what happened.
//
// ⚠️ **The id is minted HERE, and a 200 is NOT evidence the session exists.** The service's
// `POST /prompt` returns the session's ANSWER as `text/plain` and echoes no id, because the
// caller supplies one (`interactive/session-id.go` in `bborbe/agent`). So a successful call
// proves the request was well-formed and a turn ran; it does not, on its own, show WHICH
// conversation ran it. The proof is the serving pod's own log — the `turn start id=<id>` /
// `turn end id=<id>` pair from `interactive/session-cache.go` — and that is the caller's to
// read. This module reports the id it addressed and the status it got, and claims nothing
// further; a caller that treats the response as the evidence has an unfalsifiable check.
//
// ⚠️ **The id must match the service's pattern**, asserted here rather than assumed. The id
// reaches a backend CLI as a command-line argument, so a leading `-` is parsed as a flag and
// `.` or `/` escapes a session directory — the service rejects those with a 400, and a 400
// decoded after the fact is a worse error message than a refusal before the call.
//
// Nothing here reads the filesystem, the environment, or the clock.

import { randomUUID } from 'node:crypto'

// Verbatim from `interactive/session-id.go` (`sessionIDPattern`): one to 64 characters, the
// first of which is not `-`. A `randomUUID` satisfies it; a hand-built id may not, which is
// why the check is here and not only at the far end.
export const CLUSTER_SESSION_ID_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$/

// The one route this module talks to. Named rather than inlined so the contract is greppable
// from here to `interactive/service.go`, which registers it.
export const PROMPT_PATH = '/prompt'

// The header the service addresses a conversation by. Spelling is load-bearing — the service
// reads exactly this (`sessionHeader`) and an absent header silently resolves to the shared
// `identity` conversation, so a typo would put every worker into one conversation rather than
// failing. That is the failure this constant exists to make impossible.
export const SESSION_HEADER = 'X-Session-Id'

// The header and the scheme the service authenticates with, verbatim from its own contract:
// `bborbe/agent` `docs/interactive-service.md` § Authentication — "Every gated route requires
// the request header `Authorization: Bearer <token>`. The scheme is matched exactly as
// `Bearer `". Both halves are load-bearing, and each fails the same silent way: a missing
// header is a 401, and so is a scheme that differs by case or loses its trailing space — so a
// near-miss reads as "not authorized" rather than as a malformed request, and sends the
// reader looking at the token instead of at the line that built the header.
//
// ⚠️ **This header is only confidential over TLS.** `resolveClusterBaseUrl` below accepts
// `http:` as well as `https:`, and the deployed shape — a NodePort on the nuke dev node
// network — is plaintext, where a bearer token is readable by anything sharing the segment and
// grants exactly the `/prompt` access it was sent to enable. `https` is permitted, so the safe
// configuration exists; it is the operator's choice, and this note exists so the choice is
// visible where the header is built rather than only in a network diagram.
export const AUTH_HEADER = 'Authorization'
export const AUTH_SCHEME = 'Bearer '

// A cluster spawn that has not answered within this long is not slow, it is broken — the
// service holds the turn open and answers in one response, so there is no partial progress
// to wait for. Bounded rather than unbounded so a wedged pod costs one error, not a manager
// session stuck inside a tool call.
export const DEFAULT_TIMEOUT_MS = 10 * 60 * 1000

// The session id a new cluster worker addresses. A UUID rather than a slug of the task name:
// two workers on one task must not collide, and the task is bound to the id afterwards
// rather than encoded in it — the binding is the association, so the id carries no meaning
// that could disagree with the file.
export function newSessionId(random = randomUUID) {
  const id = random()
  if (typeof id !== 'string' || !CLUSTER_SESSION_ID_PATTERN.test(id)) {
    // A generator that cannot produce a legal id is a programming error, not a caller error,
    // and it is refused here because the alternative is a 400 from the service that names
    // nothing the caller can act on.
    return { error: `generated session id ${JSON.stringify(id)} does not match the service's pattern ${CLUSTER_SESSION_ID_PATTERN}` }
  }
  return { sessionId: id }
}

// Which of the two sources each cluster value comes from. The env var wins, the config file
// is the fallback — the same precedence `spawn-mode.mjs` applies to `spawn.mode`, and for the
// same reason: an env var is a per-invocation override, the file is the machine's standing
// configuration.
//
// ⚠️ **The file is not a convenience — it is the only source that reaches a RUNNING server.**
// An MCP server's `env` block is read by Claude Code and cached at session start, so a value
// added to it reaches no already-running session by any in-session route: `/mcp` Reconnect
// re-spawns the child from that cached definition rather than re-reading the file. Measured
// 2026-10-04 — a server restarted three minutes *after* an env-block write still refused with
// "no service URL is set". A file read by the *server* at its own start has no such problem,
// because a Reconnect re-execs the server and the file is read again. That distinction was
// already recorded in `config.mjs`'s header ("editing it takes effect on the next server
// start"); this function is what makes it true for the cluster pair.
//
// Raw and unvalidated, deliberately: the legal shapes stay in resolveClusterBaseUrl and
// resolveAuthToken below, so an env value and a file value meet one validator rather than two.
export function resolveClusterTarget({ envUrl, envToken, file, path = 'the supervisor config' } = {}) {
  const section = file == null ? null : file.cluster
  if (section != null && (typeof section !== 'object' || Array.isArray(section))) {
    return {
      error:
        `"cluster" in ${path} is ${JSON.stringify(section)}, which is not an object — refusing rather than ` +
        `guessing, since a half-read cluster target is discovered only by noticing it. ` +
        `Expected {"url": "…", "token": "…"}.`,
    }
  }
  const pick = (env, key) =>
    env !== undefined && env !== null && env !== '' ? env : (section?.[key] ?? null)
  return { url: pick(envUrl, 'url'), token: pick(envToken, 'token') }
}

// Normalize the service's base URL, or refuse it.
//
// Refused rather than defaulted, and the refusal is the point: the service has no Service
// object of its own, the supervisor runs on a different machine from the pod, and the
// operator's cluster rules forbid a port-forward — so there is no address to guess, and a
// guessed one either fails confusingly or reaches the wrong thing. An unset URL therefore
// means "the cluster target is not configured here", which is an answer, not an absence.
export function resolveClusterBaseUrl(raw) {
  if (raw === undefined || raw === null || raw === '') {
    return {
      error:
        'the cluster target is not configured: no service URL is set. Set "cluster.url" in the supervisor ' +
        'config file (~/.config/claude-supervisor/config.json) — a /mcp Reconnect picks that up, because the ' +
        'server re-reads the file at its own start. Setting SUPERVISOR_CLUSTER_URL on the server entry also ' +
        'works but needs a NEW session: an MCP env block is cached at session start, so a Reconnect re-spawns ' +
        'from the cached definition and cannot see a later edit. The address is the claude-interactive service ' +
        'as reachable from this machine (the pod publishes none by itself — it needs a Service and a NodePort ' +
        'in nuke dev).',
    }
  }
  if (typeof raw !== 'string') {
    return { error: `SUPERVISOR_CLUSTER_URL is ${JSON.stringify(raw)}, which is not a URL` }
  }
  let url
  try {
    url = new URL(raw)
  } catch {
    return { error: `SUPERVISOR_CLUSTER_URL is ${JSON.stringify(raw)}, which is not a valid URL` }
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return { error: `SUPERVISOR_CLUSTER_URL is ${JSON.stringify(raw)}, whose scheme is not http or https` }
  }
  return { baseUrl: url.origin + url.pathname.replace(/\/+$/, '') }
}

// The token a cluster spawn presents, or a refusal.
//
// Refused rather than omitted, following resolveClusterBaseUrl above and for the same
// reason: the service answers a header-less request with 401 *before* the route's handler
// runs, so an unconfigured supervisor and a wrong token produce one indistinguishable
// observable. Refusing here is what turns that into a sentence naming the variable to set.
export function resolveAuthToken(raw) {
  if (raw === undefined || raw === null || raw === '') {
    return {
      error:
        'the cluster target has no token: neither INTERACTIVE_AUTH_TOKEN nor "cluster.token" is set. ' +
        'The claude-interactive service requires `Authorization: Bearer <token>` on POST /prompt and refuses ' +
        'without it, so a spawn would fail as a 401 naming nothing to fix. Set "cluster.token" in the ' +
        'supervisor config file (~/.config/claude-supervisor/config.json) — a /mcp Reconnect picks that up, ' +
        'because the server re-reads the file at its own start — or set INTERACTIVE_AUTH_TOKEN on the server ' +
        'entry, which needs a NEW session, since an MCP env block is cached at session start.',
    }
  }
  if (typeof raw !== 'string') {
    return { error: `INTERACTIVE_AUTH_TOKEN is ${JSON.stringify(raw)}, which is not a token string` }
  }
  // Whitespace is refused, NOT trimmed away, and both halves of that matter.
  //
  // A value that is only whitespace builds `Bearer  ` — the scheme's own trailing space plus
  // the value's — which the counterparty matches exactly and answers with 401: precisely the
  // indistinguishability this function exists to remove. A trailing newline is the sharper
  // case, and the common one: a secret read whole rather than its value. That is not a legal
  // header value at all, so the request dies in the generic `could not reach the cluster
  // service` branch naming neither the header nor the variable.
  //
  // Trimming instead of refusing would be worse than either failure. It would silently repair
  // a value the SERVICE did not repair, so a token the operator mis-pasted would start
  // matching here and stop matching there — two ends that disagree while both look configured.
  // Refusing keeps them honest and names the variable.
  if (raw.trim() !== raw) {
    return {
      error:
        'INTERACTIVE_AUTH_TOKEN has leading or trailing whitespace, which is not a legal header value. ' +
        'A trailing newline — a secret file read whole rather than its value — is the usual cause. Fix the ' +
        'value rather than trimming it here: the service compares against the token it was started with, so a ' +
        'value this side silently repairs is one the two ends would then disagree about.',
    }
  }
  return { token: raw }
}

// One turn on the addressed conversation, and nothing else.
//
// Returns `{sessionId, status, answer}` on a 2xx, `{error}` otherwise — including the
// service's own non-2xx bodies, which carry a short human-readable reason ("empty prompt",
// "invalid session id") that is far more useful than the status alone.
export async function startClusterSession({
  baseUrl,
  prompt,
  sessionId,
  authToken,
  fetchImpl = fetch,
  timeoutMs = DEFAULT_TIMEOUT_MS,
} = {}) {
  const resolved = resolveClusterBaseUrl(baseUrl)
  if (resolved.error) return { error: resolved.error }
  if (typeof prompt !== 'string' || !prompt.trim()) {
    return { error: 'prompt is required' }
  }
  if (typeof sessionId !== 'string' || !CLUSTER_SESSION_ID_PATTERN.test(sessionId)) {
    return {
      error: `session id ${JSON.stringify(sessionId)} does not match the service's pattern ${CLUSTER_SESSION_ID_PATTERN}`,
    }
  }
  // Checked before the request for the same reason as the two above: the service's own answer
  // to a missing credential is a bare 401 that names nothing the caller can act on.
  const auth = resolveAuthToken(authToken)
  if (auth.error) return { error: auth.error }

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let response
  try {
    response = await fetchImpl(`${resolved.baseUrl}${PROMPT_PATH}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'text/plain; charset=utf-8',
        [SESSION_HEADER]: sessionId,
        [AUTH_HEADER]: `${AUTH_SCHEME}${auth.token}`,
      },
      body: prompt,
      signal: controller.signal,
    })
  } catch (error) {
    const why = error?.name === 'AbortError' ? `no answer within ${timeoutMs}ms` : error?.message || String(error)
    return { error: `could not reach the cluster service at ${resolved.baseUrl}${PROMPT_PATH}: ${why}` }
  } finally {
    clearTimeout(timer)
  }

  let body = ''
  try {
    body = await response.text()
  } catch (error) {
    return { error: `the cluster service answered ${response.status} but its body could not be read: ${error?.message || error}` }
  }

  if (!response.ok) {
    // 401 gets its own sentence, and it is the half resolveAuthToken's refusal cannot reach: a
    // token that IS set and does not match the one the service was started with. The service
    // answers an absent, malformed and wrong credential identically, so the bare status sends
    // the reader looking at their network for what is a credential mismatch.
    const hint =
      response.status === 401
        ? ' — the service rejected the credential: check that INTERACTIVE_AUTH_TOKEN matches the token the service was started with'
        : ''
    return {
      error: `the cluster service refused the prompt: HTTP ${response.status}${body.trim() ? ` — ${body.trim()}` : ''}${hint}`,
    }
  }

  // The id is returned, not discovered — see the header. `status` rides along so a caller
  // recording the spawn has the one fact the response does carry.
  return { sessionId, status: response.status, answer: body }
}
