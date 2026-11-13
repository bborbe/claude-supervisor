// The policy tier for wrapper-backed headless workers.
//
// ⚠️ **Why this lives in the supervisor and not in the wrapper.** claude-wrapper spends the
// child's single `--permission-prompt-tool` on its own MCP tool, so the `PermissionRequest`
// hook this server registers on the SDK path is never consulted for a wrapper-backed worker.
// Every prompt the bundled policy would have allowed — `Edit`/`Write` inside the worker's own
// cwd above all — therefore parked for a human. Measured 2026-11-06: `permissions.jsonl` had
// not been written since 2026-10-30, i.e. zero policy decisions on the wrapper path.
//
// The seam is deliberately supervisor-side: the park is wrapper STATE (`question` on the
// session record), readable with one roster call, and answerable by request id through the
// same `POST /sessions/{id}/answer` a manager uses. That keeps ONE decision table
// (`policy.mjs`) rather than a Go copy that drifts, needs no wrapper change, and scopes
// itself for free: each supervisor decides only for the agents in its own Map.
//
// ⚠️ **The trigger is a poll, not the event stream.** The SSE stream carries the child's raw
// JSONL only; the wrapper registers a park without emitting any event for it. So the poll
// interval is the latency this tier adds to a policy-allowed prompt.
//
// ⚠️ **Wrapper down → nothing is decided and nothing is answered.** The park stays in the
// wrapper, still answerable by a manager once the wrapper is back. A wrapper-held question
// has no deadline, so there is no 15-minute deny behind it — the same as before this tier
// existed. The outage is logged on transition, never per tick.
//
// ⚠️ **A race with a manager answer is safe by construction.** The answer names the request
// id, so a manager that answered first makes the wrapper return 409 and this tier changes
// nothing; the id is not retried.

export const WRAPPER_POLICY_INTERVAL_MS = 2000

// Wrapper answers that mean the request is gone for good — retrying cannot land.
//   404 unknown session · 409 already answered / no open question · 410 child exited
const TERMINAL_ANSWER_STATUSES = new Set([404, 409, 410])

/**
 * Is this parked question a tool-permission prompt the policy can judge?
 *
 * An `AskUserQuestion` park is a selection among options, not an allow/deny — the policy has
 * no vocabulary for it and must never answer it. `kind` is the wrapper's discriminator; a
 * record without one (an older wrapper) is judged by tool name alone.
 */
export function isPermissionQuestion(question) {
  if (!question || typeof question.request_id !== 'string' || !question.request_id) return false
  if (typeof question.tool_name !== 'string' || !question.tool_name) return false
  if (question.tool_name === 'AskUserQuestion') return false
  if (question.kind === undefined || question.kind === null || question.kind === '') return true
  return question.kind === 'permission'
}

/**
 * Build the policy poller for wrapper-backed workers.
 *
 * Every dependency is injected so a test can drive `tick()` without a wrapper or a server.
 *
 * @param {object} deps
 * @param {() => Iterable<object>} deps.agents        the live agent records
 * @param {() => Promise<Array<object>>} deps.listSessions  one wrapper roster read
 * @param {(a: {sessionId: string, requestId: string, behavior: 'allow'|'deny'}) => Promise<unknown>} deps.answer
 * @param {(toolName: string, input: unknown, cwd: string, rules: unknown) => {action: string, key: string, rule: object|null}} deps.decide
 * @param {(record: object) => void} deps.logPermission  one `permissions.jsonl` row
 * @param {(...a: string[]) => void} deps.log
 * @param {() => string} [deps.now]
 */
export function createWrapperPolicyPoller({ agents, listSessions, answer, decide, logPermission, log, now = () => new Date().toISOString() }) {
  // Request ids this tier has already judged. Each id is decided ONCE: an escalated id must
  // not be re-logged every tick, and an answered id must not be answered twice.
  const judged = new Set()
  const state = { ok: null, inFlight: false }

  // ⚠️ A row adopted from the ledger after a reconnect carries `policyPath` but no `rules` —
  // the overlay is not persisted. Judging it against the server's default rules would apply
  // a policy the worker was never spawned under, possibly a looser one, so such a row is left
  // to the manager. A row with neither ran under the default rules, so it is judged as before.
  const judgeable = (a) => Boolean(a?.wrapperSessionId) && (a.rules !== undefined || !a.policyPath)
  const backedAgents = () => [...agents()].filter(judgeable)

  async function judge(agent, question) {
    const verdict = decide(question.tool_name, question.input, agent.cwd, agent.rules)
    const base = {
      ts: now(),
      agent: agent.id,
      label: agent.label,
      request_id: question.request_id,
      tool: question.tool_name,
      key: verdict.key,
      matched_rule: verdict.rule ? `${verdict.rule.tool}:${verdict.rule.match}` : null,
      policy: agent.policyPath ?? null,
      via: 'wrapper',
    }

    if (verdict.action !== 'allow' && verdict.action !== 'deny') {
      // Escalate: leave the park exactly where it is, for the manager.
      judged.add(question.request_id)
      logPermission({ ...base, decision: null, decided_by: 'escalated' })
      return { request_id: question.request_id, action: 'escalate' }
    }

    try {
      await answer({ sessionId: agent.wrapperSessionId, requestId: question.request_id, behavior: verdict.action })
    } catch (error) {
      if (TERMINAL_ANSWER_STATUSES.has(error?.status)) {
        // Someone else answered first, or the child is gone. Not a policy decision that
        // landed, so no `decided_by: policy` row — that row means "the policy released it".
        judged.add(question.request_id)
        log(`wrapper policy: ${question.request_id} for ${agent.id} not answered (HTTP ${error.status}) — already settled elsewhere`)
        return { request_id: question.request_id, action: verdict.action, delivered: false, status: error.status }
      }
      // Transient (503, timeout): leave it unjudged so the next tick retries.
      log(`wrapper policy: answering ${question.request_id} for ${agent.id} failed, will retry: ${error?.message ?? error}`)
      return { request_id: question.request_id, action: verdict.action, delivered: false, retry: true }
    }

    judged.add(question.request_id)
    if (agent.status === 'blocked-on-permission') agent.status = 'running'
    logPermission({ ...base, decision: verdict.action, decided_by: 'policy' })
    log(`policy ${verdict.action} ${question.tool_name} for ${agent.id} (${base.matched_rule}) via wrapper`)
    return { request_id: question.request_id, action: verdict.action, delivered: true }
  }

  async function tick() {
    const backed = backedAgents()
    if (backed.length === 0) return { skipped: 'no wrapper-backed agents' }

    let sessions
    try {
      sessions = await listSessions()
      if (!Array.isArray(sessions)) throw new Error('wrapper session list was not an array')
    } catch (error) {
      if (state.ok !== false) log(`wrapper policy: wrapper unreadable — parks stay with the manager until it clears: ${error?.message ?? error}`)
      state.ok = false
      return { ok: false, error: error?.message ?? String(error) }
    }
    if (state.ok === false) log('wrapper policy: wrapper readable again')
    state.ok = true

    const live = new Set()
    const results = []
    for (const agent of backed) {
      const question = sessions.find((s) => s?.id === agent.wrapperSessionId)?.question
      if (!isPermissionQuestion(question)) continue
      live.add(question.request_id)
      if (judged.has(question.request_id)) continue
      results.push(await judge(agent, question))
    }
    // Forget ids no longer parked anywhere, so the set is bounded by the open parks.
    for (const id of judged) if (!live.has(id)) judged.delete(id)
    return { ok: true, results }
  }

  // The interval entry point: never throws (an async interval callback has no caller to
  // reject to) and never stacks a second poll behind a slow one.
  async function run() {
    if (state.inFlight) return { skipped: 'in flight' }
    state.inFlight = true
    try {
      return await tick()
    } catch (error) {
      log(`wrapper policy: tick failed: ${error?.message ?? error}`)
      return { ok: false, error: error?.message ?? String(error) }
    } finally {
      state.inFlight = false
    }
  }

  return { tick, run }
}
