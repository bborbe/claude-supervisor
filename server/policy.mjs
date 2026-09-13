// Pure policy evaluation, split out of supervisor.mjs so it can be tested without
// importing the server (importing supervisor.mjs starts the MCP server, which makes
// every function in it untestable in-process).
//
// Nothing here reads the filesystem, the environment, or the clock.

// The value a rule matches against. Raw tool input is useless for mining — every
// Write differs — so normalize to the one field a rule could actually name.
export function inputKey(input) {
  if (!input || typeof input !== 'object') return ''
  return input.file_path ?? input.path ?? input.command ?? input.pattern ?? input.url ?? ''
}

export function ruleMatches(rule, toolName, key, cwd) {
  if (!rule || typeof rule !== 'object') return false
  if (rule.tool !== '*' && rule.tool !== toolName) return false
  const match = rule.match ?? '*'
  if (match === '*') return true
  if (match === 'cwd') {
    // Compare on a path BOUNDARY, not a raw string prefix. `/work/repo-2/x` starts
    // with `/work/repo` but is a sibling directory, not a child of it — a plain
    // startsWith would silently allow writes outside the intended cwd.
    if (!key || !cwd) return false
    const base = cwd.endsWith('/') ? cwd : `${cwd}/`
    return key === cwd || key.startsWith(base)
  }
  return key.includes(match)
}

// First matching rule wins; the caller orders user rules before bundled ones so the
// overlay semantics live at the call site rather than in here.
export function decide(rules, toolName, input, cwd) {
  const key = inputKey(input)
  for (const rule of rules ?? []) {
    if (ruleMatches(rule, toolName, key, cwd)) return { action: rule.action, key, rule }
  }
  return { action: 'escalate', key, rule: null }
}
