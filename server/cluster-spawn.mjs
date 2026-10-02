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
        'the cluster target is not configured: no service URL is set. Set SUPERVISOR_CLUSTER_URL to the ' +
        'claude-interactive service address reachable from this machine (the pod publishes none by itself — ' +
        'it needs a Service and a NodePort in nuke dev), then restart the MCP server.',
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

// One turn on the addressed conversation, and nothing else.
//
// Returns `{sessionId, status, answer}` on a 2xx, `{error}` otherwise — including the
// service's own non-2xx bodies, which carry a short human-readable reason ("empty prompt",
// "invalid session id") that is far more useful than the status alone.
export async function startClusterSession({
  baseUrl,
  prompt,
  sessionId,
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

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let response
  try {
    response = await fetchImpl(`${resolved.baseUrl}${PROMPT_PATH}`, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain; charset=utf-8', [SESSION_HEADER]: sessionId },
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
    return {
      error: `the cluster service refused the prompt: HTTP ${response.status}${body.trim() ? ` — ${body.trim()}` : ''}`,
    }
  }

  // The id is returned, not discovered — see the header. `status` rides along so a caller
  // recording the spawn has the one fact the response does carry.
  return { sessionId, status: response.status, answer: body }
}
