---
description: Run the manager loop for ONE topic OR ONE goal — watch its declared set + task files (~5 min), detect problems, TTS the human (voice-mode gated), print a status table, recommend new sessions and session closes. Name via $1, or detected from the session when omitted; the branch (topic vs goal) is auto-detected, never guessed. Per the vault's Manager Session runbook.
allowed-tools:
  - Task
  - Read
  - Edit
  - Monitor
  - AskUserQuestion
  - Bash(grep:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - Bash(awk:*)
  - Bash(mkdir:*)
  - Bash(vault-cli:*)
  - Bash(pgrep:*)
  - Bash(ps:*)
  - Bash(stat:*)
  - Bash(find:*)
  - Bash(date:*)
  - Bash(python3:*)
  - Bash(echo:*)
  - Bash(wezterm cli spawn:*)
  - Bash(wezterm cli get-text:*)
  - Bash(wezterm cli send-text:*)
  - Bash(wezterm cli list:*)
  - ListAgents
  - SendMessage
  - ScheduleWakeup
  - mcp__tts__say
  - mcp__supervisor__spawn_agent
  - mcp__supervisor__list_agents
  - mcp__supervisor__answer_permission
  - mcp__supervisor__await_permission
argument-hint: "[goal|topic] (detected when omitted)"
---

Manager-loop slash command — the **narrow / deep** layer. Per the vault's Manager Session runbook.

Rationale, measured evidence and incident history for the condensed rules below live in the vault runbook: [[Manager Session]] § Command rationale (moved from /manager-loop). This file carries only what a sweep executes.

One manager per topic: owns ONE topic's declared goal set (e.g. the *Sentry* topic, the *notification* topic), read from that topic's page. The fleet manager (wide layer) may run above. **Enforced, not merely asserted** — Resolution step 6 confirms no manager already owns the name before arming, and stops on a hit.

⚠️ **Never invoke this command from a worker session.** A worker carries a task; a manager carries a topic or goal plus a loop. Starting a manager is a human act, in a session created for it. Rule: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles — who may start what.

## Manager contract — a manager manages, it does not build

The manager **manages; it does not build**. It never performs the topic's *work*: no code edits, no repo/PR/k8s verification, no investigation, no real debugging — not even small ones. (Owner rule, 2026-09-11; boundary restated by the operator 2026-09-18.)

- **The line is work vs management — not read vs write.** Management writes are the manager's own — tasks, task/goal pages, `status`/`phase`, stale stamps, topic-page scope and write-back — via `Edit` / `vault-cli task set`. Code, verification, investigation, debugging and the fix go to a worker. **Delegate the work, not the bookkeeping.**
- ⚠️ **This replaces the older "the topic page is the only file the manager may write" rule** (operator override, 2026-09-18). That rule made a one-field status flip a forbidden act and produced a real stall: a task sat `hold` while the manager could only hand the operator a command. Do not reintroduce it as a "fix" — quote this paragraph instead.
- **Never widen past the boundary.** A task-file write is management only while it changes *tracking state* (status, phase, dates, stamps, scope). If it needs the code read, a root cause judged, or a runtime fact verified, it is work — delegate it.
- **No worker → spawn one.** Ready-to-start / unowned / orphaned-restart work is started as a new Claude Code session via the Spawn mechanics below — never done in-line by the manager.
- **Task/goal anchored, always.** No work is delegated or spawned without a task (or goal) anchoring it; an unanchored request is not work. Read-only manager business (sweep, table, TTS) needs no anchor; anything else gets a task first (`/vault-cli:create-task "<title>"`).

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **subject** — a goal name matching a page in `24 Goals/`, or a topic name matching a topic page in `23 Topics/`. The branch is detected from whichever page resolves (step 0); this command never assumes one.
  - `/manager-loop "Stop Nil-Slice → Null Crashes on MDM Party Surfaces"` → goal branch
  - `/manager-loop Sentry` → reads `23 Topics/Sentry.md`, tracks its `## Goals` members + their tasks
  - `/manager-loop` (bare) → resolve the subject from § Subject resolution below
  - No page resolves → the manager reports it and stops; it never globs a substitute set
- For one look without a loop, run `/manager-status` (read-only); for one act pass, run `/manager-drive`. There is no one-sweep flag: a single sweep still spawns, resumes and writes back, so it was never a read-only look.

## Subject resolution — when `$1` is omitted

**Keep-in-sync block** — shared with `/manager-status`, `/manager-drive` and `/manager-verify`; the resolution rule is identical in all four. Enumerated differences, never counted: between the three plugin copies — the sibling list here, the STOP line, the "No fallback, ever" clause, and the closing write-contract sentence; `/manager-verify` also differs in the recording paragraphs (prose there, since its `allowed-tools` lacks `Bash(python3:*)` / `Bash(mkdir:*)`). `/manager-drive` also carries a leading **vault-resolution paragraph** (cwd → vault-cli config path), because it has no `## Resolution` section of its own to derive the vault in. Change one, change the others.

A bare invocation takes the first source that yields a **real page**:

1. **Session state** — `~/.claude/state/worker-manager/<CLAUDE_CODE_SESSION_ID>.json`, shape `{"subject","vault","branch","resolved_at"}`. Accepted only when `vault` matches the resolved vault **and** `subject` still resolves to a page. Compare **case-insensitively** — the vault is the lowercase vault-cli config `name` (`personal`), but state files written before 2026-09-23 may carry display case (`Personal`); a strict match silently drops to source 2. This is what makes a subject named once stick across the ticks of one session.
2. **Session name** — `~/.claude/sessions/$CLAUDE_PID.json` → `.name` (pid-keyed: `CLAUDE_PID`, not `CLAUDE_CODE_SESSION_ID`). Strip leading decoration (`⚙ `); accept only if what remains resolves to a goal or topic page. No match, a task-page match or a missing pid file → silent miss, fall through.
3. **Conversation** — the priority order `/vault-cli:task-status` uses in its Phase 2: the most recent `/manager-loop`, `/manager-status`, `/manager-drive` or `/manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention).
4. **Vault's last subject** — `~/.claude/state/worker-manager/last-<vault>.json`, same shape and same test. Last resort: the only source not about this session.
5. **Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /manager-loop "<name>"` and do nothing else.

**No fallback, ever.** Never a filename glob, a `goals:` scan, a theme match, or a content grep. The `$1` no-fallback rule exists because scope-by-globbing is the failure it prevents; a silent guess at the *subject* is the same failure one level up, and worse here — the manager would sweep, TTS and write back against the wrong tree, and every one of those writes lands somewhere real.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs — the same reason `/vault-cli:task-status` prints `Detected task:` before its report.

**Record on explicit use.** When `$1` is supplied and resolves, write **both** files before proceeding:

```bash
mkdir -p ~/.claude/state/worker-manager && python3 -c "
import json,os,sys,datetime
subject,vault,branch=sys.argv[1:4]; vault=vault.lower()
d=os.path.expanduser('~/.claude/state/worker-manager'); os.makedirs(d,exist_ok=True)
rec={'subject':subject,'vault':vault,'branch':branch,'resolved_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
for n in (os.environ['CLAUDE_CODE_SESSION_ID']+'.json','last-'+vault+'.json'):
    json.dump(rec,open(os.path.join(d,n),'w'),indent=2)
" "$SUBJECT" "$VAULT" "$BRANCH"
```

**Record a session-local resolution too — but only the session file.** When a bare invocation resolves from the **session name** or the **conversation**, write `<CLAUDE_CODE_SESSION_ID>.json` and **never** `last-<vault>.json`, so the subject sticks across this session's later ticks without republishing this session's identity to every other session in the vault:

```bash
mkdir -p ~/.claude/state/worker-manager && python3 -c "
import json,os,sys,datetime
subject,vault,branch=sys.argv[1:4]; vault=vault.lower()
d=os.path.expanduser('~/.claude/state/worker-manager'); os.makedirs(d,exist_ok=True)
rec={'subject':subject,'vault':vault,'branch':branch,'resolved_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
json.dump(rec,open(os.path.join(d,os.environ['CLAUDE_CODE_SESSION_ID']+'.json'),'w'),indent=2)
" "$SUBJECT" "$VAULT" "$BRANCH"
```

**Never write on a `last-<vault>` resolution — neither file.** A subject taken from that file was itself only inferred, and promoting it into session state would pin it above this session's own name for every later tick. The widening is asymmetric on purpose: session-local sources may be recorded, the cross-session one may not.

**A bare invocation writes at most the session file** — `<CLAUDE_CODE_SESSION_ID>.json`, and only on a session-local resolution; never `last-<vault>`, so a detected-subject sweep still adds no cross-session write the manager did not already have. `/manager-status` reads the same files, which is what makes a subject named in one command visible to the other.

## Resolution — detect the branch, then read the declared set

A name resolves to **either a topic or a goal**; the branch is **detected, never assumed**. Membership is **declared, never re-derived** — never widened by a glob, a theme match or a content grep. Goals and topics close differently (step G).

**The `goals:` declaration has three shapes — match all three.** Membership is read from frontmatter, so the parser must handle every shape the vault actually writes. Census over `25 Tasks/`, 2026-09-18 (826 task files):

| shape | example | count |
|---|---|---|
| list of `[[wikilinks]]` | `goals:` / `    - '[[X]]'` | 385 |
| plain scalar, no brackets | `goals: X` | 2 |
| nested list-of-list, no brackets | `goals:` / `    - - X` | 1 |

**Normalize every shape to the bare goal name, repeating until stable:** strip `[[` `]]`, strip quote marks, strip every leading `- `; re-run all three while anything changed. One fixed-order pass silently drops list or nested entries.

**A wikilink-only extractor (`\[\[([^\]]+)\]\]`) is non-compliant** — it drops the scalar and nested shapes and the set renders silently smaller.

**An empty declaration is not a shape.** `goals: []`, `goals: null` and a bare `goals:` name no goal and are correctly excluded by every parser — do not fold them into the census. 39 of the 42 non-wikilink declarations in `25 Tasks/` are empty, so counting them as misses overstates the damage roughly thirteenfold.

0. **Detect the branch before resolving anything.** Probe with the **resolved subject**, not `$1` — under detection `$1` is empty and every probe below would match nothing.
   - `find "24 Goals" -maxdepth 1 -iname "$SUBJECT.md"`, then confirm `page_type: goal` **in the frontmatter block** (`awk '/^---$/{n++; next} n==1' <page> | grep -q '^page_type: goal'`) → **goal branch, step G**.
   - `find "$TOPICS_DIR" -maxdepth 1 -iname "$SUBJECT.md"`, then confirm `page_type: topic` the same way → **topic branch, steps 1–4**.
   - **Exact basename, case-insensitive: `-iname "$SUBJECT.md"`** — never a substring glob (topics collide with member goals named after them), never `-name`. Under exact matching a same-folder double match is impossible, so the refusal below applies in both branches.
   - **Both match, or neither** → print every candidate path (or `no match`) and **stop**. Never guess between a goal and a topic, and never silently prefer one. A name that matches neither is not tracked — that is the intended pressure.
   - Print the branch and the page it came from in the header, e.g. `Branch: goal (24 Goals/<Goal>.md)`. A mis-resolved branch must be visible immediately.

   **Step G — goal branch.** Read `24 Goals/<Goal>.md`.
   - **Tracked set** = every task whose `goals:` frontmatter names this goal — exact match on the goal name, **all three shapes** (see the census above; a wikilink-only match is non-compliant). This is the declaration; there is no second source.
   - **Cross-check against the goal's `# Tasks` list.** A task listed there whose `goals:` does *not* name the goal — or a task whose `goals:` names it but which is absent from `# Tasks` — is a **finding to report**, never silently unioned. The two disagreeing means the declaration drifted, and that is the information.
   - **Closure is not yours.** The goal's `# Success Criteria` are its closure contract and they close **mechanically**. Report SC status and hand closure to `/vault-cli:complete-goal` — never tick SC or flip the goal's status yourself. A goal closing on criteria vs a topic closing on a judged gate is the branch, not a size difference.
   - **Skip steps 1–5** — every one of them is topic-only. There is no topic page, no `## Goals` list, no declared-optional set and no `# Status Summary` on a goal, and **step 5's missing-topic-page stop would fire spuriously on a goal branch**. Do not synthesise them.
   - Everything else — the sweep, buckets, orphan detection, the drive dispatch, spawn mechanics, the status table, TTS, guardrails — is **identical to the topic branch**, because the loop is level-independent.

1. **Resolve the topics folder from config, then find the topic page**: `TOPICS_DIR=$(vault-cli config list --output json 2>/dev/null | python3 -c "import json,sys,os;cwd=os.path.realpath(os.getcwd());d=json.load(sys.stdin);print(next((v.get('topics_dir') or '23 Topics' for v in d if os.path.realpath(os.path.expanduser(v['path']))==cwd),'23 Topics'))" 2>/dev/null || echo "23 Topics")`, then `find "$TOPICS_DIR" -maxdepth 1 -iname "$SUBJECT.md"`, then confirm `page_type: topic` **in the frontmatter block** (`awk '/^---$/{n++; next} n==1' <page> | grep -q '^page_type: topic'`). `-iname`, never a case-sensitive shell glob. Never accept `Topic Writing Guide.md` (no `page_type`; an unscoped grep matches its template). A `24 Goals/` match is a goal → step G.
2. **Read its `## Goals` list** (a sub-heading under `# Scope`, not `# Goals`). An entry may be a goal or a task. Tracked set = every declared goal + every task whose `goals:` names one of them (**all three shapes**) + every task named directly as an entry. Read from the page — never inferred from a task's own `goals:`, its status, or a scan.
3. **Resolve the declared-optional set** — the subset of tracked **task** names the page declares optional. Read it once at manager start, alongside the tracked set; like the tracked set it **does not widen mid-run** (Guardrail 5). Full rule: the vault's `Manager Session` runbook § Step 4. Two traps, restated because getting either wrong is silent:
   - ⚠️ **Match semantically, not on a literal string** — optional may be declared as a wikilink in the Current-work / Goals lists or as a backticked name in a **Non-goals** bullet, in varying wording.
   - ⚠️ **Never infer it from `status`.** `hold`/`backlog` are dispositions, `optional` is a declaration, and they are orthogonal. Where a topic's optional tasks happen to be `hold`/`backlog`, a status-keyed shortcut renders that page correctly and mis-groups every other topic.
   - **Member goals render as their own rows, tasks indented beneath.** The optional set is read at whichever level the page declares: a goal (all its tasks optional) or a task. A required goal owning an optional task appears in both sections — not a defect.
   - Nothing declared optional → empty set, and the sweep renders one unlabelled box as before.
4. **Read its `# Status Summary`** — hand-written context, not a source of truth; never add a counter. A change-tick reconciles it from task-file facts (runbook § Write-back on change).
5. **Topic branch only — no topic page** → print the missing page name and the fix (`create 23 Topics/<Topic>.md from the Topic Writing Guide template`), then **stop**. No fallback — no filename glob, `goals:` scan, theme match or content grep.

6. **Confirm no manager already owns this name — before arming anything.** `ListAgents` for `<Topic> Manager` (goal branch: the goal's manager). Hit → print its name and age and **stop**. Miss → re-read the roster **once**; still a miss → proceed and arm. Never probe with `pgrep -f`. Rule: runbook § Step 0.

Print the resolved set once at manager start (`Tracked (N): <task> · <task> …`) and never widen it mid-run. If the human names a set explicitly, track that set for this run — but say so in the header, since it diverges from the page.

## The open-items ledger — the operator's asks

A manager keeps the **same ledger as the fleet manager**, keyed by its own session and scoped to its subject: it owns the stretch from an instruction being said to a task existing, and from a question being asked to its answer.

Read and write it only through the **`supervisor:open-items` skill** — `/supervisor:open-items <list|add|answer|note|close> …`. The skill is the single home of the kinds, the render rule, the act-every-sweep rule, `answer`-vs-`note`, and `close --evidence`; follow it verbatim, never restate it here. The session id is derived by the script — no `SID` to pass for your own ledger.

## Procedure

1. Read the vault's `Manager Session` runbook (the canonical procedure — follow it verbatim).
2. **Resolve the topic** by reading its topic page (see Resolution above); print the tracked set + the page it came from. No page → report and stop.
3. **Sweep 1** (then repeat while the topic is in flight, every ~5 min):
   - **Read the attention feed** before acting on `waiting-on-human`: `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/who-needs-me.py` — only its `detail` field carries the gate text; `ListAgents` shows `waiting` but not what on.
   - Roster: `ListAgents` — the topic's worker sessions (by task name), status, age.
   - **Context usage** — `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/context-usage.py --compactable --threshold 70` lists sessions over 70%, not blocked, not in a tool call. Compact those idle workers yourself, no operator ask — gates, the three-send sequence and the verify step: [[Manager Session]] § Auto-compaction (read it before acting).
   - **Delegate the computation**: `Task(subagent_type: "supervisor:manager-sweep-reader", prompt: <tracked set + declared-optional set + this sweep's ListAgents roster verbatim + vault + mode: tick + timestamp>)` — the plugin prefix is required. It owns the task-file read, the bucket classification **the vault's Manager Session runbook** declares, unanchored id sets, orphan candidates, the collision count and the table render. **You own** the roster read, every liveness verdict and every action. Bucket rules: runbook § Step 4.
   - **No usable table came back** (errored, empty, or not the report — never keyed on an error string) → render it yourself with `box-table.py`. Same trigger in `/manager-status` (keep-in-sync).
     Build row JSON from the tracked set, roster and optional set you already hold; render with `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/box-table.py` (stdin contract: [[Manager Session]] § Sweep output). Same frame as a normal sweep. **Never hand-draw the box.**
     **Say in the sweep output that the fallback fired.** It cannot catch a wrong-but-plausible table.
   - **Reap is no longer inlined here — it is the drive leg, dispatched under `- Act:` below.** Its rules (the three disk reads, the deliberately-open Self-Review box, and why you cannot close a worker's session for it) live in `agents/manager-drive.md`. ⚠️ **The ordering is this command's to preserve: reap runs before drive**, which is why the dispatch sits after this sweep and never before it.
   - **Plan-gate parking (gap 7)** — a spawned worker `idle` in `planning` with no Progress write in its first ~30–60 min is **waiting-on-human** (parked on plan-task gap bullets), not stuck. One `SendMessage` check-in ("booted? first step? or parked on gap bullets?"); TTS only if unanswered >1h — voice-mode gated.
   - **Orphaned detection (gap 3)** — a task is orphaned when it reads `status: in_progress` but **no live session owns it**. Probe, per tracked task:
     - **read the task's whole id set** — `claude_session_id` **plus every `metrics_sessions` id**, extracted **unanchored** (entries are indented `    - session_id: …`; a `^session_id:` match misses them). Any id in the set is a candidate owner;
     - **the agent returns the candidates; you own the verdict.** It flags a task whose id set is empty while `in_progress`, or whose title matches no roster name, and stops there by design — it cannot probe liveness without inheriting your own ancestor-chain blind spot. Confirm each candidate before printing any row:
     - **confirm against the session registry on each id** — `grep -l "<session_id>" ~/.claude/sessions/*.json`, then `ps -p <pid>` on the file's pid; **alive if ANY id holds an entry against a running pid** (a roster is empty-not-absence). Alive → no `ORPHANED` row (at most `not in roster snapshot`). No live entry on any id → orphaned, subject to the drive leg's transcript-staleness clause before any resume;
     - **collect id sets across the whole tracked set before dispatching the drive leg** — an id on two tasks → print `⚠️ SHARED SESSION: <id> on <task A> + <task B> — neither resumed` and resume **neither**.
       - **Live iff two or more non-terminal carriers** (`status` not `completed`/`aborted`); terminal carriers are inert and neither create nor cure a collision. Print rows for live collisions only.
       - **After the human names the owner, clear the stamp yourself** (`vault-cli task set` / `Edit`): remove `claude_session_id` **and every `metrics_sessions` entry carrying the id**, re-probing liveness first — live or indeterminate stops the clear. Dispatch only a clear that needs a runtime check you cannot make.
       - **A released task is spawned, never resumed** — an empty id set cannot auto-resume; start it via Spawn mechanics (gap 7).
     - ⚠️ **`pgrep -f` / `ps -eo pid,args` are not OS-truth — they confirm life, never death.** A hit on either means alive; an empty read is **indeterminate**. A live session usually carries its id in no argv (measured 2026-09-22/23: three live sessions read 0 under both while registered), and `pgrep -f` also misses your own ancestor chain.

     This is an actionable row, not a troubleshooting note: a crashed or closed session leaves the task looking owned forever. Print it every sweep it holds.
   - **The auto-resume gate lives in the drive leg** (`agents/manager-drive.md`: its clauses, the re-probe at the spawn site, the abort branch, the crash-loop cap, the `last_auto_resume` write); this command supplies the confirmed orphan verdicts and dispatches. ⚠️ Liveness is decided by the session registry; the argv probes only confirm life — and the verdict is yours, never the agent's.
   - **Author the task before you spawn on it** — via `/vault-cli:create-task` (task-creator emits `# Success Criteria`, `# Definition of Done`, `# Tasks`), then score it with `vault-cli:task-auditor`, before any spawn. Never hand-write task files. The manager authors sections, subtasks, DoD and SC evidence shapes; *which file, which mechanism, what the system permits* stays with the worker. **Check before spawning:** `grep -cE '^# (Success Criteria|Definition of Done|Tasks)' <task-file>` returns 3. Applies at both spawn sites (gap 7 and step 7).
     - **not parked**: frontmatter `flag: true` absent (flag marks a parked/waiting-on-human task, e.g. Voice-Only — resume would be harmless but pointless), and phase not `human_review`, status not `hold`;
   - ⚠️ **NEVER dispatch a `role: human` task.** Exclude it before any spawn: `grep -m1 '^role:' <task-file>` → `human` → do not spawn; render the row as **`👤 YOURS`**, not `🚀 READY TO START`.
    - Human-only work cannot be delegated, and its colour chip looks identical to agent work ([[Claude Code Theme]]).
    - **The field is `role:`, never `mode:`.** `mode` already means spawn mechanism (`SPAWN_MODES = ['interactive','headless']`, `server/spawn-mode.mjs`); reusing it here would conflate the two.
    - **If the field is absent, the task is an ordinary agent task** — spawn as before. This gate only ever *subtracts*, so it cannot change behaviour for any task that has not declared a role.
    - **`role: manager` is likewise not a spawn target** — it marks a session that manages others, and a manager does not spawn managers.
  - **Spawn mechanics (gap 7)** — `spawn_agent(prompt='/vault-cli:work-on-task "<task>"', cwd="<dir>", label="<task>")`; never mint the session first. The shape's single home is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker; both spawn sites in this command match it. **Confirm the spawn took**: no `{error}` returned and the task flips `status: in_progress`; **a headless worker has no pane**, so its liveness comes from **the transcript file's mtime advancing** — **not from `.status`**, which is terminal only once the turn has ended (a resumed worker has been measured reporting `done` with `num_turns: 0` while it was still writing), and **not from `last_message`**, which is null until the worker writes assistant text and a tool-heavy worker may write none for most of a turn (measured 2026-09-24: 11 of 15 assistant records in one live turn were `tool_use`-only, so it read null at 95 and 106 lines and populated only at 107). **Mode check before every spawn — a separate act from the `role` gate above, reading a different field for a different purpose:** read the task's `mode:` frontmatter and pass `interactive=false` when, and only when, it reads `headless`. When the field is absent, classify the task body and write it back with `vault-cli task set "<task>" mode <interactive|headless>` before spawning. The classifier, the `mode:` storage and both headless constraints have their single home in `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 6 — **read them there and never restate them here.** ⚠️ A headless worker's gates park with *this* session, so a manager that cannot answer a parked prompt must not open one. **Cap check before every spawn:** the numbers' single home is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 5 — read them there and never restate them here, because a restated copy is the second counter a `grep` cannot tell from a real one. At the cap → print `⏸️ SPAWN CAP: <n> ready, <m> over cap`, spawn nothing further; the remainder is picked up next sweep.
   - **Read and render the open-items ledger** — `/supervisor:open-items list`, printed under `📋 Open with the operator` on **every** sweep, and acted on per § The open-items ledger above.
   - **Print the status table on every tick** — exactly per the runbook's § Sweep output (print rule, frame, columns, widths, icons — never restated here); § Cadence mechanics owns the tick interval and its frozen-tree exception. Render with `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/box-table.py`; never hand-draw the box. A non-empty declared-optional set → two boxes, `Essential (first iteration)` then `Optional (phase 2)`; empty → one unlabelled box. Optional rows never appear in `🚀 READY TO START:`. Below the box, only § Sweep output's non-empty action lines. **Names lead** — the task/session name anchors every line; the id is secondary.
   - Act:
     - **dispatch the drive leg** — `Task(subagent_type: "supervisor:manager-drive", prompt: <subject + tracked set + this sweep's classification + the confirmed orphan verdicts + roster verbatim + vault + timestamp>)`; the plugin prefix is required. It reaps, then nudges, then auto-resumes, and returns action lines. **Reproduce them under the table**, including `Not resumed` and `Escalated`. **Voice its `Nudged` lines** with `mcp__tts__say` (voice-mode gated). Never restate its rules or perform the three acts inline; no usable report → say so in the sweep. A refused outgoing call is your own `auto` classifier → Shift+Tab `accept edits`, retry.
     - waiting-on-human → **read the attention feed first** (`python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/who-needs-me.py`). TTS after ~15 min continuous wait; re-TTS at 1h — voice-mode gated. **Publish this sweep's gates to the phone in one call**: `echo '{"gates": [{"owner": "<session id or pane id>", "text": "<the gate line>", "session": "<the BLOCKED session id>"}]}' | python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/notify-gate.py --layer worker`; call it with `{"gates": []}` on a sweep that raised none of yours (it prunes only gates you raised). `--layer worker` is required. Publish only § Gate triage classes **C, D and E**; A and B stay silent. Surface the script's message; never pre-filter for cross-layer de-dup — the script skips a gate another session already raised. **Never emit an `approve:` line this session cannot execute** — neither a restated worker line nor an authored `approve:`/`pick` option whose first step belongs to another party; name the act and whose it is. **Ask here, relay back** (non-gate questions): read the live question (`wezterm cli get-text --pane-id <N>`), **claim each subject through the `supervisor:asked-ledger` skill first** — `/supervisor:asked-ledger claim …`, the single home of its rules and the claim contract; follow it verbatim, never restate them here. **Exit 0 → include it in the batch; exit 3 → the fleet (or another manager) already holds an open claim, drop it** and say so in one line. `resolve` once the answer is relayed. Then batch every blocked session into ONE `AskUserQuestion` (≤ 4), relay each answer verbatim prefixed `Operator answer, relayed verbatim from the manager session (not a peer inference):`. Confirm the gate is open before surfacing, and **re-read the pane right before relaying** — cleared → record topic, pane id and both pane texts in this sweep's output, and report the worker's own resolution. **A relay never releases a gate**: for a gate, hand over the one-line output of `scripts/jump-link.py <pane-id>` and say a direct go is needed — never a hand-built URL, never a tab id. **Re-resolve pane ids after any WezTerm restart.** This layer owns its topic's human-decisions; the fleet manager defers them here.
     - progress → the table + a short delta in chat
4. **Fleet-manager handoff (gap 4).** A fleet-loop session on `ListAgents` → `SendMessage` it the **orphaned + ready-to-start** sets after the table — one message per sweep in which either set changed, silent otherwise. Orphaned = the fleet's decision; ready-to-start = a report of what you started. No fleet manager → the table's `ORPHANED` / `READY TO START` lines are the handoff.
5. **Guardrails** (non-negotiable, runbook § Guardrails): the manager does not build — every piece of work goes to a task- or goal-anchored worker; never fabricate progress (disk facts, this sweep); **print the artifact in the same turn** as any negative or attribution claim — a failed glob is not absence, and pane text is multi-author. Management writes are yours. Work carve-outs: (a) gated auto-resume (gap 6); (b) ready-to-start spawns under the standing mandate, capped — the cap's numbers live in `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 5 and are never restated here (gap 7); (c) the topic-page write-back; (d) the manager-side close of a gate-met topic. TTS only when the operator must **do** something — voice is auto-on via `~/.claude/hooks/voice-mode.py`, `off` always wins, `/tts-mcp:on` for narrate; watch only this topic. An operator ask → `/supervisor:open-items add` before replying. **Never decide an operator gate** — which gates you may clear is [[Manager Session]] § Gate triage — who clears what; production-touching / live-trade gates are never relayed. **A headless worker's gate** arrives via `mcp__supervisor__await_permission` and is answered with `mcp__supervisor__answer_permission(request_id, behavior="allow"|"deny")` — the provenance rules still bind. `wezterm cli send-text` / `get-text` relays are the path-B fallback for tab workers only.

   ⚠️ **Answer an `AskUserQuestion` with `deny` + `message`, never `allow`** (`allow` leaves the worker waiting ~11 min, then it exits unanswered). Prefix: `Operator answer, via supervisor:` when the operator answered here, `Manager answer, via supervisor:` for your own scope decision. Neither releases an irreversible or production-touching action. `allow` only for ordinary tool approvals. Spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable. Refused → `accept edits`, retry.

   ⚠️ **A headless worker that has exited is not finished — continue it, do not restart it.** A headless worker ends its turn on a READY panel, or its parked question times out (~11 min) and it exits with the question unanswered. Neither is completion, and neither leaves a trace beyond the roster row disappearing. The continuation is a fresh turn, not a restart:

   ```
   mcp__supervisor__spawn_agent(prompt="<the answer, or the next instruction>", resume="<session-id>", interactive=false, cwd="<explicit>")
   ```

   Plain user turn, no relay prefix. **Pass `cwd` explicitly** — it is not inherited; read it from `~/.claude/sessions/<pid>.json` if unknown. A timed-out worker still holds its question, so the prompt must carry the answer. Spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § A headless worker exits at turn end.

   ⚠️ **A headless worker's prompts park only with the session that spawned it** — spawn your own workers and answer them yourself; a fleet manager cannot see them. Overlap ownership is unresolved: [[A Headless Worker's Gate Has No Channel a Manager May Honestly Use]].

**Path-B relay rules (`wezterm cli send-text`).** Legitimate only when (a) the operator answered in this session, current exchange; (b) the relay is verbatim with the `Operator answer, relayed verbatim from the manager session (not a peer inference):` prefix; (c) never on a peer's claim of an operator decision. **Never relay** a production-touching or irreversible approval, nor a selection modal (`Enter to select`) — record it and batch a `you run: /supervisor:jump <pane-id>` line. A multi-question wizard → `/supervisor:jump <pane-id>`, never relayed. A relay the worker refuses → do not retry; hand over `/supervisor:jump <pane-id>`. Emit `ACTION NEEDED` as a numbered pick list, option 1 `(recommended)`. **Verify submission**: read the pane back and send bare `\r` (`wezterm cli send-text --pane-id <N> --no-paste $'\r'`) until the composer clears and the worker is visibly working. Non-modal selection navigation: [[Manager Session]] § Relaying into a selection modal.
6. **Cadence (gap 5).** `ScheduleWakeup` every ~5 min (or a `Monitor`), re-sweeping with the same manager prompt. ⚠️ **Re-arm at the END of every tick** — one-shot per call; a missed re-arm stops the loop silently. Rule: [[Manager Session]] § Cadence mechanics. **Record every re-arm, right after the `ScheduleWakeup` call:** `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py --arm --topic "<subject>" --interval <the delaySeconds just armed>`. This is the evidence the host-side watcher (`manager-liveness.py --check`) reads: a manager whose last record is older than 2× its own interval is reported as lapsed, so pass the real delay, including a slowed idle cadence. **Arm the attention watcher once at manager start** — a `Monitor` over the feed emitting `NEW GATE` / `CLEARED` (snippet and traps: § Cadence mechanics). **Auto-compact** via `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/context-usage.py --compactable --threshold 70` plus [[Manager Session]] § Auto-compaction. Set `noop` honestly on every firing:
   - `noop: true` — no change (no bucket moved, no new Progress entry): print the marker **and the full table**, per § Sweep output; no topic-page write.
   - `noop: false` — something moved: a bucket changed, a task advanced, a problem or orphan appeared, a handoff was sent. Print the table, indented two spaces, under its timestamped marker — **and reconcile the topic page** (runbook § Write-back on change).

   A firing is a *re-sweep*; resolution (step 2) happens once at manager start. When the loop stops and how the operator stops it: runbook § Guardrails item 6 — Session end. **Close a gate-met topic yourself**: tick the gate boxes, flip `status: completed`, reconcile the Status Summary (runbook § Write-back on change), run `/vault-cli:sync-progress`, report, stop the loop, then `/vault-cli:session-close`. Verify with disk facts plus **read-only** in-line probes (a mutating verification goes to a worker); the close report records each probe's command and result. An unverifiable item, or all-terminal tasks under an unmet gate → keep the topic open and keep sweeping.
7. **Self-improvement (gap 2).** Before the manager session ends — and whenever a sweep exposes a gap between what this command says and what the run actually needed — file the lesson as a task and dispatch it. Do not leave it in the chat, where it dies with the session:
   - one task per coherent lesson cluster, filed with `/vault-cli:create-task "<title>"`, naming the observed gaps from *this* run (never invented ones);
   - name the sources of truth to change: this file and the vault's `Manager Session` runbook;
   - dispatch a session for it via the Spawn mechanics (gap 7) or hand it to the human as a `READY TO START` row.

   First instance: Improve Worker Manager Documentation and Command (Personal vault) — filed 2026-09-10 by the Discord Improvements manager from its own first run, and the source of gaps 1–5 documented here.
8. **Before declaring a task unowned, check the registry, not only the roster** — headless workers appear in `ListAgents`, but its mode column cannot tell them apart and rows vanish at turn end; read `~/.claude/sessions/<pid>.json`.

   ⚠️ **Only when the `supervisor` tools are bound in THIS session** (fixed at session start; depends on the launch PATH and on when the vault's MCP entry landed).

   - Tools bound → call `mcp__supervisor__list_agents` before concluding a member task has no owner. Interactive workers also appear in the roster (prefixed `⚙`); headless ones never do.
   - Tools **not** bound → say so in the verdict, explicitly: *"roster-only — the supervisor check could not be run in this session."* Never let it read as though the check ran.

   Advisory, never a gate on dispatch. Install and traps: Supervisor - Install and Operate.
