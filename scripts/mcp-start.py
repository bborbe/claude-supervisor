#!/usr/bin/env python3
"""Start the supervisor MCP server under the Claude home of the session that launched it.

    mcp-start.py    # exec's: bun run --cwd <plugin>/server --shell=bun --silent start

WHY THIS EXISTS. The server refuses to boot when the `CLAUDE_CONFIG_DIR` it inherits
disagrees with its resolved Claude home (`server/config.mjs`), because such a server reads
its session registry and transcript root from a place no fleet reader looks — and it
refuses rather than warns, so the client sees only `CONNECTION_CLOSED` and the tools are
simply absent. The escape hatch the refusal itself names is `SUPERVISOR_CLAUDE_CONFIG_DIR`,
and the shipped `.mcp.json` never set it, so **every profile other than `~/.claude` was
refused by construction**: a clean `CLAUDE_CONFIG_DIR`, `--config-dir`, CI, or any other
machine's home. Measured 2026-10-14 on plugin v0.142.0 — `claude mcp list` under a fresh
profile reported `✘ Failed to connect — CONNECTION_CLOSED`, and the client's own MCP log
carried the server's stderr refusal naming the variable to set.

WHY THE DECLARATION IS MADE HERE AND NOT AS AN `env` BLOCK IN `.mcp.json`. Claude Code
substitutes an *unset* `${VAR}` reference with the LITERAL text rather than an empty
string — measured 2026-10-14, `[prefix-${SOME_VAR_THAT_IS_NOT_SET_ANYWHERE}-suffix]` arrived
verbatim in the child beside a `Missing environment variables` warning. So
`"env": {"SUPERVISOR_CLAUDE_CONFIG_DIR": "${CLAUDE_CONFIG_DIR}"}` would hand the server the
string `${CLAUDE_CONFIG_DIR}` as its home on any machine that never sets the variable —
the exact silent-wrong-home defect the refusal exists to prevent, and one the refusal
cannot catch, because a declaration is precisely what disarms it. No shell runs before the
server, so the defaulting has to live in the one process that does.

THE DECLARATION IS MADE ONLY WHEN THE AMBIENT VALUE IS SET. Unset leaves the server on its
own default (`$HOME/.claude`), which is the correct home for a session that never set the
variable; the guard does not fire, because an unset variable is not a disagreement. An
explicit `SUPERVISOR_CLAUDE_CONFIG_DIR` is never overwritten — it is the deliberate opt-in,
and a launcher that pins it must keep winning.

BUN IS RESOLVED HERE, NOT ASSUMED ON PATH. `~/.bun/bin` reaches `PATH` only from
`~/.zshrc`, and an MCP server is spawned without a login shell, so a bare `bun` command is
`ENOENT` in every session launched outside a terminal — the failure mode the install
runbook documents. Resolution is `$BUN_INSTALL/bin/bun`, then `$HOME/.bun/bin/bun`, then
`PATH`. A bun that cannot be found is reported on stderr and exits non-zero, rather than
leaving the client to report an unexplained closed connection.

TOKENS ARE RESOLVED HERE TOO, FROM TEAMVAULT. The cluster and A2A bearer tokens come from
`teamvault-cli`, because a plaintext copy in `~/.config/claude-supervisor/config.json` is the
exposure the lookup removes — `server/cluster-spawn.mjs` and `server/a2a-spawn.mjs` now REFUSE
a `cluster.token` / `a2a.token` in that file rather than read one. Only the source KEY NAME is
ever logged: a TeamVault lookup key is a short identifier rather than a secret, and the value
never reaches any stream.

⚠️ AN UNRESOLVED TOKEN MUST NOT STOP THE SERVER, and this is the one place the launcher
deliberately does NOT fail closed. This process gates every supervisor tool, not only the two
spawn targets that consume a token — exiting non-zero here would cost a session `list_agents`,
`spawn_agent(target="local")` and every manager verb, so a TeamVault outage or a missing
binary would take the supervisor away from every session at once. The server already refuses a
cluster or A2A spawn with a sentence naming the missing variable (`resolveAuthToken`,
`resolveA2AControllerTarget`), which is the right layer for it. A failure is therefore logged
here and the server starts anyway; a bun that cannot be found still exits non-zero, because
without it there is no server to start.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Resolved from this file rather than from `${CLAUDE_PLUGIN_ROOT}`: the plugin root is
# substituted into `args` by the client but is not exported to the process, so reading it
# here would depend on a variable that may simply be absent.
SERVER_DIR = Path(__file__).resolve().parent.parent / "server"


def declare_claude_home(env):
    """Declare the server's Claude home from the ambient config dir, when it is set.

    Mutates and returns `env`, and returns whether a declaration was made — so a caller can
    tell "nothing to declare" from "declared the value that was already there", two states
    a value comparison alone cannot separate.
    """
    if env.get("SUPERVISOR_CLAUDE_CONFIG_DIR"):
        return False
    ambient = env.get("CLAUDE_CONFIG_DIR")
    if not ambient:
        return False
    env["SUPERVISOR_CLAUDE_CONFIG_DIR"] = ambient
    return True


def find_bun(env, which=shutil.which):
    """The bun binary, or None. `$BUN_INSTALL`, then `$HOME/.bun`, then `PATH`."""
    candidates = []
    bun_install = env.get("BUN_INSTALL")
    if bun_install:
        candidates.append(Path(bun_install) / "bin" / "bun")
    candidates.append(Path(env.get("HOME") or Path.home()) / ".bun" / "bin" / "bun")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return which("bun")


# The tokens, and the TeamVault lookup key each resolves from. ⚠️ Distinct keys, and the
# asymmetry with the server's variable names is deliberate: the cluster token is the same secret
# the claude-interactive service reads at startup, which is why the supervisor borrows that
# variable's name (`INTERACTIVE_AUTH_TOKEN`) — one value configures both halves — while each A2A
# token is a worker controller's own control secret, with no counterparty name to borrow.
#
# ⚠️ ONE A2A TOKEN PER TARGET, because every controller is a separate deployment — the docker
# controller runs on this Mac and each k8s cluster has its own. One shared variable would force
# every deployment to carry the same secret, a constraint the single-token shape imposed
# silently. Each entry names the key its own controller was started with, so a target with no
# entry here refuses by NAME — which is the intended failure, not a gap.
#
# ⚠️ THE A2A TOKENS ARE NOW PER CLUSTER (`SUPERVISOR_A2A_TOKEN_<CLUSTER>`), 2026-10-20. The name
# is uppercased with every non-alphanumeric replaced by `_`, matching `config.mjs`'s scan, so
# `octopus-dev` reads `SUPERVISOR_A2A_TOKEN_OCTOPUS_DEV`. The old `SUPERVISOR_A2A_TOKEN_K8S` is
# still ACCEPTED by the server as the default cluster's fallback, so an entry not yet migrated
# keeps working — but nothing emits it from here any more, because two names for one secret in
# one process is how the two ends drift apart. Add a `(SUPERVISOR_A2A_TOKEN_<CLUSTER>, <key>)`
# pair here for each cluster whose controller the operator deploys.
TOKEN_KEYS = (
    ("INTERACTIVE_AUTH_TOKEN", "VO0Gmw"),
    ("SUPERVISOR_A2A_TOKEN_NUKEDEV", "lO4j5q"),
    ("SUPERVISOR_A2A_TOKEN_NUKEPROD", "zL8akq"),
    ("SUPERVISOR_A2A_TOKEN_DOCKER", "pLv8Mw"),
)


def teamvault_config(env):
    """The teamvault-cli config path, following teamvault-cli's own precedence."""
    explicit = env.get("SUPERVISOR_TEAMVAULT_CONFIG") or env.get("TEAMVAULT_CONFIG")
    if explicit:
        return explicit
    home = env.get("HOME") or Path.home()
    return str(Path(home) / ".config" / "teamvault-cli" / "config.json")


def teamvault_password(key, env, run=subprocess.run):
    """The value for `key`, or None. The VALUE is never logged — only the key is.

    A TeamVault lookup key is a short `[A-Za-z0-9]{6,12}` identifier rather than a secret, so
    naming it is what makes the resolution observable without exposing anything.
    """
    cli = env.get("SUPERVISOR_TEAMVAULT_CLI") or "teamvault-cli"
    argv = [
        cli,
        "password",
        f"--teamvault-config={teamvault_config(env)}",
        f"--teamvault-key={key}",
    ]
    try:
        result = run(argv, capture_output=True, text=True)
    except OSError as error:
        print(f"supervisor: cannot run {cli} for key {key}: {error}", file=sys.stderr)
        return None
    if result.returncode != 0:
        # teamvault-cli's own stderr names the problem and carries no secret; stdout is the
        # value, and it is not echoed even on the success path.
        print(
            f"supervisor: teamvault-cli failed for key {key}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    value = result.stdout.strip()
    if not value:
        print(f"supervisor: teamvault-cli returned an empty value for key {key}", file=sys.stderr)
        return None
    return value


def declare_tokens(env, lookup=teamvault_password):
    """Fill each token from TeamVault, and return the names filled.

    ⚠️ DEGRADES RATHER THAN REFUSES. An unresolved token leaves the server startable and the
    failure to the spawn that needs it — see the module docstring for why. An explicit value
    already in the environment is never overwritten, matching `server/config.mjs`'s
    env-first precedence, so a caller's deliberate override stays an override.
    """
    declared = []
    for name, key in TOKEN_KEYS:
        if env.get(name):
            print(f"supervisor: {name} already set; TeamVault key {key} not consulted", file=sys.stderr)
            continue
        value = lookup(key, env)
        if value is None:
            print(f"supervisor: {name} unresolved (TeamVault key {key}) — starting anyway", file=sys.stderr)
            continue
        env[name] = value
        declared.append(name)
        print(f"supervisor: {name} resolved from TeamVault key {key}", file=sys.stderr)
    return declared


def main(env=None):
    env = os.environ if env is None else env
    declare_claude_home(env)

    bun = find_bun(env)
    if bun is None:
        print(
            "supervisor: bun not found — install bun (https://bun.sh) or put it on PATH. "
            "Looked in $BUN_INSTALL/bin, $HOME/.bun/bin and PATH.",
            file=sys.stderr,
        )
        return 1

    # After the bun check, so a machine with no bun exits before spending two vault lookups.
    declare_tokens(env)

    args = [bun, "run", "--cwd", str(SERVER_DIR), "--shell=bun", "--silent", "start"]
    try:
        os.execve(bun, args, env)
    except OSError as error:
        print(f"supervisor: could not exec {bun}: {error}", file=sys.stderr)
        return 1
    return 0  # unreachable — execve replaces this process


if __name__ == "__main__":
    sys.exit(main())
