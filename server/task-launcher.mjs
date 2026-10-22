// Read the `launcher:` frontmatter of a task.
//
// Split out of supervisor.mjs so it can be tested: that module connects a stdio server at
// load and cannot be imported by a test. `run` is injectable, mirroring task-binding.mjs.
//
// ⚠️ A goal's `launcher:` is deliberately NOT read here. It is a MANAGER statement — managers
// resolve through the subject's `manager_launcher:` and never through this field (operator
// decision 2026-10-19) — so letting it reach a worker handed every task under a goal its
// manager's Opus launcher. Measured 2026-10-22: 12 goals in private-personal carried
// `launcher: cc-private-claude`, and every worker beneath them ran Opus. The worker default now
// comes from the vault's config (`worker_launcher`); see `resolveTaskLauncher` in spawn-cwd.mjs.
//
// Precedence lives in `resolveTaskLauncher` (spawn-cwd.mjs); this only reads. A read that
// FAILS is an error rather than "no field": reading it as absent would open a task that
// asked for Claude on the vault default — the silent wrong launcher this server refuses
// everywhere else. An EMPTY value is a real answer: the field is unset.
//
// ⚠️ The exit code is NOT the whole error signal. Measured against the real CLI (see
// task-binding.mjs `currentOwner`): a task that does not exist answers
// `{"error": "...file not found", "success": false}` with **exit 0**. So every answer is
// checked for its expected shape — `value` a string — and anything else is an error. A `?? ''`
// here turned a missing task into "field unset".

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
    return { taskLauncher }
  } catch (error) {
    return {
      error:
        `could not read the \`launcher:\` field of task ${JSON.stringify(task)} in vault ` +
        `${JSON.stringify(vault)} — refusing rather than opening it on the vault default: ${error.message}`,
    }
  }
}
