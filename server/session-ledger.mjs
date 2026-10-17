// The session-ledger endpoint — attention-controller's answer to "what has the fleet spawned?"
//
// This is the READ side that replaces `ledger.mjs`'s filesystem reads. The defect it closes is
// not a missing row and not a colliding key: the supervisor built its roster ONCE, at process
// import, from the ledger gated on liveness at that instant, and its only later mutation could
// DELETE but never INSERT — so a session that was not live when the server started stayed
// invisible to that process for its whole life, however live it became. Measured 2026-10-13:
// 4 of 35 live-and-ledgered sessions were absent from a live roster, this session's own id
// among them, because its heartbeat is written by the very server that performs the adoption.
//
// ⚠️ **A read here happens at REQUEST time and therefore cannot freeze.** That is the whole
// property a boot-time adoption block cannot have, and it is why this module exists rather
// than a second liveness filter over the same files.
//
// ⚠️ **Identity and liveness stay separate reads, joined by the caller on `session_id`.** The
// endpoint carries no `live` flag and no staleness window, deliberately: 1480 of the legacy
// rows still say `running` for workers long dead, so a ledger row is permanent by contract and
// filtering on its `status` would resurrect the whole fleet. Liveness comes from
// `session-heartbeat.mjs` beside this one — the same split `registry-rehydrate.mjs` keeps.
import { config } from './config.mjs'

export const SESSION_LEDGER_PATH = '/api/1.0/session-ledger'

// Seconds any one read may take. The store is local and answers in milliseconds; this is a
// backstop against a wedged server, never a routine path. Mirrors `session-heartbeat.mjs`'s
// `HEARTBEAT_TIMEOUT_MS` so the two readers give up at the same point.
export const SESSION_LEDGER_TIMEOUT_MS = 5000

// Every row the store serves, or `null` when the read could not run.
//
// `endpoint` is resolved from `config.attentionStoreUrl` — the same variable and default
// `session-heartbeat.mjs` and `scripts/session-liveness.py` carry (`ATTENTION_STORE_URL`,
// else `http://localhost:18080`) — so a store moved once moves everywhere and this file
// gains no second convention to drift.
//
// ⚠️ **`null` is "could not read it" and is never folded into `[]`.** An empty row list is a
// positive claim that the fleet has spawned nothing; a transport failure, a non-200, or a
// body that is not an array is the probe failing to run. The caller must be able to tell
// them apart — collapsing the two turns a store outage into a confident empty roster, which
// is the direction that hides live workers.
export async function readSessionLedger({
  endpoint,
  fetchImpl = globalThis.fetch,
  timeoutMs = SESSION_LEDGER_TIMEOUT_MS,
} = {}) {
  const base = endpoint === undefined ? config.attentionStoreUrl : endpoint
  if (!base) return null
  const url = `${String(base).replace(/\/+$/, '')}${SESSION_LEDGER_PATH}`
  let res
  try {
    res = await fetchImpl(url, {
      headers: { Accept: 'application/json' },
      // A wedged store must not hang the roster read — this sits in front of `list_agents`.
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch {
    return null
  }
  // ⚠️ Only 200 is a read. A 404 is the per-id route's "never spawned" and is not an answer
  // to the LIST question, and any other status is the store declining to answer — both are
  // "could not read it" here.
  if (!res || res.status !== 200) return null
  let body
  try {
    body = await res.json()
  } catch {
    return null
  }
  if (!Array.isArray(body)) return null
  return body
}

// One row by session id, or `null` when there is none to read.
//
// ⚠️ **`null` covers BOTH "no such row" and "could not read it", and that is a deliberate
// match to the reader it replaces rather than a lost distinction.** `ledger.mjs`'s
// `readRecord` catches every failure — a missing file, an unreadable one, malformed JSON —
// and answers `null`, so a caller written against it already treats "absent" and "unknown" as
// one answer. Widening that here would smuggle a behaviour change in under a read swap.
// The one caller, `spawnAgent`'s decision-on-resume check, refuses on `null` — which is the
// SAFE direction for both readings, because it declines to carry a decision it cannot tie to
// a park. A caller that genuinely needs the three answers should use the list route, where
// `readSessionLedger`'s `null`-vs-`[]` split is already preserved.
//
// ⚠️ **The per-id route is an EXACT lookup, so a PREFIX is not a row** — it answers 404,
// which is `null` here, exactly as an absent `<session_id>.json` was. That matters because a
// manager holds an 8-char prefix, not a full id; the callers of this function pass a full id,
// and the one that takes a prefix (`send_agent_message`'s roster lookup) goes through the
// list route.
export async function readSessionLedgerRow(
  sessionId,
  { endpoint, fetchImpl = globalThis.fetch, timeoutMs = SESSION_LEDGER_TIMEOUT_MS } = {},
) {
  if (typeof sessionId !== 'string' || !sessionId) return null
  const base = endpoint === undefined ? config.attentionStoreUrl : endpoint
  if (!base) return null
  const url = `${String(base).replace(/\/+$/, '')}${SESSION_LEDGER_PATH}/${encodeURIComponent(sessionId)}`
  let res
  try {
    res = await fetchImpl(url, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch {
    return null
  }
  if (!res || res.status !== 200) return null
  let body
  try {
    body = await res.json()
  } catch {
    return null
  }
  // A body that is not an object is not a row — an array or a scalar here is the store
  // answering a different question, never a record to hand a caller that will index into it.
  if (!body || typeof body !== 'object' || Array.isArray(body)) return null
  return body
}

// The wire row for a file-ledger record, or `null` when it has no key to file under.
//
// ⚠️ **The two vocabularies differ by exactly two fields, and both are named rather than
// discovered.** The file record says `parent_session`; the endpoint says `spawner_session_id`
// — one name for one thing, chosen on the endpoint because that is the row the readers move
// onto. And the endpoint requires a `location`, which the file record never carried, so it is
// defaulted here rather than left to the store's 400: every row this supervisor writes is a
// local one, and the field exists for the pod workers of a sibling task.
//
// ⚠️ An absent `parent_session` becomes the empty string, NOT a dropped key: the endpoint's
// `spawner_session_id` is optional, and 448 of the legacy rows have no spawner at all — an
// unanchored spawn is a real state, not a malformed row.
export function recordToWire(record) {
  if (!record || typeof record.session_id !== 'string' || !record.session_id) return null
  const { parent_session: parentSession, ...rest } = record
  return {
    ...rest,
    spawner_session_id: parentSession ?? '',
    location: record.location ?? 'local',
  }
}

// Write one row to the endpoint. Answers whether the store took it; NEVER throws.
//
// ⚠️ **It answers a boolean rather than rejecting, because every caller is on a path that
// must not fail on a store outage.** A spawn that has already produced a working worker must
// not be turned into a failed spawn by a store that is down, and a shutdown stamp has no
// caller left to catch anything. The caller logs; nothing here does.
export async function postSessionLedger(
  row,
  { endpoint, fetchImpl = globalThis.fetch, timeoutMs = SESSION_LEDGER_TIMEOUT_MS } = {},
) {
  if (!row) return false
  const base = endpoint === undefined ? config.attentionStoreUrl : endpoint
  if (!base) return false
  try {
    const res = await fetchImpl(`${String(base).replace(/\/+$/, '')}${SESSION_LEDGER_PATH}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(row),
      signal: AbortSignal.timeout(timeoutMs),
    })
    return Boolean(res && res.ok)
  } catch {
    return false
  }
}

// Merge fields into ONE row on the endpoint. Answers whether the store took it; NEVER throws.
//
// ⚠️ **This is the write a server that did NOT spawn the row can make, and it is the whole
// reason the endpoint grew a PATCH.** `postSessionLedger` above REPLACES a row: that is correct
// for the server that spawned the worker, which holds the complete record in memory, and
// destructive for any other server, which holds a row carrying identity alone — so a stop
// recorded through it would rebuild the row and blank everything the owner had written. The
// store merges instead, so every field this caller does not name survives untouched.
//
// ⚠️ **The merge happens SERVER-side, so the caller needs no local copy of the row.** That is
// what makes it usable for a rehydrated agent, whose record lives in the supervisor that
// spawned it and is not in this process at all — a client-side merge would have nothing to
// merge into.
//
// ⚠️ **A `404` is `false`, not an error.** It is the store's "no such row" — a stop on a session
// the ledger never recorded, which is a real state for a worker spawned before the ledger
// existed. The caller logs it and carries on, exactly as for a store outage, so the two are one
// answer here on purpose. Same boolean-not-throw contract as `postSessionLedger`, and for the
// same reason: the caller is on a path that must not fail on a store outage.
export async function patchSessionLedgerRow(
  sessionId,
  patch,
  { endpoint, fetchImpl = globalThis.fetch, timeoutMs = SESSION_LEDGER_TIMEOUT_MS } = {},
) {
  if (typeof sessionId !== 'string' || !sessionId) return false
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return false
  const base = endpoint === undefined ? config.attentionStoreUrl : endpoint
  if (!base) return false
  const url = `${String(base).replace(/\/+$/, '')}${SESSION_LEDGER_PATH}/${encodeURIComponent(sessionId)}`
  try {
    const res = await fetchImpl(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
      signal: AbortSignal.timeout(timeoutMs),
    })
    return Boolean(res && res.ok)
  } catch {
    return false
  }
}

// The rows as a `Map<sessionId, row>`, keyed the way every reader joins them.
//
// ⚠️ **`session_id` is the identity and the only sound key.** `agent_id` is a per-process
// counter that restarts at 0 on every supervisor reconnect — the live ledger holds 1891 rows
// across only 187 distinct values, 215 of them claiming `agent_1` — so keying on it collapses
// ~90% of the ledger. It is carried on the row as a non-unique attribute and never keys here.
//
// A row with no usable `session_id` is skipped rather than filed under a synthesised key: an
// invented key is addressable but meaningless, and it would make the roster answer for a
// session that does not exist.
export function ledgerBySessionId(rows) {
  const known = new Map()
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    const sessionId = row.session_id
    if (typeof sessionId !== 'string' || !sessionId) continue
    known.set(sessionId, row)
  }
  return known
}
