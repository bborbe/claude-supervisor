---
description: Verify the fleet layer's own contract — five gated checks, read-only, ending in a numbered fix list
argument-hint: "[--fixture] — no argument verifies the live fleet; --fixture exercises each check against its synthetic broken fixture"
allowed-tools:
  - Read
  - Grep
  - Glob
  - Task
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/*)
  - Bash(wezterm cli list *)
  - Bash(wezterm cli get-text *)
  - Bash(git status *)
  - Bash(grep:*)
  - Bash(awk:*)
  - Bash(sort:*)
  - Bash(uniq:*)
  - Bash(wc:*)
  - Bash(mktemp:*)
---

<objective>
Verify the **fleet layer's own contract** — the fifth leg the fleet was missing. The layer has a
show (`/supervisor:fleet-status`) and an act (`/supervisor:fleet-loop`, `/supervisor:fleet-drive`) but nothing that asks whether
its own instruments report the fleet truthfully. This is that check leg.

Five checks, gated in order. **These check numbers are canonical — every reference in this file
uses them.**

1 Board count READ · 2 Phantom panes READ · 3 Exact resolution READ · 4 Classification READ · 5 Exit codes READ

⚠️ **This command is READ-ONLY.** It validates and reports; it never prunes, spawns, nudges, or
writes vault state. The act leg belongs to `/supervisor:fleet-loop` and `/supervisor:fleet-drive`. If you find
yourself about to write a file, spawn a session, or run a mutating command, stop — that is not
this command's work.

⚠️ **The defect class this exists for: a check whose failure mode is "returned nothing" is
indistinguishable from a clean fleet.** Every check below is falsifiable and carries a positive
control, and the control is **exercised**, not declared. Declaring one in prose does not satisfy it.

⚠️ **A script that fails is UNKNOWN, never clean.** If a source exits non-zero or returns nothing,
every check reading through it reports UNKNOWN. A missing script and a healthy fleet must never
render the same.
</objective>

<process>
0. **RESOLVE (preamble — not one of the five).** Resolve the four sources and **prove each is
   readable before any check reads it**. Print one line per source with its status, so a dead
   source is visible before the checks run rather than inferred from a short table.

   `S=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts`

   ⚠️ **The scripts are not executable** — invoke every one as `python3 "$S/<name>.py"`. A bare
   `"$S/fleet-board.py"` fails with `permission denied`, which is a **silent zero** at the shell
   and reads as an empty fleet.

   | source | invocation | readable when |
   |---|---|---|
   | board | `python3 "$S/fleet-board.py" --json` | exit 0 **and** stdout parses as JSON |
   | panes | `wezterm cli list --format json` | exit 0 and the array is non-empty |
   | context | `python3 "$S/context-usage.py"` | exit 0 (text only — there is no `--json`) |
   | colours | `python3 "$S/fleet-colours.py" --json` | exit 0 **and** stdout parses as JSON |

   ⚠️ **`--fixture` mode swaps the source for the synthetic one named in check 2's control** — the
   fixture is fed in **as the check's own input source**, so the same predicate runs over it. A
   fixture branch that shares no code with the live path proves nothing about the live path.

   Record each source's exit code. Any source that is not readable is **UNKNOWN** for every check
   that reads through it — never PASS, and never silently skipped.

1. **BOARD COUNT vs LIVE PANES (read).** The board's `needs-input` count is an **upper bound
   presented as a count**: it inherits `is_open_gate()` from `who-needs-me.py`, and an entry clears
   only on the worker's next tool call, so a cleared gate and a live one are byte-identical.
   Report the **delta and its direction** — `board N · live M · delta ±D`. A **negative** delta
   (board over-counts) is the known defect; a positive one means panes the board does not see.
   Count live panes from `wezterm cli list --format json`; take the board's count from
   `counts["needs-input"]` in the `--json` document. ⚠️ Do **not** read the count from the rendered
   box table — parse the JSON document.

2. **PHANTOM PANES (read).** Every pane id a fleet script reports must exist in
   `wezterm cli list`. `context-usage.py` reads `~/.claude/state/context/<sid>.json`, which
   **outlives the pane**, so a stale entry keeps reporting a tab that no longer exists — and it
   exits 0 while doing so, which is why it cannot self-report this. Extract the `pane N` column
   from its text output and intersect against the live pane ids; **name every id that does not
   exist**, with the session name and usage next to it. Take the live id set from
   `wezterm cli list --format json` (`pane_id`). `fleet-colours.py --json` carries a real `pane`
   int or `null` per session and is the cleanest join for attributing an id back to a session —
   but read its `panes_read` flag first: when that is `false` the pane read itself failed, so
   every `null` means UNKNOWN rather than "headless" and the join proves nothing.

3. **EXACT SESSION→TASK RESOLUTION (read).** A session's task must be found by an **exact full-id
   match on the `claude_session_id:` frontmatter field** — never by a prefix substring, and never
   by a body grep. A prefix search over the file's opening characters picks whichever file happens
   to come first, which is how a busy session with open boxes was once told its work was done.
   Apply the **stamp rule** ([`docs/fleet-surface.md` § Session stamps](../docs/fleet-surface.md)):
   a session may carry many stamps, but **at most one on an open task**. Run
   `python3 "$S/stamp-check.py" "<tasks_dir>" --json` and report from its document — never count
   stamps by hand. **FAIL** on any `violations` entry (one stamp on 2+ open tasks), naming the stamp
   and every open file. List `suspects` as findings without failing — a creating session's stamp
   left on an open task that `metrics_sessions` shows someone else worked. Report the `permitted`
   count too: it is the positive control that the check still *sees* multi-file stamps, so a
   PASS with `permitted 0` on a vault known to carry history is a blind check, not a clean one.
   ⚠️ **"2+ files" is not the defect.** Measured 2026-09-25: counting it flagged 33 stamps, 30 of
   them one ended session's finished tasks in turn — history that records who did the work, and
   must never be rewritten to clear a count. Exit 2 (missing dir, no task files) is **UNKNOWN**.
   The script reads the field from the frontmatter block only, and an empty `claude_session_id:`
   is no stamp — a `\s*` parse crossed its newline and read `goals:` as a stamp shared by three files.

4. **CLASSIFICATION vs PANE READ (read).** For **every** session in the board's `needs-input`
   bucket — not a sample — compare the board's bucket against that pane's **visible screen**, matched by containment (`wezterm cli get-text --pane-id <N>` returns the visible screen by default). ⚠️ **Never the last non-empty line and never a fixed `tail -N`**: every Claude Code pane's last non-empty line is its status bar (`⏵⏵ auto mode on …`), and a pane that draws content below its own modal puts the footer above trailing output — measured 2026-10-03, pane 703's live permission prompt sat 8 non-empty lines from the end.
   Report each disagreement with **both readings**. A bucket of `needs-input` whose pane shows no
   open gate is the board over-counting; the reverse is the board under-reporting a blocker.
   ⚠️ The board's rows are **lists of cell strings, not dicts**, so session identity comes from the
   `details` map (sid-keyed, `needs-input` rows only) or a join on the name column. If the bucket
   cannot be attributed to a session at all, report that as UNKNOWN rather than guessing.

5. **EXIT CODES (read).** Any source from step 0 that exited non-zero or returned nothing makes
   every check reading through it **UNKNOWN**. Report the check as UNKNOWN with the source's exit
   code and the first line of its stderr. ⚠️ This check is not a summary of the others — it is the
   one that stops a dead probe rendering as a healthy fleet, and it is the reason step 0 records
   exit codes rather than assuming success.
</process>

<positive_controls>
Each check is exercised against a **synthetic broken fixture** and observed to **FAIL**, then
against a **known-good fixture** and observed to **PASS**. Both directions are required: a check
that prints FAIL unconditionally is no more a check than one that always prints PASS.

⚠️ **Every fixture is constructed at run time and drawn from outside the live defects.** A fixture
taken from the defects named in the task's `# Impact` lets a command that hardcodes a lookup table
of those known defects satisfy both the criterion and its own control — proving only that it can
detect what it was told about. The live defects are the real-run findings, never the fixtures.

The fixture-mode FAIL must **quote the injected artifact it detected** — the scratch pane id it
wrote, the scratch file path it created — because an unconditional-FAIL branch cannot produce that.

| check | synthetic broken fixture | expected |
|---|---|---|
| 1 | a board stub whose `needs-input` count is inflated against a fixed pane list | FAIL, quoting the delta |
| 2 | a phantom pane id **not** among the live defects, injected into a scratch pane list | FAIL, naming the injected id |
| 3 | a scratch tasks dir holding two **open** task files that share one stamp (plus a finished one sharing it, which must stay permitted) | FAIL, naming the stamp and both open files |
| 4 | a board stub bucketing a session `needs-input` whose pane read shows otherwise | FAIL, quoting both readings |
| 5 | a source stubbed to exit non-zero | UNKNOWN, quoting the exit code |

Fixture files live under a `mktemp -d` scratch directory. **Nothing is written inside the vault** —
a fixture written into the vault would violate this command's read-only contract and its own
check-6 probe.
</positive_controls>

<output_format>
```
fleet-verify — <N> checks · <M> findings
Sources: board <ok|exit N> · panes <ok|exit N> · context <ok|exit N> · colours <ok|exit N>

  1 Board count ....... PASS | FAIL | UNKNOWN — board <N> · live <M> · delta <±D>
  2 Phantom panes ..... PASS | FAIL | UNKNOWN — <n> reported · <n> live · <n> phantom
  3 Exact resolution .. PASS | FAIL | UNKNOWN — <n> stamps · <n> violating · <n> suspect · <n> permitted multi-file
  4 Classification .... PASS | FAIL | UNKNOWN — <n> in needs-input · <n> disagreeing
  5 Exit codes ........ PASS | FAIL | UNKNOWN — <n> of 4 sources failed

Findings:
- <check N>: <the specific defect, naming the file/id/stamp and quoting the offending value>

Suggested fixes, in order — each one line, each naming the exact action:
1. <verb> <the thing> — <why>
2. ...
```

A check that did not run is reported **SKIPPED** or **UNKNOWN** with its reason — **never PASS**.
**The fix list is the deliverable.** A run that reports findings and stops has failed this
command's purpose: the point of a check leg is to verify *and suggest the fix*.

Under `--fixture`, print the same table with each check's fixture verdict, and state per check
which fixture was used and the artifact the FAIL quoted.
</output_format>

<constraints>
- **Read-only.** No task re-homed, no file authored, nothing spawned, no vault state written. Every
  finding is a report; every remedy is a suggestion for `/supervisor:fleet-loop`, `/supervisor:fleet-drive`, or the
  operator.
- **Reuse, never reimplement.** Consume `fleet-board.py --json`, `fleet-colours.py`,
  `context-usage.py` and `wezterm cli list` as they are. This command defines **no second
  classification** — the bucket rule lives in `fleet-board.py`, and a second predicate here would
  drift from it silently. `context-usage.py` has no JSON mode: parse its text, do not fork it.
- **Sources are not executable** — always `python3 <path>`.
- **The `needs-input` count is an upper bound, not a count.** Report it as a delta with a direction;
  never restate it as the fleet's state.
- **No user prompts during execution** — never `AskUserQuestion` mid-run. There is nothing to
  prompt for: the command only reads and advises.
- **Name the naming inconsistency in passing, do not fix it here.** `verify-task` and `verify-goal`
  lead with the verb while `manager-verify` trails it; `verify-<level>` would read better if the
  family is ever normalised. That is a separate change touching four commands and their callers.
</constraints>

<success_criteria>
- All five checks are checked, or reported SKIPPED/UNKNOWN with a reason — never silently PASS
- Each verdict line names the check, its verdict, the evidence behind it, and the source command
- Check 1 reports the delta **and its direction**, not a bare pass/fail
- Check 2 names every phantom pane id; check 3 names every stamp on 2+ open tasks, and every suspect
- Check 4 covers **every** session in the `needs-input` bucket, not a sample
- Check 5 reports a failed source as UNKNOWN, never as clean
- **Nothing was mutated** — no file written inside the vault, nothing spawned
- **The run ends with a numbered fix list**, each entry naming the exact action and who performs it
</success_criteria>
