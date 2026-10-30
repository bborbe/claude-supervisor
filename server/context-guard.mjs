// context-guard.mjs — whether resuming a session would leave it too little room to work.
//
// A resume is the manager's only recovery lever for an orphaned worker, and it fails hardest
// on exactly the sessions that need it. `spawn_agent(resume=<id>)` restores the whole prior
// conversation, so a long-lived worker comes back carrying the payload its last turn already
// ran at — and if that payload is close to the point where the harness compacts, the resumed
// session either dies on its first turn or spends every turn compacting.
//
// Measured 2026-10-27, five workers in one goal manager's tracked set:
//
//   | session    | turns survived | failure                  |
//   |------------|----------------|--------------------------|
//   | `fc025fc5` | —              | Autocompact is thrashing |
//   | `ab98608f` | —              | Autocompact is thrashing |
//   | `42e1db23` | 8              | Autocompact is thrashing |
//   | `21c0b4ff` | 1              | Prompt is too long       |
//
// ⚠️ THE MEASUREMENT IS THE TRANSCRIPT'S OWN, and that choice is the whole design. The first
// cut read `~/.claude/state/context/<sid>.json` — but that file is written by
// `~/.claude/statusline-command.sh`, which runs only for a session that DRAWS a statusline, so
// every headless worker was invisible to it: all four sessions above have transcripts and no
// state file. A guard whose instrument cannot see the population it exists for is not a guard.
// The transcript is written by every session, headless or not.
//
// Two numbers are read from it, and neither needs a window constant:
//
//   * `compactMetadata.preTokens` on a `system`/`compact_boundary` record is the size at which
//     the harness ITSELF decided to compact. That is the ceiling this session actually ran
//     against — measured per session, so the `[1m]` / non-`[1m]` window difference that makes a
//     fixed percentage meaningless simply shows up as a different number.
//   * the last assistant `usage` block's `input_tokens` + `cache_read_input_tokens` +
//     `cache_creation_input_tokens` is the payload the last turn ran with, which is what a
//     resume restores.
//
// Measured across the four sessions on 2026-10-29: ceilings 180 k–262 k, restored payloads
// 158 k–399 k, and `21c0b4ff` — the one that died on its first turn — sitting 22 k below its own
// ceiling — but that comparison is necessary and not sufficient (see `DEFAULT_THRASH_COMPACTIONS`).
//
// ⚠️ **PROXIMITY TO A COMPACTION IS NOT ITSELF A FAILURE.** `preTokens` is where the harness
// compacted, not where it ran out of window; a session 10 k from that point compacts and carries
// on, which is the harness working as designed. Measured 2026-10-30 over 3 653 tracked sessions,
// a rule that refused on proximity alone fired on **258** of them, nearly all healthy. Gating the
// refusal on the thrash count — the five recorded failures all compacted 3+ times — took that to
// **41** while still refusing every recorded failure. That is the difference between a guard a
// manager trusts and one it learns to route around.
//
// ⚠️ **THE CEILING IS MEASURED PER MODEL (2026-10-30).** The first cut took one global maximum
// over every boundary in the session, and that mixed windows whenever the router switched models
// mid-conversation: `7231d34d` compacted at 274 k under `deepseek-v4-flash-max`, then ran its
// last 315 turns on `claude-opus-5-5` at 690 k, so a 274 k ceiling called it −416 k of room and
// refused a healthy resume. Over-refusing is not harmless here — it hits exactly the long-lived
// sessions this guard exists for, which are the ones whose resume matters most. So a boundary is
// attributed to the model in force when it fired, and only boundaries under the session's LAST
// model can bind. A session that never compacted under the model it ended on has no measured
// ceiling for that window, and is allowed rather than judged by another model's number.
//
// The DECISION reads no filesystem, environment or clock: `contextStatsFrom` takes parsed
// records, so it is unit-testable without a server or a transcript on disk. `readContextStats`
// is the thin reader beside it, with its file access injected for the same reason.

// One working set, in tokens: the room a resumed session needs before its first turn can do
// anything. Taken from the four failing transcripts' own post-compaction working sets — the
// `compactMetadata.postTokens` of all 72 boundaries in them — whose p50 is 31 220, p90 is
// 39 065 and maximum is 57 877. The p90 rather than the maximum: a reserve set at the worst
// case ever observed refuses resumes that would have been fine, and the session that matters
// most (`21c0b4ff`, dead on turn 1) sits 22 409 below its ceiling, so the p90 already catches
// it with room to spare.
export const DEFAULT_RESERVE_TOKENS = 40_000

// The payload size a single assistant record reports, or 0 when it carries no usage.
//
// The three fields are disjoint parts of one prompt — uncached input, the cached prefix, and
// newly cached content — so the sum is the prompt size. Summing only `input_tokens` reads a
// 158 k-token turn as a few hundred and would make every session look empty.
function promptTokens(record) {
  const usage = record?.message?.usage
  if (!usage || typeof usage !== 'object') return 0
  const input = Number(usage.input_tokens) || 0
  const cached = Number(usage.cache_read_input_tokens) || 0
  const created = Number(usage.cache_creation_input_tokens) || 0
  const total = input + cached + created
  return Number.isFinite(total) ? total : 0
}

// The two numbers the guard compares, read from parsed transcript records.
//
// Returns `null` when the records carry neither a compaction boundary nor any usage — a
// transcript that exists but says nothing about context, which is not the same as a small one.
// `ceiling` and `restored` are independently nullable: a session can carry usage and no
// boundary (never compacted) or, in principle, the reverse.
export function contextStatsFrom(records) {
  let compactCount = 0
  let droppedTokens = 0
  let restored = null
  let model = null
  // ⚠️ THE CEILING IS KEPT PER MODEL, and that is the whole point of this shape. A session can
  // run under more than one model — the router switches them mid-conversation — and the
  // auto-compact trigger fires at a fraction of whichever window is in force. Taking one global
  // maximum mixes the two: measured 2026-10-29, `7231d34d` compacted at 274 k under
  // `deepseek-v4-flash-max` and then ran its last 315 turns on `claude-opus-5-5` at 690 k. A
  // single ceiling of 274 k called that a −416 k "room" and refused a perfectly healthy resume.
  // The ceiling that binds a resume is the one recorded under the model the resume will run in.
  const byModel = new Map()

  for (const record of records ?? []) {
    // The model is read BEFORE the boundary on the same record so a boundary is attributed to
    // the turns that produced it. `<synthetic>` records carry no model and never overwrite one.
    const named = record?.message?.model
    if (typeof named === 'string' && named !== '<synthetic>') model = named

    const meta = record?.compactMetadata
    const pre = Number(meta?.preTokens)
    if (Number.isFinite(pre)) {
      compactCount += 1
      const key = model ?? null
      const entry = byModel.get(key) ?? { ceiling: 0, count: 0 }
      entry.ceiling = Math.max(entry.ceiling, pre)
      entry.count += 1
      byModel.set(key, entry)
      const dropped = Number(meta.cumulativeDroppedTokens)
      if (Number.isFinite(dropped)) droppedTokens = Math.max(droppedTokens, dropped)
    }
    const tokens = promptTokens(record)
    if (tokens > 0) restored = tokens
  }

  if (compactCount === 0 && restored === null) return null
  // `null` when this session never compacted under the model it last ran — "not measured for
  // this window". The decision does not simply allow that case; see `resumeContextRefusal`.
  const own = byModel.get(model ?? null) ?? null
  let globalCeiling = null
  for (const entry of byModel.values()) {
    globalCeiling = globalCeiling === null ? entry.ceiling : Math.max(globalCeiling, entry.ceiling)
  }
  return {
    compactCount,
    ceiling: own ? own.ceiling : null,
    ceilingCount: own ? own.count : 0,
    globalCeiling,
    restored,
    droppedTokens,
    model,
  }
}

// The stats for one transcript file, or `null` when there is no file or nothing to read.
//
// ⚠️ READS THE WHOLE FILE, unlike the bounded tail `tab-read.mjs` uses for `agent_status`, and
// the difference is not an oversight. The ceiling is the MAXIMUM `preTokens` over the boundaries
// recorded under the session's last model, and a tail window can miss the largest one — which
// fails toward "resume it", the direction this guard exists to prevent. It also has to see the
// assistant turns that name the model, and those are scattered through the file. A resume
// happens once per orphaned worker, so the cost of the full read is paid rarely, and correctness
// on this path is worth more than the bytes.
//
// A torn final line is normal — the file is appended to while a session runs — so an
// unparseable line is skipped rather than failing the read. A file that cannot be read at all
// is `null`, which the decision treats as "not measured" and allows.
export function readContextStats(path, { readFile } = {}) {
  if (!path || typeof readFile !== 'function') return null
  let text
  try {
    text = readFile(path, 'utf8')
  } catch {
    return null
  }
  const records = []
  for (const line of String(text).split('\n')) {
    if (!line.trim()) continue
    try {
      records.push(JSON.parse(line))
    } catch {
      // Torn tail or a non-JSON line; the guard's two numbers are elsewhere in the file.
    }
  }
  return contextStatsFrom(records)
}

// Whether resuming this session may proceed — a refusal string, or `null` to allow it.
//
// ⚠️ EVERY "cannot measure" ANSWER ALLOWS, and that asymmetry is deliberate rather than sloppy.
// The liveness guards in `supervisor.mjs` fail CLOSED because the alternative is two writers on
// one conversation — corruption. Here the alternative to allowing is refusing to resume a
// session that has no local transcript at all (a cluster worker) or one that has never
// compacted, and the cost of that is an orphaned worker with no recovery lever: exactly the
// harm this guard exists to prevent. So it refuses only on a measurement that positively shows
// too little room, and the unmeasured cases are reported by the caller rather than treated as
// safe.
// The compaction count at which a session that never compacted under its final model is still
// refused. ⚠️ **A FITTED THRESHOLD, and its weakness is stated rather than hidden.** The five
// recorded failures compacted 35, 24, 10, 3 and 3 times; the healthy session the per-model
// ceiling exists to rescue (`7231d34d`) compacted **twice**. So the only evidence separating
// them is a 2-versus-3 gap, which is thin. It is used only in the one case where no same-model
// ceiling exists — everywhere else the measured comparison decides, and this constant never
// runs. A session that switches model once and is genuinely near the new window's limit can
// still be refused by it; that false positive is preferred to letting a thrashing session
// through, because the second is the harm this guard exists to stop and the first costs a
// fresh spawn.
export const DEFAULT_THRASH_COMPACTIONS = 3

export function resumeContextRefusal({ stats, reserveTokens = DEFAULT_RESERVE_TOKENS } = {}) {
  if (!stats) return null
  if (stats.restored === null) return null

  // No boundary under the model the session ended on: the window it will resume into is not
  // measured. Fall back to the thrash signature rather than to another model's ceiling.
  if (stats.ceiling === null) {
    if (stats.compactCount < DEFAULT_THRASH_COMPACTIONS) return null
    if (stats.globalCeiling === null) return null
    const room = stats.globalCeiling - stats.restored
    return refusal(stats, room, reserveTokens, stats.globalCeiling, stats.compactCount, 'another model')
  }

  const room = stats.ceiling - stats.restored
  if (room >= reserveTokens) return null
  // ⚠️ PROXIMITY ALONE IS NOT A FAILURE. `preTokens` is where the harness COMPACTED, not where
  // it ran out of window — a session 10 k from that point simply compacts and carries on, which
  // is the harness working as designed. What kills a session is doing that repeatedly, so the
  // thrash count gates the refusal on both paths. Measured 2026-10-30: without this gate the
  // rule refused 258 of 3 653 tracked sessions, most of them healthy; the five recorded failures
  // all compacted 3+ times.
  if (stats.compactCount < DEFAULT_THRASH_COMPACTIONS) return null
  return refusal(stats, room, reserveTokens, stats.ceiling, stats.ceilingCount, stats.model ?? 'an unnamed model')
}

// One refusal string, so the two paths cannot drift in wording or in what they disclose.
//
// `under` names the model whose boundary produced `ceiling` — or `'another model'` on the
// thrash fallback, which is the reader's signal that this session never compacted under the
// model it ended on and is being judged by a count rather than by its own window.
function refusal(stats, room, reserveTokens, ceiling, count, under) {
  const num = (n) => n.toLocaleString('en-US')
  return (
    `session's restored context leaves ${num(room)} tokens of room, below the ${num(reserveTokens)} one ` +
    `working set needs — refusing to resume it, because it would compact on its first turn or die with ` +
    `"Prompt is too long" rather than do any work. Measured from the transcript: the last turn ran at ` +
    `${num(stats.restored)} tokens and this session's harness compacted at ${num(ceiling)} under ` +
    `${under} (${count} compaction${count === 1 ? '' : 's'} on that model, ${num(stats.droppedTokens)} ` +
    `tokens dropped in total). Open a FRESH worker on the same task with a handover prompt instead of ` +
    `resuming — the task keeps its state, the conversation does not need to.`
  )
}
