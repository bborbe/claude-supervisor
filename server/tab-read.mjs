// Reading a tab worker's state from disk — the transcript source `agent_status` never had.
//
// `agent_status` promises `last_message`, and for every tab worker it returned null. The
// in-memory `transcript` array is pushed in exactly one place — inside the headless SDK
// loop — so a worker opened as a real session in a pane never fills it, and
// `lastAssistantText` walks an empty array forever. Nothing about the tab path is broken;
// it simply has no transcript source, and the JSONL Claude Code writes is the only one
// that exists for it.
//
// Two things here are measured rather than assumed, because each has a plausible wrong
// answer:
//
//   - **A transcript is resolved by SESSION ID, never by cwd.** `transcriptDirFor(cwd)`
//     escapes the cwd the caller passed, but the `cc-*` launcher `cd`s into its own
//     vault — so a worker spawned with `cwd: "/tmp"` runs in the vault, writes its
//     transcript there, and leaves the spawn response still saying `/tmp`. Globbing
//     `<projectsDir>/*/<session-id>.jsonl` never consults a cwd, so the derivation the
//     README calls unreliable is not on this path at all.
//   - **Gate state does not come from the transcript.** A pending `tool_use` with no
//     matching `tool_result` reads identically whether the worker is executing a tool or
//     parked on a permission prompt — measured 2026-09-19 across 25 live sessions, 3
//     `busy` sessions carried exactly that pending call, indistinguishable by transcript
//     from the 2 `waiting` ones. The session registry's `status` is the field that
//     separates them, and it is the only non-pane source for it.

import { closeSync, existsSync, openSync, readdirSync, readFileSync, readSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { config } from './config.mjs'
import { readRegistry } from './liveness.mjs'

// How much of the tail to read. A transcript grows without bound and `list_agents`
// renders every worker, so reading each file whole turns one status call into a read of
// the fleet's entire history. The last assistant message is at the end by definition, so
// a window is enough — and a window that finds nothing reports null rather than falling
// back to a full read, because the alternative is unbounded cost on the hot path.
const TAIL_BYTES = 256 * 1024

// The cap the in-memory path already applies, so a message reads the same length
// whichever source produced it.
const MAX_TEXT = 2000

/**
 * The transcript for a session, or null when none is on disk.
 *
 * Resolved by session id against the projects root, deliberately not from a cwd: see the
 * module note. Returns null rather than throwing so an unreadable root reads as "no
 * transcript", which the caller reports as unknown instead of as a worker that said
 * nothing.
 */
export function transcriptPathFor(
  sessionId,
  { dir = config.projectsDir, exists = existsSync, readdir = readdirSync } = {},
) {
  if (!sessionId) return null
  let projects
  try {
    projects = readdir(dir)
  } catch {
    return null
  }
  for (const name of projects) {
    const candidate = join(dir, name, `${sessionId}.jsonl`)
    if (exists(candidate)) return candidate
  }
  return null
}

/**
 * The last thing the worker said, read from a transcript file.
 *
 * "Last assistant record" is not the same as "last thing said": most assistant records
 * carry a `tool_use` block and no text at all — measured on a live session, 127 assistant
 * records of which only 27 carried text. So this scans for the last one with non-empty
 * text rather than taking the final record.
 *
 * The tail read is a FAST PATH, not a bound on correctness. A fixed window is not safe
 * here, and the way it fails is silent: measured 2026-09-19 on a real 700 KB transcript,
 * the last assistant-with-text record sat at byte 402 426 while a 256 KB window began at
 * byte 438 158 — the trailing records were large attachments, and they pushed the answer
 * out of the window. A miss therefore falls back to scanning the whole file. Reporting
 * null instead would make a worker that DID speak read as one that said nothing, which is
 * the exact failure this module exists to fix.
 */
export function lastAssistantTextFrom(
  path,
  { read = readFileSync, size = statSync, open = openSync, readAt = readSync, close = closeSync } = {},
) {
  if (!path) return null
  let total
  try {
    total = size(path).size
  } catch {
    return null
  }

  const io = { read, open, readAt, close }
  if (total <= TAIL_BYTES) {
    try {
      return scanForLastAssistantText(read(path, 'utf8'))
    } catch {
      return null
    }
  }

  const window = readTail(path, total, io)
  if (window === null) return null
  const found = scanForLastAssistantText(window)
  if (found !== null) return found

  try {
    return scanForLastAssistantText(read(path, 'utf8'))
  } catch {
    return null
  }
}

/**
 * The last `TAIL_BYTES` of a file, with the leading partial line dropped.
 *
 * The window begins mid-line, and parsing a truncated record as if it were whole is how a
 * tail read reports a parse error as "nothing was said". When there is no newline at all
 * the window is one partial line and the per-line parse rejects it anyway.
 */
function readTail(path, total, { open, readAt, close }) {
  try {
    const fd = open(path, 'r')
    try {
      const buf = Buffer.alloc(TAIL_BYTES)
      readAt(fd, buf, 0, TAIL_BYTES, total - TAIL_BYTES)
      const body = buf.toString('utf8')
      const firstBreak = body.indexOf('\n')
      return firstBreak >= 0 ? body.slice(firstBreak + 1) : body
    } finally {
      close(fd)
    }
  } catch {
    return null
  }
}

/** The last assistant record in `body` carrying non-empty text, or null. */
function scanForLastAssistantText(body) {
  let text = null
  for (const line of body.split('\n')) {
    // Match the quoted type VALUE rather than a whole serialized pair, so a separator
    // change upstream cannot silently turn every worker into "said nothing" — the trap
    // `fleet-colours.py` guards the same way when it matches `"agent-color"`.
    if (!line.includes('"assistant"')) continue
    let record
    try {
      record = JSON.parse(line)
    } catch {
      continue
    }
    if (record.type !== 'assistant') continue
    const content = record.message?.content
    if (!Array.isArray(content)) continue
    const joined = content
      .filter((c) => c?.type === 'text')
      .map((c) => c.text)
      .join('\n')
    if (joined.trim()) text = joined.slice(0, MAX_TEXT)
  }
  return text
}

/**
 * The registry's `status` for a session, or null when it is not listed.
 *
 * Values seen across a live fleet (2026-09-19): `idle`, `busy`, `waiting`, `shell`.
 * Null is "not in the registry", which is a different fact from any of them.
 */
export function sessionStatusFor(sessionId, { registry = readRegistry } = {}) {
  if (!sessionId) return null
  const entry = registry().find((e) => e.sessionId === sessionId)
  return entry?.status ?? null
}

/**
 * Whether a registry status means the worker is blocked on input.
 *
 * Three-way on purpose: null means the registry does not list the session — not that it
 * is not waiting. Collapsing the two would report a guess as a measurement, the failure
 * `fleet-colours.py` splits `unknown` from `default` to avoid.
 *
 * ⚠️ This means "blocked on input", which is a superset of "a permission gate is open".
 * Name it for what is verified until an end-to-end run pins the narrower claim.
 */
export const awaitingInput = (status) => (status === null ? null : status === 'waiting')

/**
 * Everything readable about a tab worker that its in-memory record does not hold.
 *
 * The convenience composition of the three functions above; a caller that needs only
 * some of them (the server already has the in-memory text) calls those directly rather
 * than paying for a transcript read it will discard.
 */
export function tabWorkerRead(sessionId, opts = {}) {
  const status = sessionStatusFor(sessionId, opts)
  return {
    last_message: lastAssistantTextFrom(transcriptPathFor(sessionId, opts), opts),
    session_status: status,
    awaiting_input: awaitingInput(status),
  }
}
