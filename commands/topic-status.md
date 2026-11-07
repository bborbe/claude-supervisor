---
description: "Read-only TOPIC snapshot — the goals this topic declares, each one's status, phase, SC count and manager state, plus a blocked-by-you jump list. No vault writes, no loop, no TTS. Subject via $1, or detected from the session when omitted. — ⚠️ not side-effect-free: it records the resolution and registers the caller as a topic manager."
allowed-tools:
  - ListAgents
  - Bash(grep:*)
  - Bash(ls:*)
  - Bash(find:*)
  - Bash(awk:*)
  - Bash(python3:*)
  - Bash(mkdir:*)
  - Bash(vault-cli:*)
  - Bash(wezterm cli list:*)
  - Read
argument-hint: "[topic] (detected when omitted)"
---

Topic-status — the **read-only, one-shot** twin of `/fleet-status` for **one topic**. It
answers *"what is this topic's goal set doing right now?"*: one row per declared goal, with
its status, phase, success-criterion count and the state of the goal manager running on it.

⚠️ **It lists GOALS, never tasks.** A goal's task set belongs to `/supervisor:goal-status
"<goal>"` — the goal tier's own snapshot. This command reads goal pages and nothing else,
which is what makes the two commands unable to disagree about a tier.

It resolves its subject **exactly as `/supervisor:topic-loop` does** — same chain, same
state files — so the two can never disagree about which topic is being reported.

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **topic name**, matching
  a topic page in the vault's topics folder *(operator's vault; not shipped with this
  plugin)*. A **goal** name is the wrong tier: print
  `❌ "<name>" is a goal, not a topic — use /supervisor:goal-status.` and stop.
  - `/supervisor:topic-status Sentry` → topic branch
  - `/supervisor:topic-status` (bare) → resolve from § Subject resolution below

## Subject resolution — when `$1` is omitted

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` — the vault
resolution, the four-source chain, the case-insensitive vault test, the "no fallback, ever"
clause, the `Subject:` line, and the recording contract. Read it there; **it is not
restated here.**

**Only source 3 stays inline**, because it is the one source no agent can be handed:

3. **Conversation** — the most recent `/supervisor:topic-loop`, `/supervisor:topic-status`,
   `/supervisor:topic-drive` or `/supervisor:topic-verify` argument in this conversation,
   then the most recent **topic** page referenced as a subject.

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a topic name: /supervisor:topic-status "<name>"` and do nothing else.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs.

**⚠️ The subject's status is left exactly as found — this command does not reconcile it.**
`${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` § *Reconcile the subject's status* names
this command as a **non-writer**: it reports the status it finds rather than changing it,
because its contract is that it writes no vault content. ⚠️ **This is a write-scope
carve-out, not a missing step.**

**⚠️ Record the resolution — a step to run, not a reference to follow.** Having resolved a
subject, run the recording block at `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md`
§ Recording **now**, before the report. ⚠️ **Which files on which resolution is the doc's
asymmetry** — running it on an *inferred* subject wrongly promotes that subject into
cross-session state for every later tick.

**Then read it back and say which happened.** Present → `✅ Registered: <subject>`. Absent →
`⚠️ Not registered — the recording step above did not run`, then run it.

## Procedure

1. **Resolve the topics folder and the topic page.** `vault-cli config list --output json` →
   this vault's `topics_dir` (default `23 Topics`) → `find "$TOPICS_DIR" -maxdepth 1 -iname
   "$SUBJECT.md"` → confirm `page_type: topic` **in the frontmatter block**. ⚠️ **`-iname`,
   not a shell glob.** Missing page → print the name and the fix, then stop; never fall back
   to a glob, a membership scan or a theme match.

2. **Read the `## Goals` list** — the topic's declared membership. ⚠️ **The heading is
   `## Goals` (a sub-heading under `# Scope`), not `# Goals`** — a literal `^# Goals` match
   returns nothing, and with no fallback that reads as an empty set rather than a miss.
   **Declared, never re-derived.**

3. **Per goal, read four things from its page** — `24 Goals/<name>.md` *(operator's vault;
   not shipped with this plugin)*, resolved by **exact basename, case-insensitive**:

   - `status` and `phase` from the frontmatter;
   - the `# Success Criteria` tally — `n` ticked of `m` total;
   - `claude_session_id`, probed with `session-liveness.py --check <id>` →
     `LIVE` / `ABSENT` / `UNKNOWN`;
   - the loop cadence — `manager-liveness.py --state --subject "<goal>" --branch goal` →
     `OK` / `STALE` / `STOPPED` / `INVALID`.

   ⚠️ **`UNKNOWN` is not `ABSENT`** — an unreadable source cannot prove a session dead, so
   the row renders `UNVERIFIED`, never `dead`. ⚠️ **`--branch goal` is not optional** — the
   state slug carries the tier.

4. **Render the table.**

   ```text
   Subject: [<topic>](obsidian://open?vault=<V>&file=<relpath>) (from <source>)
   Branch: topic (<the page it came from>)
   Goals (N): <n> managed · <n> idle · <n> waiting-approval · <n> done

     ┌──────────────────────────────┬─────────────┬──────────┬─────────┬──────────────┐
     │ Goal                         │ Status      │ Phase    │ Met     │ Manager      │
     ├──────────────────────────────┼─────────────┼──────────┼─────────┼──────────────┤
     │ <goal>                       │ 🔄 progress │ execution│ SC 2/4  │ [<sid8>] OK  │
     └──────────────────────────────┴─────────────┴──────────┴─────────┴──────────────┘
   ```

   **Manager cell** — the delimiter is the liveness verdict, as in the goal tier's Session
   cell: `[<sid8>]` live, `(<sid8>)` not live, `—` no id recorded, `UNVERIFIED` when the
   probe could not decide. The word after it is the cadence.

   ⚠️ **Never hand-draw the box** — render it with
   `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/box-table.py`,
   whose stdin contract is `${CLAUDE_PLUGIN_ROOT}/docs/sweep-output.md`.

5. **Blocked-by-you jump list** — the topic's `waiting` sessions, resolved to **pane ids**
   via `wezterm cli list`, one `python3 .../scripts/jump-link.py <PANEID>` line each. **Never
   a hand-written URL** — the token lives in a 0600 file, so a hand-built link is either
   broken or leaks it.

6. **Never fabricate state** — report only what the pages and `ListAgents` show this run. No
   recommendation, no verdict, no `👤 You:` panel, no TTS, no `SendMessage`. For "what should
   I do about this topic", that is `/supervisor:topic-loop`, not this snapshot.

⚠️ **A goal that no manager owns is a row, not an error.** It renders `—` in the Manager
cell. Starting one is `topic-spawn`'s act and this command's advice is only the table.
