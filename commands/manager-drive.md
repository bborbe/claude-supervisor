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
  - mcp__supervisor__spawn_agent
  - mcp__tts__say
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

**The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` — the vault resolution, the four-source chain, the case-insensitive vault test, the "no fallback, ever" clause, the `Subject:` line, and the recording contract (which files, on which resolution). Read it there; **it is not restated here.**

**Only source 3 stays inline**, because it is the one source no agent can be handed — a subagent runs in a fresh context and cannot see the parent conversation:

3. **Conversation** — the most recent `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` or `/supervisor:manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention).

**Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /manager-drive "<name>"` and do nothing else.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs.

⚠️ **Resolve the vault first** — this command carries no `## Resolution` section of its own, so it takes the vault paragraph from the shared doc (`cwd → vault-cli config path`) instead of deriving the vault there.

## Procedure

1. **Resolve the subject (§ Subject resolution).** This prints the `Subject:` line and nothing more — the declared-set read is step 3, deliberately after the gate, so a no-change run never pays for it.

2. **Pre-dispatch check — decide whether this run is worth a dispatch at all.** Run the gate first. Measured 2026-09-25 on a 26-task tree with 25 done: the sweep-reader cost **91,598 tokens** and the drive leg **71,275**, for zero new information — three times in one session the manager bypassed the agents by hand to avoid it.

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-predispatch.py --vault "<vault-root>" --subject "<subject>" --print
   ```

   - **exit 0 — nothing changed.** The gate has already printed the stored table under its `NO-CHANGE` marker. **Reproduce that output as this command's whole result and STOP** — dispatch no agent, run no step below. The stored table carries **no jump coordinates**, so say in one line that pane ids must be re-resolved by a fresh run; never present a replayed link as live.
   - **exit 10 — something changed, or the gate could not tell.** Continue to step 3, and persist the fresh snapshot once the table is rendered (step 8).
   - **exit 2 — usage error.** A malformed invocation (missing `--vault`, bad flag), not a verdict. Fix the call; do not read it as "changed".

   ⚠️ **The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/scripts/manager-predispatch.py` — the digest inputs, why liveness is in them, why mtime is not, the fail-open cases, and the reason this store is not the loop's. Read it there; it is **not** restated here. What stays inline is only what this command does with the verdict.

   ⚠️ **This block is shared with `/manager-status` step 1.** The two copies must keep in sync in: this exit-code branch (0/10/2), the pointer above, and the no-jump-coordinates rule. They legitimately differ in the intro measurement and in the step numbers each exit-10 branch targets. Any other difference is drift — the fail-open clause below is shared and must stay byte-identical.

   ⚠️ **Fail-open is the contract.** A missing state file, an unreadable one, a parse error, an unresolvable subject and a failed write all exit 10 — a gate that reports "no change" when it cannot tell makes a manager blind to its own subject. Never treat a non-zero exit as an error to route around, and never hand-roll a second classifier here: this gate is the only pre-dispatch decision.

3. **Read the declared set.** Dispatch `Task(subagent_type: "vault-cli:work-on-goal-assistant")` is **not** the path here — read the page directly and take the declared set the way `/manager-loop` § Resolution does: a topic's `## Goals` members plus every task whose `goals:` names one (all three declaration shapes), or a goal's own tasks. Print `Tracked (N): <task> · <task> …`. **Never widen it** — no glob, no theme match, no content grep.

4. **Compose the sweep — do not rebuild it.** This command owns no classification. Dispatch the same agent `/manager-loop` does:

   `Task(subagent_type: "supervisor:manager-sweep-reader", prompt: <tracked set + declared-optional set + the topic's member goals + vault + mode: snapshot + timestamp>)`

   ⚠️ **The roster is no longer passed, and that is the change.** This command used to read `ListAgents` and paste it into the prompt verbatim, so the roster entered the manager's own context as a **tool_result** it then re-wrote into a **dispatch prompt**. The agent now reads it itself — `mcp__supervisor__list_agents` is declared in its `tools:` — so the manager pays for neither. The roster is a point-in-time reading, and the agent's read is as current as the caller's would have been. **Honour the distinction this preserves:** a read that is a *join* can move to the agent; a read that is a *verdict* cannot (step 5). The roster is a join.

   The plugin prefix is required — a bare `manager-sweep-reader` resolves to a personal `~/.claude/agents/` copy, never the plugin agent. The agent owns the task-file read, the bucket classification **the vault's Manager Session runbook** declares, the id-set extraction and the orphan **candidates**. Bucket rules: runbook § Step 4.

   **If the delegation returns no usable table** — it errored, came back empty, or resolved to something that did not return the report — **stop and say so**. Do not fall back to a hand-rolled classification: `manager-loop` has a `box-table.py` renderer fallback, but that is a second *renderer*, not a second *classifier*, and this command has no renderer to fall back to.

5. **Confirm the orphan verdicts — this part is yours, and the clause below says why it stays.**

   ⚠️ **The liveness probes stay with the manager — all four of them — and this is a decision, not a residue of history.** Two reasons, each sufficient alone:

   1. **The verdict is atomic.** Death needs registry absence *plus* transcript staleness; life needs *any* id holding an entry against a running pid *or* an argv hit. Splitting those reads across two actors splits **one verdict across two contexts**, and a caller handed half a probe cannot tell a partial answer from a complete one.
   2. **The argv half inherits the blind spot rather than escaping it.** ⚠️ **The mechanism has one home** — `agents/manager-sweep-reader.md` § `<constraints>`, *"Why the probe is not yours"*, and step 5's ban on `pgrep`/`ps`. Read it there; it is **not** restated here. What is this command's to add is the direction: a subagent's ancestor chain **contains** the caller's, so moving the probe down **grows** the blind spot rather than shrinking it. The move does not trade a bad probe for a good one; it trades one blind spot for a larger one.

   So the agent reports candidates and the caller decides. **The roster read at step 4 is the opposite case and has moved** — a read that is a *join* can move to the agent; a read that is a *verdict* cannot. The line is drawn **per read, never per agent.** For each candidate, probe every id in its set against the **session registry** first — `grep -l "<id>" ~/.claude/sessions/*.json`, then `ps -p <pid>` on the file's pid — and treat the task as alive if **any** id holds an entry against a running pid. Then run `pgrep -f "<id>"` and `ps -eo pid,args | grep -F "<id>"`: a hit on either also means alive, but an empty argv read is **indeterminate, never dead** — a live session usually carries its id in no argv (measured 2026-09-22/23: three live sessions read 0 under both). Death needs registry absence plus the drive leg's transcript-staleness clause. Also collect the id sets across the whole tracked set first and flag any id on **two or more non-terminal** tasks as a shared collision — resume **neither**.

6. **Dispatch the act leg.**

   `Task(subagent_type: "supervisor:manager-drive", prompt: <subject + tracked set + classification + confirmed orphan verdicts + roster + vault + timestamp + snapshot provenance: this sweep's `recorded_at` and this sweep's own per-bucket name sets>)`

   ⚠️ **The provenance fields are required, not decoration — omit them and every row comes back `Held`.** `agents/manager-drive.md` holds a row whose provenance the caller did not name, because an unlabelled list is indistinguishable from the wrong one. Measured 2026-09-26: a live pass that omitted them returned **SC2 failed** — no `recorded_at`, no per-bucket sets — and the agent named the omission itself. The two halves come from **different producers**: `recorded_at` from the snapshot the sweep wrote (`~/.claude/state/sweep-gate-loop/<vault>/<subject>.snapshot.json`) — ⚠️ **not** the manager-predispatch record, which is a different file on a different cadence — and the per-bucket name sets from **this run's own classification** — the set step 4's sweep-reader computed. ⚠️ **Never from `.snapshot.history.jsonl`** — the snapshot schema carries no bucket concept, so a per-bucket set is never obtainable from it (single home: `docs/fleet-surface.md` § Session end).

   ⚠️ **Two different things are called "vault" in this command, and the prompt's `vault` is the NAME.** The dispatch passes the vault **name** — it is the `<vault>` segment of the snapshot path above, lowercased, not a path. The `--vault` argument at steps 2 and 8 is the vault **root path**, which is why that placeholder now reads `<vault-root>`. Passing a name to the script fails open; passing a path to the snapshot path segment resolves a directory that does not exist. Neither is interchangeable with the other.

   It reaps, then nudges, then decides the auto-resume gate, and returns the action lines — **decisions, not acts**: the agent holds no spawn tool, so its `To open` and `To resume` rows are yours to execute. Its rules — the reap test, the ten gate clauses, the re-probe-at-hand-off rule, the crash-loop cap — live in `agents/manager-drive.md` and are not restated here.

7. **Execute the rows it handed over — the spawn is yours, and so is the mode argument.** The agent decided each row and wrote its `mode:` to disk; you are the one that creates the session.

   - **`To resume`** → **re-probe liveness first.** The agent probed at its hand-off site; the window between that report and this spawn is yours to close, and a stale probe here puts two writers on one conversation. Then `mcp__supervisor__spawn_agent(prompt="<the next instruction>", resume="<session_id>", cwd="<the cwd the agent reported>", …)` — `cwd` is required and is **not** inherited by a resume. Only **after** the registry shows an entry for the resumed id against a **running** pid, write `vault-cli task set "<task>" last_auto_resume "<ISO8601>"`. A refused, errored or aborted spawn writes nothing.
   - **`To open`** → read the row's `mode:` back off disk and pass the argument per `docs/fleet-surface.md` § Spawn a worker item 6: `interactive=false` when it reads `mode: headless`, `interactive=true` when it reads `mode: interactive`. Then spawn with `prompt='/vault-cli:work-on-task "<task>"'`, `cwd=<dir>`, `label="<task>"`. ⚠️ **A row whose `mode:` is absent was never decided — hold it and say so.** Spawning without the argument is exactly the `mode_source=config` defect this leg exists to remove, and the ledger cannot tell it from a site that never decided at all.

   ⚠️ **Never route these through a `Skill`-invoked `/supervisor:open`, and never fall back to the wezterm path.** `/supervisor:open` requires `mcp__supervisor__*` in the *invoking* session and falls back to wezterm without it — a tab by construction, which cannot produce a headless worker and writes **no ledger row**, so the measurement this whole change serves would pass vacuously.

8. **Print what came back, voice the nudges, and escalate.** Reproduce the agent's action lines verbatim, including its `Not resumed` and `Escalated` sections — a near-miss clause is the most useful line in the report. **Voice the `Nudged` lines** with `mcp__tts__say` (voice-mode gated): the agent owns the message, you own the voice, because a subagent has no TTS. Then the operator-facing tail: any gate that needs their decision goes out as **`/supervisor:jump <pane-id>`**, never as a command for them to run here (the approval belongs to the session that raised it).

   **Then persist the snapshot — it is what makes the next run free.** First token `python3`, so the call matches this command's `Bash(python3:*)` grant and raises no prompt:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-predispatch.py --vault "<vault-root>" --subject "<subject>" --save <<'TABLE'
   <the rendered table>
   TABLE
   ```

   ⚠️ **`--save` exits 10 on success** — that code means "this run saw a change", which is exactly why it saved. Do not read it as a failure, and do not retry.

   ⚠️ **`--vault` takes the vault ROOT PATH, never the vault name — and a name fails open silently.** The script's own help says *"vault root"*. Measured **2026-09-26**: `--vault "Personal"` returns `CHANGE fail-open: subject unresolvable (no subject page for 'Manager Layer' in 24 Goals/ or 23 Topics/)`, while the absolute path returns a real verdict. This call site previously wrote `--vault "<vault>"`, which reads as the vault *name* everywhere else in this command — so a manager filling it verbatim got a fail-open on **every** run and **no snapshot was ever saved**. The failure is silent in the worst way: it reports `CHANGE`, the run proceeds, and the prior record the next sweep diffs against never comes into existence. Pass the path the vault paragraph resolved.

   ⚠️ **An empty table is refused and reports CHANGE** rather than clobbering a good snapshot — a snapshot with no table can never be replayed, so a failed render must leave the previous one standing and force the next run to re-sweep. Never write the snapshot by hand, and never save a table you did not print. The gate strips OSC 8 jump links before storing, so the persisted copy carries no jump token.

## What this command must never do

- **Never classify.** The sweep is `supervisor:manager-sweep-reader`'s, and a second classification here would drift from the one `/manager-loop` acts on.
- **Never decide an operator gate.** You surface it; the operator answers it in the session that raised it.
- **Never close a worker's session.** `/vault-cli:sync-progress` and `/vault-cli:session-close` read the parent conversation, and every route into a worker's pane is refused. Reaping means *sending the evidence*, nothing more.
- **Never claim an act the agent reported it could not perform.** If a tool failed to bind, the agent says so; carry that through rather than smoothing it.
