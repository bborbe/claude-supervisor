---
# Read by agents/fleet-keepalive-actor.md § 1b, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Retired — no goal-manager keep-alive

The goal-manager tier is merged into the topic tier (2026-11-24). There is no goal-manager
session, so there is nothing for the fleet round to restart here: a topicless goal is driven
by the fleet round directly, through `agents/goal-drive.md`.

`scripts/topicless-goal-keepalive.py` now answers `retired` and exits 1. Do not run it, and do
not restore this procedure — the population it selected no longer exists.

Record the leg as not run, so the digest's `LEGS` section still carries a reading:

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/fleet-legs.py" --digest "<this round's digest>" --leg topicless-goal-managers \
  --not-run "retired — the goal-manager tier is merged into the topic tier"
```

`agents/fleet-keepalive-actor.md` § 1b is the caller; the count line's shape lives in
`commands/steps/output-shape.md` item 12.
