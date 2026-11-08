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

function failClosed(reason) {
  return { tier: 'operator', form: 'unverifiable', reason }
}

// `context.runnerConfigClean` is passed through for the confined `make test` / `bun test`
// entries; the classifier never computes it, for the same reason `manager-allowlist.mjs` does not.
export function permissionTier(toolName, input, { script = PERMISSION_TIER_SCRIPT, runnerConfigClean, timeoutMs = 5000 } = {}) {
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
