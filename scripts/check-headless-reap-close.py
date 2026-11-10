#!/usr/bin/env python3
r"""A finished headless worker is closed by the round, not merely messaged.

Measured 2026-11-10: session `3702c7ae` sat idle 45 minutes after its task read
`completed` / `done` / zero open boxes and its PR had merged. `close-worker.py --dry-run`
answered `✅ would close pid 5229 (entrypoint sdk-cli, no pane)` — the mechanism worked and
nothing invoked it. The round's reap leg drafted a message telling the worker to run
`/vault-cli:session-close` + `/exit`, which cannot work: vault-cli's `session-self-exit.py`
closes only the session's own WezTerm pane, proven by tty match, and an `entrypoint: sdk-cli`
worker has no controlling tty, so it always answers `SELF-EXIT: stay`. Only
`/supervisor:fleet-triage` and a hand-run `/supervisor:close` called `close-worker.py`.

This gate keeps the fix in place. Five assertions, all required:

  (a) `scripts/close-worker.py` exists on disk. A prose mention is satisfied by a name
      alone, and a round that shells out to a script that is not there dies at runtime
      with `No such file` while every mention count stays green. The same (d)-before-(a)
      lesson `check-drive-ledger-writes.py` records.

  (b) `agents/fleet-drive.md` names `close-worker.py` at least TWICE — the `--dry-run`
      tier read and the real close — and names the `sdk-cli` entrypoint. One mention is
      the pre-fix state with a comment added.

  (c) `agents/goal-drive.md` names `close-worker.py` and `sdk-cli`: the goal sweep's reap
      has the same gap, and a fix that lands only in the fleet leg leaves every goal
      manager's finished headless workers immortal.

  (d) `commands/fleet-drive.md` still carries a close rule that names `close-worker.py`
      and no longer carries the retired literal `Close via \`/supervisor:close\` only.`
      — the INLINE rule the pre-fix round enforced.

  (e) `commands/steps/reap.md` names `close-worker.py`, so the reap contract's
      "evidence plus the session's own close" carries the headless carve-out rather than
      contradicting the leg that now closes.

⚠️ **What this does not prove.** It reads what the files *say*. A round that never runs
the dry-run, or a `close-worker.py` whose guards regress, passes here and fails at
runtime — that half is behavioural, and `scripts/tests/test_close_worker.py` is where the
guards themselves are exercised (headless close, and every refusal).
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SCRIPT = "close-worker.py"
DRIVE = "agents/fleet-drive.md"
GOAL_DRIVE = "agents/goal-drive.md"
DRIVE_CMD = "commands/fleet-drive.md"
REAP = "commands/steps/reap.md"

#: The `--dry-run` tier read and the real close are two separate calls; one mention is
#: the pre-fix text with a name dropped into it.
MIN_DRIVE_MENTIONS = 2

#: The retired INLINE rule. Its absence is the assertion, so it is spelled exactly as the
#: pre-fix command carried it.
RETIRED_RULE = "Close via `/supervisor:close` only."


def read(rel):
    path = REPO / rel
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def main():
    failures = []

    # (a) the callee exists — checked first, because a mention count cannot tell a
    # working call from a name.
    if not (REPO / "scripts" / SCRIPT).exists():
        failures.append(
            "scripts/%s does not exist — every leg below names a script the round would "
            "run and fail to find" % SCRIPT
        )

    # (b) the fleet drive leg: dry-run + real close + the headless entrypoint.
    drive = read(DRIVE)
    if drive is None:
        failures.append("%s does not exist — the fleet reap leg has no body" % DRIVE)
    else:
        mentions = drive.count(SCRIPT)
        if mentions < MIN_DRIVE_MENTIONS:
            failures.append(
                "%s names `%s` %d time(s), fewer than the %d a tier read and a real close "
                "need — a finished headless row is being messaged, not closed"
                % (DRIVE, SCRIPT, mentions, MIN_DRIVE_MENTIONS)
            )
        if "sdk-cli" not in drive:
            failures.append(
                "%s never names `sdk-cli` — the leg cannot distinguish a headless row from "
                "a tab one, so it cannot know which close to run" % DRIVE
            )

    # (c) the goal drive leg mirrors it.
    goal = read(GOAL_DRIVE)
    if goal is None:
        failures.append("%s does not exist — the goal reap leg has no body" % GOAL_DRIVE)
    else:
        if SCRIPT not in goal:
            failures.append(
                "%s never names `%s` — a goal manager's finished headless workers keep "
                "their spawn-cap slot forever" % (GOAL_DRIVE, SCRIPT)
            )
        if "sdk-cli" not in goal:
            failures.append(
                "%s never names `sdk-cli` — the tier read cannot be applied there" % GOAL_DRIVE
            )

    # (d) the round's INLINE rule: names the mechanism, dropped the retired literal.
    cmd = read(DRIVE_CMD)
    if cmd is None:
        failures.append("%s does not exist" % DRIVE_CMD)
    else:
        if SCRIPT not in cmd:
            failures.append(
                "%s never names `%s` — the round's close rule still points only at "
                "`/supervisor:close`" % (DRIVE_CMD, SCRIPT)
            )
        if RETIRED_RULE in cmd:
            failures.append(
                "%s still carries the retired rule %r — it reads as forbidding the close "
                "the drive leg now runs" % (DRIVE_CMD, RETIRED_RULE)
            )

    # (e) the reap contract's carve-out.
    reap = read(REAP)
    if reap is None:
        failures.append("%s does not exist" % REAP)
    elif SCRIPT not in reap:
        failures.append(
            "%s never names `%s` — the reap contract still says the close is never a kill, "
            "contradicting the leg that closes a headless row" % (REAP, SCRIPT)
        )

    if failures:
        print("check-headless-reap-close: FAIL", file=sys.stderr)
        for failure in failures:
            print("  - %s" % failure, file=sys.stderr)
        return 1

    print(
        "headless-reap-close ok: scripts/%s exists, %s names it %d time(s) and `sdk-cli`, "
        "%s names it, %s carries the rule without the retired literal, and %s carries the "
        "carve-out"
        % (SCRIPT, DRIVE, drive.count(SCRIPT), GOAL_DRIVE, DRIVE_CMD, REAP)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
