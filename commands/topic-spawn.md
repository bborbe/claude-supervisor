---
description: Start ONE goal manager for a goal this topic declares — the topic tier's only spawn verb. Refuses with a named token: goal-not-in-scope, goal-not-startable, goal-terminal, goal-unflagged, manager-cap, or the worker refusal. Holds no spawn tool; delegates to /supervisor:open --goal. The `--topic-open` leg opens the topic's own manager instead (tokens: topic-unflagged, manager-cap, topic-not-found).
argument-hint: "<goal> [--rank] (the topic is resolved from the session)"
allowed-tools:
  - Read
  - Skill
  - Bash(python3:*)
  - Bash(find:*)
  - Bash(awk:*)
  - Bash(grep:*)
  - Bash(vault-cli:*)
---

`<tier>-spawn` starts the level directly below. For a **topic** manager that level is a
**goal manager** — never another topic manager. ⚠️ **A worker is this tier's to open, but not
this verb's**: it goes through `/supervisor:open --task`, the same path a goal manager uses
(`docs/session-tiers.md` § The four tiers).

⚠️ **This command holds no `spawn_agent` tool, deliberately.** The session is created by
`/supervisor:open --goal "<goal>"`, whose resume and spawn branches already carry the hold
check, the `session-liveness.py` guard and the spawn-recipe rules. A second, hand-rolled
spawn route in the topic tier would be a session created outside every one of those gates —
the failure the wrapper's open endpoint exists to prevent.

⚠️ **A topic-tier verb — never run it from a worker session.** A worker carries a task; it
never starts a session (`docs/session-tiers.md` § The worker boundary).

## Procedure

1. **Resolve the topic.** `/supervisor:topic-spawn` takes no topic argument: the subject is
   the one this session is already watching, resolved exactly as `/supervisor:topic-loop`
   does — `docs/subject-resolution.md` § The four sources, source 1 first. Nothing resolves
   → print `❌ No subject detected. Pass a topic name to /supervisor:topic-loop first.` and stop.

2. **Gate the start — the script decides, not this prose.**

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/topic-spawn.py \
     --vault "<vault-root>" --topic "<topic>" --goal "<goal>" --print
   ```

   The **first line of stdout is the verdict** and the exit code is non-zero for every
   refusal, so this is checkable rather than a convention:

   | first line | exit | what it means | what you do |
   |---|---|---|---|
   | `spawn-ok: <goal>` | 0 | every gate held | go to step 3 |
   | `goal-not-in-scope` | 1 | the goal is not in this topic's `## Goals` | report it; the topic's declared membership is the boundary. Do **not** widen the list to make it fit |
   | `goal-not-startable` | 1 | the goal's `phase` is not `planning`/`execution` | report it as `⌛ waiting-approval`. The `todo → planning` flip is the operator's, never yours |
   | `goal-terminal` | 1 | the goal's `status` is `completed` or `aborted` — ⚠️ **not `hold`**, which is a pause, not a closure, and does not refuse here | report it as `⚪ done`. Closure leaves `phase` untouched, so this is the record that decides it — never reopen a closed goal, and never flip its phase to make it fit |
   | `goal-unflagged` | 1 | the goal does not carry a `flag: true` the operator set — an agent-set or absent `flag_set_by` is refused | report it as `⏸ held — no flag`. Opening a manager is an open, and this tier opens only what carries an operator-set flag; do **not** set the flag yourself, and do not treat the refusal as a stall — the wrapper's flagged tick still opens its workers |
   | `manager-cap` | 1 | a bound is reached; the **second line** names it: `goal-cap` (this topic runs N) or `manager-cap` (the fleet ceiling) | print the bound and the ranked `held` rows; start nothing this tick |
   | `workers are not this verb's to start` | 1 | a `--worker` argument reached the gate | report it; a task is a worker's anchor, and a worker is opened with `/supervisor:open --task`, not this verb |

   ⚠️ **The fleet ceiling always wins over N.** N = 3 is a per-topic share; `spawn.maxManagersSoft`
   is the outer bound. **Never raise the cap to make room** — rank and wait, or say which
   goal is starved and why.

3. **Start it.** On `spawn-ok`, run the line the script printed:

   ```
   /supervisor:open --goal "<goal>"
   ```

   ⚠️ **Never a bare `spawn_agent` call** — see the header. If `open` reports that a manager
   already owns the goal, that is the answer: report it and stop.

4. **Report one line** — the goal, the token, and the session it started. No table, no loop.

## Which goals get the N slots — `--rank`

When more startable goals exist than slots, the ranking picks. Run it rather than choosing
by feel:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/topic-spawn.py \
  --vault "<vault-root>" --topic "<topic>" --rank
```

It prints `rank: <n> startable of <m> declared · N = 3`, then one row per goal marked
`start`, `held` or `unflagged`. ⚠️ **`<n>` counts only the FLAGGED rows** — the rows
eligible to compete for a slot, which includes a flagged row beyond N that `rank()` marks
`held` — while every non-terminal, startable-phase goal is still listed.
A row marked `unflagged` carries its rank and can never take one of the N: opening a manager
is an open, so it waits on a `flag: true` the operator set — never on a slot. The order is **the first key that discriminates wins**:

1. **Operator pin** — `manager_pin: true` on the goal, or a written `pinned (operator, …)`
   next to its entry on the topic page. Read as a written marking, never inferred from `status`.
2. **Startability** — `phase` must be `planning` or `execution`.
3. **Ready work, descending** — approved, non-terminal, live-phase tasks naming the goal.
4. **Goal `priority` ascending** (1 is highest).
5. **Oldest first** — so no goal starves behind a permanently busier sibling.

⚠️ **The binding stage is an annotation, not a key.** No goal→stage mapping exists, so the
round prints it and does not rank on it.

## Opening a topic's manager — `--topic-open`

The topic tier opens its manager with the same script, on the `--topic-open` branch. The
caller is the wrapper's open endpoint — `bborbe/claude-wrapper` `pkg/open_tab.go:292-297`
(`runTopicCeiling`), which passes `--topic-open --hand-run`. It runs on a topic **create**
only; `runTopicCeiling` returns early on a resume (`:287-289`). No file inside this plugin
invokes the flag.

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/topic-spawn.py \
  --vault "<vault-root>" --topic "<topic>" --topic-open --hand-run
```

The first line of stdout is the verdict: `open-ok: <topic>` on success, or a refusal token
(`topic-unflagged`, `manager-cap`, `topic-not-found`, …) with a non-zero exit. ⚠️ **The
`--hand-run` flag matters**: without it the flag gate at `scripts/topic-spawn.py:543` applies
and the call refuses `topic-unflagged` for exactly the unflagged case this section describes.

⚠️ **Set the Bash call's timeout to at least 300 s.** `commands/open.md:243-246` states the
rule: a short call is killed by the harness before the script answers, and an in-script
`timeout=` cannot rescue it. The gate fans out over the topic's goal pages, so its runtime
grows with the topic and is not bounded by the script.

⚠️ **`commands/open.md` no longer documents this invocation.** Commit `dcd2d991` made it a
thin caller of the wrapper's open endpoint, and `scripts/check-open-hand-run.py` now forbids
`topic-spawn.py` there — so this block is the invocation's only written home in the plugin.
Elsewhere in the docs the flag appears at `docs/session-tiers.md:70`, `docs/session-tiers.md:85`
and `docs/fleet-surface.md:1235`.
