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
import { frameMessage, isReady, listPanes, sendToPane, tabIdForPane, waitUntilReady } from './tab.mjs'

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

test('isReady reads the prompt glyph, and null when the pane cannot be read', () => {
  assert.equal(isReady(PANE, { wezterm: fakeWezterm({ 'cli get-text': () => ok(`some output ${PROMPT} `) }) }), true)
  assert.equal(isReady(PANE, { wezterm: fakeWezterm({ 'cli get-text': () => ok('still booting') }) }), false)
  assert.equal(isReady(PANE, { wezterm: fakeWezterm({ 'cli get-text': () => fail('no such pane') }) }), null)
})

test('waitUntilReady returns as soon as the glyph appears, not on a fixed delay', async () => {
  let reads = 0
  const wezterm = fakeWezterm({
    'cli get-text': () => {
      reads += 1
      return ok(reads < 3 ? 'booting' : `ready ${PROMPT}`)
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

  const activateAt = wezterm.calls.findIndex((c) => c.startsWith('cli activate-tab'))
  const sendAt = wezterm.calls.findIndex((c) => c.startsWith('cli send-text'))
  assert.ok(activateAt >= 0, 'the tab must be activated')
  assert.ok(sendAt > activateAt, 'activation must precede the send — without it the send is silently dropped')
  assert.ok(wezterm.calls[sendAt].endsWith('/color pink\r'), 'the message is submitted with a carriage return')
})

test('sendToPane sends nothing at all when the tab cannot be activated', async () => {
  const wezterm = fakeWezterm({
    'cli list': () => ok(panesJson),
    'cli activate-tab': () => fail('cannot activate'),
  })
  const res = await sendToPane(PANE, 'hello', { wezterm, sleep: sleepNoop, timeoutMs: 200 })
  assert.match(res.error, /could not activate tab/)
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
