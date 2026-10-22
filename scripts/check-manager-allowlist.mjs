// Assert that docs/permission-allowlist.md and server/manager-allowlist.json still describe the
// same list.
//
// Two artifacts, one policy: the JSON is what the server matches against, the doc is what a human
// reads. Without this check they drift the moment someone edits one — and the drift is silent in
// the worst direction, because the doc is the thing a reader trusts and the JSON is the thing that
// actually decides.
//
// Reads the doc's entry table (the rows between the header row and the first blank line) and
// compares the command prefixes, as sets. A prefix present in one and not the other is a failure
// in both directions: in the JSON only means the system allows something the doc never describes;
// in the doc only means the doc promises something the system will not do.

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
// positionally rather than by any markdown parser: this is a fixed two-column contract, and a
// lenient parse would report "no rows" on a reshaped table — the same empty result a correct
// empty list produces.
const lines = doc.split('\n')
const headerIndex = lines.findIndex((line) => /^\|\s*Tool\s*\|\s*Prefix\s*\|/.test(line))
if (headerIndex === -1) die(`${DOC} has no entry table with a "Tool | Prefix" header.`)

const docPrefixes = new Set()
for (let i = headerIndex + 2; i < lines.length; i += 1) {
  const line = lines[i]
  if (!line.startsWith('|')) break
  const cells = line.split('|').map((cell) => cell.trim())
  const prefix = (cells[2] ?? '').replace(/^`|`$/g, '').trim()
  if (prefix) docPrefixes.add(prefix)
}
if (docPrefixes.size === 0) die(`${DOC}'s entry table parsed to zero rows.`)

const jsonPrefixes = new Set(parsed.entries.map((entry) => String(entry?.prefix ?? '').trim()))

const onlyJson = [...jsonPrefixes].filter((p) => !docPrefixes.has(p))
const onlyDoc = [...docPrefixes].filter((p) => !jsonPrefixes.has(p))

if (onlyJson.length > 0) {
  die(`in ${JSON_FILE} but not documented: ${onlyJson.join(', ')} — the system would allow an entry the doc never describes.`)
}
if (onlyDoc.length > 0) {
  die(`documented but not in ${JSON_FILE}: ${onlyDoc.join(', ')} — the doc promises an entry the system will not honour.`)
}

console.log(`check-manager-allowlist: ${jsonPrefixes.size} entries agree between the doc and the JSON.`)
