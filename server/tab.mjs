// Talking to a tab worker's terminal.
//
// A tab worker is a real Claude Code session in a wezterm pane, so the only way to
// reach it mid-run is to type into that pane. Three things were measured 2026-09-14
// against live panes rather than assumed, and each one shapes the code below:
//
//  1. `send-text` reaches a pane ONLY once its tab is activated. Two attempts against
//     a running worker did nothing — first with "\n", then with "\r" — and its
//     transcript was unchanged for three minutes. Running `activate-tab` first made
//     the same call land immediately. Hence activate-before-send, always.
//  2. Readiness is an EMPTY composer in `get-text`, not merely a drawn one. The glyph
//     alone appears too early: the TUI paints the composer with a placeholder
//     suggestion (`Try "fix lint errors"`) before it will honour a submit, and a
//     message sent into that phase is accepted and then swallowed — the text lands in
//     the composer and the Enter does nothing. Measured 2026-09-20 against live panes:
//     two spawns whose colour text arrived 81ms and 137ms after a placeholder sample
//     both stranded unsubmitted, while the one that arrived against a plain `❯ `
//     submitted. That race is why roughly half of them failed. A fixed sleep is a race
//     that fails on a loaded machine. A pane TITLE is not a signal — it only echoes the
//     `-n` we passed — and the transcript does not exist until the first message, so it
//     cannot gate that first message.
//  3. A terminal's Enter is "\r", not "\n". `\n` types a line break into the input box
//     without submitting it, which looks exactly like a message that was ignored.
//
// The channel drives a terminal, not an API. It steals focus when it activates, and
// it types — so it is deliberately confined to tab workers, which are real sessions a
// human could drive by hand anyway.

import { spawnSync } from 'node:child_process'

export const PROMPT_GLYPH = '❯' // ❯ — the input box's prompt, drawn when the TUI is up

// What a tab worker cannot do, stated rather than silently ignored.
//
// `resume` reaches the SDK query on the headless branch only: the tab path launches the
// `cc-*` launcher, which is never handed the flag. So `{ interactive: true, resume }`
// used to pass the two-writer guard — doing real work, refusing a live session and
// failing closed on an unreadable registry — and then drop the argument it had just
// guarded, opening a FRESH conversation while the caller believed it was continuing
// one. Same shape as the colour seed, where the consequence was cosmetic; here it is a
// lost conversation.
//
// Returns null when there is nothing to refuse, so a caller can use it as a gate
// without a second condition that could drift from this one.
export function resumeSupportError({ resume, interactive }) {
  if (!resume || !interactive) return null
  return (
    `resume reaches a headless worker only — the tab path launches the cc-* launcher, ` +
    `which is not handed the flag, so ${resume} would be dropped and you would get a ` +
    `fresh conversation while believing you were continuing one. ` +
    `Pass interactive:false to resume, or omit resume to open a new tab.`
  )
}

// The policy hook is installed on the headless query only — see makePermissionHook in
// supervisor.mjs. A tab worker is a separate process that answers its own prompts in its
// own tab, so the server never sees them and a policy handed to one would be accepted and
// then never consulted. That is precisely the failure this repo already shipped once,
// with policy.json itself: a file that existed, was documented, and was not read.
// Refused rather than dropped, the same way a resume a tab worker cannot honour is.
export function policySupportError({ policy, interactive }) {
  if (!policy || !interactive) return null
  return (
    `policy reaches a headless worker only — a tab worker answers its own prompts in ` +
    `its tab, so the server never sees them and ${policy} would never be consulted. ` +
    `Pass interactive:false to run the worker under it, or omit policy to use the tab's ` +
    `own permissions.`
  )
}

const defaultWezterm = (args) => spawnSync('wezterm', args, { encoding: 'utf8' })
const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// Submit with a carriage return. Idempotent so a caller that already framed a message
// does not end up sending two.
export function frameMessage(message) {
  return String(message).endsWith('\r') ? String(message) : `${message}\r`
}

// null means "could not ask", which callers must keep distinct from "no tab" — the
// same distinction the liveness probe makes, and for the same reason: an unreadable
// answer and a negative one lead to opposite actions.
export function listPanes({ wezterm = defaultWezterm } = {}) {
  const res = wezterm(['cli', 'list', '--format', 'json'])
  if (!res || res.error || res.status !== 0 || !res.stdout) return null
  try {
    const parsed = JSON.parse(res.stdout)
    return Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

export function tabIdForPane(paneId, opts = {}) {
  const panes = listPanes(opts)
  if (!panes) return null
  const hit = panes.find((pane) => String(pane.pane_id) === String(paneId))
  return hit ? String(hit.tab_id) : null
}

// `scrollback` reads past the viewport, which confirmation needs: a success marker is
// drawn once and then scrolls away, so a viewport-only read can miss a submission that
// really happened.
export function paneText(paneId, { wezterm = defaultWezterm, scrollback = 0 } = {}) {
  const args = ['cli', 'get-text', '--pane-id', String(paneId)]
  if (scrollback) args.push('--start-line', String(-Math.abs(scrollback)))
  const res = wezterm(args)
  if (!res || res.error || res.status !== 0) return null
  return res.stdout ?? ''
}

// The composer is the LAST line drawn with the prompt glyph at its start. Every echoed
// submitted prompt above it carries the same glyph, so position — not the glyph alone —
// is what identifies the composer.
export function composerLine(text) {
  const lines = String(text).split('\n')
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (lines[i].startsWith(PROMPT_GLYPH)) return lines[i]
  }
  return null
}

// true / false / null, never a guess. An EMPTY composer is the test, not a drawn one:
// the glyph appears while the TUI is still painting a placeholder suggestion, and a
// submit sent into that phase is swallowed (see the header note). A composer already
// holding text is not ready either — a caller must never press Enter into a line it
// did not write.
export function isReady(paneId, opts = {}) {
  const text = paneText(paneId, opts)
  if (text === null) return null
  const line = composerLine(text)
  if (line === null) return false
  return line.slice(PROMPT_GLYPH.length).trim() === ''
}

export async function waitUntilReady(paneId, { timeoutMs = 8000, intervalMs = 200, sleep = defaultSleep, ...opts } = {}) {
  const deadline = Date.now() + timeoutMs
  let unreadable = false
  for (;;) {
    const ready = isReady(paneId, opts)
    if (ready === true) return { ready: true }
    if (ready === null) unreadable = true
    if (Date.now() >= deadline) {
      return {
        ready: false,
        reason: unreadable
          ? `the pane could not be read within ${timeoutMs}ms`
          : `no input prompt in the pane within ${timeoutMs}ms`,
      }
    }
    await sleep(intervalMs)
  }
}

// Confirmation is OPT-IN and off by default, because `send_agent_message` shares this
// module to relay into a worker mid-run: a mandatory marker poll would make every relay
// wait on a marker that call site never produces.
//
// A send's exit code reports that keystrokes reached the pty — true whether or not they
// submitted. So when a caller asks for confirmation, the pane is polled for `marker`,
// and when it does not appear the Enter is retried. Measured 2026-09-15 and again
// 2026-09-20: a submit can be swallowed outright, and a bare `\r` repeated until it
// takes is the remedy — extra Enters on an empty composer are harmless.
//
// An EMPTY composer is deliberately NOT the success signal. A stranded message can also
// be discarded without ever submitting (observed 2026-09-20: the text sat unsubmitted
// for 14 minutes, then vanished, with the colour never applied), so a cleared composer
// proves nothing. Only the marker does.
export async function confirmInPane(paneId, marker, opts = {}) {
  const {
    wezterm = defaultWezterm,
    timeoutMs = 1000,
    intervalMs = 100,
    retries = 2,
    sleep = defaultSleep,
  } = opts

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    if (attempt > 0) {
      const retry = wezterm(['cli', 'send-text', '--pane-id', String(paneId), '--no-paste', frameMessage('')])
      if (!retry || retry.error || retry.status !== 0) {
        const why = retry?.error?.message || retry?.stderr?.trim() || `exit ${retry?.status}`
        return { error: `retrying Enter into pane ${paneId} failed: ${why}` }
      }
    }
    const deadline = Date.now() + timeoutMs
    for (;;) {
      const text = paneText(paneId, { wezterm, scrollback: 600 })
      if (text !== null && text.includes(marker)) return { confirmed: true, attempts: attempt + 1 }
      if (Date.now() >= deadline) break
      await sleep(intervalMs)
    }
  }

  return {
    error:
      `pane ${paneId} never showed "${marker}" after ${retries + 1} attempts — the message did ` +
      `not take effect and must not be reported as delivered.`,
  }
}

// Activate, wait for the prompt, type, submit. Every failure is returned rather than
// swallowed: a message that did not arrive and a message that arrived look identical
// from the caller's side, which is exactly the confusion this must not create.
export async function sendToPane(paneId, message, opts = {}) {
  const { wezterm = defaultWezterm, confirm = null } = opts
  if (!paneId) return { error: 'no pane to send into' }

  // Activate by PANE id, never tab id. A tab id is renumbered when its tab moves
  // windows (2026-09-18: tabs 158/159/160 in window 0 became 163/164/165 in
  // window 2, and `activate-tab --tab-id 159` failed outright while
  // `activate-pane --pane-id 239` worked). Pane ids survive the move, and this
  // function already holds one — so the tab lookup is only needed for the return
  // value, not for the activation.
  const tabId = tabIdForPane(paneId, opts)
  if (!tabId) return { error: `no tab owns pane ${paneId} — is it still open?` }

  const activated = wezterm(['cli', 'activate-pane', '--pane-id', String(paneId)])
  if (!activated || activated.error || activated.status !== 0) {
    const why = activated?.error?.message || activated?.stderr?.trim() || `exit ${activated?.status}`
    return { error: `could not activate pane ${paneId}: ${why}` }
  }

  const ready = await waitUntilReady(paneId, opts)
  if (!ready.ready) return { error: `pane ${paneId} is not accepting input (${ready.reason})` }

  const sent = wezterm(['cli', 'send-text', '--pane-id', String(paneId), '--no-paste', frameMessage(message)])
  if (!sent || sent.error || sent.status !== 0) {
    const why = sent?.error?.message || sent?.stderr?.trim() || `exit ${sent?.status}`
    return { error: `send-text to pane ${paneId} failed: ${why}` }
  }

  if (!confirm) return { sent: true, tabId, paneId: String(paneId) }

  const confirmed = await confirmInPane(paneId, confirm.marker, { ...opts, ...confirm })
  if (confirmed.error) return { error: confirmed.error }
  return { sent: true, confirmed: true, attempts: confirmed.attempts, tabId, paneId: String(paneId) }
}
