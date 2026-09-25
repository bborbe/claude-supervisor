// resume-guard.mjs — which in-process worker, if any, blocks a resume of a session.
//
// A headless worker this server spawned is an in-process SDK `query()`: no pid for the
// session registry, no argv for a process probe. So `checkLiveness` cannot see it and
// answers `live: false`, and the agent table here is the sole evidence it is alive.
// That is why this guard reads the table rather than trusting the registry's answer —
// a `false` from the registry would otherwise be read as "closed" and a live worker
// resumed, putting two writers on one conversation.
//
// The distinction that matters is finished vs not, not running vs not. A `query()`
// sets one of TERMINAL_STATUSES when it ends (see agent-loop.mjs); every other status
// is a live holder. `blocked-on-permission` is the one that bit: a parked worker is
// blocked *inside* a tool call — mid-turn, by any reading — but it does not carry
// `running`, so a guard matching only `running` let it through, it fell through to the
// registry probe, read as closed, and was resumable while still parked.

export const TERMINAL_STATUSES = new Set(['done', 'error'])

export const isFinished = (status) => TERMINAL_STATUSES.has(status)

// The agent record holding `sessionId` that has not finished, or null.
//
// `agentRecords` is any iterable of records carrying `sessionId` and `status` — the
// caller passes `agents.values()`.
export function findLiveHolder(agentRecords, sessionId) {
  return [...agentRecords].find((a) => a.sessionId === sessionId && !isFinished(a.status)) ?? null
}
