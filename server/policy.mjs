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

// Anything that lets a SECOND command ride along on an allowed one, plus redirection,
// which clobbers a file without running anything extra. A substring rule cannot see
// these: `match: 'ls '` is contained in `rm -rf ~/Documents && ls `, so an allow written
// that way is a universal bypass — append the allowed text to any command.
//
// Deliberately over-refuses: `grep ';' file` is harmless and still rejected, because the
// alternative is parsing a shell, and a false refusal costs one escalation while a false
// allow costs the filesystem.
const SHELL_METACHARACTERS = /[;&|`<>\n(){}]/

// The command's argv0, or null when the command is compound and therefore unsafe to
// match on. Leading `VAR=value` assignments are skipped, so `BRANCH=dev make buca`
// reports `make` rather than the assignment.
export function commandHead(command) {
  if (typeof command !== 'string' || command.length === 0) return null
  if (SHELL_METACHARACTERS.test(command)) return null
  const tokens = command.trim().split(/\s+/)
  let i = 0
  while (i < tokens.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(tokens[i])) i += 1
  return tokens[i] ?? null
}

export function ruleMatches(rule, toolName, key, cwd) {
  if (!rule || typeof rule !== 'object') return false
  if (rule.tool !== '*' && rule.tool !== toolName) return false
  const match = rule.match ?? '*'

  // Anchored mode. `match` names the command itself, and a compound command never
  // matches at all — which is what makes an `allow` safe to write here. Under this mode
  // `match: '*'` means "any single uncompounded command", NOT "anything".
  // An absent `matchType` keeps the substring behaviour below, so every rule written
  // before this existed — bundled, user, or per-spawn — evaluates exactly as it did.
  if (rule.matchType === 'command') {
    const head = commandHead(key)
    if (head === null) return false
    return match === '*' || head === match
  }

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

// A per-spawn override, placed ahead of the server's rules. Overlay rather than replace:
// first match wins, so a rule here beats any later one, while the bundled set still
// covers whatever the override does not name — a permissive override must not silently
// drop the `rm -rf` deny along with it. Full replacement stays reachable: end the
// override file with a `{"tool":"*","match":"*","action":"escalate"}` catch-all, which
// then matches before the bundled rules ever get a turn.
export function overlayRules(override, base) {
  return [...(override ?? []), ...(base ?? [])]
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
