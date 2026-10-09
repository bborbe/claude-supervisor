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

  return {
    error:
      `agent ${id} is a headless worker (status "${agent.status}"), which has neither a pane nor a ` +
      `cluster session, so no follow-up can reach it. Answer its parked prompts with ` +
      `answer_permission, or reach it mid-task with SendMessage.`,
  }
}
