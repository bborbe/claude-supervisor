---
description: Restart ONE named worker in a single verb — kill its registry pid, resume the same session in a new tab, and tell it what changed underneath it only when that is safe. Takes a session id (or a pid). Refuses on a dirty worktree the target owns alone, and leaves a resumed session holding a human gate untouched.
argument-hint: "<session-id | pid>"
allowed-tools:
  - Read
  - Bash(wezterm cli get-text:*)
  - Bash(wezterm cli send-text:*)
  - Bash(wezterm cli list:*)
  - Bash(python3:*)
  - Bash(ls:*)
  - Bash(~/.claude/plugins/cache/claude-supervisor/supervisor/*/scripts/restart-precheck.py:*)
  - Bash(~/.claude/plugins/cache/claude-supervisor/supervisor/*/scripts/restart-worker.py:*)
---

Collapse kill → resume → re-orient into one verb. The Fleet Manager did this by hand on 2026-09-20 — killed a pid, re-spawned with `claude --resume`, then told the resumed session it had been restarted — and the third step was forgotten on the first pass, because a resumed session replays its own pre-kill transcript and cannot tell on its own that the thing it was waiting for already happened.

**This command is the orchestrator. It is not a second kill mechanism.** The kill+resume leg belongs to `scripts/restart-worker.py`, which carries the single-pid kill, the `pkill`/`killall` ban, the raw `wezterm --resume` recipe *including its `--cwd`*, and seven target refusals. Calling it is the whole point: a second copy of the resume recipe is how the fleet ends up with two mechanisms that disagree. What this file owns is what the script does not — the pre-kill worktree probe, the cause-of-death branch, the conditional message, and the report.

**The two-axis contract is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker — read it there, do not restate it here.** The row's declared `mode:` picks the path first, liveness then decides only among the rows path A can serve, and cause of death picks whether anything is typed afterwards. That section is the single home of the rule.

## Argument

A session id, or a pid. A pid resolves through `~/.claude/sessions/<pid>.json` — the registry is authoritative because Claude Code deletes the entry on exit. **Never resolve a target from a process listing**: `pgrep -fl` and `ps` print command lines that carry MCP `Authorization:` headers, and they can confirm life but never death. ⚠️ That includes `ps -o comm=` — it reads like the safe column and is not: on macOS it prints the full argv (observed 2026-10-01).

## Step 0 — Resolve the load path and the target

```bash
LOAD_PATH=$(ls -d ~/.claude/plugins/cache/claude-supervisor/supervisor/*/ | sort -V | tail -1)
```

The **cache** path, never the marketplace clone: the clone updates independently of what a running session loaded, and the `settings.json` allow rule is scoped to the cache. Order by version numerically — two versions installed in one operation get near-identical mtimes and an mtime sort picks the older one (measured 2026-09-24, PR #194).

Then read the registry entry and confirm you have a session id. A manager target is refused by the script in Step 2, not here.

## Step 1 — Pre-check, read-only

```bash
"$LOAD_PATH"scripts/restart-precheck.py <session-id>
```

Three probes, all read-only, and **the refusal token is the first line of stdout** — branch on it, never on the prose below it:

| Token | Then |
|---|---|
| `dirty-worktree` | **Stop.** The target is the only live session in that worktree and it carries tracked changes. Print the paths the report listed and hand the operator the choice: commit, stash, or pick another target. |
| `ambiguous-pid` | **Stop.** More than one registry pid claims that id; killing one is a coin flip on someone else's work. |
| `unknown-session-id` | **Stop.** No entry carries it, or the registry could not be read. |

**A dirty worktree does NOT refuse when other live sessions share it** — the report says so, with the occupancy count, and the run continues. Measured 2026-09-25: 19 of 25 live interactive sessions sat in a dirty worktree, and all 17 primary-vault sessions were dirty by the *same three* tracked files, none of them the target's. Sharing is the discriminator, so it is measured rather than assumed; refusing on dirt alone would have blocked most of the fleet on somebody else's work.

**Carry the report's `classification:` line forward — Step 4 branches on it.** The three values and what they mean are the script's docstring; the short form is `mid-work` (nothing is waiting on a human), `gate-held` (a real `👤 You:` gate is open), `undetermined` (no transcript — fail closed).

## Step 2 — Kill and resume, via the script

```bash
"$LOAD_PATH"scripts/restart-worker.py <session-id>
```

**Invoke it directly — the script path is the first token.** It carries a `#!/usr/bin/env python3` shebang and is executable, and the `settings.json` allow rule is shaped `Bash(<script path>:*)`, which matches a command whose first token is that path. `python3 <path>` does not match it and would fall back to a broad interpreter grant — the exact permission surface that script exists to avoid.

**Surface a non-zero exit verbatim and stop.** Its refusals (`busy-target`, `headless-target`, `manager-target`, `role-undetermined`, `stale-load-path`, and the two above) each print a token on the first line. `busy-target` is what "the session is mid-turn" reduces to here — do not re-derive it, and do not retry around it.

⚠️ **A refused path is terminal — never fall back to another resume route.** `mcp__supervisor__spawn_agent` refuses `resume` alongside `interactive: true` because *that tool's* tab path cannot carry the flag; the raw path the script uses hands it to the launcher directly. Falling back is not a retry — it is a second resume the gate never authorised.

## Step 3 — Take the new pane id from the script's own output

The script prints `✅ killed pid <n>; resumed session <id> in a new tab (pane <N>)`. **Parse the pane id from that line and use it for everything below.**

⚠️ **The pre-kill pane id is not the resumed one, and the old id is never handed to anyone.** The report may name it once as the before-value; nothing downstream may. WezTerm renumbers panes on a restart, so a recorded coordinate goes stale within the hour.

## Step 4 — The message, gated on cause of death

Branch on Step 1's `classification:`. **The transcript tail classifies; the pane vetoes. Where they disagree the veto wins.**

| `classification:` | Then |
|---|---|
| `gate-held` | **Type nothing.** The resumed pane comes up holding its own unanswered gate; idling there is the correct state. Driving it answers a question on the operator's behalf. |
| `undetermined` | **Type nothing.** An unreadable transcript proves nothing about what the pane is holding. |
| `mid-work` | **Check the pane, then type.** Read it first — the veto is not optional. |

**Read the pane before typing:**

```bash
wezterm cli get-text --pane-id <NEW-PANE-ID>
```

**Veto — type nothing, and report `vetoed` with the pane's `jump-link.py` line — when either holds:**

- the pane carries `Enter to select` at the start of a line (a selection modal), or
- the composer is not empty.

A pane that came up holding a modal is precisely the state the 2026-09-20 run misread as a live gate. Typing into it turns any keystroke into a menu selection, and handing the operator the pane is the honest alternative to answering for them.

**If the veto clears, deliver the note.** Keep it what it is: *you were restarted, here is what changed underneath you*. It is **not** an answer to anything, and it must not read as one.

```bash
wezterm cli send-text --pane-id <NEW-PANE-ID> --no-paste $'You were restarted by /supervisor:worker-restart. Nothing about your task changed — re-read anything you concluded from the old plugin version. This is not an answer to any gate; if you are holding one, it is still yours.'
```

**How to submit it — and why a trailing `\r` on the text is not enough — is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker. Read it there and follow it.** Do not restate the submission steps here: it is the same recipe every other call site uses, and a second copy is how the two drift.

⚠️ **Typing is legitimate only into the pane this command just spawned, while it is idle** — the same section's rule. Answering another session's operator gate by `send-text` stays forbidden.

## Step 5 — Report

One block. Name the **new** pane id, the session id, the killed pid, and the branch taken (`noted` / `vetoed` / `gate-held` / `undetermined`).

Hand over the jump link, generated — never hand-built, never a tab id:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py <NEW-PANE-ID>
```

## Rules

- **One target per run, named by the operator.** This command never hunts for which worker needs restarting.
- **Workers only.** A manager session holds fleet-wide state no task file carries; the script refuses it and this command does not override that.
- **Never reimplement the kill, the resume, or the liveness probe.** One mechanism, called.
- **Never type into a pane the veto flagged.** The asymmetry is the whole design: an undriven mid-work resume wastes a session, a driven gate-death resume answers a question nobody gave.
