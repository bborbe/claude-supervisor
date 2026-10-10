#!/usr/bin/env node
// The session start rate, for the caller the server cannot see.
//
// ⚠️ THE WEZTERM `--resume` BRANCH OF `commands/open.md` § 3.1 BYPASSES THE SERVER ENTIRELY
// and writes no spawn-ledger record, so the enforcement in `server/supervisor.mjs` does not
// reach it. That path is the one the recovery runbook uses, which is why SC3 exists. This
// script is how it reaches the SAME pacing.
//
// ⚠️ IT IMPORTS THE SAME MODULE RATHER THAN RESTATING THE RULE. A second implementation here
// would be a second counter a reader cannot tell from the real one — the defect
// `docs/fleet-surface.md` names for any restated spawn rule, and the reason the server and
// this script share one state file rather than each keeping their own.
//
// Usage, before each `wezterm cli spawn` in the § 3.1 loop:
//
//   node <plugin>/scripts/start-rate.mjs || exit 1
//
// It RESERVES a slot and, when the slot is in the future, sleeps until it — so the caller
// needs no timing logic of its own. Exit 0 means the slot is yours; non-zero means the rate
// refused it, and the reason is on stderr. It never exits 0 without having reserved.

import { join } from 'node:path'
import { config } from '../server/config.mjs'
import {
  RATE_STATE_FILE,
  readReservedAt,
  resolveMaxStarts,
  startRateDecision,
  writeReservedAt,
} from '../server/start-rate.mjs'

const rate = resolveMaxStarts({
  env: config.maxStartsPerMinute,
  file: config.configFileContents,
  path: config.configFile,
})

const stateFile = join(config.stateDir, RATE_STATE_FILE)
const decision = startRateDecision({
  rate,
  lastReservedAt: readReservedAt(stateFile),
  now: Date.now(),
  configFile: config.configFile,
})

if (decision.error) {
  process.stderr.write(`${decision.error}\n`)
  process.exit(1)
}

// Reserved BEFORE the sleep, for the reason `reserveStart` gives: a batch that read the same
// stale stamp would otherwise all sleep to the same instant and arrive together.
writeReservedAt(stateFile, decision.reservedAt)

if (decision.action === 'delay') {
  process.stderr.write(`${decision.message}\n`)
  await new Promise((resolve) => setTimeout(resolve, decision.waitMs))
}

process.stdout.write(`${decision.reservedAt}\n`)
