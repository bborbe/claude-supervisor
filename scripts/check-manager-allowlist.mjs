// Assert that docs/permission-allowlist.md and server/manager-allowlist.json still describe the
// same list.
//
// Two artifacts, one policy: the JSON is what the server matches against, the doc is what a human
// reads. Without this check they drift the moment someone edits one — and the drift is silent in
// the worst direction, because the doc is the thing a reader trusts and the JSON is the thing that
// actually decides.
//
// ⚠️ IT COMPARES THE WHOLE ENTRY, NOT THE PREFIX. An earlier version compared prefix sets, so
// dropping `confine: "clean-runner-config"` from the JSON left the doc promising a confinement the
// system no longer enforced, and the check still reported agreement. A prefix-only comparison also
// cannot see a `tool` change, or an entry demoted from `exact` to a bare prefix. Each entry is
// therefore keyed on its prefix and compared as a (tool, exact, confine) signature.
//
// A prefix present in one artifact and not the other is a failure in both directions: in the JSON
// only means the system allows something the doc never describes; in the doc only means the doc
// promises something the system will not do. A duplicate prefix in either artifact is also a
// failure — with two entries on one prefix, "which one matched" is undecidable.

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const DOC = join(ROOT, 'docs', 'permission-allowlist.md')
const JSON_FILE = join(ROOT, 'server', 'manager-allowlist.json')

function die(message) {
  console.error(`check-manager-allowlist: ${message}`)
  process.exit(1)
}

let doc
try {
  doc = readFileSync(DOC, 'utf8')
} catch (err) {
  die(`${DOC} is unreadable (${err.code ?? err.message}) — the allowlist has no human-readable half.`)
}

let parsed
try {
  parsed = JSON.parse(readFileSync(JSON_FILE, 'utf8'))
} catch (err) {
  die(`${JSON_FILE} is unreadable or not JSON (${err.code ?? err.message}).`)
}

if (!Array.isArray(parsed?.entries)) die(`${JSON_FILE} carries no "entries" list.`)

// The entry table is the first table whose header names a "Prefix" column. Rows are matched
// positionally rather than by any markdown parser: this is a fixed four-column contract, and a
// lenient parse would report "no rows" on a reshaped table — the same empty result a correct
// empty list produces.
const lines = doc.split('\n')
const headerIndex = lines.findIndex((line) => /^\|\s*Tool\s*\|\s*Prefix\s*\|/.test(line))
if (headerIndex === -1) die(`${DOC} has no entry table with a "Tool | Prefix" header.`)

function unquote(cell) {
  return (cell ?? '').replace(/`/g, '').trim()
}

// A confinement cell is a comma-separated list of machine names in backticks, or `any path`.
function parseConfinement(cell) {
  const text = unquote(cell)
  if (text === '' || text === 'any path') return { exact: false, confine: null }
  const parts = text.split(',').map((p) => p.trim()).filter(Boolean)
  const exact = parts.includes('exact')
  const confine = parts.find((p) => p !== 'exact') ?? null
  return { exact, confine }
}

const docEntries = new Map()
for (let i = headerIndex + 2; i < lines.length; i += 1) {
  const line = lines[i]
  if (!line.startsWith('|')) break
  const cells = line.split('|').map((cell) => cell.trim())
  const tool = unquote(cells[1])
  const prefix = unquote(cells[2])
  if (!prefix) continue
  if (docEntries.has(prefix)) die(`${DOC} lists "${prefix}" twice — which entry matched would be undecidable.`)
  docEntries.set(prefix, { tool, ...parseConfinement(cells[3]) })
}
if (docEntries.size === 0) die(`${DOC}'s entry table parsed to zero rows.`)

const jsonEntries = new Map()
for (const entry of parsed.entries) {
  const prefix = String(entry?.prefix ?? '').trim()
  if (!prefix) die(`${JSON_FILE} carries an entry with an empty prefix.`)
  if (jsonEntries.has(prefix)) die(`${JSON_FILE} lists "${prefix}" twice — which entry matched would be undecidable.`)
  jsonEntries.set(prefix, { tool: entry.tool, exact: entry.exact === true, confine: entry.confine ?? null })
}

const signature = (e) => `${e.tool}|exact=${e.exact}|confine=${e.confine ?? 'none'}`

const onlyJson = [...jsonEntries.keys()].filter((p) => !docEntries.has(p))
const onlyDoc = [...docEntries.keys()].filter((p) => !jsonEntries.has(p))
if (onlyJson.length > 0) {
  die(`in ${JSON_FILE} but not documented: ${onlyJson.join(', ')} — the system would allow an entry the doc never describes.`)
}
if (onlyDoc.length > 0) {
  die(`documented but not in ${JSON_FILE}: ${onlyDoc.join(', ')} — the doc promises an entry the system will not honour.`)
}

const mismatched = []
for (const [prefix, docEntry] of docEntries) {
  const jsonEntry = jsonEntries.get(prefix)
  if (signature(docEntry) !== signature(jsonEntry)) {
    mismatched.push(`"${prefix}" — doc says ${signature(docEntry)}, JSON says ${signature(jsonEntry)}`)
  }
}
if (mismatched.length > 0) {
  die(`the two artifacts disagree about what an entry DOES:\n  ${mismatched.join('\n  ')}`)
}

console.log(`check-manager-allowlist: ${jsonEntries.size} entries agree between the doc and the JSON, including tool, exact and confine.`)
