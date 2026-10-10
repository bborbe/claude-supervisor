// The start-rate limit — how fast sessions may be OPENED, as distinct from how many may be
// live at once.
//
// ⚠️ THIS IS NOT A SECOND CONCURRENCY CAP, and reading it as one is the mistake this module
// exists to prevent. `resolveMaxConcurrent` bounds simultaneous work; this bounds arrivals.
// Measured 2026-10-10: both concurrency caps were in force and did not prevent the incident,
// because 22 sessions were started in 2.8 minutes — every one of them below the cap, since
// the fleet was never full. The shared model backend went down under the arrival rate, not
// the population, and a concurrency cap structurally cannot see that.
//
// ⚠️ DELAY IS THE NORMAL PATH AND REFUSE IS THE CEILING. The callers are automated bulk
// paths — the `--flagged` batch, a post-stop recovery, a manager's To-open set — and
// refusing a 22-row batch outright fails half of it and forces a manual re-run, while pacing
// the same starts spreads the first-turn context loads that took the backend down. The
// ceiling exists because delay alone sheds no load: a caller that is only ever delayed keeps
// its own queue full and arrives at the same instant it always would have.
//
// ⚠️ ONE IMPLEMENTATION, TWO CALL SITES. The server enforces this, and the wezterm `--resume`
// branch of `commands/open.md` § 3.1 — which bypasses the server entirely and writes no
// spawn-ledger record — reaches the same pacing through `scripts/start-rate.mjs`. A second
// implementation for that path would be a second counter a reader cannot tell from the real
// one, which is the defect `docs/fleet-surface.md` names for restated spawn rules.
//
// Nothing in the PURE half below reads the filesystem, the environment, or the clock.

import {
  mkdirSync as defaultMkdirSync,
  readFileSync as defaultReadFileSync,
  renameSync as defaultRenameSync,
  writeFileSync as defaultWriteFileSync,
} from 'node:fs'
import { dirname } from 'node:path'

export const MAX_STARTS_ENV = 'SUPERVISOR_MAX_STARTS_PER_MINUTE'

// ⚠️ 4, CHOSEN AGAINST THE MEASURED INCIDENT RATHER THAN ROUNDED. The 2026-10-10 burst was
// 22 starts in 2.8 minutes — about 8/min, read from the spawn ledger's `spawned_at` values.
// A default at or above that number would not have prevented the incident it is named for,
// which is the one property a default here must have. 4/min halves the observed peak and
// spreads a 22-start batch over roughly 5.5 minutes.
export const DEFAULT_MAX_STARTS_PER_MINUTE = 4

// How long a start may be made to wait before it is refused instead — the ceiling that keeps
// "delay" from meaning "wait forever".
//
// ⚠️ A CONSTANT, NOT A CONFIG KEY, AND DELIBERATELY SO. A second key here would be a second
// number that can disagree with the first: raise the rate far enough and the ceiling silently
// becomes the binding constraint, or the reverse, with no error on either. The rate is the
// operator's number; the ceiling is a property of what a caller can tolerate. Five minutes is
// where a batch has stopped being paced and started being parked, and at the default rate it
// admits 20 queued starts — so a 22-row batch completes with its last rows refused rather
// than hanging.
export const MAX_START_DELAY_MS = 5 * 60 * 1000

// The start rate this server runs under, plus the source that decided it — or `null` for
// unlimited, which only an explicit `0` produces.
//
// Validation follows `resolveSpawnMode` and `resolveMaxConcurrent` exactly: every source is
// checked, not only the winner, so a typo in config.json cannot stay invisible merely because
// this particular spawn was paced by the environment instead. `0` is the off switch, matching
// `spawn.maxConcurrent`'s documented meaning for the same value — an operator reaching for
// "turn this off" must not meet a syntax error.
export function resolveMaxStarts({ env, file, path = 'the supervisor config' } = {}) {
  const sources = []
  if (env !== undefined && env !== null && env !== '') sources.push({ source: 'env', value: env })
  const fileValue = file?.spawn?.maxStartsPerMinute
  if (fileValue !== undefined && fileValue !== null && fileValue !== '') {
    sources.push({ source: 'config', value: fileValue })
  }

  for (const { source, value } of sources) {
    const where = source === 'env' ? MAX_STARTS_ENV : `"spawn.maxStartsPerMinute" in ${path}`
    // Only a number or a numeric string is a candidate. `Number(true)` is 1 and
    // `Number(null)` is 0, so coercing blindly would read a stray boolean in config.json as
    // a rate of one start per minute — the silent acceptance this module refuses.
    const parsed = typeof value === 'number' || typeof value === 'string' ? Number(value) : NaN
    if (!Number.isInteger(parsed) || parsed < 0) {
      return {
        error:
          `${where} is ${JSON.stringify(value)}, which is not a start rate — refusing to spawn rather than ` +
          `guessing, since a rate that silently governs nothing is discovered only by the burst it was meant ` +
          `to bound. Valid values: a non-negative integer (0 for no rate limit), or omit the key for the ` +
          `default of ${DEFAULT_MAX_STARTS_PER_MINUTE}.`,
      }
    }
  }

  const [first] = sources
  if (!first) {
    return {
      limit: DEFAULT_MAX_STARTS_PER_MINUTE,
      intervalMs: 60_000 / DEFAULT_MAX_STARTS_PER_MINUTE,
      source: 'default',
    }
  }
  const parsed = Number(first.value)
  if (parsed === 0) return { limit: null, intervalMs: 0, source: first.source }
  return { limit: parsed, intervalMs: 60_000 / parsed, source: first.source }
}

// Whether a start may proceed now, must wait, or is refused — the whole pacing decision.
//
// ⚠️ PURE, AND SPLIT OUT OF `supervisor.mjs` FOR THAT REASON, exactly as
// `concurrentLimitRefusal` is: the server module starts an MCP server on import and so cannot
// be unit-tested, which leaves the decision — the part that must not ship uncovered — as the
// part no test can reach.
//
// ⚠️ PACING, NOT COUNTING. Each start RESERVES a slot at `max(now, last + interval)`, so the
// spacing is a property of the state file rather than of how many callers happen to arrive
// together. A plain "count starts in the last minute" rule would let a batch arriving against
// an empty window all pass at once and then all wait at once — the thundering herd the limit
// exists to prevent, arriving on the far side of the check.
export function startRateDecision({
  rate,
  lastReservedAt,
  now,
  maxDelayMs = MAX_START_DELAY_MS,
  configFile,
} = {}) {
  if (rate.error) return { error: rate.error }
  // `null` is the off switch.
  if (rate.limit === null) return { action: 'allow', reservedAt: new Date(now).toISOString(), waitMs: 0 }

  const last = lastReservedAt ? Date.parse(lastReservedAt) : null
  // An unreadable or absent stamp is NOT a reason to refuse: it is the state of a fleet that
  // has never been paced, and treating it as "unknown, therefore block" would make the first
  // start after every state-file loss fail. The slot is simply free.
  const usableLast = last === null || Number.isNaN(last) ? null : last
  const earliest = usableLast === null ? now : Math.max(now, usableLast + rate.intervalMs)
  const waitMs = Math.max(0, earliest - now)

  if (waitMs > maxDelayMs) {
    return {
      error:
        `the session start rate is ${rate.limit}/min (source: ${rate.source}) and this start is ` +
        `${Math.round(waitMs / 1000)}s behind the queue — past the ${Math.round(maxDelayMs / 1000)}s ceiling, so ` +
        `it is refused rather than parked. Wait for the queue to drain and retry, or raise ` +
        `spawn.maxStartsPerMinute in ${configFile ?? 'the supervisor config'} (0 disables the rate limit).`,
    }
  }

  const reservedAt = new Date(earliest).toISOString()
  if (waitMs === 0) return { action: 'allow', reservedAt, waitMs: 0 }
  return {
    action: 'delay',
    reservedAt,
    waitMs,
    message:
      `the session start rate is ${rate.limit}/min (source: ${rate.source}), so this start is delayed ` +
      `${Math.round(waitMs / 1000)}s to ${reservedAt}. It is paced, not dropped.`,
  }
}

// ── the impure half ──────────────────────────────────────────────────────────────────────
//
// ⚠️ THE READ-MODIFY-WRITE IS NOT ATOMIC, AND THAT IS NAMED RATHER THAN HIDDEN. Two callers
// that read the same `last_reserved_at` and both write back will each compute the same slot,
// so a simultaneous pair can slip through where the rate allows one. The window is the width
// of one read+write on a local file (sub-millisecond), and the callers this bounds are bulk
// batches spaced by whole seconds, so the exposure is far below the burst it exists to bound.
// Closing it properly needs an exclusive lock; that is deliberately not built here, because a
// lock held by a crashed caller would refuse every subsequent start — a worse failure than
// the race it removes.

export const RATE_STATE_FILE = 'start-rate.json'

// The last reserved start, or `null` when nothing has been reserved yet.
//
// An unreadable or malformed file reads as `null` rather than refusing: the alternative is a
// fleet that cannot start anything because one JSON file was truncated by a full disk, and
// the failure this bounds is a burst, not a lost slot.
export function readReservedAt(file, { fs } = {}) {
  const read = fs?.readFileSync ?? defaultReadFileSync
  try {
    const parsed = JSON.parse(read(file, 'utf8'))
    return typeof parsed?.last_reserved_at === 'string' ? parsed.last_reserved_at : null
  } catch {
    return null
  }
}

// Record a reservation. Written to a temp file and renamed, so a reader never sees a partial
// file — the same discipline `ledger.mjs` uses for the same reason.
export function writeReservedAt(file, reservedAt, { fs } = {}) {
  const mkdir = fs?.mkdirSync ?? defaultMkdirSync
  const write = fs?.writeFileSync ?? defaultWriteFileSync
  const rename = fs?.renameSync ?? defaultRenameSync
  mkdir(dirname(file), { recursive: true })
  const tmp = `${file}.tmp`
  write(tmp, `${JSON.stringify({ last_reserved_at: reservedAt }, null, 2)}\n`)
  rename(tmp, file)
  return reservedAt
}
