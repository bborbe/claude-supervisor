---
description: Verify ONE topic and suggest the fixes — the topic tier's sixteen-row check (eleven page-consistency checks plus five review rows), read-only, ending in a numbered fix list
argument-hint: "[topic-name] — a bare name, never a path; omit it to resolve from the session, like /supervisor:topic-status"
allowed-tools:
  - Read
  - Task
  - Bash(find:*)
  - Bash(awk:*)
  - Bash(grep:*)
  - Bash(python3:*)
---

<objective>
Verify ONE **topic** and **suggest** the fixes. Resolve the subject, confirm it is a topic page,
resolve the vault's three folders, dispatch `supervisor:topic-verify`, and print what it returns.
**The checks, their forks and the report shape live in `agents/topic-verify.md`.**
</objective>

⚠️ **READ-ONLY.** It diagnoses and advises; it does not prune, author, plan or spawn. Every
mutating step is a **suggestion in the report**. There is **no `--dry-run` and none is needed** —
the whole run *is* the preview. Do not reintroduce a mutating mode behind a flag.
⚠️ **A topic-tier verb — never run it from a worker session.** A worker carries a task, a manager
a subject and a loop; a worker needing a subject verified routes it to its manager with
`SendMessage` and says so.

## Subject resolution — when `$1` is omitted

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md`. Read it there; it
is not restated here. **Only source 3 stays inline** — it is the one an agent cannot reach:

3. **Conversation** — the most recent `/supervisor:topic-*` argument in this conversation, then
   the most recent **topic** page referenced as a subject.

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a topic name: /supervisor:topic-verify "<name>"`.

**Print the source** on the first line: `Subject: <name> (from <explicit|session|name|conversation|last>)`.

## Resolve the folders — once, from the vault's own config

The eleven checks read three folders, each **declared per vault, never fixed**. One call resolves
all three, for the vault owning the session's cwd **or its nearest ancestor**:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/resolve-folders.py"
```

It prints **absolute** `TOPICS_DIR|GOALS_DIR|TASKS_DIR` and exits 0, or `UNKNOWN <reason>` and
exits 1 — never a bare `23 Topics`, which resolves only from the vault root. ⚠️ **On `UNKNOWN`,
stop and report that reason** against every row that reads a folder; never substitute the
literals, because three empty paths make every membership check report a topic with no members.
⚠️ **Never fall back to the marketplace clone** — it is a *different, usually stale* copy, so a borrow runs an old resolver against a new command; an unset `CLAUDE_PLUGIN_ROOT` is `SKIPPED — resolver not found`.

⚠️ **Only `awk`, `grep`, `find` and this plugin's own scripts are granted** — a pipeline reaching
for `sed`, `tr`, `jq` or `vault-cli` parks on an approval this command was never scoped for, and
`find` is for reads only (never `-delete`, never `-exec`).
⚠️ **This copy resolves but never writes.** Its re-scope trims `Bash(mkdir:*)` and the broad
`Bash(vault-cli:*)`, so the recording block and § *Reconcile the subject's status* are unavailable
here: it resolves, records nothing, and leaves `status` as it finds it — the carve-out at
`docs/subject-resolution.md:123`.

## Branch check — never assume one

Probe with the resolved `$SUBJECT` — a bare name, so a path or `.md` suffix is reduced to its
basename first. ⚠️ **The branch must be known *before* the `Task` dispatch**, which is why this
probe is the command's own.

- **Both** `find "$GOALS_DIR" -maxdepth 1 -iname "$SUBJECT.md"` and the same under `$TOPICS_DIR`
  match → print **every** candidate path and **STOP**. Never silently prefer one.
- **Goal only** → print `❌ "<name>" is a goal, not a topic — use /supervisor:goal-verify.` and stop.
- **Topic only**, confirming `page_type: topic` in the frontmatter block → **this branch**.
- **Topic only, but no `page_type: topic`** → print `❌ "<name>" exists under the topics folder but
  does not declare page_type: topic — fix its frontmatter first.` and **STOP**. Never run the
  checks against a page that has not declared itself a topic.
- **Neither** → print `no match` and **STOP**. Never guess.

**`-iname`, not a shell glob** — a plain `ls` glob is case-sensitive under zsh.

## Procedure

1. Resolve the subject (§ above); print `Subject:`.
2. Resolve the folders (§ above); print `Folders:`.
3. Take the branch (§ above); print `Branch: topic`.
4. **Dispatch the coverage mode** — one call per declared goal page:

   `Task(subagent_type: "vault-cli:goal-manager-agent", prompt: "Action: coverage. TASK_PATH: <absolute path to the goal page>. MODE: tool.")`

   ⚠️ **The dispatch is this command's act, never the agent's** — that agent holds no `Task`
   grant, so a dispatch written into the agent would be a promise it cannot keep.

5. **Dispatch the agent:**

   `Task(subagent_type: "supervisor:topic-verify", prompt: <subject + resolution source + the three folders + the coverage output + vault + timestamp>)`

   ⚠️ **No external instrument to call any more** — the eleven checks live in
   `agents/topic-verify.md`, so this step passes the folders and lets the agent read the page
   itself; the old `verify-topic` call is gone, step 2 reports the empty-folder `UNKNOWN`, and
   the **`supervisor:` prefix is required** — a bare `topic-verify` hits a personal agents copy.

6. **Print what came back, verbatim** — the frame, the `Issues` block and the numbered fix list.
   A `Route:` line is the topic manager's to send; **do not** restate it as an `approve:` line.
