// Read the `launcher:` frontmatter of a task and of each of its goals.
//
// Split out of supervisor.mjs so it can be tested: that module connects a stdio server at
// load and cannot be imported by a test. `run` is injectable, mirroring task-binding.mjs.
//
// Precedence lives in `resolveTaskLauncher` (spawn-cwd.mjs); this only reads. A read that
// FAILS is an error rather than "no field": reading it as absent would open a task that
// asked for Claude on the vault default — the silent wrong launcher this server refuses
// everywhere else. An EMPTY value is a real answer: the field is unset.
//
// ⚠️ The exit code is NOT the whole error signal. Measured against the real CLI (see
// task-binding.mjs `currentOwner`): a task that does not exist answers
// `{"error": "...file not found", "success": false}` with **exit 0**. So every answer is
// checked for its expected shape — `value` a string, `goals` an array — and anything else
// is an error. A `?? ''` here turned a missing task into "field unset".

import { runVaultCli } from './task-binding.mjs'

export function readTaskLaunchers({ task, vault, run = runVaultCli }) {
  const call = (args) => {
    const res = run([...args, '--vault', vault, '--output', 'json'])
    const what = `vault-cli ${args.slice(0, 2).join(' ')} ${JSON.stringify(args[2])}`
    if (res.error) throw new Error(`${what}: ${res.error}`)
    if (res.status !== 0) throw new Error(`${what} exited ${res.status}: ${(res.stderr || res.stdout || '').trim()}`)
    try {
      return JSON.parse(res.stdout)
    } catch {
      throw new Error(`${what} returned unparseable output: ${String(res.stdout).trim()}`)
    }
  }
  const field = (parsed, what) => {
    if (typeof parsed?.value !== 'string') throw new Error(`${what}: ${parsed?.error ?? 'no value in answer'}`)
    return parsed.value.trim()
  }

  try {
    const taskLauncher = field(call(['task', 'get', task, 'launcher']), `task ${JSON.stringify(task)}`)
    // A task naming its own launcher wins outright, so its goals are never read — a broken
    // goal must not refuse a spawn the precedence says should open.
    if (taskLauncher) return { taskLauncher, goalLaunchers: [], warnings: [] }

    const shown = call(['task', 'show', task])
    if (!Array.isArray(shown?.goals)) {
      // `goals` is omitted when the task has none; an error envelope carries `error`.
      if (shown?.error || shown?.success === false) throw new Error(`task ${JSON.stringify(task)}: ${shown.error ?? 'show failed'}`)
      return { taskLauncher: '', goalLaunchers: [], warnings: [] }
    }
    const goals = shown.goals.map((g) => String(g).replace(/^\[\[|\]\]$/g, '').trim()).filter(Boolean)
    // A goal link that does not resolve is SKIPPED with a warning, never an error: stale goal
    // links are a normal vault state, and refusing on one would stop every task carrying it
    // from opening at all. Same rule as vault-cli's `ops.ResolveTaskLauncher`.
    const goalLaunchers = []
    const warnings = []
    for (const g of goals) {
      try {
        goalLaunchers.push(field(call(['goal', 'get', g, 'launcher']), `goal ${JSON.stringify(g)}`))
      } catch (error) {
        warnings.push(`goal ${JSON.stringify(g)} skipped for launcher resolution: ${error.message}`)
      }
    }
    return { taskLauncher: '', goalLaunchers, warnings }
  } catch (error) {
    return {
      error:
        `could not read the \`launcher:\` field of task ${JSON.stringify(task)} or its goals in vault ` +
        `${JSON.stringify(vault)} — refusing rather than opening it on the vault default: ${error.message}`,
    }
  }
}
