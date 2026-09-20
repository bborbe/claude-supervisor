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
//   - **Which call is pending is a different question from whether it is parked, and the
//     transcript does answer it.** The in-flight call is the LAST `tool_use` record in the
//     file, and it is in flight exactly when no `tool_result` carries its id. "Any
//     unmatched `tool_use`" is the plausible wrong answer: an interrupted call leaves an
//     unmatched `tool_use` behind and the conversation carries on, so a scan that reports
//     any of them keeps naming a call the worker abandoned — measured 2026-09-20 over
//     1 667 live transcripts, 58 carried an unmatched `tool_use` and only 36 had it as
//     their last one. Its record's `timestamp` is what makes a DURATION answerable, and
//     no other source on this path carries one.

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

// How much of a tool call's input survives into `input_summary`. A call's input can be a
// whole file (`Write`), a heredoc (`Bash`) or a subagent prompt (`Agent`), and this field
// exists to say WHAT the worker is doing, not to reproduce the payload — a manager reads it
// across a fleet, in a table. The full input is a different call's job:
// `pending_permissions` carries it verbatim for a parked call.
const MAX_SUMMARY = 200

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
 * The tool call a worker is inside right now, and how long it has been held, or null.
 *
 * `{ name, input_summary, started_at, held_seconds }`. The operator's stated debugging need
 * — "the current tool call, and how long it has been held" — is exactly this, and it is the
 * only signal that separates a worker executing a long call from one parked on a gate
 * without a pane: both read `busy`-looking from outside, and `agent_status` names neither.
 *
 * In flight means the LAST `tool_use` record in the transcript with no `tool_result`
 * carrying its id — see the module note for why "any unmatched `tool_use`" is wrong. The
 * record's own `timestamp` is the start, so `held_seconds` is measured from the transcript
 * rather than from when this call first happened to observe the worker, which is what makes
 * it survive a manager that polls every few minutes.
 *
 * `held_seconds` and `started_at` are null together when the record carries no usable
 * timestamp; `name` is still reported, because "which tool" and "for how long" fail
 * independently and collapsing them would throw away the half that answered.
 *
 * The read is the same bounded tail window as the message read, and — unlike that one — it
 * has NO full-file fallback. The reasoning is different here: a `tool_result` always follows
 * its `tool_use`, so a call found inside the window cannot have its result outside it, and
 * the only shape the window can miss is a call older than the whole window. Reporting null
 * then reads as "no call in flight" for a worker that is mid-call; it is the one blind spot
 * and it is named rather than papered over with an unbounded read on the status path.
 */
export function currentToolCallFrom(
  path,
  { now = Date.now(), read = readFileSync, size = statSync, open = openSync, readAt = readSync, close = closeSync } = {},
) {
  if (!path) return null
  let total
  try {
    total = size(path).size
  } catch {
    return null
  }

  if (total <= TAIL_BYTES) {
    try {
      return scanForCurrentToolCall(read(path, 'utf8'), now)
    } catch {
      return null
    }
  }

  const window = readTail(path, total, { open, readAt, close })
  return window === null ? null : scanForCurrentToolCall(window, now)
}

/** The in-flight `tool_use` in `body`, or null when nothing is in flight. */
function scanForCurrentToolCall(body, now) {
  let call = null
  const finished = new Set()
  for (const line of body.split('\n')) {
    // Match the quoted type VALUES, so a `tool_result` line is not skipped as a `tool_use`
    // one and vice versa — `"tool_use_id"` is a key on the result, and matching it as the
    // call type is how a scan ends up counting results as calls.
    if (!line.includes('"tool_use"') && !line.includes('"tool_result"')) continue
    let record
    try {
      record = JSON.parse(line)
    } catch {
      continue
    }
    const content = record.message?.content
    if (!Array.isArray(content)) continue
    for (const block of content) {
      if (block?.type === 'tool_use') call = { id: block.id, name: block.name, input: block.input, at: record.timestamp }
      else if (block?.type === 'tool_result' && block.tool_use_id) finished.add(block.tool_use_id)
    }
  }
  if (call === null || finished.has(call.id)) return null

  const startedAt = typeof call.at === 'string' ? call.at : null
  const parsed = startedAt === null ? NaN : Date.parse(startedAt)
  return {
    name: typeof call.name === 'string' ? call.name : null,
    input_summary: summarizeInput(call.input),
    started_at: startedAt,
    // Never negative: a transcript timestamp ahead of this process's clock is a clock skew,
    // not a call that starts in the future, and "-3 seconds" in a status table is noise a
    // manager has to reason about instead of a fact it can act on.
    held_seconds: Number.isNaN(parsed) ? null : Math.max(0, Math.round((now - parsed) / 1000)),
  }
}

/**
 * One line naming what a call is doing, from its input.
 *
 * `command` first because `Bash` is the call that gets held longest and its command is the
 * whole story; anything else is the compact JSON of its input. Whitespace is flattened so
 * the summary survives a table cell, and an input that carries nothing readable reads as
 * null rather than as the string `"{}"`.
 */
function summarizeInput(input) {
  if (input === null || input === undefined) return null
  let text
  if (typeof input === 'string') text = input
  else if (typeof input.command === 'string') text = input.command
  else {
    // A call made with no arguments is a real call with nothing to say about itself, and
    // `{}` in a status table reads as a bug rather than as an answer.
    if (typeof input === 'object' && Object.keys(input).length === 0) return null
    try {
      text = JSON.stringify(input)
    } catch {
      return null
    }
  }
  if (typeof text !== 'string') return null
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat ? flat.slice(0, MAX_SUMMARY) : null
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
  const path = transcriptPathFor(sessionId, opts)
  return {
    last_message: lastAssistantTextFrom(path, opts),
    // What it is doing and for how long — the other half of "what is it doing", and the half
    // that answers whether a long silence is a long call or a stuck worker.
    current_tool_call: currentToolCallFrom(path, opts),
    session_status: status,
    awaiting_input: awaitingInput(status),
  }
}
