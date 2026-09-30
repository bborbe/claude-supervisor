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
  - `/manager-drive "Sample Task"` → explicit subject
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

3. **Read the declared set.** Dispatch `Task(subagent_type: "vault-cli:work-on-goal-assistant")` is **not** the path here — read the page directly and take the declared set the way `/manager-loop` § Resolution does: a topic's `## Goals` members plus every task whose `goals:` names one (all three declaration shapes), or a goal's own tasks. Print `Tracked (N): <task> · <task> …`. **Never widen it** — no glob, no theme match, no content grep. ⚠️ **Then write the set to a file — through `manager-predispatch.py --write-tracked`, never a shell redirect — and carry its *path* from here on, never the names.**

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-predispatch.py --vault "<vault-root>" --subject "<subject>" --write-tracked <<'TRACKED'
<the names you just resolved, one per line>
TRACKED
```

It prints the path it wrote (`~/.claude/state/manager-predispatch/<slug>.tracked.txt`) and refuses an empty stdin rather than clobbering a good set. ⚠️ **The first token is `python3` so the call matches this command's `Bash(python3:*)` grant and raises no prompt** — **`Write` is granted by none of the three commands**, so a bare redirect or heredoc would prompt. The printed line is for the operator; **the file is what both dispatches below carry.** It must come from **this run's own read** — never reconstructed from the roster, a checkpoint, or the gate's own snapshot, which is a different derivation and can disagree with the page (measured 2026-09-27: 154 declared vs 153 in the snapshot, differing by a case-only name mismatch nothing else could see). ⚠️ **Then compare that set against the gate's own membership before either dispatch leaves** — the two are derived by different code from the same declarations, so each reads as self-consistent while they disagree:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-predispatch.py --vault "<vault-root>" --subject "<subject>" --compare-tracked
```

It prints **both counts** and, on a disagreement, the symmetric difference under `⚠️ DIVERGENCE:`. Exit **0** = identical; **10** = they diverge **or** the snapshot could not be read; **2** = usage error. ⚠️ **Never collapse the two 10s** — "I could not check" must not read as "they match", which is the defect one level down. ⚠️ **Report a divergence, never reconcile it here:** this command holds no input for which source is right. Print it; reconciling the two sources belongs to a separate task.

4. **Compose the sweep — do not rebuild it.** This command owns no classification. Dispatch the same agent `/manager-loop` does:

   ⚠️ **Write no reading into this dispatch that you have not taken this tick — and never batch the read with the dispatch that consumes it.** A **reading** is any value the prompt states as current: a feed count, a roster line, a liveness verdict, a task count. If a read's output is needed by this dispatch, the read **completes before the prompt is authored** — the read and the dispatch are **not** emitted in one message, because a prompt written in the same message as its own input has no output to quote and fills the slot from the previous tick instead. ⚠️ **A stale carry is worse than an omitted field:** an omission is visibly missing and the reader reports it as a caller bug, while a stale value has the same shape, position and confidence as a fresh one, so it silently disables the check it feeds. **When the read has not completed, leave the slot unstated** — never fill it from a prior tick. The rule's home is `65 Runbooks/Manager Session.md` § Step 4 — read it there; this clause is its operational half, **not** the full statement. ⚠️ **Same clause, same wording, in `/manager-loop` and `/manager-status`** — this defect is **mode-dependent**, so a fix in one command says nothing about the others.

   `Task(subagent_type: "supervisor:manager-sweep-reader", prompt: <tracked-set path + declared-optional set + the topic's member goals + this run's ListAgents roster verbatim + vault + mode: snapshot + timestamp + today>)` — ⚠️ **the first element is the path step 3 wrote, not the names.** ⚠️ **`today` (ISO `YYYY-MM-DD`) is a separate input from `timestamp`, and this dispatcher was missing it** — `timestamp` is a time-of-day, `today` is the date the `deferred` overlay compares `defer_date` against, and the agent may read no clock of its own. Omitting it makes every deferred task report as uncomputable rather than deferred. The agent reads the set off disk; an inline list is the defect this placeholder removes, since the agent cannot tell a recalled list from a scanned one.

   ⚠️ **The roster is read here and passed verbatim — it was moved to the agent on 2026-09-27 and moved back the same day, because the move did not work.** The agent cannot fetch the roster itself: the harness `ListAgents` tool is **main-session-only**. Measured — **4,867 calls across 714 main transcripts, zero across 16,027 subagent transcripts**, and a probe subagent holding `Tools: *` reported no such tool. `mcp__supervisor__list_agents` **does** bind in a subagent, which is what the move rested on, but it is a **different tool with a different population**: it lists *supervisor-spawned* agents, and against a 28-session fleet it returned `[]` on every call. The agent therefore read an **empty** roster, and its join matched nothing while reporting success. ⚠️ **The rule this corrects is stated at step 5** — read it there.

   The plugin prefix is required — a bare `manager-sweep-reader` resolves to a personal `~/.claude/agents/` copy, never the plugin agent. The agent owns the task-file read, the bucket classification **the vault's Manager Session runbook** declares, the id-set extraction and the orphan **candidates**. Bucket rules: runbook § Step 4.

   **If the delegation returns no usable table** — it errored, came back empty, or resolved to something that did not return the report — **stop and say so**. Do not fall back to a hand-rolled classification: `manager-loop` has a `box-table.py` renderer fallback, but that is a second *renderer*, not a second *classifier*, and this command has no renderer to fall back to.

5. **Confirm the orphan verdicts — this part is yours, and the clause below says why it stays.**

   ⚠️ **The liveness probes stay with the manager — all four of them — and this is a decision, not a residue of history.** Two reasons, each sufficient alone:

   1. **The verdict is atomic.** Death needs registry absence *plus* transcript staleness; life needs *any* id holding an entry against a running pid *or* an argv hit. Splitting those reads across two actors splits **one verdict across two contexts**, and a caller handed half a probe cannot tell a partial answer from a complete one.
   2. **The argv half inherits the blind spot rather than escaping it.** ⚠️ **The mechanism has one home** — `agents/manager-sweep-reader.md` § `<constraints>`, *"Why the probe is not yours"*, and step 5's ban on `pgrep`/`ps`. Read it there; it is **not** restated here. What is this command's to add is the direction: a subagent's ancestor chain **contains** the caller's, so moving the probe down **grows** the blind spot rather than shrinking it. The move does not trade a bad probe for a good one; it trades one blind spot for a larger one.

   So the agent reports candidates and the caller decides. ⚠️ **The line the roster move rested on — *"a read that is a join can move to the agent; a read that is a verdict cannot"* — is necessary but not sufficient, and the roster is what proved it.** The roster **is** a join, and it still could not move: the agent holds no tool that returns the same thing. **The complete rule has three clauses — a read may move to the agent only if it is a *join*, and the agent holds a tool for it, and that tool returns the same set the caller would have read.** ⚠️ **Binding is not equivalence.** Measured 2026-09-27: `mcp__supervisor__list_agents` binds in a subagent and returned `[]` against 28 live peer sessions, while the harness `ListAgents` returned all 28 — so a join against it matched nothing and said nothing about it. The line is still drawn **per read, never per agent.** For each candidate, probe every id in its set against the **session registry** first — `${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/session-liveness.py --check <id>` (exit `0` LIVE / `1` ABSENT / `2` UNKNOWN / `3` AMBIGUOUS) — and treat the task as alive if **any** id returns `LIVE`. ⚠️ **`UNKNOWN` is not `ABSENT`, and `AMBIGUOUS` resumes neither** — the instrument is one file; do not restate a registry read here. Then run `pgrep -f "<id>"` and `ps -eo pid,args | grep -F "<id>"`: a hit on either also means alive, but an empty argv read is **indeterminate, never dead** — a live session usually carries its id in no argv (measured 2026-09-22/23: three live sessions read 0 under both). Death needs registry absence plus the drive leg's transcript-staleness clause. Also collect the id sets across the whole tracked set first and flag any id on **two or more non-terminal** tasks as a shared collision — resume **neither**.

6. **Dispatch the act leg.**

   `Task(subagent_type: "supervisor:manager-drive", prompt: <subject + tracked-set path + classification + confirmed orphan verdicts + roster + vault + timestamp + snapshot provenance: this sweep's `recorded_at` and the manager-predispatch store path carrying `bucket_sets`>)` — ⚠️ **the tracked set travels as the same path step 3 wrote, never inlined.** The leg reads it off disk, which is what makes clause (3a)'s corpus the set the caller actually swept rather than a list the agent has no way to check.

   ⚠️ **The roster is passed here verbatim, for the same reason step 4 gives** — the act agent cannot read it either, and the 2026-09-27 attempt to move it left the agent reading `[]` while reporting success. Both dispatches take their roster from **one** `ListAgents` read, which is why `ListAgents` is back in the `allowed-tools` above. ⚠️ **The duplication is unavoidable at this tool layout, and that is the honest cost of the revert:** the manager is the only actor holding a roster tool and the agent is the only actor consuming it, so the text must appear both as the manager's `tool_result` and inside the dispatch prompt. Removing one of those two payments requires the agent to gain a tool that returns the **same** set — not one that merely binds. ⚠️ **`manager-loop` passes its roster verbatim too, and always has** — the loop owns its own read for the fleet-manager handoff — so the two paths now agree rather than diverge.

   ⚠️ **The provenance fields are required, not decoration — omit them and every row comes back `Held`.** `agents/manager-drive.md` holds a row whose provenance the caller did not name, because an unlabelled list is indistinguishable from the wrong one. Measured 2026-09-26: a live pass that omitted them returned **SC2 failed** — no `recorded_at`, no per-bucket sets — and the agent named the omission itself. The two halves come from **different producers**: `recorded_at` from the snapshot the sweep wrote (`~/.claude/state/sweep-gate-loop/<vault>/<subject>.snapshot.json`) — ⚠️ **not** the manager-predispatch record, which is a different file on a different cadence — and the per-bucket name sets from **this run's own classification**, persisted this tick under the store's **`bucket_sets`** key (`~/.claude/state/manager-predispatch/<slug>.json`) — ⚠️ **pass that store *path* as the half, not the sets themselves.** The leg reads `bucket_sets` from the same record `recorded_at`'s snapshot sits beside, so a fresh manager can read it too; a hand-named stand-in is exactly what `<constraints>` refuses, and quoting the sets inline puts them back in prose where nothing can check them. ⚠️ **Never from `.snapshot.history.jsonl`** — the snapshot schema carries no bucket concept, so a per-bucket set is never obtainable from it (single home: `docs/fleet-surface.md` § Session end).

   ⚠️ **Two different things are called "vault" in this command, and the prompt's `vault` is the NAME.** The dispatch passes the vault **name** — it is the `<vault>` segment of the snapshot path above, lowercased, not a path. The `--vault` argument at steps 2 and 8 is the vault **root path**, which is why that placeholder now reads `<vault-root>`. Passing a name to the script fails open; passing a path to the snapshot path segment resolves a directory that does not exist. Neither is interchangeable with the other.

   It reaps, then nudges, then decides the auto-resume gate, and returns the action lines — **decisions, not acts**: the agent holds no spawn tool, so its `To open` and `To resume` rows are yours to execute. Its rules — the reap test, the eleven gate clauses, the re-probe-at-hand-off rule, the crash-loop cap — live in `agents/manager-drive.md` and are not restated here.

7. **Execute the rows it handed over — the spawn is yours, and so is the mode argument.** The agent decided each row and wrote its `mode:` to disk; you are the one that creates the session.

   - **`To resume`** → **re-probe liveness first.** The agent probed at its hand-off site; the window between that report and this spawn is yours to close, and a stale probe here puts two writers on one conversation. Then `mcp__supervisor__spawn_agent(prompt="<the next instruction>", resume="<session_id>", cwd="<the cwd the agent reported>", …)` — `cwd` is required and is **not** inherited by a resume. Only **after** the registry shows an entry for the resumed id against a **running** pid, write `vault-cli task set "<task>" last_auto_resume "<ISO8601>"`. A refused, errored or aborted spawn writes nothing.
   - **`To open`** → read the row's `mode:` back off disk and pass the argument per `docs/fleet-surface.md` § Spawn a worker item 6: `interactive=false` when it reads `mode: headless`, `interactive=true` when it reads `mode: interactive`. Then spawn with `prompt='/vault-cli:work-on-task "<task>"'`, `cwd=<dir>`, `label="<task>"`. ⚠️ **A row whose `mode:` is absent was never decided — hold it and say so.** Spawning without the argument is exactly the `mode_source=config` defect this leg exists to remove, and the ledger cannot tell it from a site that never decided at all.

   ⚠️ **Never route these through a `Skill`-invoked `/supervisor:open`, and never fall back to the wezterm path.** `/supervisor:open` requires `mcp__supervisor__*` in the *invoking* session and falls back to wezterm without it — a tab by construction, which cannot produce a headless worker and writes **no ledger row**, so the measurement this whole change serves would pass vacuously.

8. **Print what came back, voice the nudges, and escalate.** Reproduce the agent's action lines verbatim, including its `Not resumed` and `Escalated` sections — a near-miss clause is the most useful line in the report. **Voice the `Nudged` lines** with `mcp__tts__say` (voice-mode gated): the agent owns the message, you own the voice, because a subagent has no TTS. Then the operator-facing tail: any gate that needs their decision goes out as **`/supervisor:jump <pane-id>`**, never as a command for them to run here (the approval belongs to the session that raised it).

   **Then persist the snapshot — it is what makes the next run free.** First token `python3`, so the call matches this command's `Bash(python3:*)` grant and raises no prompt. ⚠️ **Stage the per-bucket classification in the same step, or clause (0) has no durable source** — the sets are the ones **this run's own sweep-reader computed** (step 4), never re-derived here and never read back from the snapshot, whose schema has no bucket concept:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-predispatch.py --vault "<vault-root>" --subject "<subject>" --write-buckets <<'BUCKETS'
   {"<bucket>": ["<task name>", "…"], "…": ["…"]}
   BUCKETS
   ```

   It prints the path it wrote and refuses a malformed set rather than staging one that cannot gate. **Every declared bucket must appear**, each mapping to a non-empty list of names — a single bucket, a bucket mapped to a count, **or a bucket mapped to an empty list** does not satisfy the half. ⚠️ **The empty list is the case that got through:** `all(...)` over `[]` is vacuously true, so an all-empty set passed the check whose message says "non-empty", staged at exit 0 and was saved — measured 2026-09-28.

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-predispatch.py --vault "<vault-root>" --subject "<subject>" --save --buckets "<the path --write-buckets printed>"
   ```

   The record then carries them under **`bucket_sets`** — the key the drive leg's provenance names, and the reason the half survives a compaction or a fresh manager instead of living only in this session's context.

   ⚠️ **`--save` exits 10 on success** — that code means "this run saw a change", which is exactly why it saved. Do not read it as a failure, and do not retry. ⚠️ **Never hand this call a table on stdin** — a non-empty stdin is refused with a usage error (exit 2, nothing written), because the save reads the payload the render wrote and dates the record from that file's mtime, never from save time.

   ⚠️ **`--vault` takes the vault ROOT PATH, never the vault name — and a name fails open silently.** The script's own help says *"vault root"*. Measured **2026-09-26**: `--vault "my-vault"` returns `CHANGE fail-open: subject unresolvable (no subject page for 'Manager Layer' in 24 Goals/ or 23 Topics/)`, while the absolute path returns a real verdict. This call site previously wrote `--vault "<vault>"`, which reads as the vault *name* everywhere else in this command — so a manager filling it verbatim got a fail-open on **every** run and **no snapshot was ever saved**. The failure is silent in the worst way: it reports `CHANGE`, the run proceeds, and the prior record the next sweep diffs against never comes into existence. Pass the path the vault paragraph resolved.

   ⚠️ **An empty table is refused and reports CHANGE** rather than clobbering a good snapshot — a snapshot with no table can never be replayed, so a failed render must leave the previous one standing and force the next run to re-sweep. Never write the snapshot by hand, and never save a table you did not print. The gate strips OSC 8 jump links before storing, so the persisted copy carries no jump token.

## What this command must never do

- **Never classify.** The sweep is `supervisor:manager-sweep-reader`'s, and a second classification here would drift from the one `/manager-loop` acts on.
- **Never decide an operator gate.** You surface it; the operator answers it in the session that raised it.
- **Never close a worker's session.** `/vault-cli:sync-progress` and `/vault-cli:session-close` read the parent conversation, and every route into a worker's pane is refused. Reaping means *sending the evidence*, nothing more.
- **Never claim an act the agent reported it could not perform.** If a tool failed to bind, the agent says so; carry that through rather than smoothing it.
