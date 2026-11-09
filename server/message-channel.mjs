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
//   headless → its REGISTRY NAME, handed back to the caller, who sends it with `SendMessage`
//
// A pure function over the agent record, so it is testable without a server, a pane or a
// pod. Nothing here reads the filesystem, the environment, or the clock — the registry read
// the headless branch needs arrives as the injected `peerNamesFor`, because the id → name
// join belongs to the caller that holds the session id (`namesForSessionId` in liveness.mjs).

import { isA2ATarget } from './a2a-spawn.mjs'

export function resolveMessageChannel(agent, { peerNamesFor = null, peerHoldersFor = null } = {}) {
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

  // ⚠️ A headless worker IS reachable, and this branch used to say it was not. Its claude
  // child registers a NAME in the session registry, and the peer channel (`SendMessage`)
  // takes a name — so the delivery is the CALLER's and what this server owes it is the
  // address. Measured 2026-11-08: session 6d16758d was live and registered as `claude-1f`,
  // while this refusal told the caller to "reach it mid-task with SendMessage" — which, given
  // only the id the caller held, answers "No agent named '6d16758d…' is reachable". A remedy
  // the caller cannot perform is the defect, not the wording.
  //
  // The resolver is INJECTED rather than imported: this module stays a pure function over the
  // record, and the registry read belongs to the caller that already holds the session id.
  // `peerNamesFor` returning null means "the registry could not be read" — retryable, and
  // deliberately not folded into "no entry", which means the worker is gone.
  const names = typeof peerNamesFor === 'function' ? peerNamesFor(agent.sessionId) : null
  if (!Array.isArray(names)) {
    return {
      error:
        `agent ${id} is a headless worker (status "${agent.status}") whose session registry could not be ` +
        `read, so its SendMessage name is unknown — retry, and answer its parked prompts with ` +
        `answer_permission meanwhile.`,
    }
  }
  const registered = [...new Set(names)]
  if (registered.length === 1) {
    // ⚠️ THE NAME MUST BE THIS WORKER'S ALONE, not merely one this id answers to. `SendMessage`
    // resolves a NAME to a session, so a name two live sessions hold reaches whichever the
    // harness picks — a delivery that reports success while landing in a different worker's
    // conversation. The registry is the only place that can see the collision, and the caller
    // that read it is the only one that can answer the question, so it is asked here.
    const holders = typeof peerHoldersFor === 'function' ? peerHoldersFor(registered[0]) : null
    if (Array.isArray(holders) && holders.filter((sid) => sid !== agent.sessionId).length > 0) {
      return {
        error:
          `agent ${id} is a headless worker registered as "${registered[0]}", a name another live session ` +
          `also answers to — SendMessage would reach whichever the harness picks, so nothing is ` +
          `addressed. Pick the worker from list_agents, or answer its parked prompts with answer_permission.`,
      }
    }
    return { channel: 'peer', name: registered[0] }
  }
  if (registered.length > 1) {
    // ONE session id can be registered under MORE than one name (a renamed session whose
    // older entry has not been pruned), and picking either would deliver to the wrong
    // conversation — so the candidates are named and the choice is the caller's. Repeats of
    // the SAME name are deduped above rather than refused: one id answering to one name is
    // not ambiguous however many entries carry it.
    return {
      error:
        `agent ${id} is a headless worker registered under ${registered.length} names ` +
        `(${registered.join(', ')}), so a bare name would reach the wrong conversation — pick the ` +
        `worker from list_agents, or answer its parked prompts with answer_permission.`,
    }
  }
  return {
    error:
      `agent ${id} is a headless worker (status "${agent.status}") with no session-registry entry — ` +
      `the session exited, its first turn has not reported an id yet, or it runs in-process under ` +
      `the SDK, which registers no name — so no SendMessage can address it. Check it with ` +
      `agent_status, or answer its parked prompts with answer_permission.`,
  }
}
