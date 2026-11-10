---
description: Clear the fleet's remembered state — stop every session, empty the five stores, and (only behind a separate opt-in) strip the task-file links a keep-alive leg resumes. The strip unlinks only a session that is positively ABSENT. Destructive; refuses without a confirm token.
allowed-tools:
  - Read
  - Bash(python3:*)
argument-hint: "--confirm fleet-reset [--dry-run] [--strip-task-links]"
---

Reset the fleet **to a clean remembered state**. This is the fleet-tier, operator-run verb;
it is deliberately not a manager's own reset.

**⚠️ This is NOT `/supervisor:reset`.** That verb re-discovers a manager's state and
**never deletes** — its contract is the vault's `Manager Session` runbook § Reset, and it
stays exactly as written. This verb deletes. The two share no code, and neither may be
folded into the other.

<process>
1. **Read the ask.** The operator wants the fleet to start fresh: the sessions stopped, the
   remembered-state stores emptied, and — only if they say so — the task files' session
   links stripped so a keep-alive leg cannot resume what the reset retired. If they have not
   said which of the two levels they want, ask before running; the strip is destructive in
   the vault and is never implied.

2. **Always run the dry-run first and print it verbatim.** It names every target — the
   per-store counts, each pid it would signal, and the task-file count — and changes nothing:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-reset.py --dry-run [--strip-task-links] [--task-dir "<dir>"]
   ```

3. **Hand the dry-run to the operator and get an explicit go-ahead for THAT plan.** A reset
   stops live sessions and deletes stores; the dry-run is what they are approving. If the
   counts or the pid list differ from what they expected, stop and ask.

4. **Run the real thing with the token.** The token is the only thing that authorises
   deletion, and it is the literal string `fleet-reset`:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-reset.py --confirm fleet-reset [--strip-task-links] [--task-dir "<dir>"]
   ```

   Add `--strip-task-links` **only** when the operator asked for the task-link level. A
   state-only run must leave every task file byte-identical.

5. **Print the script's output verbatim** — it is the report: the per-session stop line
   (`stopped` / `stale` / `keep` / `refuse` / `SURVIVED`), and each store's `before → after`
   count. Do not summarise it; a summary is where a surviving pid hides.

6. **Name what the reset did not touch.** Transcripts under `~/.claude/projects/`, the
   attention store's history, and the vault's task files (unless the strip ran) are out of
   scope. Say so in one line, so "reset" is not read as "everything is gone".
</process>

<constraints>
- **The dry-run is not optional and not skippable.** The confirm token authorises the plan
  the operator has seen, not a plan they have not.
- **Never run the strip without an explicit request.** `--strip-task-links` is a separate
  opt-in; a state clear never implies it, and the two are reported as separate levels.
- **The strip keeps a live session's link, by construction.** It probes each candidate's
  `claude_session_id` through `session-liveness.py --check` and unlinks only on `ABSENT`;
  `LIVE`, `UNKNOWN`, `AMBIGUOUS` and `MALFORMED` all keep the link. Do not "finish the job"
  by hand when the run reports a `kept` line — an unreadable probe is not evidence of death.
- **Never remove a live process's registry entry.** The script stops sessions first and
  keeps the entry of any pid it could not stop (and of this session, and its ancestors).
  Do not "clean up" a `SURVIVED` or `refuse` line by deleting the file by hand.
- **Do not edit `scripts/reset.py`.** This verb is additive; `reset`'s contract is correct
  as written and is owned by the vault runbook.
- End the turn with the session's normal closer. This report is not one.
</constraints>

<error_handling>
- **`❌ … refusing to act without --confirm fleet-reset`** (exit 2) — nothing was changed.
  Re-run with the token once the operator has approved the dry-run.
- **`❌ … --strip-task-links found no task directory`** (exit 2) — pass `--task-dir "<dir>"`
  naming the vault's tasks directory.
- **`⚠️ N session(s) survived …`** (exit 3) — those pids are still alive and their registry
  entries were kept on purpose. Report them; do not delete the entries. Investigate why the
  process ignored SIGTERM/SIGKILL before retrying.
- **No `vault-cli`** — the task-dir default cannot resolve; pass `--task-dir` explicitly.
</error_handling>
