// The manager permission allowlist — the commands a MANAGER may answer on its own.
//
// This is the decision layer `agents/goal-wrangler.md` was missing. The wrangler used to decide
// from the worker's brief alone, so the same command was answered differently depending on how
// the task happened to be worded. A written list makes the answer the same every time, and makes
// "the manager allowed something it should not have" a decidable question.
//
// ⚠️ THIS IS NOT `policy.mjs` AND MUST NOT BECOME IT. The policy decides what runs WITHOUT
// WAKING ANYONE — an `allow` rule there returns before `canUseTool` and no manager ever sees the
// request. This list decides what a manager answers once a prompt HAS reached one. Two
// consequences: a command the policy already allows (`Read`, `Glob`, `Grep`, cwd-confined
// `Write`/`Edit`) must never be listed here, because it is unreachable; and an entry here never
// stops a prompt being raised.
//
// ⚠️ EVERY BRANCH REFUSES. A missing, unreadable, malformed or empty file yields NO ENTRIES,
// which means every prompt goes to the operator — the safe direction, and the same fail-closed
// standing rule `cluster-allowlist.mjs` and `spawn-cwd.mjs` state. A manager allowlist that
// failed open would be a fleet-wide permission grant produced by a typo.
//
// ⚠️ THE CONFINEMENT IS AN INPUT, NOT A READ. `policy.mjs` is pure by contract (its header: no
// filesystem, no environment, no clock) and this module keeps that property so it stays
// unit-testable. A `clean-runner-config` entry therefore matches only when the caller passes
// `runnerConfigClean: true`, having actually checked. A caller that cannot check passes nothing,
// and the entry does not match — which is why the doc can say every listed entry is one the
// system enforces.

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { commandTokens } from './policy.mjs'

// The file the repo ships. Resolved relative to this module so the tests and the server read the
// same one, and so a worktree carries its own copy rather than reaching for an installed one.
export const MANAGER_ALLOWLIST_FILE = join(dirname(fileURLToPath(import.meta.url)), 'manager-allowlist.json')

// Every confinement a rule may name. An unknown name is a shape error, not a silent no-op: a
// typo'd confinement that read as "no confinement" would widen the entry it was meant to narrow.
export const CONFINEMENTS = new Set(['clean-runner-config'])

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim() !== ''
}

// Validate the documented shape — `{ "entries": [{ "tool", "prefix", "confine"? }, …] }` — and
// report a shape error by name rather than reading it leniently. A lenient read of
// `{"entries": {"Bash": "ls"}}` (a map where a list belongs) would otherwise yield zero entries
// and silently refuse everything, which looks identical to a correct empty list.
export function parseManagerAllowlist(parsed, path = MANAGER_ALLOWLIST_FILE) {
  if (parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {
      entries: null,
      error: `${path} must hold a JSON object with an "entries" list, e.g. {"entries": [{"tool": "Bash", "prefix": "ls"}]}.`,
    }
  }
  const { entries } = parsed
  if (!Array.isArray(entries)) {
    return {
      entries: null,
      error: `${path} must carry an "entries" LIST (got ${entries === undefined ? 'no "entries" key' : typeof entries}).`,
    }
  }
  const cleaned = []
  for (const [index, entry] of entries.entries()) {
    if (entry == null || typeof entry !== 'object' || Array.isArray(entry)) {
      return { entries: null, error: `${path} entries[${index}] must be an object.` }
    }
    if (!isNonEmptyString(entry.tool)) {
      return { entries: null, error: `${path} entries[${index}] needs a non-empty "tool".` }
    }
    if (!isNonEmptyString(entry.prefix)) {
      return { entries: null, error: `${path} entries[${index}] needs a non-empty "prefix".` }
    }
    if (entry.confine !== undefined && !CONFINEMENTS.has(entry.confine)) {
      return {
        entries: null,
        error: `${path} entries[${index}] names unknown confine "${entry.confine}" (known: ${[...CONFINEMENTS].join(', ')}).`,
      }
    }
    cleaned.push(
      entry.confine === undefined
        ? { tool: entry.tool, prefix: entry.prefix.trim() }
        : { tool: entry.tool, prefix: entry.prefix.trim(), confine: entry.confine },
    )
  }
  return { entries: cleaned, error: null }
}

// Read and validate. Every failure path returns `{ entries: null, error }`, never a permissive
// default — see the header. The caller decides what to do with `error`; `matchManagerEntry`
// treats a null list as "nothing is allowlisted".
export function loadManagerAllowlist(path = MANAGER_ALLOWLIST_FILE) {
  let raw
  try {
    raw = readFileSync(path, 'utf8')
  } catch (err) {
    return { entries: null, error: `${path} is unreadable (${err.code ?? err.message}) — no manager allowlist is in force.` }
  }
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch (err) {
    return { entries: null, error: `${path} is not valid JSON (${err.message}) — no manager allowlist is in force.` }
  }
  return parseManagerAllowlist(parsed, path)
}

// The entry whose PREFIX matches this prompt, ignoring any confinement — or null.
//
// Exists so the caller can decide whether the confinement input is worth computing: the server
// should not run `git status` on every Bash prompt, only on the handful that could match a
// confined entry. It must never be used as the authorisation itself — an entry returned here may
// still fail `matchManagerEntry`, and that is the whole point.
export function confinedCandidate(entries, toolName, key) {
  if (!Array.isArray(entries) || entries.length === 0) return null
  const tokens = commandTokens(key)
  if (tokens === null) return null
  for (const entry of entries) {
    if (entry.tool !== '*' && entry.tool !== toolName) continue
    const prefix = entry.prefix.trim().split(/\s+/).filter(Boolean)
    if (prefix.length === 0 || prefix.length > tokens.length) continue
    if (prefix.every((token, i) => tokens[i] === token)) return entry
  }
  return null
}

// The entry that authorises this prompt, or null when nothing does.
//
// `context` carries the confinement inputs — today only `runnerConfigClean`. An entry that names
// a confinement matches ONLY when its input is exactly `true`; `undefined`, `false` and any other
// value all fail closed. That is deliberate: a caller that forgot to check must not get the
// permission by omission.
//
// Longest prefix first, so `make test` is considered before any shorter `make`-ish entry that a
// future edit might add. Ties keep file order.
export function matchManagerEntry(entries, toolName, key, context = {}) {
  if (!Array.isArray(entries) || entries.length === 0) return null
  const tokens = commandTokens(key)
  if (tokens === null) return null
  const ordered = [...entries].sort(
    (a, b) => b.prefix.trim().split(/\s+/).length - a.prefix.trim().split(/\s+/).length,
  )
  for (const entry of ordered) {
    if (entry.tool !== '*' && entry.tool !== toolName) continue
    const prefix = entry.prefix.trim().split(/\s+/).filter(Boolean)
    if (prefix.length === 0 || prefix.length > tokens.length) continue
    if (!prefix.every((token, i) => tokens[i] === token)) continue
    if (entry.confine === 'clean-runner-config' && context.runnerConfigClean !== true) continue
    return entry
  }
  return null
}
