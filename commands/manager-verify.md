---
description: Verify one topic or goal and suggest the fixes — seven gated checks, read-only, ending in a numbered fix list
argument-hint: "[topic-or-goal-name-or-path] — omit the name to resolve it from the session, like /supervisor:manager-status"
allowed-tools:
  - Read
  - Task
  - Skill
  - Bash(find:*)
  - Bash(awk:*)
  - Bash(grep:*)
---

<objective>
Verify ONE subject — **a topic or a goal** — and **suggest** the fixes. Resolve the subject, detect the branch, call that branch's instrument, dispatch `supervisor:manager-verify`, and print what it returns. **The seven gated checks, their per-branch forks and the report shape live in `agents/manager-verify.md` and are not restated here.**
</objective>

⚠️ **This command is READ-ONLY, re-scoped 2026-09-20 on the operator's instruction:** *"we don't need 20 different commands to do stuff. We have worker status that shows the status and then we have worker verify that should verify and suggest fix."* It **diagnoses and advises**; it does not prune, author, plan or spawn. Every mutating step is a **suggestion in the report**. There is **no `--dry-run`, and none is needed** — the flag was removed 2026-09-20 in the same pass, so the whole run *is* the preview. Do not reintroduce a mutating mode behind a flag.

⚠️ **A manager-tier verb — never run it from a worker session.** A worker carries a *task*; a manager carries a topic or goal and a loop. Verifying a subject's tracked set from inside a worker collapses the two roles silently: the session keeps its task anchor while its turns report on another set's tree. A worker that needs a subject verified routes it to its manager with `SendMessage` and says so.

⚠️ **Why this stays a command and not a 20-line wrapper.** Three things an agent cannot do, and each is load-bearing here: **source 3** of the resolution chain reads the parent conversation, and a subagent runs in a fresh context; **`Skill` dispatch** exists only in the REPL, so the branch's instrument can only be called from here; and the **branch must be known before the instrument can be chosen**, which puts the probe on this side of the dispatch.

## Subject resolution — when `$1` is omitted

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` — the four-source chain, the case-insensitive vault test, the recording contract and the "no fallback, ever" clause. Read it there; **it is not restated here.**

**Only source 3 stays inline**, because it is the one an agent cannot reach:

3. **Conversation** — the most recent `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` or `/supervisor:manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention).

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /supervisor:manager-verify "<name>"` and do nothing else.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs — the same reason `/vault-cli:task-status` prints `Detected task:`.

⚠️ **This copy can run neither half of the shared doc's executable contract, and that is deliberate — but both halves must be stated, not just one.** Its 2026-09-20 read-only re-scope trimmed `Bash(python3:*)`, `Bash(mkdir:*)` **and `Bash(vault-cli:*)`**, so the vault lookup and the recording block are both unavailable here. So it **resolves the vault the way it always has** — from cwd, through the literal `24 Goals/` / `23 Topics/` probes in § Branch detection, which already assume the vault root — and **records nothing**. Neither divergence is a regression: this command could never run either block. Both are named in `docs/subject-resolution.md` § *Recording*, which carries the same carve-out.

## Branch detection — never assume one

Probe with the resolved `$SUBJECT`, never the raw argument (under detection the argument is empty and every probe would match nothing). ⚠️ **This probe is the documented exception to `agent-cmd/command-thin`** — the branch must be known *before* the `Skill` dispatch, and `Skill` exists only in the REPL, so the probe cannot move into the agent. Do not re-file it as leaked logic:

- `find "24 Goals" -maxdepth 1 -iname "$SUBJECT.md"` → **goal branch**. The folder is the discriminator on this side — **no `page_type: goal` confirmation**; see `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` § The page test.
- `find "23 Topics" -maxdepth 1 -iname "$SUBJECT.md"`, confirm `page_type: topic` the same way → **topic branch**.
- **Both match, or neither** → print every candidate path (or `no match`) and **stop**. Never guess between a goal and a topic, and never silently prefer one.

**`-iname`, not a shell glob** — a plain `ls` glob is case-sensitive under zsh, so a lowercase argument resolves nothing against Title Case filenames.

⚠️ **Both folder names are literals, and that is a known defect left in place.** This step already prepended the hardcoded `23 Topics/` rather than resolving `topics_dir` from `vault-cli config` — the exact fault `verify-topic.md:18` fixed for its own path. It adds a second literal (`24 Goals`) rather than repairing the class; the repair belongs to `[[Vault-Cli Slash Commands Resolve Folders From Config, Not Literals]]`, not here.

## Procedure

1. **Resolve the subject** (§ above) and print the `Subject:` line.

2. **Detect the branch** (§ above) and print the `Branch:` line.

3. **Call the branch's instrument** — the topic branch's `verify-topic`, or the goal branch's `/vault-cli:verify-goal`, with the subject. ⚠️ The independent-checks branch is **NOT TAKEN**: both verifiers landed, so this command calls them; forking would duplicate and drift. `verify-topic` is still vault-local (tracked by *Move verify-topic Into the Vault-Cli Plugin*); in a vault without it, pass no instrument output and let the agent report step 1 as `UNKNOWN`.

4. **Dispatch the agent.**

   `Task(subagent_type: "supervisor:manager-verify", prompt: <subject + resolution source + branch + the instrument's output + vault + timestamp>)`

   The **`supervisor:` prefix is required** — a bare `manager-verify` resolves to a personal `~/.claude/agents/` copy, never the plugin agent. Its rules — the seven steps, the per-branch forks, the two-dispositions rule, the report frame — live in `agents/manager-verify.md` and are not restated here.

5. **Print what came back, verbatim.** Reproduce the agent's frame, its `Issues` block and its numbered fix list. If it returns a `Route:` line, that line is the subject's manager's to send — **do not** restate it as an `approve:` line for the operator.
