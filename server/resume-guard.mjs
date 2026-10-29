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
//
// ⚠️ `interactive` is the status that NEVER settles, and it is why `resumeRefusal`
// exists rather than `findLiveHolder` alone. A TAB worker's row is set to
// `status: 'interactive'` at spawn (supervisor.mjs) and no agent-loop ever runs for it,
// so nothing moves it to `done`/`error` — not when the tab exits, not ever. Read as a
// plain live holder, that row blocks a resume of its own session for the spawning
// server's WHOLE LIFE. Measured 2026-10-24: two sessions unresumable with no pid, no
// registry entry and a 42-minute-stale transcript, and every verb that could clear the
// flag refusing *because of* it. A tab worker is a real `claude` process and registers
// in the session registry for as long as it lives, so the registry answering "gone" is
// evidence this map cannot supply on its own.

export const TERMINAL_STATUSES = new Set(['done', 'error'])

export const isFinished = (status) => TERMINAL_STATUSES.has(status)

// The agent record holding `sessionId` that has not finished, or null.
//
// `agentRecords` is any iterable of records carrying `sessionId` and `status` — the
// caller passes `agents.values()`.
export function findLiveHolder(agentRecords, sessionId) {
  return [...agentRecords].find((a) => a.sessionId === sessionId && !isFinished(a.status)) ?? null
}

// The refusal a resume of `sessionId` should carry, or `null` when nothing holds it.
//
// `live` is `checkLiveness`'s own answer for that session — the caller has already
// computed it, and it is what separates the two cases this map cannot tell apart:
//
//   - A HEADLESS row is an in-process `query()` this server owns. The registry cannot
//     always see it (that is why this guard exists at all), so `live: false` is NOT
//     evidence the worker is gone. The row keeps holding.
//   - A TAB row is a real `claude` process in its own pane, and it is in the registry
//     for as long as it lives. `live: false` therefore means the tab is gone — and
//     since nothing ever settles an `interactive` row, releasing it is the only way
//     its session ever becomes resumable again.
//
// `live: null` means neither probe could be read. That is not evidence of death, so it
// never releases a row here; the caller refuses on it separately, fail-closed.
export function resumeRefusal({ agentRecords, sessionId, live }) {
  const holder = findLiveHolder(agentRecords, sessionId)
  if (!holder) return null
  // A rehydrated row's worker lives in the supervisor process that SPAWNED it, not in
  // this one (`registry-rehydrate.mjs` — adoption crosses processes), so naming "this
  // server" would send the operator to close the wrong thing. It is never released
  // here either: this process cannot see that worker at all.
  if (holder.rehydrated) {
    return `session ${sessionId} is still running (worker ${holder.id} is mid-turn in the supervisor process that spawned it) — close it before resuming, or you will have two writers on one conversation`
  }
  // The one release. `live === false` only — never `null`, never a headless row.
  if (holder.status === 'interactive' && live === false) return null
  return `session ${sessionId} is still running (worker ${holder.id} is mid-turn in this server) — close it before resuming, or you will have two writers on one conversation`
}
