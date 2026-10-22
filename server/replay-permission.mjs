// Replay the manager permission allowlist through the path the server actually uses.
//
// Five pinned probes, each with the result it must produce. The point is discrimination, not
// coverage: every probe names the wrong implementation it catches, and the negative control at
// the end is what proves the append — not the code — is doing the work.
//
// ⚠️ WHAT THIS PROVES AND WHAT IT DOES NOT. It drives `loadManagerAllowlist` + `matchManagerEntry`,
// which is exactly what `managerAllowlistEntry()` in `supervisor.mjs` calls at park time, and it
// uses a REAL git repository for the confinement so `runnerConfigClean`'s shell-out is exercised
// rather than stubbed. It does NOT park a live prompt on a running server, and it does not observe
// a manager answering one — that half is the task's Definition of Done (deploy, then re-run this
// from a fresh session). The log check at the end reports what it found rather than assuming.
//
// Usage: node server/replay-permission.mjs [--allowlist <path>] [--verbose]

import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { loadManagerAllowlist, matchManagerEntry, MANAGER_ALLOWLIST_FILE } from './manager-allowlist.mjs'
import { config } from './config.mjs'

// The five pinned probes.
//
// ⚠️ `APPENDED_ENTRY` is deliberately NOT one of the shipped entries. The probe's job is to prove
// the LIST drives the answer, so it has to start absent — appending an entry the file already
// carries would pass whether or not anything read the file. (`make test` cannot play this part: it
// ships as a confined entry, so it is covered by probe 3b's clean/dirty pair instead.)
const APPENDED_ENTRY = 'sha256sum'
const CONFINED_ENTRY = 'make test'
const NON_PROD_MISS = 'curl https://example.com'
const PROD_SCOPE_MISS = 'kubectlprod -n prod get deploy x-nonexistent'

let verbose = false
let allowlistPath = MANAGER_ALLOWLIST_FILE
for (let i = 2; i < process.argv.length; i += 1) {
  if (process.argv[i] === '--verbose') verbose = true
  else if (process.argv[i] === '--allowlist') allowlistPath = process.argv[++i]
}

const results = []
function record(name, ok, detail) {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`)
}

function git(cwd, args) {
  return execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
}

// A throwaway repository standing in for a worker's cwd. Its only job is to let the confinement
// run a real `git status`, so the probe fails for the reason it claims.
function makeWorkerRepo() {
  const dir = mkdtempSync(join(tmpdir(), 'replay-permission-'))
  git(dir, ['init', '-q'])
  git(dir, ['config', 'user.email', 'replay@example.com'])
  git(dir, ['config', 'user.name', 'replay'])
  writeFileSync(join(dir, 'Makefile'), 'test:\n\t@echo ok\n')
  git(dir, ['add', 'Makefile'])
  git(dir, ['commit', '-qm', 'base'])
  return dir
}

function writeAllowlist(path, entries) {
  writeFileSync(path, JSON.stringify({ version: 1, entries }, null, 2))
}

const baseEntries = () => {
  const { entries, error } = loadManagerAllowlist(allowlistPath)
  if (!entries) {
    console.error(`cannot read the shipped allowlist: ${error}`)
    process.exit(2)
  }
  return entries
}

const workerRepo = makeWorkerRepo()
const work = mkdtempSync(join(tmpdir(), 'replay-allowlist-'))
const appendedPath = join(work, 'manager-allowlist.json')

try {
  const entries = baseEntries()
  if (verbose) console.log(`loaded ${entries.length} entries from ${allowlistPath}`)

  // Probe 1 — baseline, BEFORE the append. `make test` must not match: it is not on the list yet.
  // Catches a matcher that hardcodes the command, or a list that already carries it.
  const before = matchManagerEntry(entries, 'Bash', APPENDED_ENTRY, { runnerConfigClean: true })
  record(
    `probe 1: baseline — ${APPENDED_ENTRY} does not match before the append`,
    before === null,
    before ? `unexpectedly matched ${before.prefix}` : 'no match, as required',
  )

  // Probe 2 — append the entry to a copy of the list, no code change.
  writeAllowlist(appendedPath, [...entries, { tool: 'Bash', prefix: APPENDED_ENTRY }])
  const { entries: appended, error: appendError } = loadManagerAllowlist(appendedPath)
  record('probe 2: appended allowlist parses', appended !== null, appendError ?? `${appended?.length ?? 0} entries`)

  // Probe 3 — the same command now matches, because the list changed and nothing else did.
  const matchedAppended = appended ? matchManagerEntry(appended, 'Bash', APPENDED_ENTRY, {}) : null
  record(
    `probe 3: ${APPENDED_ENTRY} matches after the append`,
    matchedAppended !== null && matchedAppended.prefix === APPENDED_ENTRY,
    matchedAppended ? `matched "${matchedAppended.prefix}"` : 'no match',
  )

  // Probe 3b — the confinement, not the prefix, is what admits the shipped test-run entry. The
  // same command against two trees: clean matches, dirty does not. Catches a matcher that ignores
  // `confine`, and a `runnerConfigClean` that always returns true.
  const cleanTree = git(workerRepo, ['status', '--porcelain', '--', 'Makefile']).trim() === ''
  const confinedClean = matchManagerEntry(entries, 'Bash', CONFINED_ENTRY, { runnerConfigClean: cleanTree })
  record(
    `probe 3b-clean: ${CONFINED_ENTRY} matches on a clean runner config`,
    cleanTree && confinedClean !== null,
    cleanTree ? `matched "${confinedClean?.prefix ?? 'nothing'}"` : 'tree was not clean — probe could not fire',
  )

  writeFileSync(join(workerRepo, 'Makefile'), 'test:\n\t@echo tampered\n')
  const dirtyTree = git(workerRepo, ['status', '--porcelain', '--', 'Makefile']).trim() !== ''
  const confinedDirty = matchManagerEntry(entries, 'Bash', CONFINED_ENTRY, { runnerConfigClean: !dirtyTree })
  record(
    `probe 3b-dirty: ${CONFINED_ENTRY} does not match on a dirty runner config`,
    dirtyTree && confinedDirty === null,
    dirtyTree ? 'tree read as dirty, no match' : 'tree was not dirty — probe could not fire',
  )

  // Probe 4 — a command absent from the list, not prod-scoped. Must not match, which is what
  // defeats an "answer everything except prod" predicate.
  const nonProd = matchManagerEntry(entries, 'Bash', NON_PROD_MISS, { runnerConfigClean: true })
  record(
    'probe 4: an unlisted non-prod command does not match',
    nonProd === null,
    nonProd ? `unexpectedly matched ${nonProd.prefix}` : 'no match, as required',
  )

  // Probe 5 — a prod-scope command. Read-only and against a name that does not exist, so a
  // wrongly-allowed probe fails harmlessly instead of touching a live deployment.
  const prod = matchManagerEntry(entries, 'Bash', PROD_SCOPE_MISS, { runnerConfigClean: true })
  record(
    'probe 5: a prod-scope command does not match',
    prod === null,
    prod ? `unexpectedly matched ${prod.prefix}` : 'no match, as required',
  )

  // Negative control — revert the append and probe 3's expectation must fail. This is what makes
  // the append load-bearing rather than incidental.
  writeAllowlist(appendedPath, [...entries])
  const { entries: reverted } = loadManagerAllowlist(appendedPath)
  const afterRevert = matchManagerEntry(reverted, 'Bash', APPENDED_ENTRY, { runnerConfigClean: true })
  record(
    'negative control: reverting the append removes the match',
    afterRevert === null,
    afterRevert ? 'still matched after revert — the append was not what authorised it' : 'no match, as required',
  )

  // The live half. Report what is actually on disk rather than asserting a pass: an empty or
  // absent log means no manager has answered under the policy here yet. The path comes from
  // `config` — no module but `config.mjs` reads the environment, which `config.test.mjs` enforces.
  const logPath = config.permissionLog
  let logNote
  if (!logPath) {
    logNote = 'permission logging is off (SUPERVISOR_PERMISSION_LOG=off) — the live half cannot be read'
  } else
    try {
      const lines = readFileSync(logPath, 'utf8').split('\n').filter(Boolean)
      const managerAnswers = lines
        .map((line) => {
          try {
            return JSON.parse(line)
          } catch {
            return null
          }
        })
        .filter((rec) => rec && rec.decided_by === 'manager')
      const withEntry = managerAnswers.filter((rec) => rec.matched_entry != null)
      logNote = `${managerAnswers.length} manager answer(s), ${withEntry.length} carrying matched_entry`
    } catch (err) {
      logNote = `no permission log at ${logPath} (${err.code ?? err.message}) — the live half is unproven here`
    }
  console.log(`INFO  live log: ${logNote}`)
} finally {
  rmSync(workerRepo, { recursive: true, force: true })
  rmSync(work, { recursive: true, force: true })
}

const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} probes passed`)
if (failed.length > 0) {
  console.error(`failed: ${failed.map((r) => r.name).join('; ')}`)
  process.exit(1)
}
