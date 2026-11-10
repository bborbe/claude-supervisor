#!/usr/bin/env python3
r"""A finished headless worker is closed by the fleet round, not merely messaged.

Measured 2026-11-10: session `3702c7ae` sat idle 45 minutes after its task read
`completed` / `done` / zero open boxes and its PR had merged. `close-worker.py --dry-run`
answered `✅ would close pid 5229 (entrypoint sdk-cli, no pane)` — the mechanism worked and
nothing invoked it. The round's reap leg drafted a message telling the worker to run
`/vault-cli:session-close` + `/exit`, which cannot work: vault-cli's `session-self-exit.py`
closes only the session's own WezTerm pane, proven by tty match, and an `entrypoint: sdk-cli`
worker has no controlling tty, so it always answers `SELF-EXIT: stay`. Only
`/supervisor:fleet-triage` and a hand-run `/supervisor:close` called `close-worker.py`.

⚠️ **The read runs in the COMMAND, not the drive agent.** `agents/fleet-drive.md`'s
`allowed-tools` grants no `Bash(python3:*)` — the measured split in
`docs/blocked-row-reread.md` § Who runs the probe, and the reason the caller does the I/O
and the agent renders. So the tier read and the close live in `commands/steps/drive.md`;
a rule telling the agent to run them would execute in no layer. `goal-drive` is different:
its `allowed-tools` does grant `Bash(python3:*)`, so its reap step owns the read itself.

Five assertions, all required:

  (a) `scripts/close-worker.py` exists on disk. A prose mention is satisfied by a name
      alone, and a round that shells out to a script that is not there dies at runtime
      with `No such file` while every mention count stays green.

  (b) `commands/steps/drive.md` names `close-worker.py` at least TWICE — the `--dry-run`
      tier read and the real close — and names the `sdk-cli` entrypoint. This is the
      caller that holds `python3`.

  (c) `agents/fleet-drive.md` names `close-worker.py` and `commands/steps/drive.md` and
      `sdk-cli`: the agent must point at the caller that closes, not run the read itself.

  (d) `agents/goal-drive.md` names `close-worker.py` at least twice and `sdk-cli`: the
      goal sweep's reap has the same gap, and it holds `python3` itself.

  (e) `commands/fleet-drive.md` carries a close rule that names `close-worker.py` and no
      longer carries the retired literal `Close via \`/supervisor:close\` only.`

  (f) `commands/steps/reap.md` names `close-worker.py`, so the reap contract's
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
DRIVE_STEP = "commands/steps/drive.md"
GOAL_DRIVE = "agents/goal-drive.md"
DRIVE_CMD = "commands/fleet-drive.md"
REAP = "commands/steps/reap.md"

#: The `--dry-run` tier read and the real close are two separate calls; one mention is
#: the pre-fix text with a name dropped into it.
MIN_CALLER_MENTIONS = 2
MIN_GOAL_MENTIONS = 2

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

    # (b) the command-side caller: dry-run + real close + the headless entrypoint.
    step = read(DRIVE_STEP)
    if step is None:
        failures.append("%s does not exist — the caller that holds python3 has no body" % DRIVE_STEP)
    else:
        mentions = step.count(SCRIPT)
        if mentions < MIN_CALLER_MENTIONS:
            failures.append(
                "%s names `%s` %d time(s), fewer than the %d a tier read and a real close "
                "need — a finished headless row is being messaged, not closed"
                % (DRIVE_STEP, SCRIPT, mentions, MIN_CALLER_MENTIONS)
            )
        if "sdk-cli" not in step:
            failures.append(
                "%s never names `sdk-cli` — the caller cannot distinguish a headless row "
                "from a tab one" % DRIVE_STEP
            )

    # (c) the drive agent points at the caller and does not claim the read itself.
    drive = read(DRIVE)
    if drive is None:
        failures.append("%s does not exist — the fleet reap leg has no body" % DRIVE)
    else:
        if SCRIPT not in drive:
            failures.append(
                "%s never names `%s` — the leg does not say how a finished headless row "
                "is closed" % (DRIVE, SCRIPT)
            )
        if DRIVE_STEP not in drive:
            failures.append(
                "%s never names `%s` — the leg must point at the caller that runs the "
                "tier read, since it holds no `python3`" % (DRIVE, DRIVE_STEP)
            )
        if "sdk-cli" not in drive:
            failures.append(
                "%s never names `sdk-cli` — the caller cannot know which close to run" % DRIVE
            )

    # (d) the goal drive leg: it holds python3, so it owns the read itself.
    goal = read(GOAL_DRIVE)
    if goal is None:
        failures.append("%s does not exist — the goal reap leg has no body" % GOAL_DRIVE)
    else:
        mentions = goal.count(SCRIPT)
        if mentions < MIN_GOAL_MENTIONS:
            failures.append(
                "%s names `%s` %d time(s), fewer than the %d a tier read and a real close "
                "need — a goal manager's finished headless workers keep their slot forever"
                % (GOAL_DRIVE, SCRIPT, mentions, MIN_GOAL_MENTIONS)
            )
        if "sdk-cli" not in goal:
            failures.append("%s never names `sdk-cli` — the tier read cannot be applied there" % GOAL_DRIVE)

    # (e) the round's INLINE rule: names the mechanism, dropped the retired literal.
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
                "the round now runs" % (DRIVE_CMD, RETIRED_RULE)
            )

    # (f) the reap contract's carve-out.
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
        "%s points at the caller, %s names it %d time(s) and `sdk-cli`, %s carries the rule "
        "without the retired literal, and %s carries the carve-out"
        % (
            SCRIPT, DRIVE_STEP, step.count(SCRIPT), DRIVE, GOAL_DRIVE,
            goal.count(SCRIPT), DRIVE_CMD, REAP,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
