---
description: Drive ONE subject by hand — reap the finished, nudge stuck or error-marked workers, auto-resume confirmed orphans. The act leg of the show/check/act triad, runnable without arming a manager loop; reap runs before drive, always. The subject is detected when omitted, and the classification is composed from the existing sweep, never rebuilt.
allowed-tools:
  - Task
  - Read
  - Bash(grep:*)
  - Bash(vault-cli:*)
  - Bash(pgrep:*)
  - Bash(ps:*)
  - Bash(find:*)
  - Bash(stat:*)
  - Bash(python3:*)
  - Bash(mkdir:*)
  - Bash(date:*)
  - ListAgents
argument-hint: "[goal|topic] (detected when omitted)"
---

Manager drive slash command — the **act leg**, run once, by hand, against one subject.

This is the third leg of the triad `manager-status` (show) · `manager-verify` (check) · **`manager-drive` (act)**, and it is the same act leg `/manager-loop` composes on every tick. Running it by hand is what makes the act leg testable on its own: you see what drive would do to a subject without arming a loop over it.

⚠️ **"Testable" here means operator-runnable, not unit-tested — and that is this repo's existing shape, not a gap this change introduces.** Commands and agents in this plugin are markdown prompts: there is no harness that executes them, and no test file exists for any of the eleven commands or four agents already shipped. What this repo *does* test is the server (`server/*.test.mjs`) and the render scripts (`scripts/tests/`), and this change touches neither. The verification this artifact actually carries is the marketplace-clone e2e run recorded in its PR — the command appears in the loaded `slash_commands`, the agent registers and dispatches, and the loaded `manager-loop.md` is byte-identical to the worktree. A test asserting that a markdown prompt contains certain sentences would restate the file rather than exercise it.

⚠️ **A manager-tier verb — never run it from a worker session.** A worker carries a *task*; a manager carries a topic or goal and a loop. Driving a subject's tracked set from inside a worker collapses the two roles silently: the session keeps its task anchor while its turns reap, nudge and auto-resume another set's sessions (`${CLAUDE_PLUGIN_ROOT}/docs/session-tiers.md`; `docs/fleet-surface.md` § Session roles). A worker that needs a subject driven routes it to its manager with `SendMessage` and says so. Exercised in a manager session's runtime, never from the session that authored it — being one pass with no cadence limits the cadence, not the blast radius.

⚠️ **One-shot.** This command arms nothing and schedules nothing — it runs once and exits. Arming a *loop* remains a human act performed in a session created for it.

⚠️ **Reap runs before drive, always.** A completed task with zero open boxes is also idle, so a drive pass that runs first nudges a finished session to continue — and a session with nothing left to do that is told to continue will invent work. The sequence is **sweep → reap → drive → escalate**.

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **subject** — a goal name matching a page in `24 Goals/`, or a topic name matching a topic page in `23 Topics/`. The branch is detected from whichever page resolves; this command never assumes one.
  - `/manager-drive "MDM Bugs"` → explicit subject
  - `/manager-drive` (bare) → resolve the subject from § Subject resolution below — the same rule `/manager-loop` and `/manager-status` use, so a manager session's bare drive acts on the subject it is already watching

## Subject resolution — when `$1` is omitted

**Keep-in-sync block** — shared with `/manager-loop`, `/manager-status` and `/manager-verify`; the resolution rule is identical in all four. Enumerated differences, never counted: between the three plugin copies — the sibling list here, the STOP line, the "No fallback, ever" clause, and the closing write-contract sentence; `/manager-verify` also differs in the recording paragraphs (prose there, since its `allowed-tools` lacks `Bash(python3:*)` / `Bash(mkdir:*)`). `/manager-drive` also carries a leading **vault-resolution paragraph** (cwd → vault-cli config path), because it has no `## Resolution` section of its own to derive the vault in. Change one, change the others.

**Resolve the vault first — from the session's cwd, never a default.** `VAULT=$(vault-cli config list --output json | python3 -c "import json,sys,os;cwd=os.path.realpath(os.getcwd());print(next((v['name'] for v in json.load(sys.stdin) if os.path.realpath(os.path.expanduser(v['path']))==cwd),''))")`. Every page test below (`24 Goals/`, the vault's `topics_dir`) runs inside that vault. No match → STOP with `❌ cwd is not a configured vault — pass a goal or topic name and run from the vault root`; never fall back to `personal`. A session in `~/Documents/Obsidian/Brogrammers` named *MDM Bugs* must resolve `Brogrammers/23 Topics/MDM Bugs.md`.

A bare invocation takes the first source that yields a **real page**:

1. **Session state** — `~/.claude/state/worker-manager/<CLAUDE_CODE_SESSION_ID>.json`, shape `{"subject","vault","branch","resolved_at"}`. Accepted only when `vault` matches the resolved vault **and** `subject` still resolves to a page. Compare **case-insensitively** — the vault is the lowercase vault-cli config `name` (`personal`), but state files written before 2026-09-23 may carry display case (`Personal`); a strict match silently drops to source 2. This is what makes a subject named once stick across the ticks of one session.
2. **Session name** — `~/.claude/sessions/$CLAUDE_PID.json` → `.name` (pid-keyed: `CLAUDE_PID`, not `CLAUDE_CODE_SESSION_ID`). Strip leading decoration (`⚙ `); accept only if what remains resolves to a goal or topic page. No match, a task-page match or a missing pid file → silent miss, fall through.
3. **Conversation** — the priority order `/vault-cli:task-status` uses in its Phase 2: the most recent `/manager-loop`, `/manager-status`, `/manager-drive` or `/manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention).
4. **Vault's last subject** — `~/.claude/state/worker-manager/last-<vault>.json`, same shape and same test. Last resort: the only source not about this session.
5. **Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /manager-drive "<name>"` and do nothing else.

**No fallback, ever.** Never a filename glob, a `goals:` scan, a theme match, or a content grep. The `$1` no-fallback rule exists because scope-by-globbing is the failure it prevents; a silent guess at the *subject* is the same failure one level up, and worse here — drive would reap, nudge and resume workers in the wrong tree, and every one of those writes lands somewhere real.

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

**A bare invocation writes at most the session file** — `<CLAUDE_CODE_SESSION_ID>.json`, and only on a session-local resolution; never `last-<vault>`, so a detected-subject drive adds no cross-session write. `/manager-loop` and `/manager-status` read the same files, which is what makes a subject named in one command visible to the others.

## Procedure

1. **Resolve the subject (§ Subject resolution) and read its declared set.** Dispatch `Task(subagent_type: "vault-cli:work-on-goal-assistant")` is **not** the path here — read the page directly and take the declared set the way `/manager-loop` § Resolution does: a topic's `## Goals` members plus every task whose `goals:` names one (all three declaration shapes), or a goal's own tasks. Print `Tracked (N): <task> · <task> …`. **Never widen it** — no glob, no theme match, no content grep.

2. **Compose the sweep — do not rebuild it.** This command owns no classification. Dispatch the same agent `/manager-loop` does:

   `Task(subagent_type: "supervisor:manager-sweep-reader", prompt: <tracked set + declared-optional set + this run's ListAgents roster verbatim + vault + mode: snapshot + timestamp>)`

   The plugin prefix is required — a bare `manager-sweep-reader` resolves to a personal `~/.claude/agents/` copy, never the plugin agent. The agent owns the task-file read, the bucket classification **the vault's Manager Session runbook** declares, the id-set extraction and the orphan **candidates**. Bucket rules: runbook § Step 4.

   **If the delegation returns no usable table** — it errored, came back empty, or resolved to something that did not return the report — **stop and say so**. Do not fall back to a hand-rolled classification: `manager-loop` has a `box-table.py` renderer fallback, but that is a second *renderer*, not a second *classifier*, and this command has no renderer to fall back to.

3. **Confirm the orphan verdicts — this part is yours.** The sweep-reader returns **candidates** and stops by design; it cannot probe liveness without inheriting the caller's own ancestor-chain blind spot. For each candidate, probe every id in its set against the **session registry** first — `grep -l "<id>" ~/.claude/sessions/*.json`, then `ps -p <pid>` on the file's pid — and treat the task as alive if **any** id holds an entry against a running pid. Then run `pgrep -f "<id>"` and `ps -eo pid,args | grep -F "<id>"`: a hit on either also means alive, but an empty argv read is **indeterminate, never dead** — a live session usually carries its id in no argv (measured 2026-09-22/23: three live sessions read 0 under both). Death needs registry absence plus the drive leg's transcript-staleness clause. Also collect the id sets across the whole tracked set first and flag any id on **two or more non-terminal** tasks as a shared collision — resume **neither**.

4. **Dispatch the act leg.**

   `Task(subagent_type: "supervisor:manager-drive", prompt: <subject + tracked set + classification + confirmed orphan verdicts + roster + vault + timestamp>)`

   It reaps, then nudges, then runs the auto-resume gate, and returns the action lines. Its rules — the reap test, the ten gate clauses, the re-probe-at-spawn-site rule, the crash-loop cap — live in `agents/manager-drive.md` and are not restated here.

5. **Print what came back, voice the nudges, and escalate.** Reproduce the agent's action lines verbatim, including its `Not resumed` and `Escalated` sections — a near-miss clause is the most useful line in the report. **Voice the `Nudged` lines** with `mcp__tts__say` (voice-mode gated): the agent owns the message, you own the voice, because a subagent has no TTS. Then the operator-facing tail: any gate that needs their decision goes out as **`/supervisor:jump <pane-id>`**, never as a command for them to run here (the approval belongs to the session that raised it).

## What this command must never do

- **Never classify.** The sweep is `supervisor:manager-sweep-reader`'s, and a second classification here would drift from the one `/manager-loop` acts on.
- **Never decide an operator gate.** You surface it; the operator answers it in the session that raised it.
- **Never close a worker's session.** `/vault-cli:sync-progress` and `/vault-cli:session-close` read the parent conversation, and every route into a worker's pane is refused. Reaping means *sending the evidence*, nothing more.
- **Never claim an act the agent reported it could not perform.** If a tool failed to bind, the agent says so; carry that through rather than smoothing it.
