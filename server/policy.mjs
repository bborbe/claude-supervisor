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

// The shell splits words on space and tab only. JavaScript's `\s` also matches U+00A0
// and other Unicode spaces, which bash keeps INSIDE a word — so `ls<U+00A0>x` would read
// as `ls` here while bash looks up a program literally named `ls<U+00A0>x`. Refusing any
// other whitespace keeps this tokenizer and the shell in agreement.
const NON_SHELL_WHITESPACE = /[^\S \t]/

// The command's space/tab-separated tokens, or null when the command is compound,
// carries an environment assignment, or contains whitespace the shell would not split
// on — any of which makes it unsafe to match on at all.
export function commandTokens(command) {
  if (typeof command !== 'string') return null
  if (SHELL_METACHARACTERS.test(command)) return null
  if (NON_SHELL_WHITESPACE.test(command)) return null
  const tokens = command.trim().split(/[ \t]+/).filter(Boolean)
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
  // only when EVERY extension of it is read-only: `ls` is, `sed -n` is not (`sed -n -i`),
  // `find` is not (`-delete`). No `git` prefix is: git runs commands named in the repo's
  // own config (`core.fsmonitor` fires on `git status`), and the bundled policy lets a
  // worker edit `.git/config` in its cwd — so an allowed git subcommand is code execution.
  //
  // Under this mode `match: '*'` means "any single uncompounded command", NOT "anything".
  // An absent `matchType` keeps the substring behaviour below, so every rule written
  // before this existed — bundled, user, or per-spawn — evaluates exactly as it did.
  if (rule.matchType === 'command') {
    if (typeof match !== 'string') return false
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

// Stderr discards that change no file and run nothing. Only these exact tokens are
// dropped before a segment is tokenized; any other redirect still reaches
// commandTokens and refuses the segment.
const HARMLESS_REDIRECTS = new Set(['2>/dev/null', '2>&1'])

// The segments of a chained command — split on `&&`, `||`, `;` and `|` — each with its
// harmless redirects removed, or null when any segment is empty. Splitting ignores
// quoting on purpose: a separator inside quotes only yields extra, stranger segments,
// each of which must still pass the anchored match on its own, so mis-splitting can
// refuse a command but never allow one the shell would run differently.
export function commandSegments(command) {
  if (typeof command !== 'string') return null
  const segments = command.split(/&&|\|\||;|\|/).map((segment) =>
    segment.split(/[ \t]+/).filter((token) => !HARMLESS_REDIRECTS.has(token)).join(' ').trim())
  if (segments.length < 2 || segments.some((segment) => segment === '')) return null
  return segments
}

// Is the rule that decided the whole command just the generic fallthrough? Only that
// verdict may be revisited segment by segment; a rule that named the command — a deny,
// or an escalate the owner wrote on purpose — always stands.
function isCatchAll(rule) {
  return rule === null || (rule.match === '*' && rule.matchType === undefined && rule.action === 'escalate')
}

// A chained Bash command is allowed when EVERY segment, decided alone, is allowed by an
// anchored (`matchType: 'command'`) rule. Substring allows never count here: they are
// the rules composition smuggles past, which is why the anchored mode exists.
function allSegmentsAnchoredAllow(rules, toolName, key, cwd) {
  const segments = commandSegments(key)
  if (segments === null) return false
  return segments.every((segment) => {
    const { action, rule } = decideWhole(rules, toolName, segment, cwd)
    return action === 'allow' && rule?.matchType === 'command'
  })
}

function decideWhole(rules, toolName, key, cwd) {
  for (const rule of rules ?? []) {
    if (ruleMatches(rule, toolName, key, cwd)) return { action: rule.action, key, rule }
  }
  return { action: 'escalate', key, rule: null }
}

// First matching rule wins; the caller orders user rules before bundled ones so the
// overlay semantics live at the call site rather than in here. A Bash command that only
// the catch-all escalated gets a second look segment by segment.
export function decide(rules, toolName, input, cwd) {
  const key = inputKey(input)
  const whole = decideWhole(rules, toolName, key, cwd)
  if (toolName === 'Bash' && whole.action === 'escalate' && isCatchAll(whole.rule)
      && allSegmentsAnchoredAllow(rules, toolName, key, cwd)) {
    return { action: 'allow', key, rule: { tool: 'Bash', match: 'segments', matchType: 'segments', action: 'allow' } }
  }
  return whole
}
