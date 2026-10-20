---
description: Verify ONE topic and suggest the fixes — the topic tier's seven gated checks, read-only, ending in a numbered fix list
argument-hint: "[topic-name-or-path] — omit the name to resolve it from the session, like /supervisor:topic-status"
allowed-tools:
  - Read
  - Task
  - Skill
  - Bash(find:*)
  - Bash(awk:*)
  - Bash(grep:*)
---

<objective>
Verify ONE **topic** and **suggest** the fixes. Resolve the subject, confirm it is a topic
page, call the topic's own instrument, dispatch `supervisor:topic-verify`, and print what it
returns. **The checks, their forks and the report shape live in `agents/topic-verify.md` and
are not restated here.**
</objective>

⚠️ **This command is READ-ONLY.** It **diagnoses and advises**; it does not prune, author,
plan or spawn. Every mutating step is a **suggestion in the report**. There is **no
`--dry-run`, and none is needed** — the whole run *is* the preview. Do not reintroduce a
mutating mode behind a flag.

⚠️ **A topic-tier verb — never run it from a worker session.** A worker carries a task; a
manager carries a subject and a loop. A worker that needs a subject verified routes it to its
manager with `SendMessage` and says so.

⚠️ **A goal name is the wrong tier** — print `❌ "<name>" is a goal, not a topic — use
/supervisor:goal-verify.` and stop. The goal tier has its own checks against a goal's own
task set.

## Subject resolution — when `$1` is omitted

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md`. Read it
there; **it is not restated here.**

**Only source 3 stays inline**, because it is the one an agent cannot reach:

3. **Conversation** — the most recent `/supervisor:topic-loop`, `/supervisor:topic-status`,
   `/supervisor:topic-drive` or `/supervisor:topic-verify` argument in this conversation,
   then the most recent **topic** page referenced as a subject.

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a topic name: /supervisor:topic-verify "<name>"` and do nothing else.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`.

⚠️ **This copy can run none of the shared doc's executable contract, and that is deliberate —
but every half must be stated, not just one.** Its read-only re-scope trims
`Bash(python3:*)`, `Bash(mkdir:*)` **and `Bash(vault-cli:*)`**, so three things are
unavailable here: the vault lookup, the recording block, and § *Reconcile the subject's
status*. So it **resolves the vault the way it always has** — from cwd, through the literal
topics-folder probe below — **records nothing**, and **leaves the subject's `status` exactly
as it finds it**, reporting that status rather than reconciling it. None of the three is a
regression: this command could never run any of them. ⚠️ **The reconcile is the one that
would read as an omission rather than a refusal** — a reader who knows `/supervisor:topic-loop`
and `/supervisor:topic-drive` flip a `todo` subject to `in_progress` could take its absence
here for a gap, so it is stated rather than left to be inferred. It is the write-scope
carve-out in `docs/subject-resolution.md` § *Reconcile the subject's status*, which names
this command and `/supervisor:topic-status` as resolving-but-not-writing.

## Branch check — never assume one

Probe with the resolved `$SUBJECT`, never the raw argument. ⚠️ **This probe is the
documented exception to `agent-cmd/command-thin`** — the branch must be known *before* the
`Skill` dispatch, and `Skill` exists only in the REPL:

- `find "24 Goals" -maxdepth 1 -iname "$SUBJECT.md"` *(operator's vault; not shipped with
  this plugin)* → **a goal** → print `❌ "<name>" is a goal, not a topic — use
  /supervisor:goal-verify.` and stop.
- `find "$TOPICS_DIR" -maxdepth 1 -iname "$SUBJECT.md"`, confirm `page_type: topic` **in the
  frontmatter block** → **this command's branch**.
- **Neither** → print `no match` and **stop**. Never guess.

**`-iname`, not a shell glob** — a plain `ls` glob is case-sensitive under zsh.

## Procedure

1. **Resolve the subject** (§ above) and print the `Subject:` line.

2. **Confirm it is a topic** (§ above) and print the `Branch: topic` line.

3. **Call the topic's instrument** — `verify-topic` with the subject. ⚠️ The
   independent-checks branch is **NOT TAKEN**: the instrument landed, so this command calls
   it; forking would duplicate and drift. `verify-topic` is vault-local; in a vault without
   it, pass no instrument output and let the agent report step 1 as `UNKNOWN`.

4. **Dispatch the coverage mode** — one call per closure-box owner, the same split the goal
   tier uses:

   - each declared goal's boxes →
     `Task(subagent_type: "vault-cli:goal-manager-agent", prompt: "Action: coverage. TASK_PATH: <absolute path to the goal page>. MODE: tool.")`

   ⚠️ **The dispatch is this command's act and never the agent's** — that agent holds no
   `Task` grant, so a dispatch written into the agent would be a promise it cannot keep.

5. **Dispatch the agent.**

   `Task(subagent_type: "supervisor:topic-verify", prompt: <subject + resolution source + the instrument's output + the coverage output + vault + timestamp>)`

   The **`supervisor:` prefix is required** — a bare `topic-verify` resolves to a personal
   `~/.claude/agents/` copy, never the plugin agent.

6. **Print what came back, verbatim.** Reproduce the agent's frame, its `Issues` block and
   its numbered fix list. If it returns a `Route:` line, that line is the topic's manager's
   to send — **do not** restate it as an `approve:` line for the operator.
