// decision-settle.mjs — answering the gate a RESUMED worker raises again.
//
// What this closes: a worker parked on a permission, its spawner died, and it was resumed
// carrying a decision. The motivating run (`spawner-exit-resume`) did not demonstrate this
// — it was resumed with a bare `Continue.` and carried no decision, so its re-raise is
// just what a worker does when resumed with no information. What stopped the re-ask in the
// run that DID carry a decision is `renderResumePrompt`'s closing line: the worker read it
// and declined to re-run the call. These functions are the second line for a worker that
// ignores that note — correct, narrow and unit-tested, but no observed live run has made
// them fire.
//
// Note what this module is NOT: it does not settle the *original* promise. That promise
// died with the spawner's process, and for a spawner-gone worker there is nothing left to
// settle. The gate worth answering is the one the resumed worker raises in the server
// that now hosts it, and that is what these two functions decide.

// What a resume carries forward: enough of the park to recognise its re-raise, plus the
// decision itself. Built once at resume time from the already-validated decision and the
// park record, so nothing here re-derives or re-validates.
export function buildCarriedDecision({ decision, parkRecord }) {
  if (!decision || !parkRecord) return null
  return {
    item_id: decision.item_id,
    behavior: decision.behavior,
    message: decision.message ?? null,
    // What to recognise the re-raise by. `tool` is required — a park that cannot say what
    // it was asking about cannot be matched, and an unmatched decision must park rather
    // than fire at whatever comes next.
    tool: parkRecord.tool,
    blocked_path: parkRecord.blocked_path ?? null,
  }
}

// Does this carried decision answer the gate now being raised?
//
// Deliberately narrow. A decision answers ONE park, and the failure mode of a loose match
// is the replay failure the resume validation already refuses one level up: applying a
// decision to a gate it never answered. So a different tool never matches, and when the
// park named a blocked path, a different path does not either.
export function settleFromCarried(carried, { toolName, blockedPath = null } = {}) {
  if (!carried) return null
  if (!toolName || carried.tool !== toolName) return null
  if (carried.blocked_path && blockedPath && carried.blocked_path !== blockedPath) return null
  return { behavior: carried.behavior, message: carried.message, item_id: carried.item_id }
}

// A carried `allow` must not become a way around the guards a live gate still applies.
//
// A carried `deny` is always safe to apply — refusing an action can only ever withhold
// something, and a worker that stops is the outcome the operator asked for. An `allow` is
// not, so it is only honoured when the caller says the server would have allowed that
// tool anyway. Absent that, the gate parks for a human exactly as it would have, which is
// the conservative direction and the one that cannot launder.
export function mayApplyAllow(carried, { serverWouldAllow = false } = {}) {
  if (!carried) return false
  if (carried.behavior === 'deny') return true
  return serverWouldAllow === true
}
