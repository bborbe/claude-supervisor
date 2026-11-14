---
# Read by agents/fleet-keepalive-actor.md § 1b, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Keep alive — goal managers whose goal no topic declares

When: every tick, as `agents/fleet-keepalive-actor.md` § 1b (that body is at its 30 KB budget, so this procedure lives here)

`docs/session-tiers.md` assigns the fleet manager *"every topic manager, and every goal with
no topic"*. The goal-branch keep-alive runs inside a topic manager's round
(`agents/topic-drive.md`), so before this procedure a goal manager whose goal no topic
declares was kept by **nobody**. Measured 2026-11-08 on private-personal: 29 non-terminal
goals carried a manager id and no topic declared any of them. This is the same restart as
the actor's step 1, one tier down. **Restart is not open.**

**Population: `scripts/topicless-goal-keepalive.py` selects it and decides each verdict.** It
takes every goal page at `status: in_progress` + `phase: execution` that carries a
`claude_session_id` and that no topic page's `## Goals` list names (`crashed-managers.py`
`fleet_keeps`, one home). For each one it asks `topic-keepalive.py --branch goal`. It never
acts and never writes the cap. It resolves the vault's goals and topics folders itself, by the
vault's path (`resolve-folders.py --vault`), never from the first config entry.
- ⚠️ **The phase filter is not optional.** `topic-keepalive.py` reads liveness and cadence only,
  so on its own it answers `resume` for a backlog goal. Measured 2026-11-08: 5 of the 8
  `resume` verdicts were outside `execution`.
- ⚠️ **A goal a topic declares is never selected, even while that topic's manager is down.** The
  actor's step 1 restarts the topic manager, and the topic manager restarts its goal managers.
  Taking the goal here as well makes a second keeper, which is a double writer.

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/topicless-goal-keepalive.py" --vault "<vault>"
```

One call. Output:
- `selected: <n>`
- one `<goal>\t<verdict>\t<command>\t<reason>` row per goal
- `actionable: <n>` — the `resume`/`restart` rows handed to the round

Exit **2** means REFUSED: an unconfigured or unreadable folder, page or helper. Record it as not
run (below). A refusal is never "nothing to resume".

**Act on each row as the actor's step 1 does**, including step 0's heldness read on the goal
page's session id: a held goal manager is neither restarted nor resumed.
- `restart` → run the command column, `/supervisor:restart <id>`.
- `resume` → run the command column, `/supervisor:open --goal "<name>"`.
- After either, once `session-liveness.py --check` shows the id live, record the resume with
  `topic-keepalive.py --vault "<vault>" --subject "<name>" --branch goal --record-resume <id>`.
  That write is the 3-per-30-minute cap; a resume that did not take is never recorded.
- `crash-loop` → report its `⚠️ MANAGER CRASH-LOOP` line and stop on that goal for the round.
- `jump` → hand the operator the pane. `leave` → nothing. `cannot-tell` → report it, never act.

⚠️ **Not an open.** A topic-less goal with no recorded id is not in the population. Opening it
is the wrapper's flagged reopen tick's job (`claude-wrapper`'s `flagged-tick`), and only when the goal is flagged.

**Record the count in the digest.**
`python3 "$P/fleet-legs.py" --digest "<this round's digest>" --leg topicless-goal-managers --count <n>`
- `<n>` is the rows that needed action and got none: every `crash-loop` and `cannot-tell` row,
  plus each `resume`/`restart` whose act was refused or held. It differs from `actionable:` by
  design.
- On exit 2, write `--leg topicless-goal-managers --not-run "<reason>"` instead — key
  granularity, so the other keep-alive readings stay intact.
- The line's format has its single home in `commands/steps/output-shape.md` item 14.

Report `topicless-goal-managers=<n>|not-run` on the actor's hand-back, and count this
procedure's acts into its `restarted` / `resumed` / `crash-loop` / `cannot-tell` totals.
