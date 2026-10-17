// The machine-wide record of deliberate A2A worker stops.
//
// ⚠️ **Why a file at all, when there is already a ledger.** `writeLedger` refuses to patch a
// rehydrated row (`supervisor.mjs`'s single-writer guarantee), because the session-ledger
// endpoint's `Post` REPLACES a whole row: a write from a server that did not spawn the worker
// would rebuild that row from identity alone and blank everything the owner had written. That
// guard is correct and stays. But `stop_agent` reads the request-time roster *precisely because*
// "the worker a manager stops is often the one it did NOT spawn" — so the row a stop must record
// is exactly the row the ledger refuses to take. The stop therefore needs a surface the stopping
// server owns, and this is it.
//
// ⚠️ **Machine-wide, not per-process and not per-vault.** Every supervisor server on this Mac
// reads and writes the same file, so a stop recorded by one is visible to the sweep run by
// another — which is the whole point, since the server that stops a worker is routinely not the
// one that will next sweep it.
//
// ⚠️ **Append-only, and never rewritten.** A stop is a fact that happened; nothing here edits or
// deletes a line. That also makes the file safe for concurrent writers on one host: an append of
// a single short line is atomic enough for this purpose, while a read-modify-write would race
// exactly the way the endpoint's `Post` does.
//
// ⚠️ **Keyed on `worker_id`, never on the task.** One task can be spawned again after its first
// worker stopped, and a task-keyed record would read the successor as stopped. The same rule the
// stop call itself keeps — `a2aStopCallFor` takes the worker id `create` recorded and refuses a
// task — so a reader joining on the task name here would undo it one layer up.
//
// ⚠️ **It never throws.** A stop that has already reached the controller must not be turned into
// a failed stop by an unwritable file, and the caller has nothing to catch with. The caller logs;
// this returns a boolean.

import { appendFileSync, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

import { config } from './config.mjs'

// ⚠️ **The path comes from `config.mjs`, never from `process.env` here.** A server module may not
// read the environment directly — `config.test.mjs` fails the build for it — because the
// configuration surface has to stay enumerable. `config.stopFile` carries the env override and
// the reasoning for the root it sits under.
export function stopFilePath() {
  return config.stopFile
}

// One JSON line per stop: the worker, the task it served, when, and who stopped it.
//
// ⚠️ `session` is the STOPPING server's session, not the worker's — the worker's is the id the
// row is already keyed by everywhere else, and recording it twice would tell a reader nothing
// about who acted. It is here so a later reader can tell which server ended a worker, which is
// the question the ledger's ownership rule makes unanswerable from the row alone.
export function stopEntry({ workerId, task, at = new Date().toISOString(), session }) {
  if (typeof workerId !== 'string' || workerId === '') return null
  return {
    worker_id: workerId,
    task: typeof task === 'string' ? task : '',
    stopped_at: at,
    stopping_session: typeof session === 'string' ? session : '',
  }
}

// Append one stop. Answers whether it landed; NEVER throws.
export function appendStop({ workerId, task, at, session, path = stopFilePath() } = {}) {
  const entry = stopEntry({ workerId, task, at, session })
  if (!entry) return false
  try {
    mkdirSync(dirname(path), { recursive: true })
    appendFileSync(path, `${JSON.stringify(entry)}\n`, 'utf8')
    return true
  } catch {
    return false
  }
}
