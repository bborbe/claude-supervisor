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

// EVERY record holding `sessionId` that has not finished. Usually one; more than one is
// possible, and the release below depends on seeing all of them (see its ⚠️).
//
// `agentRecords` is any iterable of records carrying `sessionId` and `status` — the
// caller passes `agents.values()`.
export function findLiveHolders(agentRecords, sessionId) {
  return [...agentRecords].filter((a) => a.sessionId === sessionId && !isFinished(a.status))
}

// The first such record, or null. Kept because callers that only ask "is anything
// holding this session" do not need the set.
export function findLiveHolder(agentRecords, sessionId) {
  return findLiveHolders(agentRecords, sessionId)[0] ?? null
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
  const holders = findLiveHolders(agentRecords, sessionId)
  if (holders.length === 0) return null

  const refusal = (holder, where) =>
    `session ${sessionId} is still running (worker ${holder.id} is mid-turn ${where}) — close it before resuming, or you will have two writers on one conversation`

  // A rehydrated row's worker lives in the supervisor process that SPAWNED it, not in
  // this one (`registry-rehydrate.mjs` — adoption crosses processes), so naming "this
  // server" would send the operator to close the wrong thing. It is never released
  // here either: this process cannot see that worker at all.
  const foreign = holders.find((h) => h.rehydrated)
  if (foreign) return refusal(foreign, 'in the supervisor process that spawned it')

  // The one release. `live === false` only — never `null` — and only when EVERY holder
  // for this session is a tab row.
  //
  // ⚠️ **EVERY holder, not the first one.** This is not defensive: releasing a dead tab
  // is what lets the session be resumed, and a resume mints a NEW row under a new
  // `agent_N` while the old `interactive` row is never deleted (`agents` is keyed by
  // `id`, and nothing removes the stale one). The Map therefore ends up holding
  // `[interactive (stale), running (live)]` for one sessionId, stale-first by insertion
  // order. Deciding on `holders[0]` alone would find the dead tab, release, and miss the
  // live writer the previous resume created — two writers on one conversation, the exact
  // corruption this module exists to prevent.
  //
  // ⚠️ Why this cannot release a LIVE tab: a tab row's `sessionId` is only ever set from
  // the registry — `spawnInteractiveAgent` polls until the session appears and the caller
  // assigns what it found — so a row carrying an `interactive` status has already proved
  // it was registry-visible. If the registry now cannot see it, the process is gone. The
  // guard was adding nothing to that case but permanence.
  if (live === false && holders.every((h) => h.status === 'interactive')) return null

  // Something is not a dead tab — name it, preferring a non-interactive holder so the
  // operator is pointed at the live writer rather than the stale row beside it.
  const blocker = holders.find((h) => h.status !== 'interactive') ?? holders[0]
  return refusal(blocker, 'in this server')
}
