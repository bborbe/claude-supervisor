---
description: Verify one topic or goal and suggest the fixes — seven gated checks, read-only, ending in a numbered fix list
argument-hint: "[topic-or-goal-name-or-path] — omit the name to resolve it from the session, like /supervisor:goal-status"
allowed-tools:
  - Read
  - Task
  - Skill
  - Bash(find:*)
  - Bash(awk:*)
  - Bash(grep:*)
---

<objective>
Verify ONE subject — **a topic or a goal** — and **suggest** the fixes. Resolve the subject, detect the branch, call that branch's instrument, dispatch `supervisor:goal-verify`, and print what it returns. **The seven gated checks, their per-branch forks and the report shape live in `agents/goal-verify.md` and are not restated here.**
</objective>

⚠️ **This command is READ-ONLY, re-scoped 2026-09-20 on the operator's instruction:** *"we don't need 20 different commands to do stuff. We have worker status that shows the status and then we have worker verify that should verify and suggest fix."* It **diagnoses and advises**; it does not prune, author, plan or spawn. Every mutating step is a **suggestion in the report**. There is **no `--dry-run`, and none is needed** — the flag was removed 2026-09-20 in the same pass, so the whole run *is* the preview. Do not reintroduce a mutating mode behind a flag.

⚠️ **A manager-tier verb — never run it from a worker session.** A worker carries a *task*; a manager carries a topic or goal and a loop. Verifying a subject's tracked set from inside a worker collapses the two roles silently: the session keeps its task anchor while its turns report on another set's tree. A worker that needs a subject verified routes it to its manager with `SendMessage` and says so.

⚠️ **Why this stays a command and not a 20-line wrapper.** Three things an agent cannot do, and each is load-bearing here: **source 3** of the resolution chain reads the parent conversation, and a subagent runs in a fresh context; **`Skill` dispatch** exists only in the REPL, so the branch's instrument can only be called from here; and the **branch must be known before the instrument can be chosen**, which puts the probe on this side of the dispatch.

## Subject resolution — when `$1` is omitted

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` — the four-source chain, the case-insensitive vault test, the recording contract, the subject-status reconcile and the "no fallback, ever" clause. Read it there; **it is not restated here.**

**Only source 3 stays inline**, because it is the one an agent cannot reach:

3. **Conversation** — the most recent `/supervisor:goal-loop`, `/supervisor:goal-status`, `/supervisor:goal-drive` or `/supervisor:goal-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention).

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /supervisor:goal-verify "<name>"` and do nothing else.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs — the same reason `/vault-cli:task-status` prints `Detected task:`.

⚠️ **This copy can run none of the shared doc's executable contract, and that is deliberate — but every half must be stated, not just one.** Its 2026-09-20 read-only re-scope trimmed `Bash(python3:*)`, `Bash(mkdir:*)` **and `Bash(vault-cli:*)`**, so three things are unavailable here: the vault lookup, the recording block, and § *Reconcile the subject's status*. So it **resolves the vault the way it always has** — from cwd, through the literal `24 Goals/` / `23 Topics/` probes in § Branch detection, which already assume the vault root — **records nothing**, and **leaves the subject's `status` exactly as it finds it**, reporting that status rather than reconciling it. None of the three is a regression: this command could never run any of them. ⚠️ **The reconcile is the one that would read as an omission rather than a refusal** — a reader who knows `/goal-loop` and `/goal-drive` flip a `todo` subject to `in_progress` could take its absence here for a gap, so it is stated rather than left to be inferred. It is the write-scope carve-out in `docs/subject-resolution.md` § *Reconcile the subject's status*, which names this command and `/goal-status` as resolving-but-not-writing; § *Resolve the vault first* and § *Recording* carry the same shape for the other two halves.

## Branch detection — never assume one

Probe with the resolved `$SUBJECT`, never the raw argument (under detection the argument is empty and every probe would match nothing). ⚠️ **This probe is the documented exception to `agent-cmd/command-thin`** — the branch must be known *before* the `Skill` dispatch, and `Skill` exists only in the REPL, so the probe cannot move into the agent. Do not re-file it as leaked logic:

- `find "24 Goals" -maxdepth 1 -iname "$SUBJECT.md"` *(operator's vault; not shipped with this plugin)* → **goal branch**. The folder is the discriminator on this side — **no `page_type: goal` confirmation**; see `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` § The page test.
- `find "23 Topics" -maxdepth 1 -iname "$SUBJECT.md"` *(operator's vault; not shipped with this plugin)*, confirm `page_type: topic` **in the frontmatter block** → **topic branch**.
- **Both match, or neither** → print every candidate path (or `no match`) and **stop**. Never guess between a goal and a topic, and never silently prefer one.

**`-iname`, not a shell glob** — a plain `ls` glob is case-sensitive under zsh, so a lowercase argument resolves nothing against Title Case filenames.

⚠️ **Both folder names are literals, and that is a known defect left in place.** This step already prepended the hardcoded `23 Topics/` rather than resolving `topics_dir` from `vault-cli config` — the exact fault `verify-topic.md:18` fixed for its own path. It adds a second literal (`24 Goals`) rather than repairing the class; the repair belongs to a separate task, not here.

## Procedure

1. **Resolve the subject** (§ above) and print the `Subject:` line.

2. **Detect the branch** (§ above) and print the `Branch:` line.

3. **Call the branch's instrument** — the topic branch's `verify-topic`, or the goal branch's `/vault-cli:verify-goal`, with the subject. ⚠️ The independent-checks branch is **NOT TAKEN**: both verifiers landed, so this command calls them; forking would duplicate and drift. `verify-topic` is still vault-local (tracked by *a separate task*); in a vault without it, pass no instrument output and let the agent report step 1 as `UNKNOWN`.

4. **Dispatch the coverage mode — check 3's instrument, and the reason it no longer reports `GAP`.** The criterion-coverage check had no instrument of its own: `agents/goal-verify.md` check 3 read `GAP` by hand. It now consumes the same reverse map the drive rounds use. ⚠️ **The dispatch is this command's act and never the agent's** — that agent holds **no `Task` grant** (`agents/goal-verify.md:5`, `tools: Read, Bash`) and its own contract at `:69` says so outright, so a dispatch written into the agent would be a promise it cannot keep. One call per closure-box owner, the same split `commands/steps/goal-coverage.md` uses:

   - a tracked task's boxes → `Task(subagent_type: "vault-cli:task-manager-agent", prompt: "Action: coverage. TASK_PATH: <absolute path to the task file>. MODE: tool.")`
   - the subject page's own boxes → `Task(subagent_type: "vault-cli:goal-manager-agent", prompt: "Action: coverage. TASK_PATH: <absolute path to the subject page>. MODE: tool.")`

   ⚠️ **The row format, the evidence rating rule and the reconciliation line are not restated here** — they have one home in the agent. Pass the output to step 5 and let check 3 read it there.

5. **Dispatch the agent.**

   `Task(subagent_type: "supervisor:goal-verify", prompt: <subject + resolution source + branch + the instrument's output + the coverage output + vault + timestamp>)`

   The **`supervisor:` prefix is required** — a bare `goal-verify` resolves to a personal `~/.claude/agents/` copy, never the plugin agent. Its rules — the seven steps, the per-branch forks, the two-dispositions rule, the report frame — live in `agents/goal-verify.md` and are not restated here.

6. **Print what came back, verbatim.** Reproduce the agent's frame, its `Issues` block and its numbered fix list. If it returns a `Route:` line, that line is the subject's manager's to send — **do not** restate it as an `approve:` line for the operator.
