#!/usr/bin/env python3
"""Replay the five measured foreign-gate firings through `gate-owner-filter.py`.

This is the executable half of the fixture: `feed.txt`, `state/`, `registry/`, `claims.json`
and `ledger.json` are the stubs, and this starts the `--endpoint` stub, then runs
the filter twice — once for the bare feed (what would wake the manager) and once for
`--explain --json` (what it can still see, with the owner named).

Why it is a script rather than a shell pipeline: the ledger is served over HTTP, so a replay
that cannot start a stub server cannot run the fixture at all. `endpoint_fixture.py` is the
same stub the unit suite uses, so the two cannot drift.

    replay.py                        # the tree's own filter, with the vault scope
    replay.py --scope off            # no scope input at all — the PRE-FIX behaviour
    replay.py --script ~/.claude/plugins/cache/claude-supervisor/supervisor/<ver>/scripts/gate-owner-filter.py
                                     # the DEPLOYED copy (Definition of Done's replay)

⚠️ **`--scope off` omits the scope arguments entirely, and that is not cosmetic.** A copy
that predates the scope input has no `--no-scope` flag, so passing one would be an argparse
error rather than a pre-fix run — the replay would fail loudly on the wrong thing.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(os.path.dirname(HERE))
DEFAULT_SCRIPT = os.path.join(os.path.dirname(TESTS), "gate-owner-filter.py")

sys.path.insert(0, TESTS)
import endpoint_fixture  # noqa: E402

SELF = "49b471f1-a8a6-48b0-b2b2-14b9cb2437e3"
#: The five foreign panes, in the order the fixture's feed carries them.
FOREIGN = ("269", "260", "272", "278", "279")
#: The negative control: a pane spawned by `--self`, in the goal's own `# Tasks`.
CONTROL = "410"


def _ledger_rows():
    with open(os.path.join(HERE, "ledger.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _supports_no_scope(script):
    """Whether the target copy carries the `--no-scope` flag.

    ⚠️ **Omission alone does not disable the scope.** `--scope-vault` is only an OVERRIDE of
    the default source, so a run that merely omits it still reads `vault-cli`'s config — the
    live vaults — and resolves the same pages. Measured 2026-10-29: the first cut of this
    script "ran pre-fix" and printed the fixed output, because the live vault answered.
    A copy that predates the input has no flag to pass and nothing to disable; a copy that
    has it must be told explicitly.
    """
    try:
        with open(script, encoding="utf-8", errors="replace") as handle:
            return "--no-scope" in handle.read()
    except OSError:
        return False


def _command(script, scope, shape):
    argv = [
        sys.executable, script,
        "--feed", "--self", SELF,
        "--state-dir", os.path.join(HERE, "state"),
        "--registry-dir", os.path.join(HERE, "registry"),
        "--claims-file", os.path.join(HERE, "claims.json"),
    ]
    if scope:
        argv += ["--scope-vault", os.path.join(HERE, "vault")]
    elif _supports_no_scope(script):
        argv += ["--no-scope"]
    argv += shape
    return argv


def run(script=DEFAULT_SCRIPT, scope=True):
    """`{shape: CompletedProcess}` for the two shapes, plus the endpoint URL used."""
    out = {}
    with endpoint_fixture.FixtureEndpoint(ledger_rows=_ledger_rows()) as endpoint:
        for name, shape in (("feed", []), ("json", ["--explain", "--json"])):
            argv = _command(script, scope, shape)
            argv += ["--endpoint", endpoint.url]
            with open(os.path.join(HERE, "feed.txt"), encoding="utf-8") as feed:
                out[name] = subprocess.run(
                    argv, stdin=feed, capture_output=True, text=True,
                )
        out["endpoint"] = endpoint.url
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script", default=DEFAULT_SCRIPT)
    parser.add_argument("--scope", choices=("on", "off"), default="on")
    args = parser.parse_args(argv)

    result = run(args.script, scope=args.scope == "on")
    print(f"script: {args.script}")
    print(f"scope:  {args.scope}")
    print(f"ledger: {result['endpoint']}")
    for name in ("feed", "json"):
        proc = result[name]
        print(f"\n--- {name} stdout ---")
        sys.stdout.write(proc.stdout)
        print(f"--- {name} stderr ---")
        sys.stdout.write(proc.stderr)
        print(f"--- {name} exit {proc.returncode} ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
