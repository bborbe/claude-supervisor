---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Resolve — the topic and its declared goals

When: manager start; the declaration read re-runs every tick

Rationale: `docs/subject-resolution.md` and `commands/topic-loop.md` § Resolution.

1. **Resolve the vault first — from the session's cwd, never a default.**

   ```bash
   VAULT=$(vault-cli config list --output json | python3 -c "import json,sys,os;cwd=os.path.realpath(os.getcwd());print(next((v['name'] for v in json.load(sys.stdin) if os.path.realpath(os.path.expanduser(v['path']))==cwd),''))")
   ```

   No match → STOP with `❌ cwd is not a configured vault — run from the vault root`. **Never
   fall back to a default vault.**

2. **Resolve the topics folder from config, then the page.** `vault-cli config list --output
   json` → this vault's `topics_dir` (default `23 Topics`) → `find "$TOPICS_DIR" -maxdepth 1
   -iname "$SUBJECT.md"` → confirm `page_type: topic` **inside the frontmatter block**.
   ⚠️ **`-iname`, not a shell glob** — a plain `ls` glob is case-sensitive under zsh.

   ⚠️ **A name that resolves to a goal page is the wrong tier.** Print `❌ "<name>" is a goal,
   not a topic — use /supervisor:goal-loop.` and stop. Never take the goal branch here.

3. **Read the `## Goals` list — the topic's declared membership.** ⚠️ **The heading is
   `## Goals`, a sub-heading under `# Scope`, not `# Goals`** — a literal `^# Goals` match
   returns nothing, and with no fallback that reads as an empty set rather than a miss.

   **Declared, never re-derived.** An entry may be a goal or a task; a goal admits the tasks
   that name it, and a task entry admits that task directly. Never widen the list by a
   filename glob, a theme match or a content grep.

4. **Record the resolution — a step to run, not a reference to follow.** Having resolved a
   subject, run the recording block at `docs/subject-resolution.md` § Recording **now**,
   before the sweep. ⚠️ **Which files on which resolution is the doc's asymmetry** — running
   it on an *inferred* subject wrongly promotes that subject into cross-session state for
   every later tick.

   Then read it back and say which happened: `✅ Registered: <subject>` or
   `⚠️ Not registered — the recording step did not run`, then run it.

4b. **⚠️ Record the manager's session on the TOPIC page — the durable half, and a
   step to run.** Write this session's id onto the topic page:
   `vault-cli topic set "<subject>" manager_session_id "$CLAUDE_CODE_SESSION_ID" --vault "<vault>"`,
   then read the page back and say what it now holds.

   ⚠️ **Without this write the topic tier has no durable record of its own
   manager at all.** `commands/steps/goal-resolve.md` names "the `topic` verb for
   a topic page" but nothing in the topic branch called it, so the fleet
   manager's keep-alive of a topic manager read a stale or absent id and could
   not tell a stopped manager from a running one.

   ⚠️ **The writer carve-out is the same one § Reconcile the subject's status
   carries** — `topic-loop` and `topic-drive` write it; `topic-status` is
   read-only by its own contract and `topic-verify` holds no `Bash(vault-cli:*)`,
   so neither writes. Both still resolve and still report the id they find.

   ⚠️ **`--vault` is not optional:** without it the write targets the default
   vault, so a subject in any other vault lands on the wrong page or nowhere.

   ⚠️ **This field is a lead, never a verdict.** Every reader runs
   `session-liveness.py --check` on it first and takes the action that exit code
   maps to in `docs/fleet-surface.md` § *Which manager owns a subject* — the id
   is evidence of *who*, never of *alive*.

5. **⚠️ Reconcile the subject's status — a step to run, not a reference to follow.** Having resolved a subject, run `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` § *Reconcile the subject's status* **now** — once the page and the branch are known, and before the sweep. ⚠️ **That precondition is this command's own page test, wherever the file puts it** — not this line: where the two sit in different sections, run the step at the page test. Read the subject page's frontmatter `status` and, when it reads `todo`/`next` **or is absent**, set it to `in_progress`; then read the page back and say what it now reads — or say plainly that the write failed, never proceeding as though it landed. ⚠️ **Once at resolution, never again mid-run**, and it writes `in_progress` over `todo`/`next`/absent **only** — an already-`in_progress` subject, and one at `hold`/`backlog`/terminal, is left alone. ⚠️ **The rule, its rationale, the exact commands and the write-scope table live in the doc — read them there; they are not restated here.**

   ⚠️ **This is a resolution-time write, never closure.** The topic's `# Success Criteria` still close mechanically through the vault's own completion path.

6. **Print the resolution header**, then the declared set once:

   ```text
   Subject: [<name>](obsidian://open?vault=<V>&file=<relpath>) (from <source>)
   Branch: topic (<the page it came from>)
   Tracked (N): <goal> · <goal> …
   ```

   Re-print it whenever the declaration read changes it. Membership is **never** widened
   mid-run; the subject and branch detection resolve **once** at manager start.
