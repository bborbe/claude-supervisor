#!/usr/bin/env python3
"""The fleet-wide worker target, resolved by the code that enforces it.

A manager tick compares a live count against this number, and until now only **one half of
that comparison had an instrument**. The count has `scripts/worker-sessions.py --count`; the
target had a *pointer* — `docs/fleet-surface.md` § Spawn a worker item 5 — which states where
the value lives and what it defaults to, but is not a read. So a manager measured one side of
the comparison every tick and remembered the other.

Measured 2026-10-02. Manager Layer read `spawn.maxConcurrent` by hand **once**, at 09:36
local, on its pre-spawn cap check, and carried `18` from then on. The key was changed to `12`
twice afterwards (10:31 and 11:05); its 11:38 tick still read *"Workers 14 < 18 … the
under-target branch fired"*, citing the number as *"per the checkpoint"*. Its own drive leg
then **derived** the config from that assertion — *"So the fleet config sets maxConcurrent
18"* — and never read it either. A target set specifically to make a manager suppress its
card therefore left it posting one, and nothing anywhere reported a disagreement.

⚠️ **The number is not restated here, and must not be.** This script does not reimplement the
resolution — it calls `resolveMaxConcurrent` in `server/spawn-mode.mjs`, the same function
`supervisor.mjs` enforces at spawn time, so the target a manager reads and the cap the server
applies cannot drift apart. A Python reimplementation would be a second counter, which is the
failure `scripts/check-worker-target.py` exists to refuse.

  (no flag)  the resolved target, and nothing else on stdout — an integer, or `unlimited`
             when the key is `0`. `$(…)` goes straight into a comparison.
  --source   `<target> <source>`, where source is `default` | `config` | `env`: which source
             decided the number. That is the diagnostic whose absence cost the run above —
             a bare `18` cannot be told from a bare `12`.

⚠️ **An unreadable or invalid config is UNKNOWN, never the default.** A manager that cannot
read the target must not silently compare against 20, because that is a number nobody chose
reported as though it were configured — and it is the direction that reads as healthy. A
**missing** config file is not that case: no file means no key, which resolves to the
documented default, exactly as the server treats it. The two are kept apart here because
collapsing them is how a permissions error becomes a confident fleet-wide limit.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Resolved the way `server/config.mjs` does — `XDG_CONFIG_HOME`, then `SUPERVISOR_CONFIG`
# over it. A reader that resolved a different path than the server would report the default
# while the operator's file said otherwise, and the default is the answer that reads as
# healthy. `SUPERVISOR_MAX_CONCURRENT` still wins over both, exactly as it does at spawn time.
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
CONFIG = os.environ.get("SUPERVISOR_CONFIG") or os.path.join(
    CONFIG_HOME, "claude-supervisor", "config.json"
)

# Calls the real resolver rather than restating it. `spawn-mode.mjs` imports nothing and
# reads nothing — its own header says so — which makes this a pure call, the same one
# `supervisor.mjs` makes per spawn.
RESOLVE = (
    "import { readFileSync } from 'node:fs';"
    "import { resolveMaxConcurrent } from './spawn-mode.mjs';"
    "const path = process.env.WORKER_TARGET_CONFIG;"
    "let file = {}, readError = null;"
    "try { file = JSON.parse(readFileSync(path, 'utf8')); }"
    "catch (e) {"
    "  if (e.code === 'ENOENT') { file = {}; }"
    "  else { readError = e.message; }"
    "}"
    "if (readError) { console.log(JSON.stringify({ readError })); }"
    "else {"
    "  const r = resolveMaxConcurrent({"
    "    env: process.env.SUPERVISOR_MAX_CONCURRENT,"
    "    file,"
    "    path,"
    "  });"
    "  console.log(JSON.stringify("
    "    r.error ? { resolveError: r.error } : { limit: r.limit, source: r.source }));"
    "}"
)


def resolve():
    """`(limit, source)` — or None when the config could not be read or the value is invalid.

    `limit` is None for unlimited, which is the shape `resolveMaxConcurrent` returns for an
    explicit `0` and the only way left to reach it.
    """
    env = dict(os.environ, WORKER_TARGET_CONFIG=CONFIG)
    try:
        out = subprocess.run(
            ["node", "--input-type=module", "-e", RESOLVE],
            capture_output=True, text=True, cwd=os.path.join(REPO, "server"), env=env,
        )
    except OSError as error:
        print(f"UNKNOWN — cannot run node to resolve the target: {error}", file=sys.stderr)
        return None

    try:
        facts = json.loads(out.stdout.strip())
    except ValueError:
        # Never let a broken probe read as a value: the last stderr line is the reason, and
        # a caller that sees exit 0 here would compare against nothing.
        detail = (out.stderr.strip().splitlines() or ["(no output)"])[-1]
        print(f"UNKNOWN — resolving the target produced no answer: {detail}", file=sys.stderr)
        return None

    if facts.get("readError"):
        print(f"UNKNOWN — cannot read {CONFIG}: {facts['readError']}", file=sys.stderr)
        return None
    if facts.get("resolveError"):
        print(f"UNKNOWN — {facts['resolveError']}", file=sys.stderr)
        return None
    return facts["limit"], facts["source"]


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        action="store_true",
        help="print `<target> <source>` instead of the target alone",
    )
    args = parser.parse_args(argv)

    resolved = resolve()
    if resolved is None:
        return 2
    limit, source = resolved

    # `unlimited` is the token `commands/fleet-loop.md` already prints on its marker line for
    # a `0` key. The two surfaces say the same word for the same state rather than one
    # printing `0` and the other `unlimited`.
    target = "unlimited" if limit is None else str(limit)
    print(f"{target} {source}" if args.source else target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
