---
description: Verify one topic or goal and suggest the fixes — seven gated checks, read-only, ending in a numbered fix list
argument-hint: "[topic-or-goal-name-or-path] — omit the name to resolve it from the session, like /supervisor:manager-status"
allowed-tools:
  - Read
  - Grep
  - Glob
  - Task
  - Skill
  - Bash(vault-cli task *)
  - Bash(find:*)
  - Bash(awk:*)
---

<objective>
Verify ONE subject — **a topic or a goal** — and **suggest** the fixes. Seven steps, gated in
order. **These step numbers are canonical — every reference in this file uses them.**

⚠️ **This command is READ-ONLY, re-scoped 2026-09-20 on the operator's instruction:** *"we don't
need 20 different commands to do stuff. We have worker status that shows the status and then we
have worker verify that should verify and suggest fix."* It **diagnoses and advises**; it does not
prune, author, plan or spawn. Every mutating step below is now a **suggestion in the report**, and
the manager decides what to do with it. That is the whole point of the split: `/supervisor:manager-status`
shows, `/supervisor:manager-verify` verifies and suggests, and the human or the manager acts.

`verify-topic` (topic branch) and `verify-goal` (goal branch) are the INSTRUMENTS this command
**calls in step 1** — never re-implement either.

⚠️ **Dependency gap:** `verify-topic` is still a vault-local command (the Personal vault's
`.claude/commands/verify-topic.md`), not shipped by any plugin — tracked by *Move verify-topic Into the
Vault-Cli Plugin*. In a vault without it, the topic branch's step 1 has no instrument: report step 1 as
`UNKNOWN — verify-topic not installed` and continue, never re-implement it here. `verify-goal` ships
with the vault-cli plugin (`/vault-cli:verify-goal`).

1 Necessity READ · 2 Prune SUGGEST · 3 Gap READ · 4 Fill SUGGEST · 5 Ready READ · 6 Plan SUGGEST · 7 Start SUGGEST

⚠️ **THE ORDER RULE: do not descend while the level above fails.** A task bucketed ready-to-start
because its dependency is prose-only is a **goal-page** defect; a task invisible because the topic's
`## Goals` used a checkbox is a **topic-page** defect. When a level-3 check fails, **walk back up.**
</objective>

<process>
0. **RESOLVE (preamble — not one of the seven).** With `$ARGUMENTS`: no `.md` → append it; then
   **probe both folders rather than prepending one** — the subject may be a goal or a topic, and
   prepending `23 Topics/` alone is how an explicitly-named goal resolves to nothing. **With no
   argument, resolve the subject from the session**
   — the same chain `/supervisor:manager-status` and `/supervisor:manager-loop` carry, so a subject named once sticks
   and a bare invocation works.

   **This block is shared with `/supervisor:manager-status`, `/supervisor:manager-loop` and `/supervisor:manager-drive`. The resolution rule is
   identical in all four commands; the sentences that legitimately differ are enumerated here
   rather than counted** — a count is the part that rots: this line read *"exactly four"* for two
   copies, and any third consumer edits it again, which makes it a liability rather than a
   guarantee. **Between the three plugin copies exactly four differ** — the sibling names on this
   line, the STOP line, the clause ending "No fallback, ever", and the closing sentence about
   which contract the write changes. **This copy differs additionally in exactly one region** —
   the recording paragraphs, which stay prose here because this command's `allowed-tools`
   deliberately omits `Bash(python3:*)` and `Bash(mkdir:*)`, so the siblings' runnable blocks are
   not available to it. `/supervisor:manager-drive` also carries a leading **vault-resolution paragraph** (cwd → vault-cli config path), because it has no `## Resolution` section of its own to derive the vault in. Change one, change the others; the keep-in-sync contract is the same one
   `/vault-cli:prepare-compact` and `/vault-cli:post-compact` carry for their check blocks. Three
   commands that resolve a subject differently will disagree about which tree is being reported,
   and the disagreement is silent.

   A bare invocation takes the first source that yields a **real page**:

   1. **Session state** — `~/.claude/state/worker-manager/<CLAUDE_CODE_SESSION_ID>.json`, shape
      `{"subject","vault","branch","resolved_at"}`. Accepted only when `vault` matches the resolved
      vault **and** `subject` still resolves to a page. Compare **case-insensitively** — the vault is the lowercase vault-cli config `name` (`personal`), but state files written before 2026-09-23 may carry display case (`Personal`); a strict match silently drops to source 2. This is what makes a subject named once
      stick across the ticks of one session.
   2. **Session name** — the name this session carries, read from
      `~/.claude/sessions/$CLAUDE_PID.json` → `.name`. **`CLAUDE_PID`, not
      `CLAUDE_CODE_SESSION_ID`** — that directory is pid-keyed, so the session-id key the source
      above uses does not address it; both variables are exported, and this is the one lookup that
      needs the pid. Strip leading decoration before matching (`⚙ ` prefixes 11 of 47 live names),
      then accept only when the stripped name resolves to a goal or topic page — the same test
      every other source uses. A name that resolves to nothing, a name that resolves only to a
      **task** page, and a missing pid file are all **silent misses**: fall through to the next
      source, never error. Pid files are transient (the record this rule was filed from was gone
      hours later), and most session names are task names — measured over the 24 named Personal
      sessions on disk, 1 resolved to a topic, 0 to a goal, 20 to a task. **The source is narrow by
      construction.** It does not exist to resolve most sessions; it exists so that a session named
      after its own subject can never be overruled by another session's leftovers.
   3. **Conversation** — the priority order `/vault-cli:task-status` uses in its Phase 2: the most
      recent `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` or `/supervisor:manager-verify` argument in this conversation,
      then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited
      path — not a prose mention).
   4. **Vault's last subject** — `~/.claude/state/worker-manager/last-<vault>.json`, same shape and
      same test. **The fallback of last resort** — below this session's own state, below its own
      name, below the conversation. It still earns its place: it carries a subject named in one
      session into another, and the vault in the filename is why two vaults never clobber each
      other. It ranks last because it is the only source that is not about *this* session. On
      2026-09-18 a session named *Dark Factory Pipeline Hygiene* rendered a full, correct-looking
      snapshot of **Notification System** — this file had been written that morning by a different
      session, and at position 2 it outranked both the session's own name and the conversation.
   5. **Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name:
      /supervisor:manager-verify "<name>"` and do nothing else.

   **Then DETECT THE BRANCH — never assume one.** Probe with the resolved `$SUBJECT`, never
   `$ARGUMENTS` (under detection `$ARGUMENTS` is empty and every probe would match nothing). The
   test is the one `/supervisor:manager-status` and `/supervisor:manager-loop` carry:
   - `find "24 Goals" -maxdepth 1 -iname "$SUBJECT.md"`, confirm `page_type: goal` in the
     frontmatter block (`awk '/^---$/{n++; next} n==1' <page> | grep -q '^page_type: goal'` —
     unscoped would match a guide's YAML template) → **goal branch**.
   - `find "23 Topics" -maxdepth 1 -iname "$SUBJECT.md"`, confirm `page_type: topic` the same way
     → **topic branch**.
   - **Both match, or neither** → print every candidate path (or `no match`) and **stop**. Never
     guess between a goal and a topic, and never silently prefer one.
   **`-iname`, not a shell glob** — a plain `ls` glob is case-sensitive under zsh, so a lowercase
   argument resolves nothing against Title Case filenames.
   ⚠️ **Both folder names are literals, and that is a known defect left in place.** Step 0 already
   prepended the hardcoded `23 Topics/` rather than resolving `topics_dir` from `vault-cli config`
   — the exact fault `verify-topic.md:18` fixed for its own path. This step adds a second literal
   (`24 Goals`) rather than repairing the class; the repair belongs to
   `[[Vault-Cli Slash Commands Resolve Folders From Config, Not Literals]]`, not to this command.

   **No fallback, ever** — never a filename glob, a `goals:` scan, a theme match, or a content
   grep. A silent guess at the subject is the failure this whole chain exists to prevent.

   ⚠️ **WHY THE SOURCE AND THE BRANCH ARE PRINTED — and what this guard no longer claims.** This
   block used to read *"**This command mutates**: steps 2, 4, 6 and 7 prune, author, plan and
   spawn … A wrong subject here does not misreport, it **acts on the wrong tree**"*. **That is no
   longer true.** The 2026-09-20 re-scope (see `<constraints>`, *"This command MUTATES NOTHING"*)
   made the command read-only and turned steps 2,
   4, 6 and 7 into **suggestions**. A wrong subject now **misreports**, exactly as in the
   read-only siblings — so the guard is the same one they carry, and it is **`print the source`**.
   Kept as a correction rather than deleted: the retracted claim is the one a reader would
   otherwise re-derive from the block's absence.

   **Print the source.** The first output line is `Subject: <name> (from
   <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report
   runs — the same reason `/vault-cli:task-status` prints `Detected task:` before its report. The
   branch line follows it, exactly as in the siblings: `Branch: <goal|topic> (<the page it came
   from>)`. An auto-detected subject reported only at the end of the run is not the same guarantee
   and does not satisfy this step.

   **Record on explicit use** — with `$ARGUMENTS` supplied and resolving, write **both**
   `~/.claude/state/worker-manager/<session-id>.json` and `last-<vault>.json` (same JSON shape as
   the siblings). On a **session-name** or **conversation** resolution, write the **session file
   only**, never `last-<vault>` — promoting an inferred subject into cross-session state pins it
   above this session's own name for every later tick. On a `last-<vault>` resolution, write
   **neither**.

1. **NECESSITY (read)** — **delegate to the instrument for the branch; never duplicate its checks
   here.** **Topic branch → `verify-topic`**, whose **check 5 Necessity** answers this step,
   already two-level: *"advances at least one criterion of the topic **or of its member goal**"*.
   **Goal branch → `verify-goal`**, whose **goal necessity** check (`verify-goal.md:27`) is the
   exact analog: *"each linked task advances ≥ 1 of the goal's success criteria"*. Flag any task
   advancing neither.
   ⚠️ Bounded to each verifier's **current revision** — a revision that renumbers the checks
   changes this mapping without any change to this command. Re-read it before calling a mismatch a defect.

2. **PRUNE (suggest)** — for each task step 1 flagged, **recommend** one of exactly two
   dispositions. Do not perform either:
   - **FIRST read the task's other `goals:` links.** A task unneeded for *this* topic may be real
     work under another goal.
   - Dispositions are exactly two: **re-home** (to the goal it actually serves) or **`aborted`
     with the reason written on the task**.
   - ⚠️ **NEVER delete.** A moved criterion is never a met one; Out-of-Scope prose turns "not yet"
     into "never".
   - ⚠️ **A THIRD shape exists, and neither of the two fits it: a task already `completed` that
     serves no criterion of its declared goal.** `aborted` misreports finished work as killed — the
     vault's legend keeps `✅ done · aborted` distinct from `✅ done` for exactly that reason — and
     `re-home` needs a goal that actually owns the work, which the file may not name. Measured
     2026-09-22 on `[[Phase-Gated Topic Flow]]`: **4** of 12 tracked tasks advanced no criterion, all
     four `completed` / `done`, and all four declared **only** that goal in `goals:` — so the
     two-disposition menu offered nothing that applied to any of them. The disposition used in
     practice, and the one to recommend, is **drop the `goals:` link and let the task rest on
     `themes:`** — the act applied to `Repair the Two Topic Gate Conditions…` earlier that same day,
     and to `Reconcile the Sentry Agent Status Summary Count` before it. ⚠️ **That is a
     tracking-state change on another session's file, so it is operator-gated:** name the tasks, name
     the act, and let the owner decide. Do not perform it from a verify run — this command mutates
     nothing, and the third shape is a finding to report, not a licence to widen step 2's menu.

3. **GAP (read)** — **this command's OWN check; no instrument covers it.** `verify-topic` has no
   criterion-coverage check and `verify-goal` has none either. **Topic branch:** every open
   criterion of the topic **and** of each member goal must have ≥1 task. **Goal branch:** every
   open criterion of the goal itself must have ≥1 task. Either way, a criterion with no task is
   precisely what a one-level check reports as "ready".

4. **FILL (suggest)** — for each gap, **name the task that should be authored** and the bar it
   must clear: authored through `vault-cli:task-creator`, then scored by `vault-cli:task-auditor`
   at **≥9/10**. **Do not author it here.** The bar is not "a file exists" — a hand-written task
   fails step 5, because the spawn precondition greps for the three sections.

5. **READY (read)** — **this command's OWN check; task-level, not topic-level.** The three
   required sections are on disk — `# Success Criteria`, `# Definition of Done`, `# Tasks` — and
   the auditor passes clean.

6. **PLAN (suggest)** — **TWO jobs, not one, and neither is performed here.** Authoring
   sections/subtasks is the **manager's own**; defect resolution is judgement and usually belongs
   to a **worker**. Say which each open defect is and who should take it. Merging the two makes a
   manager either over-reach or stall.

7. **START (suggest)** — **recommend** what to start, in order, and state the cap it must respect:
   **2 per sweep**, **4 per 30 min**, **one at a time headless**. The executor is
   **`/open "<task>"`** ([[Manager Session]] Guardrail 2) — headless session + tab in one
   command. **Do not spawn.** Name what should go first and what the cap would hold back.
</process>

<output_format>
```
Subject: <name> (from <explicit|session|name|conversation|last>)
Branch: <goal|topic> (<path>)   Steps: <n>/7 checked
  1 Necessity ..... PASS | FAIL | UNPROVEN — <detail>
  2 Prune ......... <n> to re-home · <n> to abort — 0 deleted
  3 Gap ........... PASS | FAIL — <n> criteria with no task
  4 Fill .......... <n> to author (bar: auditor >=9/10)
  5 Ready ......... PASS | FAIL — <n> missing sections
  6 Plan .......... <n> for the manager · <n> for a worker
  7 Start ......... <n> to start (cap 2/sweep, 4/30min) · <n> the cap would hold
Issues:
- <step N>: <specific issue, naming the file and quoting the offending line>

Suggested fixes, in order — each one line, each naming the exact action:
1. <verb> <the thing> — <why>, via <the command or agent>
2. ...
```
A step that did not run is reported **SKIPPED** with its reason — **never PASS**. **The fix list is
the deliverable.** A run that reports issues and stops has failed this command's purpose: the
operator asked for *"verify and suggest fix"*, and a suggestion is the second half.
</output_format>

<constraints>
- **Step 1 calls the branch's own instrument** — `verify-topic` on the topic branch, `verify-goal`
  on the goal branch. ⚠️ The independent-checks branch is **NOT TAKEN** — both verifiers landed, so
  this command calls them. Forking would duplicate and drift.
- **Steps 3 and 5 are this command's own.** No instrument covers either. Step 3 reads the goal's
  own criteria on the goal branch, and the topic's plus each member goal's on the topic branch.
- ⚠️ **This command MUTATES NOTHING — re-scoped 2026-09-20.** No task is re-homed or aborted, no
  file is authored, nothing is spawned. Every step 2/4/6/7 output is a **suggestion**. If you find
  yourself about to write, spawn, or run a mutating command, stop: that is `/supervisor:manager-loop`'s
  work, or the operator's, and this command's job is to make it findable and well-framed.
- **Suggestions never delete.** A recommendation to remove a task is a **re-home** or an
  **abort-with-reason**, never a deletion — and read the task's other `goals:` links first, since a
  task unneeded for *this* topic may be real work under another goal.
- **Step 4's bar is ≥9/10**, not file existence. **Step 6 separates planning from defect fixing.**
- **Step 0 resolves the subject from the session when no argument is given**, using the same four-source
  chain `/supervisor:manager-status` and `/supervisor:manager-loop` carry — and **prints the source before any mutating step**.
  ⚠️ The `never guess` line this step used to carry is **replaced, not deleted**: its purpose was to stop an
  actuator acting on a guess, and that purpose is now served by printing the pick before step 2 acts. Do not
  reintroduce the refusal, and do not drop the printed line — either one alone breaks the guarantee.
  ⚠️ **The chain is duplicated here, not shared.** The siblings are plugin commands and this one is
  vault-local, so there is no common file to reference; the keep-in-sync sentence in the plugin names two
  commands and now has a third consumer it cannot see. When the plugin block changes, change this one.
- **No user prompts during execution** (`agent-cmd/no-user-prompts`) — and now nothing to prompt
  for: the command only reads and advises. Never `AskUserQuestion` mid-run.
- ⚠️ **There is no `--dry-run`, and none is needed** — the flag was removed 2026-09-20, and the
  command was made read-only in the same pass, so the whole run *is* the preview. **Do not
  reintroduce a mutating mode behind a flag**: the operator's instruction was that this command
  verifies and suggests, full stop. Acting belongs to `/supervisor:manager-loop` (the loop, with its own
  mandate and caps) or to the operator.
</constraints>

<success_criteria>
- All seven steps are checked, or reported SKIPPED with a reason. Step 1 delegates to the branch's
  own instrument (`verify-topic` / `verify-goal`); steps 3 and 5 are this command's own and say so
- **Nothing was mutated** — no task re-homed or aborted, no file authored, nothing spawned
- **The run ends with a numbered fix list**, each entry naming the exact action and the command or
  agent that performs it. A run that reports issues without suggesting fixes has failed
- Every step-2 suggestion is re-home or abort-with-reason — **never a deletion**
- Step 4 names the ≥9/10 bar · step 6 splits authoring from defect fixing · step 7 states the cap · no `AskUserQuestion`
</success_criteria>
