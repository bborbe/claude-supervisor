#!/bin/bash
# Wrapper for the supervisor MCP server: resolves the cluster and A2A bearer tokens from
# TeamVault and starts the server over stdio. Tokens travel through the environment only --
# never argv (which any process of the same user can read back out of `ps`), never a file,
# never stdout.
#
# ⚠️ stdout carries MCP frames, so EVERY line this script prints goes to stderr. A `printf`
# here without `>&2` does not log badly, it corrupts the protocol.
#
# ⚠️ The two keys are NOT interchangeable, and the asymmetry is the point. The cluster token
# is the same secret the claude-interactive service reads at startup, which is exactly why
# the supervisor borrows that variable's name (`INTERACTIVE_AUTH_TOKEN`): one value
# configures both halves. The A2A token is the worker controller's own control secret and
# has no counterparty name to borrow. Resolving one key into both variables looks configured
# and fails on one leg only.
#
# Env-first, matching server/config.mjs: a token already in the environment wins and its
# TeamVault key is not consulted, so an explicit override stays an override rather than
# being silently replaced by the cluster value.
#
# Usage: supervisor-launch.sh   (the MCP config names this as `command`, with no args)

set -euo pipefail

# stderr, always -- see the stdout note above.
log() { printf 'supervisor-launch: %s\n' "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

TEAMVAULT_CONFIG="${SUPERVISOR_TEAMVAULT_CONFIG:-${TEAMVAULT_CONFIG:-$HOME/.config/teamvault-cli/config.json}}"
TEAMVAULT_CLI="${SUPERVISOR_TEAMVAULT_CLI:-teamvault-cli}"
CLUSTER_TOKEN_KEY="${SUPERVISOR_CLUSTER_TOKEN_KEY:-VO0Gmw}"
A2A_TOKEN_KEY="${SUPERVISOR_A2A_TOKEN_KEY:-lO4j5q}"
BUN_BIN="${SUPERVISOR_BUN:-$HOME/.bun/bin/bun}"

# A mis-set key would hand one secret to both legs and read as configured, so refuse before
# spending two lookups on it.
[ "$CLUSTER_TOKEN_KEY" != "$A2A_TOKEN_KEY" ] \
  || fail "cluster and a2a resolve from the same TeamVault key ($CLUSTER_TOKEN_KEY)"

# Resolve one key to its value on stdout. The VALUE is never logged: only the key name is,
# and a TeamVault lookup key is a short `[A-Za-z0-9]{6,12}` identifier rather than a secret
# (teamvault-conventions RULE teamvault/short-alphanumeric-is-lookup-key-not-secret).
teamvault_password() {
  local key="$1" value
  command -v "$TEAMVAULT_CLI" >/dev/null 2>&1 \
    || fail "cannot run $TEAMVAULT_CLI for key $key: not found on PATH"
  # stderr stays attached so teamvault-cli's own diagnostic reaches the operator; stdout
  # carries the secret and is captured, never echoed.
  value=$("$TEAMVAULT_CLI" password \
    --teamvault-config="$TEAMVAULT_CONFIG" \
    --teamvault-key="$key") \
    || fail "TeamVault lookup failed for key $key (see the error above)"
  [ -n "$value" ] || fail "TeamVault returned an empty value for key $key"
  printf '%s' "$value"
}

if [ -z "${INTERACTIVE_AUTH_TOKEN:-}" ]; then
  INTERACTIVE_AUTH_TOKEN=$(teamvault_password "$CLUSTER_TOKEN_KEY")
  export INTERACTIVE_AUTH_TOKEN
  log "INTERACTIVE_AUTH_TOKEN resolved from TeamVault key $CLUSTER_TOKEN_KEY"
else
  log "INTERACTIVE_AUTH_TOKEN already set; TeamVault key $CLUSTER_TOKEN_KEY not consulted"
fi

if [ -z "${SUPERVISOR_A2A_TOKEN:-}" ]; then
  SUPERVISOR_A2A_TOKEN=$(teamvault_password "$A2A_TOKEN_KEY")
  export SUPERVISOR_A2A_TOKEN
  log "SUPERVISOR_A2A_TOKEN resolved from TeamVault key $A2A_TOKEN_KEY"
else
  log "SUPERVISOR_A2A_TOKEN already set; TeamVault key $A2A_TOKEN_KEY not consulted"
fi

[ -x "$BUN_BIN" ] || BUN_BIN=$(command -v bun) \
  || fail "cannot find bun (looked at \$SUPERVISOR_BUN's default, $HOME/.bun/bin/bun, and PATH)"

# Resolve the server relative to this script, so the wrapper works from the workspace
# checkout and from a plugin install alike.
SERVER_DIR="$(cd "$(dirname "$0")/../server" && pwd)"
[ -f "$SERVER_DIR/supervisor.mjs" ] \
  || fail "no server/supervisor.mjs beside this script (looked in $SERVER_DIR)"

exec "$BUN_BIN" run --cwd "$SERVER_DIR" --shell=bun --silent start "$@"
