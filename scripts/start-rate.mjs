#!/usr/bin/env node
// The session start rate, for the caller the server cannot see.
//
// ⚠️ THE WEZTERM `--resume` BRANCH OF `commands/open.md` § 3.1 BYPASSES THE SERVER ENTIRELY
// and writes no spawn-ledger record, so the enforcement in `server/supervisor.mjs` does not
// reach it. That path is the one the recovery runbook uses, which is why SC3 exists. This
// script is how it reaches the SAME pacing.
//
// ⚠️ IT CALLS THE SAME `reserveSlot` RATHER THAN RESTATING THE RULE. A second implementation
// here would be a second counter a reader cannot tell from the real one — the defect
// `docs/fleet-surface.md` names for any restated spawn rule, and the reason the server and
// this script share one state file rather than each keeping their own. The write-before-wait
// ordering in particular lives in that one function; restating it here is how the two copies
// drift.
//
// Usage, before each `wezterm cli spawn` in the § 3.1 loop:
//
//   node <plugin>/scripts/start-rate.mjs || exit 1
//
// ⚠️ ALLOW IT AT LEAST 300s OF WALL CLOCK — MAX_START_DELAY_MS, the ceiling it may sleep to.
// It reserves a slot and sleeps until it, and THE RESERVATION IS WRITTEN BEFORE THE SLEEP, so
// a caller that kills this process mid-sleep has already spent the slot without spawning
// anything — and `|| exit 1` then aborts the row. Claude Code's Bash tool defaults to 120s,
// BELOW this ceiling, so a paced batch run under the default is killed in exactly the bulk
// scenario the rate exists to serve. Pass an explicit timeout of ≥300s (the tool's maximum is
// 600s), or the batch cannot finish pacing. `commands/open.md` § 3.1 carries the same
// requirement at the call site.
//
// Exit 0 means the slot is yours; non-zero means the rate refused it, and the reason is on
// stderr. It never exits 0 without having reserved.

import { join } from 'node:path'
import { config } from '../server/config.mjs'
import { RATE_STATE_FILE, reserveSlot, resolveMaxStarts } from '../server/start-rate.mjs'

const result = await reserveSlot({
  rate: resolveMaxStarts({
    env: config.maxStartsPerMinute,
    file: config.configFileContents,
    path: config.configFile,
  }),
  stateFile: join(config.stateDir, RATE_STATE_FILE),
  configFile: config.configFile,
  log: (message) => process.stderr.write(`${message}\n`),
})

if (result.error) {
  process.stderr.write(`${result.error}\n`)
  process.exit(1)
}

process.stdout.write(`${result.reservedAt}\n`)
