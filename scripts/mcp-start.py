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
"""

import os
import shutil
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

    args = [bun, "run", "--cwd", str(SERVER_DIR), "--shell=bun", "--silent", "start"]
    try:
        os.execve(bun, args, env)
    except OSError as error:
        print(f"supervisor: could not exec {bun}: {error}", file=sys.stderr)
        return 1
    return 0  # unreachable — execve replaces this process


if __name__ == "__main__":
    sys.exit(main())
