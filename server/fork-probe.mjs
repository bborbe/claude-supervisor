#!/usr/bin/env node
// Measure what `forkSession` and `resumeSessionAt` actually do to a session id.
//
// The task that added `resume` carried this as a Definition-of-Done item precisely
// because unverified SDK assumptions were wrong two times in three on this surface.
// So this does not reason about the types — it runs the SDK and prints the session id
// each variant reports back.
//
// Not part of `make test`: it makes real model calls through claude-code-router and
// needs a live session to adopt. Run it by hand:
//
//   ANTHROPIC_BASE_URL=http://127.0.0.1:8788 node server/fork-probe.mjs
//
// It lives in `server/` rather than `scripts/` for a mechanical reason: the SDK is
// resolved from `server/node_modules`, and the repo root has none, so a copy under
// `scripts/` cannot import it.
//
// Read-only with respect to the repo. Creates throwaway sessions under /tmp.

import { query } from '@anthropic-ai/claude-agent-sdk'
import { readFileSync, realpathSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const CWD = '/tmp/fork-probe'
// The RESOLVED cwd, not the one we passed: Claude Code escapes the real path, and on
// macOS /tmp is a symlink to /private/tmp, so the unresolved form looks in a directory
// that never exists. Same trap transcriptDirFor() documents in supervisor.mjs.
let resolved = CWD
try {
  resolved = realpathSync(CWD)
} catch {}
const PROJECT_DIR = join(homedir(), '.claude', 'projects', resolved.replace(/\//g, '-'))

async function run(prompt, options) {
  const seen = []
  let sessionId = null
  const q = query({ prompt, options: { cwd: CWD, settingSources: ['user', 'project', 'local'], ...options } })
  for await (const message of q) {
    seen.push(message.type)
    if (message.type === 'system' && message.subtype === 'init') sessionId = message.session_id
    if (message.type === 'result') break
  }
  return { sessionId, messages: seen.length }
}

// The transcript holds one entry per message; its `uuid` is what resumeSessionAt takes.
function firstMessageUuid(sessionId) {
  try {
    const file = join(PROJECT_DIR, `${sessionId}.jsonl`)
    const lines = readFileSync(file, 'utf8').split('\n').filter(Boolean)
    for (const line of lines) {
      try {
        const entry = JSON.parse(line)
        if (entry.uuid && entry.type === 'user') return entry.uuid
      } catch {}
    }
  } catch {}
  return null
}

const results = []
const note = (label, value) => {
  results.push([label, value])
  console.log(`${label.padEnd(34)} ${value ?? '(none)'}`)
}

console.log('1. a fresh session, to adopt')
const base = await run('Reply with exactly: PING', {})
note('fresh session id', base.sessionId)
if (!base.sessionId) {
  console.error('FAIL: no session id reported; cannot measure resume behaviour')
  process.exit(1)
}

console.log('\n2. resume the same conversation, no flags')
const plain = await run('Reply with exactly: PONG', { resume: base.sessionId })
note('resume -> session id', plain.sessionId)
note('  continued (same id)?', plain.sessionId === base.sessionId ? 'YES — continues' : 'NO — new id')

console.log('\n3. resume with forkSession: true')
const forked = await run('Reply with exactly: FORK', { resume: base.sessionId, forkSession: true })
note('resume+forkSession -> id', forked.sessionId)
note('  continued (same id)?', forked.sessionId === base.sessionId ? 'YES — continues' : 'NO — FORKED to a new id')

console.log('\n4. resumeSessionAt, truncating at the first message')
const at = firstMessageUuid(base.sessionId)
note('message uuid to resume at', at)
if (at) {
  const truncated = await run('Reply with exactly: TRUNC', { resume: base.sessionId, resumeSessionAt: at })
  note('resumeSessionAt -> id', truncated.sessionId)
  note('  continued (same id)?', truncated.sessionId === base.sessionId ? 'YES — continues' : 'NO — new id')
} else {
  note('resumeSessionAt', 'SKIPPED — no user-message uuid found in the transcript')
}

console.log('\n=== summary ===')
for (const [label, value] of results) console.log(`${label.padEnd(34)} ${value ?? '(none)'}`)
