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

// The operator-only class: the doc's `| Form |` table must name exactly the forms the shared
// fixture exercises. A form documented but untested is a promise nothing checks; a form tested but
// undocumented is a gate the operator cannot read about. Both directions fail, as above.
const FIXTURE = join(ROOT, 'server', 'permission-tier.fixtures.json')
let fixture
try {
  fixture = JSON.parse(readFileSync(FIXTURE, 'utf8'))
} catch (err) {
  die(`${FIXTURE} is unreadable or not JSON (${err.code ?? err.message}) — the tier classifier has no contract.`)
}
const fixtureForms = new Set((fixture.cases ?? []).filter((c) => c.form).map((c) => c.form))
const formHeader = lines.findIndex((line) => /^\|\s*Form\s*\|/.test(line))
if (formHeader === -1) die(`${DOC} has no operator-only table with a "Form" header.`)
const docForms = new Set()
for (const line of lines.slice(formHeader + 2)) {
  if (!line.startsWith('|')) break
  const cell = line.split('|')[1].trim()
  const m = cell.match(/^`([^`]+)`$/)
  if (!m) die(`${DOC} operator-only row has a Form cell that is not one backticked name: ${cell}`)
  docForms.add(m[1])
}
const formsOnlyDoc = [...docForms].filter((f) => !fixtureForms.has(f))
const formsOnlyFixture = [...fixtureForms].filter((f) => !docForms.has(f))
if (formsOnlyDoc.length > 0) die(`documented operator-only forms no fixture case exercises: ${formsOnlyDoc.join(', ')}`)
if (formsOnlyFixture.length > 0) die(`fixture forms the doc never describes: ${formsOnlyFixture.join(', ')}`)

console.log(`check-manager-allowlist: ${jsonEntries.size} entries agree between the doc and the JSON, including tool, exact and confine; ${docForms.size} operator-only forms agree between the doc and the fixture.`)
