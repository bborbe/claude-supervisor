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

// A leading `VAR=value` picks the environment the allowed program runs in, and that is
// enough to pick the program: `PATH=/tmp/evil ls` runs /tmp/evil/ls, and
// `LD_PRELOAD=/tmp/x.so ls` / `DYLD_INSERT_LIBRARIES=… ls` load attacker code into a
// genuine `ls`. So an assignment is refused rather than skipped.
const ENV_ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/

// The command's whitespace-separated tokens, or null when the command is compound or
// carries an environment assignment and is therefore unsafe to match on at all.
export function commandTokens(command) {
  if (typeof command !== 'string') return null
  if (SHELL_METACHARACTERS.test(command)) return null
  const tokens = command.trim().split(/\s+/).filter(Boolean)
  if (tokens.length === 0 || ENV_ASSIGNMENT.test(tokens[0])) return null
  return tokens
}

export function ruleMatches(rule, toolName, key, cwd) {
  if (!rule || typeof rule !== 'object') return false
  if (rule.tool !== '*' && rule.tool !== toolName) return false
  const match = rule.match ?? '*'

  // Anchored mode. `match` is a whole-token PREFIX of the command: `git status` matches
  // `git status -sb` but not `git push`, and not `git -c alias.x=!cmd status` either,
  // because `-c` is not `status`. A compound or env-prefixed command never matches.
  //
  // Only the prefix is anchored — tokens after it are unconstrained. So allow a prefix
  // only when EVERY extension of it is read-only: `git status` is safe, bare `git` is
  // not (`git push --force`), `sed -n` is not (`sed -n -i`), `find` is not (`-delete`).
  //
  // Under this mode `match: '*'` means "any single uncompounded command", NOT "anything".
  // An absent `matchType` keeps the substring behaviour below, so every rule written
  // before this existed — bundled, user, or per-spawn — evaluates exactly as it did.
  if (rule.matchType === 'command') {
    const tokens = commandTokens(key)
    if (tokens === null) return false
    if (match === '*') return true
    const prefix = match.trim().split(/\s+/).filter(Boolean)
    if (prefix.length === 0 || prefix.length > tokens.length) return false
    return prefix.every((token, i) => tokens[i] === token)
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
