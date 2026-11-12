---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Reap contract

When: the drive leg runs

### The reap contract

`agents/fleet-drive.md` step 1b owns it — the three disk reads, the rule that the file is the fact and the session's claim is not, and the deliberately-open-box carve-out. This command does not act on it and does not restate it; read it there.

⚠️ **That contract is TASK-shaped, and a manager has no task file.** The manager-subject
half — a goal or topic page whose `status` is terminal, joined to the session id recorded on
it — is `commands/steps/manager-reap.md`, and its reader is `scripts/subject-reap.py`. Two
populations, one rule: the reap is evidence plus the session's own close.

⚠️ **The carve-outs are the two round-run closes: a finished or deferred tab row, and a finished headless (`sdk-cli`) row.** The headless one has no self-close path — vault-cli `session-self-exit.py` proves a controlling tty and always answers `SELF-EXIT: stay` for it. The tab one has a self-close path the session never takes: a worker whose task is terminal sits waiting for an operator click, and that wait is the gap this close removes. Both are closed by the round through `scripts/close-worker.py`. ⚠️ **The tier read is the
caller's** (`commands/steps/drive.md`), not the drive agent's: that agent's `allowed-tools`
grants no `Bash(python3:*)`, so `agents/fleet-drive.md` step 1b only points at the caller and
drafts the message for every finished row. That is not a kill by hand: it is the same guarded
mechanism `/supervisor:close` runs, with the busy / gate / worktree / hold / terminal refusals
intact.

