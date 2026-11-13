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
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.dirname(TESTS)
DEFAULT_SCRIPT = os.path.join(SCRIPTS, "gate-owner-filter.py")

sys.path.insert(0, TESTS)
import endpoint_fixture  # noqa: E402

SELF = "49b471f1-a8a6-48b0-b2b2-14b9cb2437e3"
#: The five foreign panes, in the order the fixture's feed carries them.
FOREIGN = ("269", "260", "272", "278", "279")
#: The goal this manager serves — the page the vault fixture stamps `SELF` on.
GOAL = "Managers Unblock Their Own Workers and Approve Routine Permissions Within Policy"
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


def _owner_env(root, stopped=False):
    """Environment making `SELF` an OWNER — session `LIVE` **and** loop `OK`.

    ⚠️ **Ownership is resolved by `scripts/owner.py` now (`docs/session-tiers.md` § Keep-alive),
    so the fixture must model the FACT rather than assume it.** Before that, a page stamp was
    enough; a manager that ran `/supervisor:stop` keeps its stamp while its round has stopped,
    and a scope applied on the stamp alone hides gates the manager no longer serves. The filter
    therefore asks the resolver about `--self` first, and a fixture that left this unset would
    exercise the stood-down path by accident and see every foreign row emitted.

    The three variables are the seams `owner.py` already reads, so the child resolves ownership
    through the real code path — no verdict is stubbed:

      * `SUPERVISOR_WORKER_MANAGER_DIR` → the manager's own registration, which names the
        subject its loop is read under;
      * `MANAGER_LIVENESS_STATE_DIR` → a freshly armed cadence for that subject, which is what
        `manager-liveness.py --state` reads `OK` from;
      * `ATTENTION_STORE_URL` → the stub heartbeat, which answers `LIVE` for `SELF` (set by the
        caller, since it needs the endpoint's URL).

    ⚠️ **`stopped=True` is the negative control, and it is what makes the state load-bearing.**
    It writes the `.stopped` marker instead of an armed cadence, so the same session and the
    same stamp resolve `not-owner` — and the filter must then exclude NOTHING. A fixture that
    could not express this would pass just as happily if the ownership gate were deleted.
    """
    sweep = os.path.join(root, "sweep-gate")
    managers = os.path.join(root, "worker-manager")
    os.makedirs(sweep, exist_ok=True)
    os.makedirs(managers, exist_ok=True)

    # The slug carries the branch (`goal--<base>`), and it is taken from
    # `manager-liveness.py`'s own function so the fixture cannot drift from the reader.
    spec = importlib.util.spec_from_file_location(
        "manager_liveness", os.path.join(SCRIPTS, "manager-liveness.py"))
    liveness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(liveness)
    slug = liveness.slug(GOAL, "goal")
    if stopped:
        with open(os.path.join(sweep, slug + ".stopped"), "w", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    else:
        with open(os.path.join(sweep, slug + ".cadence"), "w", encoding="utf-8") as handle:
            handle.write("900\n")

    with open(os.path.join(managers, SELF + ".json"), "w", encoding="utf-8") as handle:
        json.dump({"subject": GOAL, "branch": "goal",
                   "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, handle)

    env = dict(os.environ)
    env["MANAGER_LIVENESS_STATE_DIR"] = sweep
    env["SUPERVISOR_WORKER_MANAGER_DIR"] = managers
    return env


def run(script=DEFAULT_SCRIPT, scope=True, stopped=False):
    """`{shape: CompletedProcess}` for the two shapes, plus the endpoint URL used."""
    out = {}
    with tempfile.TemporaryDirectory() as root, \
            endpoint_fixture.FixtureEndpoint(
                rows=[endpoint_fixture.row(SELF, live=True)],
                ledger_rows=_ledger_rows(),
            ) as endpoint:
        env = _owner_env(root, stopped=stopped)
        env["ATTENTION_STORE_URL"] = endpoint.url
        for name, shape in (("feed", []), ("json", ["--explain", "--json"])):
            argv = _command(script, scope, shape)
            argv += ["--endpoint", endpoint.url]
            with open(os.path.join(HERE, "feed.txt"), encoding="utf-8") as feed:
                out[name] = subprocess.run(
                    argv, stdin=feed, capture_output=True, text=True, env=env,
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
