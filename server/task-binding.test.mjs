// Tests for binding a spawned session to the vault task it was opened for.
//
// The load-bearing case is the DISPLACEMENT one: a task that already names an owner must not
// have that owner's id overwritten by the session being bound. That is the defect this module
// was extracted to fix (measured 2026-10-05 — a cluster spawn replaced a live owner's id with
// a probe session's, leaving the row naming a session that can never read LIVE), and it is
// asserted on the argv the binding issues rather than on a file's contents, so it holds
// without a vault.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { OWNER_KEY, bindSessionToTask, currentOwner } from './task-binding.mjs'

const OWNER = '2de021b6-f758-404a-a953-065a9ec2b5a4'
const NEW = '3bcfc670-b790-4c00-a232-904d7238de35'
const TASK = 'Some Task'

// A fake vault-cli. `owner` is what `task get` answers with; '' models the absent-key case,
// which the real CLI reports as `{"value": ""}` with exit 0.
function fakeCli({ owner = '', read = null, write = null } = {}) {
  const calls = []
  const run = (args) => {
    calls.push(args)
    if (args[1] === 'get') {
      if (read) return read
      return {
        status: 0,
        stdout: JSON.stringify({ key: OWNER_KEY, name: TASK, value: owner }),
        stderr: '',
      }
    }
    if (write) return write
    return { status: 0, stdout: '', stderr: '' }
  }
  return { run, calls }
}

const writes = (calls) => calls.filter((a) => a[1] !== 'get')

test('an owned task keeps its owner: the binding appends instead of stamping', () => {
  const { run, calls } = fakeCli({ owner: OWNER })
  const res = bindSessionToTask({ task: TASK, vault: 'private-personal', sessionId: NEW, run })

  assert.deepEqual(res, { vault: 'private-personal', action: 'appended', owner: OWNER })
  // The defect in one assertion: `task set … claude_session_id` is the write that displaces.
  assert.equal(writes(calls).length, 1)
  assert.deepEqual(writes(calls)[0], [
    'task',
    'append-metrics-session',
    TASK,
    NEW,
    '--vault',
    'private-personal',
  ])
  assert.ok(
    !calls.some((a) => a[1] === 'set' && a[3] === OWNER_KEY),
    'the binding must never `task set claude_session_id` on a task that already names an owner',
  )
})

test('an unowned task is stamped — the ordinary case the goal requires', () => {
  const { run, calls } = fakeCli({ owner: '' })
  const res = bindSessionToTask({ task: TASK, sessionId: NEW, run })

  assert.deepEqual(res, { vault: null, action: 'stamped', owner: null })
  assert.deepEqual(writes(calls)[0], ['task', 'set', TASK, OWNER_KEY, NEW])
})

test('an unreadable owner REFUSES rather than reading as unowned', () => {
  // The failure this prevents: a transient vault-cli error defaulting to "unowned" would
  // stamp over a live owner — the exact displacement, reached by the error path instead.
  const { run, calls } = fakeCli({ read: { status: 1, stdout: '', stderr: 'vault not found' } })
  assert.deepEqual(bindSessionToTask({ task: TASK, sessionId: NEW, run }), { error: 'vault not found' })
  assert.deepEqual(writes(calls), [])
})

test('a task that does not exist REFUSES — vault-cli reports it in the BODY with exit 0', () => {
  // The shape the guard above cannot see, measured against the real CLI:
  //   $ vault-cli task get <missing> claude_session_id --output json ; echo $?
  //   {"error": "find task: find task file: <missing>: file not found", "success": false}
  //   0
  // A guard on the exit code alone reads this as UNOWNED and issues the stamping write —
  // the displacement, reached by the error path, exactly as the sibling test's comment
  // warns. The two are different shapes of the same failure and both must refuse.
  const { run, calls } = fakeCli({
    read: {
      status: 0,
      stdout: JSON.stringify({ error: 'find task: find task file: Gone: file not found', success: false }),
      stderr: '',
    },
  })
  const res = bindSessionToTask({ task: TASK, sessionId: NEW, run })
  assert.match(res.error, /file not found/)
  assert.deepEqual(writes(calls), [])
})

test('unparseable read output REFUSES', () => {
  const { run, calls } = fakeCli({ read: { status: 0, stdout: 'not json', stderr: '' } })
  const res = bindSessionToTask({ task: TASK, sessionId: NEW, run })
  assert.match(res.error, /unparseable output/)
  assert.deepEqual(writes(calls), [])
})

test('a failing write surfaces the error', () => {
  const { run } = fakeCli({ owner: OWNER, write: { status: 1, stdout: '', stderr: 'task not found' } })
  assert.deepEqual(bindSessionToTask({ task: TASK, sessionId: NEW, run }), { error: 'task not found' })
})

test('currentOwner treats an absent key as unowned', () => {
  const { run } = fakeCli({ read: { status: 0, stdout: JSON.stringify({ key: OWNER_KEY, value: '' }), stderr: '' } })
  assert.deepEqual(currentOwner({ task: TASK, run }), { owner: null })
})

test('currentOwner reads a set owner', () => {
  const { run } = fakeCli({ owner: OWNER })
  assert.deepEqual(currentOwner({ task: TASK, run }), { owner: OWNER })
})
