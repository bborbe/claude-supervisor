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
// ceiling. The rule below is that comparison and nothing else.
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
  let ceiling = null
  let droppedTokens = 0
  let restored = null
  let model = null

  for (const record of records ?? []) {
    const meta = record?.compactMetadata
    const pre = Number(meta?.preTokens)
    if (Number.isFinite(pre)) {
      compactCount += 1
      ceiling = ceiling === null ? pre : Math.max(ceiling, pre)
      const dropped = Number(meta.cumulativeDroppedTokens)
      if (Number.isFinite(dropped)) droppedTokens = Math.max(droppedTokens, dropped)
    }
    const tokens = promptTokens(record)
    if (tokens > 0) {
      restored = tokens
      const named = record?.message?.model
      if (typeof named === 'string' && named !== '<synthetic>') model = named
    }
  }

  if (compactCount === 0 && restored === null) return null
  return { compactCount, ceiling, restored, droppedTokens, model }
}

// The stats for one transcript file, or `null` when there is no file or nothing to read.
//
// ⚠️ READS THE WHOLE FILE, unlike the bounded tail `tab-read.mjs` uses for `agent_status`, and
// the difference is not an oversight. The ceiling is the MAXIMUM `preTokens` over every
// boundary in the session, so a tail window can miss the largest one and under-report the
// ceiling — which fails toward "resume it", the direction this guard exists to prevent. A
// resume happens once per orphaned worker, so the cost of the full read is paid rarely, and
// correctness on this path is worth more than the bytes.
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
export function resumeContextRefusal({ stats, reserveTokens = DEFAULT_RESERVE_TOKENS } = {}) {
  if (!stats) return null
  if (stats.ceiling === null || stats.restored === null) return null

  const room = stats.ceiling - stats.restored
  if (room >= reserveTokens) return null

  const num = (n) => n.toLocaleString('en-US')
  return (
    `session's restored context leaves ${num(room)} tokens of room, below the ${num(reserveTokens)} one ` +
    `working set needs — refusing to resume it, because it would compact on its first turn or die with ` +
    `"Prompt is too long" rather than do any work. Measured from the transcript: the last turn ran at ` +
    `${num(stats.restored)} tokens and this session's harness compacted at ${num(stats.ceiling)} ` +
    `(${stats.compactCount} compaction${stats.compactCount === 1 ? '' : 's'}, ${num(stats.droppedTokens)} ` +
    `tokens dropped in total). Open a FRESH worker on the same task with a handover prompt instead of ` +
    `resuming — the task keeps its state, the conversation does not need to.`
  )
}
