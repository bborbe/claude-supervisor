// Unit tests for the tab channel.
//
// The one that matters most is `activates before sending`: measured 2026-09-14, a
// `send-text` against a live pane silently did nothing until its tab was activated,
// and the failure looked identical to a message the worker ignored. If activation is
// ever skipped, or reordered after the send, the channel becomes a no-op that reports
// success — so it is asserted on call order, not on the result.
//
// These do NOT replace the end-to-end drill: that a worker actually *acts on* a
// message is a fact about a real TUI, which a fake wezterm cannot see.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { composerLine, confirmInPane, frameMessage, isReady, listPanes, policySupportError, resumeSupportError, sendToPane, tabIdForPane, waitUntilReady } from './tab.mjs'

const PANE = '1710'
const TAB = '1105'
const PROMPT = '❯'

const ok = (stdout = '') => ({ status: 0, stdout, stderr: '' })
const fail = (stderr = 'boom') => ({ status: 1, stdout: '', stderr })

// Records every call so order can be asserted; `handlers` keys are "<cmd> <subcmd>".
function fakeWezterm(handlers = {}) {
  const calls = []
  const fn = (args) => {
    calls.push(args.join(' '))
    const key = `${args[0]} ${args[1] ?? ''}`
    const handler = handlers[key]
    return handler ? handler(args, calls) : ok('')
  }
  fn.calls = calls
  return fn
}

const panesJson = JSON.stringify([{ pane_id: Number(PANE), tab_id: Number(TAB), title: 'x' }])
const sleepNoop = async () => {}

test('frameMessage submits with a carriage return, and only once', () => {
  assert.equal(frameMessage('hello'), 'hello\r', 'a terminal Enter is \\r, not \\n')
  assert.equal(frameMessage('hello\r'), 'hello\r', 'already-framed input must not gain a second')
})

test('tabIdForPane finds the owning tab and reports a missing pane as null', () => {
  const wezterm = fakeWezterm({ 'cli list': () => ok(panesJson) })
  assert.equal(tabIdForPane(PANE, { wezterm }), TAB)
  assert.equal(tabIdForPane('9999', { wezterm }), null, 'a pane that is gone has no tab')
})

test('an unreadable pane list is null, never an empty list', () => {
  assert.equal(listPanes({ wezterm: fakeWezterm({ 'cli list': () => fail() }) }), null)
  assert.equal(listPanes({ wezterm: fakeWezterm({ 'cli list': () => ok('not json') }) }), null)
  assert.deepEqual(listPanes({ wezterm: fakeWezterm({ 'cli list': () => ok('[]') }) }), [])
})

test('isReady requires an EMPTY composer, not merely a drawn glyph', () => {
  const ready = (text) => isReady(PANE, { wezterm: fakeWezterm({ 'cli get-text': () => ok(text) }) })
  assert.equal(ready(`${PROMPT} `), true, 'an empty composer is ready')
  assert.equal(ready(PROMPT), true)
  // The regression this exists for: the TUI paints the composer with a placeholder
  // suggestion before it will honour a submit, and a message sent into that phase is
  // accepted and then swallowed (measured 2026-09-20 on live panes).
  assert.equal(ready(`${PROMPT} Try "fix lint errors"`), false, 'a placeholder is not readiness')
  assert.equal(ready(`${PROMPT} /color pink`), false, 'text already in the composer is not readiness')
  assert.equal(ready('still booting'), false, 'no glyph at all is not readiness')
  assert.equal(isReady(PANE, { wezterm: fakeWezterm({ 'cli get-text': () => fail('no such pane') }) }), null)
})

test('composerLine takes the LAST glyph line, so an echoed prompt is not the composer', () => {
  const text = `${PROMPT} /vault-cli:work-on-task "do the thing"\noutput\n${PROMPT} `
  assert.equal(composerLine(text), `${PROMPT} `, 'the echoed prompt above carries the same glyph')
  assert.equal(composerLine('no glyph here'), null)
})

test('waitUntilReady returns as soon as the glyph appears, not on a fixed delay', async () => {
  let reads = 0
  const wezterm = fakeWezterm({
    'cli get-text': () => {
      reads += 1
      return ok(reads < 3 ? 'booting' : `${PROMPT} `)
    },
  })
  const res = await waitUntilReady(PANE, { wezterm, sleep: sleepNoop, timeoutMs: 2000, intervalMs: 1 })
  assert.equal(res.ready, true)
  assert.equal(reads, 3, 'polled until the glyph appeared rather than sleeping a fixed time')
})

test('waitUntilReady distinguishes "never ready" from "could not read"', async () => {
  const never = await waitUntilReady(PANE, {
    wezterm: fakeWezterm({ 'cli get-text': () => ok('booting') }),
    sleep: sleepNoop, timeoutMs: 40, intervalMs: 1,
  })
  assert.equal(never.ready, false)
  assert.match(never.reason, /no input prompt/)

  const unreadable = await waitUntilReady(PANE, {
    wezterm: fakeWezterm({ 'cli get-text': () => fail() }),
    sleep: sleepNoop, timeoutMs: 40, intervalMs: 1,
  })
  assert.equal(unreadable.ready, false)
  assert.match(unreadable.reason, /could not be read/, 'an unreadable pane is not the same as a slow one')
})

test('sendToPane activates the tab before it types', async () => {
  const wezterm = fakeWezterm({
    'cli list': () => ok(panesJson),
    'cli get-text': () => ok(PROMPT),
  })
  const res = await sendToPane(PANE, '/color pink', { wezterm, sleep: sleepNoop, timeoutMs: 1000 })
  assert.deepEqual(res, { sent: true, tabId: TAB, paneId: PANE })

  const activateAt = wezterm.calls.findIndex((c) => c.startsWith('cli activate-pane'))
  const sendAt = wezterm.calls.findIndex((c) => c.startsWith('cli send-text'))
  assert.ok(activateAt >= 0, 'the pane must be activated')
  assert.ok(
    !wezterm.calls.some((c) => c.startsWith('cli activate-tab')),
    'activation must go through the pane id — a tab id is renumbered when its tab moves windows',
  )
  assert.ok(sendAt > activateAt, 'activation must precede the send — without it the send is silently dropped')
  assert.ok(wezterm.calls[sendAt].endsWith('/color pink\r'), 'the message is submitted with a carriage return')
})

test('sendToPane sends nothing at all when the pane cannot be activated', async () => {
  const wezterm = fakeWezterm({
    'cli list': () => ok(panesJson),
    'cli activate-pane': () => fail('cannot activate'),
  })
  const res = await sendToPane(PANE, 'hello', { wezterm, sleep: sleepNoop, timeoutMs: 200 })
  assert.match(res.error, /could not activate pane/)
  assert.equal(
    wezterm.calls.some((c) => c.startsWith('cli send-text')),
    false,
    'an unactivated send would appear to succeed and deliver nothing',
  )
})

test('sendToPane refuses to type into a pane that is not accepting input', async () => {
  const wezterm = fakeWezterm({
    'cli list': () => ok(panesJson),
    'cli get-text': () => ok('booting'),
  })
  const res = await sendToPane(PANE, 'hello', { wezterm, sleep: sleepNoop, timeoutMs: 40, intervalMs: 1 })
  assert.match(res.error, /not accepting input/)
  assert.equal(wezterm.calls.some((c) => c.startsWith('cli send-text')), false)
})

test('sendToPane surfaces a send-text failure rather than reporting success', async () => {
  const wezterm = fakeWezterm({
    'cli list': () => ok(panesJson),
    'cli get-text': () => ok(PROMPT),
    'cli send-text': () => fail('pane gone'),
  })
  const res = await sendToPane(PANE, 'hello', { wezterm, sleep: sleepNoop, timeoutMs: 1000 })
  assert.match(res.error, /send-text to pane/)
})

test('sendToPane reports a pane whose tab has disappeared', async () => {
  const wezterm = fakeWezterm({ 'cli list': () => ok('[]') })
  const res = await sendToPane(PANE, 'hello', { wezterm, sleep: sleepNoop, timeoutMs: 200 })
  assert.match(res.error, /no tab owns pane/)
  assert.equal(wezterm.calls.some((c) => c.startsWith('cli send-text')), false)
})

// Confirmation exists because a send's exit code reports only that keystrokes reached
// the pty — true whether or not they submitted. Measured 2026-09-20: 3 of 6 spawns
// reported `applied: true` for a colour that never applied.
const MARKER = 'Session color set to'

test('confirmInPane succeeds on the marker, and says how many attempts it took', async () => {
  const wezterm = fakeWezterm({ 'cli get-text': () => ok(`done\n${MARKER}: pink\n${PROMPT} `) })
  const res = await confirmInPane(PANE, MARKER, { wezterm, sleep: sleepNoop, timeoutMs: 50, intervalMs: 1 })
  assert.deepEqual(res, { confirmed: true, attempts: 1 })
})

test('confirmInPane retries a bare Enter, and reports an error when the marker never appears', async () => {
  const wezterm = fakeWezterm({ 'cli get-text': () => ok(`${PROMPT} /color pink`) })
  const res = await confirmInPane(PANE, MARKER, { wezterm, sleep: sleepNoop, timeoutMs: 10, intervalMs: 1, retries: 2 })
  assert.match(res.error, /never showed/)
  const enters = wezterm.calls.filter((c) => c.startsWith('cli send-text') && c.endsWith('\r'))
  assert.equal(enters.length, 2, 'exactly one retry Enter per retry, so the loop stays bounded')
})

test('a cleared composer is NOT treated as delivery', async () => {
  // Observed 2026-09-20: a stranded message can also be discarded without ever
  // submitting — the text sat unsubmitted for 14 minutes, then vanished, with the
  // colour never applied. An empty composer therefore proves nothing.
  const wezterm = fakeWezterm({ 'cli get-text': () => ok(`${PROMPT} `) })
  const res = await confirmInPane(PANE, MARKER, { wezterm, sleep: sleepNoop, timeoutMs: 10, intervalMs: 1, retries: 0 })
  assert.match(res.error, /never showed/, 'an empty composer must not satisfy confirmation')
})

test('sendToPane does not confirm unless the caller asks', async () => {
  const wezterm = fakeWezterm({ 'cli list': () => ok(panesJson), 'cli get-text': () => ok(PROMPT) })
  const res = await sendToPane(PANE, 'hello', { wezterm, sleep: sleepNoop, timeoutMs: 1000 })
  assert.deepEqual(res, { sent: true, tabId: TAB, paneId: PANE }, 'a relay must not wait on a marker it never produces')
})

test('sendToPane surfaces an unconfirmed send as an error, never as applied', async () => {
  // Ready first (empty composer), then the stranded composer the send left behind.
  let reads = 0
  const wezterm = fakeWezterm({
    'cli list': () => ok(panesJson),
    'cli get-text': () => {
      reads += 1
      return ok(reads === 1 ? `${PROMPT} ` : `${PROMPT} /color pink`)
    },
  })
  const res = await sendToPane(PANE, '/color pink', {
    wezterm,
    sleep: sleepNoop,
    timeoutMs: 1000,
    confirm: { marker: MARKER, timeoutMs: 10, intervalMs: 1, retries: 0 },
  })
  assert.match(res.error, /never showed/)
  assert.equal(res.applied, undefined, 'an unconfirmed send must never report applied')
})

// The regression these exist for: `{ interactive: true, resume }` used to pass the
// two-writer guard and then drop the id, opening a fresh conversation while the caller
// believed it was continuing one. A silent drop is the failure, so the test is that a
// refusal exists at all — not merely that the message reads well.
test('a resume a tab worker cannot honour is refused, not dropped', () => {
  const error = resumeSupportError({ resume: 'abc-123', interactive: true })
  assert.ok(error, 'this must refuse rather than open a fresh conversation')
  assert.match(error, /headless worker only/)
  assert.match(error, /abc-123/, 'the error names the id that would have been dropped')
  assert.match(error, /interactive:false/, 'and names the alternative, so the caller can act')
})

test('the refusal is exactly scoped to what cannot be honoured', () => {
  assert.equal(resumeSupportError({ resume: 'abc-123', interactive: false }), null, 'headless resume is the supported path')
  assert.equal(resumeSupportError({ interactive: true }), null, 'a tab with no resume has nothing to refuse')
  assert.equal(resumeSupportError({}), null)
})

test('a policy a tab worker cannot consult is refused, not dropped', () => {
  const error = policySupportError({ policy: 'strict.json', interactive: true })
  assert.ok(error, 'this must refuse rather than accept a policy that is never read')
  assert.match(error, /headless worker only/)
  assert.match(error, /strict\.json/, 'the error names the path that would have been ignored')
  assert.match(error, /interactive:false/, 'and names the alternative, so the caller can act')
})

test('the policy refusal is exactly scoped to what cannot be honoured', () => {
  assert.equal(policySupportError({ policy: 'strict.json', interactive: false }), null, 'headless is the path the hook runs on')
  assert.equal(policySupportError({ interactive: true }), null, 'a tab with no policy has nothing to refuse')
  assert.equal(policySupportError({}), null)
})
