// The permission tier of a parked call — `allowlist`, `manager` or `operator` — as decided by
// `scripts/permission-tier.py`, the ONE classifier.
//
// ⚠️ THIS IS A WRAPPER, NOT A PORT, ON PURPOSE. Two classifiers for one security boundary drift:
// a case fixed in one and not the other is a gate that holds in the watcher and opens in the
// server (or the reverse), and nothing would notice until a prod deploy was routed to a manager.
// So the server asks the same module the attention watcher loads, and both run the same fixture
// (`permission-tier.fixtures.json`) in their own test suites.
//
// ⚠️ EVERY FAILURE IS `operator`. No python, a timeout, a non-zero exit, unparseable output, a
// tier name outside the three — each returns `{tier: 'operator', form: 'unverifiable'}`. A tier
// that failed open would hand a manager a prod deploy because a subprocess hiccuped.

import { spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const PERMISSION_TIER_SCRIPT = join(dirname(fileURLToPath(import.meta.url)), '..', 'scripts', 'permission-tier.py')

const TIERS = new Set(['allowlist', 'manager', 'operator'])

// ⚠️ THE BUDGET IS A MEASURED MULTIPLE, NOT A FIXED FIVE SECONDS. A fixed 5 s ceiling was crossed
// by a single spawn on a loaded Mac (measured 2026-11-09: 1.78 s per call, 40+ calls in one fixture
// run), and the wrapper then fails closed to `operator` — correct, but the suite reports it as a
// routing difference and a reader cannot tell a timeout from a decision. Calibrating against one
// real spawn keeps the ceiling proportional to the machine, instead of a number chosen on an idle
// box that a busy one cannot meet.
const BUDGET_MULTIPLE = 20
const BUDGET_FLOOR_MS = 10_000
const BUDGET_CAP_MS = 120_000
const BASELINE_INPUT = '{"command":"true"}'

let measuredSpawnMs = null

function measureSpawnMs() {
  const started = Date.now()
  try {
    // Always the real classifier, never a caller's `script` override: a deliberately slow script
    // must not buy itself a larger budget, or the timeout test could never fire.
    spawnSync('python3', ['-I', PERMISSION_TIER_SCRIPT, '--tool', 'Bash', '--input', '-'], {
      encoding: 'utf8', input: BASELINE_INPUT, timeout: BUDGET_FLOOR_MS,
    })
  } catch {
    // A baseline that cannot start is itself the classifier's failure path; the floor below
    // keeps the budget sane rather than letting the measurement collapse to zero.
  }
  return Math.max(1, Date.now() - started)
}

// The default budget: `BUDGET_MULTIPLE` × one measured spawn, floored and capped. `measuredMs` is
// injectable so the formula is pinned by a test instead of only observed under load.
export function permissionTierTimeoutMs({ measuredMs = null } = {}) {
  const measured = measuredMs ?? (measuredSpawnMs ??= measureSpawnMs())
  return Math.min(BUDGET_CAP_MS, Math.max(BUDGET_FLOOR_MS, Math.round(measured * BUDGET_MULTIPLE)))
}

function failClosed(reason) {
  return { tier: 'operator', form: 'unverifiable', reason }
}

// `context.runnerConfigClean` is passed through for the confined `make test` / `bun test`
// entries; the classifier never computes it, for the same reason `manager-allowlist.mjs` does not.
export function permissionTier(toolName, input, { script = PERMISSION_TIER_SCRIPT, runnerConfigClean, timeoutMs = permissionTierTimeoutMs() } = {}) {
  if (typeof toolName !== 'string' || toolName === '') return failClosed('no tool name')
  const arg = toolName === 'Bash'
    ? (input && typeof input.command === 'string' ? input.command : '')
    : JSON.stringify(input ?? {})
  // ⚠️ The input travels on STDIN, not argv. A Write/Edit input can exceed the per-argument
  // limit (~128 KiB on Linux); as an argv it fails E2BIG and the call is mislabelled `operator`,
  // which is the routine-write half of what this tier exists to route to a manager.
  const args = ['-I', script, '--tool', toolName, '--input', '-']
  if (runnerConfigClean === true) args.push('--runner-config-clean')
  let out
  try {
    out = spawnSync('python3', args, { encoding: 'utf8', timeout: timeoutMs, input: arg })
  } catch (err) {
    return failClosed(`classifier did not start: ${err.message}`)
  }
  if (out.error) return failClosed(`classifier did not run: ${out.error.message}`)
  if (out.status !== 0 || typeof out.stdout !== 'string') {
    return failClosed(`classifier exited ${out.status}`)
  }
  let parsed
  try {
    parsed = JSON.parse(out.stdout)
  } catch {
    return failClosed('classifier printed no JSON')
  }
  if (!parsed || !TIERS.has(parsed.tier)) return failClosed(`classifier returned tier ${parsed?.tier}`)
  return { tier: parsed.tier, form: parsed.form ?? null, reason: parsed.reason ?? null }
}
