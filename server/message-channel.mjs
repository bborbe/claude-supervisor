// Where a follow-up message to a worker goes — or a refusal naming why it goes nowhere.
//
// ⚠️ **The guard this replaces was right for one worker kind and silently wrong for two
// others.** `send_agent_message` decided with `status !== 'interactive' || !paneId`, which
// reads as a general "can this be messaged?" and is really "does this have a pane?". A
// cluster worker is written with `paneId: null` by construction (`supervisor.mjs`, the
// cluster agent literal), so the moment a second transport existed that guard would have
// refused every cluster worker while reading exactly as it did before.
//
// Three kinds, three answers, and the difference between them is the *reason*, not the
// boolean:
//
//   tab      → its pane, typed into and submitted
//   cluster  → its session id, one more turn over HTTP
//   headless → neither, and the refusal says which channel DOES reach it
//
// A pure function over the agent record, so it is testable without a server, a pane or a
// pod. Nothing here reads the filesystem, the environment, or the clock.

import { isA2ATarget } from './a2a-spawn.mjs'

export function resolveMessageChannel(agent) {
  if (!agent || typeof agent !== 'object') {
    return { error: 'no agent record to resolve a message channel from' }
  }
  const id = agent.id ?? '(unknown id)'

  if (agent.status === 'interactive') {
    // ⚠️ The pane check lives INSIDE this branch, never beside it. `!paneId` alone is true
    // for a headless worker AND a cluster worker, so hoisting it turns the tab branch into
    // a general refusal that names the wrong reason for both.
    if (!agent.paneId) {
      return {
        error:
          `agent ${id} is a tab worker whose pane is gone (status "interactive", no pane id) — ` +
          `there is nothing to type into. Drive its tab by hand, or restart the session.`,
      }
    }
    return { channel: 'pane', paneId: agent.paneId }
  }

  if (agent.status === 'cluster') {
    // A cluster turn is addressed BY session id — the service reads it from the
    // `X-Session-Id` header and falls back to its shared `identity` conversation when the
    // header is absent. With no id recorded there is no conversation to address, and
    // posting anyway would put the message in that shared one, which looks like a
    // successful send. Refused here rather than discovered at the far end.
    if (!agent.sessionId) {
      return {
        error:
          `agent ${id} is a cluster worker with no session id recorded, so there is no conversation ` +
          `to address — the session id is the only handle a cluster turn takes.`,
      }
    }
    return { channel: 'cluster', sessionId: agent.sessionId }
  }

  if (isA2ATarget(agent.status)) {
    // An A2A turn goes to the WORKER's own endpoint, never the controller (SC3), on the
    // conversation the first turn opened. All three handles are held only by the process that
    // spawned the worker: a REHYDRATED row has no token, because the ledger deliberately never
    // stores one, so it can be seen live but not driven — refused here rather than sent
    // unauthenticated.
    if (!agent.a2aUrl || !agent.workerToken) {
      return {
        error:
          `agent ${id} is an A2A worker (status "${agent.status}") whose endpoint or token this process ` +
          `does not hold — a row re-adopted after a reconnect carries neither, because the token is ` +
          `never written to disk. It can be watched, but no follow-up turn can reach it from here.`,
      }
    }
    if (!agent.contextId) {
      return {
        error:
          `agent ${id} is an A2A worker with no conversation recorded yet — its first turn has not ` +
          `been sent, so there is no context to continue.`,
      }
    }
    // `protocolVersion` travels with the endpoint and token because a follow-up turn is a fresh
    // request to the same worker, and the worker's version gate does not remember the first turn.
    // ⚠️ SPREAD CONDITIONALLY, so a worker that advertised no version yields the same channel shape
    // as before this existed — an `undefined` key here would be a shape change nothing asked for.
    return {
      channel: 'a2a',
      endpoint: agent.a2aUrl,
      token: agent.workerToken,
      contextId: agent.contextId,
      ...(agent.protocolVersion ? { protocolVersion: agent.protocolVersion } : {}),
    }
  }

  return {
    error:
      `agent ${id} is a headless worker (status "${agent.status}"), which has neither a pane nor a ` +
      `cluster session, so no follow-up can reach it. Answer its parked prompts with ` +
      `answer_permission, or reach it mid-task with SendMessage.`,
  }
}
