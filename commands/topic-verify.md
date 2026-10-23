---
description: Verify ONE topic and suggest the fixes — the topic tier's sixteen-row check (eleven page-consistency checks plus five review steps), read-only, ending in a numbered fix list
argument-hint: "[topic-name-or-path] — omit the name to resolve it from the session, like /supervisor:topic-status"
allowed-tools:
  - Read
  - Task
  - Skill
  - Bash(find:*)
  - Bash(awk:*)
  - Bash(grep:*)
  - Bash(vault-cli config list:*)
  - Bash(python3:*)
---

<objective>
Verify ONE **topic** and **suggest** the fixes. Resolve the subject, confirm it is a topic
page, resolve the vault's three folders, dispatch `supervisor:topic-verify`, and print what
it returns. **The checks, their forks and the report shape live in `agents/topic-verify.md`
and are not restated here.**
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

## Resolve the folders — once, from the vault's own config

The eleven checks read three folders, and each is **declared per vault, never fixed** — this
vault renumbered `23 Goals` → `24 Goals` and `24 Tasks` → `25 Tasks` on 2026-09-13. Resolve
all three from `vault-cli config list` against the session's cwd, before anything reads a page:

```bash
FOLDERS=$(vault-cli config list --output json 2>/dev/null | python3 -c "
import json,sys,os
cwd=os.path.realpath(os.getcwd())
d=json.load(sys.stdin)
v=next((x for x in d if os.path.realpath(os.path.expanduser(x['path']))==cwd), {})
print('|'.join([v.get('topics_dir') or '23 Topics', v.get('goals_dir') or '24 Goals', v.get('tasks_dir') or '25 Tasks']))
" 2>/dev/null)
if [ -n "$FOLDERS" ]; then
  IFS='|' read -r TOPICS_DIR GOALS_DIR TASKS_DIR <<< "$FOLDERS"
  echo "Folders: $TOPICS_DIR · $GOALS_DIR · $TASKS_DIR (from vault-cli config)"
else
  TOPICS_DIR="23 Topics"; GOALS_DIR="24 Goals"; TASKS_DIR="25 Tasks"
  echo "⚠️ Folders: vault-cli config unavailable — falling back to the default literals. A renumbered vault will read the wrong folders."
fi
```

⚠️ **Never skip the fallback, and never let it be silent.** Without it a vault whose config is
missing makes all three variables empty, `find ""` matches nothing, and every membership check
reports a topic with no members — a wrong answer that looks like a real one.

⚠️ **Only `awk`, `grep`, `find`, `python3` and this one `vault-cli config list` verb are
granted.** A pipeline reaching for `sed`, `tr` or `jq` parks on an approval this command was
never scoped for.

## This copy resolves but never writes

⚠️ **Every half stated, not just one.** Its read-only re-scope still trims `Bash(mkdir:*)` and
the broad `Bash(vault-cli:*)`, so two things remain unavailable here: **the recording block**
and **§ *Reconcile the subject's status***. What it *does* hold is a narrow
`Bash(vault-cli config list:*)` plus `Bash(python3:*)`, used for exactly one thing — the folder
resolution above, so the checks work in any vault instead of a hardcoded literal.

So this command **resolves**, **records nothing**, and **leaves the subject's `status` exactly
as it finds it**, reporting that status rather than reconciling it. None of that is a
regression: this command could never run any of them. ⚠️ **The reconcile is the one that would
read as an omission rather than a refusal** — a reader who knows `/supervisor:topic-loop` and
`/supervisor:topic-drive` flip a `todo` subject to `in_progress` could take its absence here
for a gap, so it is stated rather than left to be inferred. It is the write-scope carve-out in
`docs/subject-resolution.md` § *Reconcile the subject's status*, which names this command and
`/supervisor:topic-status` as resolving-but-not-writing.

## Branch check — never assume one

Probe with the resolved `$SUBJECT`, never the raw argument. ⚠️ **This probe is the
documented exception to `agent-cmd/command-thin`** — the branch must be known *before* the
`Skill` dispatch, and `Skill` exists only in the REPL:

- `find "$GOALS_DIR" -maxdepth 1 -iname "$SUBJECT.md"` → **a goal** → print `❌ "<name>" is a
  goal, not a topic — use /supervisor:goal-verify.` and stop.
- `find "$TOPICS_DIR" -maxdepth 1 -iname "$SUBJECT.md"`, confirm `page_type: topic` **in the
  frontmatter block** → **this command's branch**.
- **Neither** → print `no match` and **stop**. Never guess.

**`-iname`, not a shell glob** — a plain `ls` glob is case-sensitive under zsh.

## Procedure

1. **Resolve the subject** (§ above) and print the `Subject:` line.

2. **Resolve the folders** (§ above) and print the `Folders:` line.

3. **Confirm it is a topic** (§ above) and print the `Branch: topic` line.

4. **Dispatch the coverage mode** — one call per closure-box owner, the same split the goal
   tier uses:

   - each declared goal's boxes →
     `Task(subagent_type: "vault-cli:goal-manager-agent", prompt: "Action: coverage. TASK_PATH: <absolute path to the goal page>. MODE: tool.")`

   ⚠️ **The dispatch is this command's act and never the agent's** — that agent holds no
   `Task` grant, so a dispatch written into the agent would be a promise it cannot keep.

5. **Dispatch the agent.**

   `Task(subagent_type: "supervisor:topic-verify", prompt: <subject + resolution source + the resolved topics/goals/tasks folders + the coverage output + vault + timestamp>)`

   ⚠️ **There is no external instrument to call any more.** The eleven checks live in
   `agents/topic-verify.md`, so this step passes the folders and lets the agent read the page
   itself — the old `verify-topic` call and its `UNKNOWN — instrument absent` branch are gone.

   The **`supervisor:` prefix is required** — a bare `topic-verify` resolves to a personal
   `~/.claude/agents/` copy, never the plugin agent.

6. **Print what came back, verbatim.** Reproduce the agent's frame, its `Issues` block and
   its numbered fix list. If it returns a `Route:` line, that line is the topic's manager's
   to send — **do not** restate it as an `approve:` line for the operator.
