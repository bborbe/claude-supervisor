---
description: The topic manager ceiling check (`--topic-open`) — may a manager be opened for this TOPIC page? Refuses `topic-unflagged` (the topic page carries no operator-set flag) or `manager-cap` (the fleet manager ceiling). Invoked by the wrapper's open endpoint before a topic create; holds no spawn tool, starts no session.
argument-hint: "(no argument — the wrapper passes --vault/--topic)"
allowed-tools:
  - Read
  - Bash(python3:*)
  - Bash(find:*)
  - Bash(awk:*)
  - Bash(grep:*)
---

The topic tier's spawn-adjacent surface is now **one gate**: the ceiling the wrapper's open
endpoint runs before a topic create. There is no goal-manager tier to start (merged into the
topic tier 2026-11-24), so `topic-spawn` no longer starts anything — it answers the topic
tier's own question, "may a manager be opened for this TOPIC page?".

⚠️ **This command holds no `spawn_agent` tool, deliberately.** A topic manager is created by
the wrapper's open endpoint; a second, hand-rolled spawn route in the topic tier would be a
session created outside every gate — the failure the wrapper's open endpoint exists to prevent.

⚠️ **A topic-tier surface — never run it from a worker session.** A worker carries a task; it
never starts a session (`docs/session-tiers.md` § The worker boundary).

## Opening a topic's manager — `--topic-open`

The caller is the wrapper's open endpoint — `bborbe/claude-wrapper` `pkg/open_tab.go`
(`runTopicCeiling`), which passes `--topic-open --hand-run`. It runs on a topic **create**
only; `runTopicCeiling` returns early on a resume. No file inside this plugin invokes the flag.

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/topic-spawn.py \
  --vault "<vault-root>" --topic "<topic>" --topic-open --hand-run
```

The first line of stdout is the verdict: `open-ok: <topic>` on success, or a refusal token
(`topic-unflagged`, `manager-cap`, `topic-not-found`) with a non-zero exit. ⚠️ **The
`--hand-run` flag matters**: without it the flag gate applies and the call refuses
`topic-unflagged` for exactly the unflagged case this section describes. ⚠️ **The
`open-ok: <subject>` contract is byte-identical to what the wrapper matches on** — same
prefix, same subject echo, same exit codes — because the wrapper's tests cannot run from this
repo.

⚠️ **Set the Bash call's timeout to at least 300 s.** `commands/open.md:243-246` states the
rule: a short call is killed by the harness before the script answers, and an in-script
`timeout=` cannot rescue it.

⚠️ **`commands/open.md` no longer documents this invocation.** Commit `dcd2d991` made it a
thin caller of the wrapper's open endpoint, and `scripts/check-open-hand-run.py` now forbids
`topic-spawn.py` there — so this block is the invocation's only written home in the plugin.
