#!/usr/bin/env python3
"""Does THIS session have an answer path for a headless worker's parked prompt?

A headless worker's permission gates park with the session that spawned it — no other
session can answer them (`docs/fleet-surface.md` § Spawn a worker item 6, constraint (a)).
A spawner that cannot answer must not open one, because the prompt outlives the spawner
and dies on the 15-minute deny.

⚠️ **This adds no new counter — it asks the instrument the fleet already runs.**
`scripts/manager-liveness.py --state` already answers "is this subject's manager loop
alive?" from evidence the manager leaves on disk at every re-arm. A second lock file
written by a second writer would be exactly the two-counters defect this repo refuses
everywhere else: the day one writer changes, the two disagree with no error on either.

⚠️ **The subject comes from the session's own registration, not from an argument.**
`~/.claude/state/worker-manager/<session-id>.json` carries `subject`, `branch` and
`vault` — written by the tier resolution that already decides which manager a session
is. Reading it here means the check and the tier cannot disagree about who this
session is. A session with no registration is not a manager, so it has no answer path
of its own to offer.

⚠️ **EVERY branch refuses.** No registration, an unreadable registration, a missing or
broken `manager-liveness.py`, and any state that is not `OK` all refuse. A check that
failed open would open a headless worker whose every gate dies unanswered, which is the
2026-09-20 stranding this rule exists to prevent.

Exit 0 = an answer path is running. Exit 1 = refuse, with the reason on stdout.

⚠️ **The OK branch is UNPROVEN from a worker session, and that is recorded rather than
hidden.** Measured 2026-10-23 over every registration in
`~/.claude/state/worker-manager/` (106 of them): **52 STOPPED, 36 STALE, 18 UNARMED,
0 OK**. So the refuse direction is exercised on live data and the pass direction has no
live positive to point at. Proving it needs a manager session whose loop is genuinely
armed — a worker may not arm one (`docs/session-tiers.md`: a worker opens nothing), so
the control belongs to the manager that runs the SC2 probe.

⚠️ **That measurement is also a rollout constraint worth stating where the detector
lives.** If the refusal is wired to this check before any manager loop is armed, it
refuses **every** headless spawn, which would hold SC5(c)'s headless share at its
baseline instead of raising it. The refusal and the arming have to land together.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIVENESS = os.path.join(HERE, "manager-liveness.py")
MANAGER_DIR = os.path.expanduser(
    os.environ.get("WORKER_MANAGER_STATE_DIR", "~/.claude/state/worker-manager")
)


def registration(session_id):
    """(subject, branch) for this session, or (None, reason)."""
    path = os.path.join(MANAGER_DIR, f"{session_id}.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None, (
            f"no manager registration at {path} — this session is not a manager, so it "
            f"has no answer path to serve a headless worker's parked prompt"
        )
    except (OSError, ValueError) as err:
        return None, f"{path} is unreadable ({err}) — cannot prove an answer path exists"
    subject = data.get("subject")
    branch = data.get("branch")
    if not subject or not branch:
        return None, f"{path} carries no subject/branch — cannot name the loop to check"
    return (subject, branch), None


def liveness_state(subject, branch):
    """The subject's manager-liveness state word, or (None, reason)."""
    try:
        done = subprocess.run(
            [sys.executable, LIVENESS, "--state", "--subject", subject, "--branch", branch],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return None, f"manager-liveness.py could not run ({err})"
    # `--state` exits 0 whatever the state: the state IS the answer.
    if done.returncode != 0:
        return None, (
            f"manager-liveness.py --state exited {done.returncode}: "
            f"{(done.stderr or done.stdout).strip()[:200]}"
        )
    first = (done.stdout or "").strip().split("\n")[0]
    if not first:
        return None, "manager-liveness.py --state printed nothing"
    state = first.split("\t")[0].strip()
    return state, None


def check(session_id):
    """(ok, subject, branch, detail) — never raises."""
    found, reason = registration(session_id)
    if found is None:
        return False, None, None, reason
    subject, branch = found
    state, reason = liveness_state(subject, branch)
    if state is None:
        return False, subject, branch, reason
    if state != "OK":
        return False, subject, branch, (
            f"this session's manager loop on {branch} '{subject}' reads {state}, not OK — "
            f"nothing is armed to answer a headless worker's parked prompt"
        )
    return True, subject, branch, f"{branch} '{subject}' loop is OK"


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--session-id",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        help="defaults to $CLAUDE_CODE_SESSION_ID",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.session_id:
        # The JSON envelope is emitted here too, not only on the branches below: a caller
        # that passed --json must be able to read every refusal the same way, and a plain
        # line would reach it as "nothing parseable" — still a refusal, but one whose reason
        # the caller cannot quote.
        detail = "no session id (pass --session-id or set $CLAUDE_CODE_SESSION_ID)"
        if args.json:
            print(json.dumps(
                {"ok": False, "subject": None, "branch": None, "detail": detail},
                ensure_ascii=True,
            ))
        else:
            print(f"REFUSED\t{detail}")
        return 1

    ok, subject, branch, detail = check(args.session_id)
    if args.json:
        print(json.dumps(
            {"ok": ok, "subject": subject, "branch": branch, "detail": detail},
            ensure_ascii=True,
        ))
    else:
        print(f"{'OK' if ok else 'REFUSED'}\t{detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
