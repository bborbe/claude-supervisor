---
description: Stateful fleet round — snapshot, diff against the previous sweep, classify every peer (progressing / stalled / parked / done), dispatch the drive leg, and act within a strict autonomy boundary. Composes /fleet-status; repeats by default at its `--interval` (default 15 min) via ScheduleWakeup, TTS on problems (voice-mode gated), routes to managers. (Renamed from /fleet-sweep 2026-09-10 when the two were merged.)
allowed-tools:
  - ListAgents
  - SendMessage
  - Monitor
  - AskUserQuestion
  - ScheduleWakeup
  - mcp__supervisor__spawn_agent
  - mcp__supervisor__list_agents
  - mcp__supervisor__agent_status
  - mcp__supervisor__answer_permission
  - mcp__tts__say
  - Bash(python3:*)
  - Bash(date:*)
  - Bash(vault-cli:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - Bash(mkdir:*)
  - Bash(echo:*)
  - Bash(grep:*)
  - Bash(pgrep:*)
  - Bash(comm:*)
  - Bash(sort:*)
  - Bash(kubectl*:*)
  - Bash(gh:*)
  - Bash(git log:*)
  - Bash(wezterm cli list:*)
  - Task
  - Read
  - Write
  - Edit
argument-hint: "[--interval <N>m] (interval defaults to 15m; the round repeats by default)"
---

Answer one question: **does anything in the fleet need attention right now?**

Not `/supervisor:worker-drive` (session-scoped) and not `/fleet-status` (stateless). `/fleet-loop` is the stateful round on top of `/fleet-status`: it remembers the last sweep, diffs against it, and acts only on evidence. It has side effects — a message consumes a peer's turn — so it never runs tighter than its 15-minute floor (§ Cadence) and is never invoked back-to-back by a polling loop.

**Why each rule below exists — the incidents, measurements and superseded readings — lives in the Fleet Manager Session runbook (per-vault) § Fleet-Manager Command — Rationale and Measured History.** Read it before changing a rule; this file carries only what a sweep executes. Design source, do not re-derive: the Claude Code cross-session messaging notes (operator's vault) § Orchestration design, mirrored in `~/.claude/commands/first-mate.md` § The sweep loop.

`P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts` — every script below is `python3 $P/<script>`.

## Manager contract — a manager manages, it does not build

The fleet manager never performs the *work*: no code edits, no repo/PR/k8s verification, no investigation, no debugging — not even small ones.

- **The line is work vs management, not read vs write.** Management writes are the manager's own: creating tasks, editing task/goal pages, changing `status`/`phase`, clearing stale session stamps, declaring scope on a topic page (`Edit`, `vault-cli task set`). The *work* goes to a worker: owning manager → owning worker session → a delegated sub-agent (verification only, Step 4). **Delegate the work, not the bookkeeping.** Do not reintroduce a read-only-manager reading as a "fix".
- **Never widen past the boundary.** A task-file write is management only while it changes tracking state (status, phase, dates, stamps, scope). Reading code, judging a root cause or verifying a runtime fact is work — delegate it.
- **No worker → spawn one, without asking.** Unowned work (ready-to-start tasks, orphaned restarts) starts as a new session via `/supervisor:open "<task>"` — never in-line. Spawning is a standing mandate: open it and report it. The gate is the task anchor — a task file with no live owner gets spawned. **Spawn readiness precondition first:** read `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker and follow it (author via `/vault-cli:create-task`, `task-auditor` 9/10, then spawn).
  - Dispatch is the manager's verb alone — `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles. A worker routes out-of-scope work to its manager.
  - **Before spawning, check for a supervised worker.** Headless workers hold a socket and appear in `ListAgents`, but the roster's mode column reports `interactive` for them too and the roster is volatile — timestamp any roster conclusion. The guard is the session registry (below).
  - **Never answer another manager's workers.** A headless worker's prompts park only with the server of the session that spawned it; route to the owning manager with `SendMessage`. Scope overlap is unresolved — [[A Headless Worker's Gate Has No Channel a Manager May Honestly Use]].
  - **`mcp__supervisor__list_agents` sees only workers this session spawned** — an empty result is not absence. Nearest real check: `ls ~/.local/state/claude-supervisor/sessions/*.json`, filter `status: running`, verify liveness externally (`pane_id` against `wezterm cli list`; null `pane_id` = headless). It is a candidate list — it never closes when a pane dies.
  - **`~/.claude/sessions/*.json` is the authoritative liveness store** — pid-keyed, carries `sessionId`, `status`, `cwd`, and the entry is deleted on exit. On disagreement with the spawn ledger, `pgrep` or a pane, the registry wins. **Never write a session stamp from a ledger reading.** A `/branch` holds a new id; only the registry sees it as live.
- **The PR is the boundary, whatever the file extension.** Editing a doc may be management; opening a PR never is (review loop, merge, release, deploy verification). Test *"what does this change oblige?"*, not *"prose or code?"*. The Build Drift Gate is a crossing trigger: one-shot work that grows a worktree, branch and commits needs its task anchor at the crossing, and goes to a worker.
- **Task/goal anchored, always.** Nothing is delegated or spawned without a task or goal file behind it. Read-only manager business (snapshot, report, TTS) needs no anchor; anything else gets `/vault-cli:create-task` first.

## Cadence

The fleet manager is a role (Fleet Manager Session runbook — the wide/shallow layer over managers); this command is its engine: one invocation runs one round, and the round re-arms itself. For one look without a loop, run `/fleet-status` (read-only); for one act pass, run `/fleet-drive`. There is no one-sweep flag — a single round still dispatches the drive leg, sends and writes back, so it was never a read-only look. `--interval <N>m` sets the tick — default 15m, floor 15m; any other argument is ignored. The round repeats by default.

- **One round:** Steps 0–6 below.
- **Re-arm at the END of every round:** `ScheduleWakeup` with the same `/supervisor:fleet-loop --interval <N>m` prompt, at the **clamped** interval. Default **15 min**; floor **15 min** — a below-floor value is **clamped up and announced** (`⚠️ --interval 5m is below the 15m floor — clamped to 15m`), never rejected, because a loop that refuses its own interval stops silently — the failure this flag exists to reduce. **Never tighter than 15 min, never self-re-invoking** — cadence belongs to the scheduler. A round that prints its output and stops has killed the loop silently: no error, no marker, no stale-loop signal.
- **Additions on every round** — there is no non-recurring mode left to contrast with:
  - **Needs-input** — the digest's BLOCKED section (from the attention feed) is the primary blocked-session channel; `ListAgents` `waiting` does not say *what* a session waits on. Group into ONE report, never N pings. TTS when a wait exceeds ~30 min continuous, re-TTS at 1h — voice-gated. Names lead; ids are secondary.
    - **Manager first.** Before including an entry, check `ListAgents` for its owning manager; if live, it already reports its `waiting-on-human` sessions (`/manager-loop` step 3) — drop the entry and say so in one line.
    - **Claim before asking — this is what makes it asked ONCE.** Manager-first is not sufficient alone: the fleet and a live manager can both hold the same blocked session and neither sees the other's batch. Claim each subject through the **`supervisor:asked-ledger` skill** — `/supervisor:asked-ledger claim …`, the single home of its rules and the claim contract; follow it verbatim, never restate them here. **Exit 0 → include the subject in the batch; exit 3 → another layer holds it, drop it from the batch** and say so in one line. `resolve` once the answer is relayed.
    - **Render the consolidated list once per sweep** — `/supervisor:asked-ledger list`, under the Output shape's **Needs-input** item (where Step 5's blocked set renders): every open claim across every layer in one list. That is what makes "one grouped decision" true even though the asks stay per-layer.
    - **Ask here, relay back — the operator never needs a worker tab.** Collect **every** entry no manager covers **and that `claim` returned 0 for**, then read all of their live questions in **one** dispatch of the **`gate-relay-read` agent** — `Task(subagent_type: "supervisor:gate-relay-read", prompt: <every pane id + which worker each belongs to>)`; the plugin prefix is required. ⚠️ **One read dispatch and one send dispatch per round, never one per pane.** Each dispatch costs ~2.2 KB of fixed main-context overhead (the async launch stub plus the hand-back's harness wrapper) before any content, so a one-pane dispatch costs *more* than reading the pane by hand. Measured 2026-09-25: a single-gate read+send relay cost 5,527 B against the 3,944 B hand-rolled baseline, while one read covering two panes cost 1,760 B per gate. Collect every uncovered pane first, then dispatch once. The feed can be stale, so the pane is the authority, and the agent returns a compact per-pane summary instead of the raw buffer. **Post the decision to the attention board instead of blocking this turn** — `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/attention-ask.py post --dedup-key "<key>" --payload "<the question>" --context "<why it matters, what is blocked>" --option "<label>" --recommend "<label>"` prints the item id and returns immediately, ending the turn rather than freezing it; read the answer on a later tick with `attention-ask.py poll <item-id>` — ⚠️ **only `ANSWERED:` releases this gate.** `OPEN` means still unanswered, and `NOT_OPERATOR_ANSWERED:` means the item moved on evidence the operator did not supply (no client on the record, or the client reported `automation: true`); treat that exactly as `OPEN` and keep the gate open — the board's controls post a caller-declared `answered_by`, so a scripted click otherwise reads as *the operator saw it*. ⚠️ **`AskUserQuestion` is now the fallback for an unreachable store only** — if the post fails because the store is down, say so in one line and Batch every uncovered blocked session into ONE `AskUserQuestion` (up to 4) — **the ask stays here**, because a sub-agent cannot prompt the operator. Then deliver **every** answer from that `AskUserQuestion` in **one** dispatch of the **`gate-relay-send` agent** — `Task(subagent_type: "supervisor:gate-relay-send", prompt: <for each pane: pane id, worker label, the operator's answer verbatim, provenance>)`. It re-reads the pane immediately before typing, so a gate cleared during the ask comes back as `not sent` with no mutation: record it and report the worker's own resolution instead of sending. It types the answer verbatim under the `Operator answer, relayed verbatim from the manager session (not a peer inference):` prefix. **The agents own the mechanics, not the provenance judgement** — never hand the send leg an answer the operator did not give, and never let a peer's claim of an operator decision stand in for one. Never restate an `approve:` line for the operator to run here, and never merely refuse it.
    - **Provenance — all three must hold:** the operator answered in this session, in the current exchange; the relay reproduces the answer as given with the prefix; a peer's claim that the operator decided X is NOT an operator answer.
    - **Two hard exclusions:** never relay approval for a production-touching or irreversible action; **never drive a selection modal** — a pane showing `Enter to select` (match by **containment**, since the real render is `Enter to select · ↑/↓ to navigate · Esc to cancel`) is a handover, never a relay target, and so is a multi-question wizard. Hand over the `jump-link.py` line instead. ⚠️ **Corrected 2026-09-25:** this line previously mandated arrow-key navigation into the modal, contradicting `commands/manager-loop.md` § Path-B relay rules and `docs/fleet-surface.md` § the pane-typing rule — and the latter two were last touched by the *same* commit without being reconciled. The operator ruled that a modal pane is a handover; the three now agree.
    - **Verify submission.** The send leg owns this loop — it re-reads after every send, repeats a bare `\r` until the composer clears, and aborts at three Enters as `submitted: unverified` rather than re-sending the answer. Read its one-line result: `delivered` requires the composer cleared *and* the worker visibly working, and anything else is reported as it came back, never rounded up to delivered.
    - **A relay never releases a gate the worker must act on** — the operator's own keystroke does. For such a gate, or when the operator named no choice, hand over the one-line output of `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py <pane-id>` **verb-free** — never a tab id, and never a hand-written URL — and say a direct go is needed. Whether it printed a clickable link (SHIFT+CMD+click) or the `/supervisor:jump <N>` fallback, the line is a handover the operator acts on, not a command this session runs: `/supervisor:jump` is the executor and these commands emit links without running it. One action per line — `commands/manager-loop.md` § Path-B relay rules. A relay refused at the receiving end is not your error: do not retry, hand over the pane.
  - **Routing:** on `stalled`/crashed-looking, `SendMessage` the owning manager first (*"`<session>` looks stalled — your topic"*); TTS the human only if it persists — voice-gated.
  - **Attention watcher:** arm ONE `Monitor` over the attention feed, alongside the tick — and **re-arm it whenever it expires or is found dead, never once at loop start**: a `Monitor` is bounded at 30 min, so an arm-once watcher dies silently mid-loop and the fleet's only push channel goes quiet. ⚠️ **`--interval` does not govern this watcher** — it keeps its own arm, so at a long interval it still wakes the manager once per watcher event; that is intended, not a leak, because the watcher is the push channel and the tick is the poll. Doorbell only — emits `NEW GATE` / `CLEARED` firings (the exact line shape is § Cadence mechanics'); read `/who-needs-me` on a firing. **A `CLEARED` is not progress until confirmed** — then **close the corresponding ledger/attention entry on that evidence and continue the sweep, never asking the operator to confirm a clear by hand.** ⚠️ Never auto-close a `message` item awaiting a board answer — [[Managers Ask the Operator on the Attention Board Instead of Blocking on AskUserQuestion]]. The confirmation mechanism — which source answers for which worker, and its three-state reading — plus the snippet and traps have one home: [[Manager Session]] § Cadence mechanics, and are not restated here.
  - **Auto-compaction:** the digest's CONTEXT section lists sessions over 70%. On an idle one **no manager covers**, compact it yourself — no operator ask — following [[Manager Session]] § Auto-compaction (gates, three-send sequence, verify). Where a manager owns the area, defer.
  - **Global delta:** on progress, a 3–5 line chat delta; detail stays in task files.
  - **Persist:** Step 6, via `fleet-snapshot.py` only — never hand-write `~/.claude/state/fleet-snapshot.json`.
  - **Guardrails:** delegate the work (manager → worker session → `/supervisor:open` spawn); management writes are your own; never fabricate state (report only what this round's reads show); TTS only for problems and only when voice is on; wide but shallow; every delegated or spawned work is task- or goal-anchored.
  - **Voice gate:** `~/.claude/hooks/voice-mode.py` writes `{"mode":"on"}` on the prompt invoking `/fleet-loop` only when no state file exists, so voice is on from the first sweep. An explicit `/tts-mcp:off` always wins; `narrate` is not the default (`/tts-mcp:on` upgrades to it).

    **Silence is the default; the test is an ACTION, not a finding.** Speak only when this round produced something the operator must **do** — an `ACTION NEEDED`, a gate needing their keystroke, a decision. A clean round, a no-change tick, the table, a summary of checks stay on screen. When in doubt, do not speak.

    **The notification follows the same test, through the plugin's publisher — never a vault command.** Once per round, with every gate the round raised:

    ```bash
    echo '{"gates": [{"owner": "<session id or pane id>", "text": "<the gate line>", "session": "<the BLOCKED session id>"}]}' \
      | python3 $P/notify-gate.py --layer fleet
    ```

    Send `{"gates": []}` on a round that raised none — the empty call prunes **your own** cleared gates only, never another manager's. **`--layer fleet` is required** (the ledger is per layer; this sweep is a subset). Publish only gates the operator must decide — § Gate triage classes **C, D, E** in [[Manager Session]]; **A and B** are yours to clear and stay silent. Surface the script's message rather than swallowing it. **Do not pre-filter for cross-layer duplicates** — the script de-dups by escalating session id and prints the skip; publish and let it decide.
  - **Stop** when the human stops it or no sessions remain in flight.

## The four read channels

A round reads four channels and none replaces another — the attention feed (**who is blocked**, `who-needs-me.py`), the `ListAgents` roster (**who exists**), `fleet-sessions.py` (**task mapping + mtime**), and `context-usage.py --compactable --threshold 70` (**who is filling up**). `fleet-sessions.py` is ~2000 lines raw: never let an uncompacted dump reach this context. Steps 0b–3 read all of them inside the `fleet-sweep-reader` sub-agent, so only its digest reaches this session.

- ⚠️ **The feed answers "was a gate raised", never "is a gate open".** Confirm a gate is open by reading the pane through the `gate-relay-read` agent before reporting it as open — fold those panes into the round's single read dispatch rather than dispatching per report; this command holds no pane-read grant of its own.

## The open-items ledger — the operator's asks

Not a fifth channel: it holds what the operator asked and is still waiting on — the stretch before a task exists, and between a question and its answer. Read and write it only through the **`supervisor:open-items` skill** — `/supervisor:open-items <list|add|answer|note|close> …`, the single home of its kinds and rules; never restate them here. The fleet layer's specifics:

- **Read at round start** (Step 0b, in the digest), **render every round** under `📋 Open with the operator`.
- **Close on evidence only:** a task file's `status: completed` read *this round* (Step 4), or the operator's own answer.

## Steps 0–3 — Read half, delegated to `supervisor:fleet-sweep-reader`

**Step 0 — roster.** Call `ListAgents` — the only place this command calls it. (This is `/fleet-status`'s roster half; do not reimplement its join here.)

**Steps 0b–3 — delegate.** Dispatch:

`Task(subagent_type: "supervisor:fleet-sweep-reader", prompt: <this round's ListAgents roster verbatim + SID + vault path and tasks dir + round timestamp>)`

The plugin prefix is required — a bare `fleet-sweep-reader` resolves to a personal `~/.claude/agents/` copy. The agent (Sonnet) reads the four channels and the ledger, loads the previous snapshot, stats each `busy`/`shell` task file, runs the orphan reverse index (`orphan-candidates.py --tasks-dir . --max-age-days 7`, park filter + 7-day upper bound, exit code checked), finds collision and unmanaged-topic candidates, classifies every session, persists the next snapshot through `fleet-snapshot.py`, and returns a **≤ 40-line digest**. It owns those rules — read them in `agents/fleet-sweep-reader.md`; this command does not restate them.

**What stays here:** every liveness verdict, every confirmation of a candidate, and every action.

**When the delegation returns no usable digest** — it errored, came back empty, or resolved to something that returned no digest — run the reads yourself for this round (`/fleet-status`, then `/supervisor:open-items list`, `orphan-candidates.py` as above), compacting `fleet-sessions.py` through `grep -oE '\b[0-9a-f]{8}\b'`. Trigger on the missing digest, never on a matched error string.

**Classes the digest reports** (Step 3): progressing · **stalled** (`busy`/`shell`, task mtime unchanged ≥2 sweeps) · parked (`idle`, open boxes) · **finished — reap** (`idle`, task complete) · **orphan** candidate (open work, dead session) · unclassified (insufficient data — never guess). `waiting` never counts toward `stalled` or `parked`.

### Confirming the digest's candidates

- **Orphan:** a candidate is open work with a dead session — for the operator to pick up or close, not a fault and not a stall. Report as its own group. `UNKNOWN` (check failed) is reported as unknown, never as clean.
- **Collision (Step 2c):** a shared subject is not automatically a collision — confirm the overlap is on the same artifact. On a real one, **tell both sides without asking** (read-only context):

  > "Read-only: `<other session>` also appears to be working on `<shared artifact>`. Flagging so you two don't duplicate or overwrite each other — I have not asked either of you to stop, and I have not decided who owns it."

  Then report it in Step 5's grouped report, as its own group. **Never draft a stand-down** — that is a course correction and needs the operator's yes.
- **Unmanaged topic (Step 2d):** **suggest, never auto-spawn** a manager — report the topic, its live workers and the command that would start one. When the operator says go, apply the spawn readiness precondition. If the topic page is absent, suggest creating the page (manager work) together with the spawn.

## Step 3b — Drive: dispatch the drive leg

Reap is no longer inlined here — it is the drive leg's, and the ordering is this command's to preserve: **reap runs before drive**, which is why the dispatch sits after the sweep and never before it.

`Task(subagent_type: "supervisor:fleet-drive", prompt: <this round's digest verbatim + this session's name and id + vault path + round timestamp + "dry-run: false">)`

The plugin prefix is required — a bare `fleet-drive` resolves to a personal `~/.claude/agents/` copy. The agent loads the ledger, suppresses re-nudges, verifies each `parked` session's blocker live, reaps the finished (its step 1b), persists the ledger, and returns drafted `REAPS` and `NUDGES` plus a grouped escalation batch. It owns those rules — read them in `agents/fleet-drive.md`; this command does not restate them.

⚠️ **The agent never sends.** It has no cross-session address, so the sends stay here — the same split `commands/fleet-drive.md` uses.

**Send, from this session.** Every `REAPS` line first, then every `NUDGES` line — reap before drive, so a finished session is never told to continue. For each line, re-check the target's roster status first: moved to `busy`/`shell` since Step 0 → skip and report the skip (never preempt a busy peer); otherwise `SendMessage(to: <exact roster name>, message: <text>)`. Report any failed send — the ledger already counts it as nudged, so the next round suppresses rather than nags.

**Print** the agent's report verbatim — table, escalation groups, ledger line — followed by `Sent: <n>` with one line per recipient and `Skipped: <n>` with reasons.

**No usable report** — errored, came back empty, or is not the report — → say so in the round's output. Nothing was reaped and nothing was sent this round; never fall back to a hand-rolled classification, because every verdict would be UNKNOWN and an empty fleet and a dead source must never render the same.

**Every `ESCALATION` row carries its jump link.** Append to each row the one-line output of `python3 $P/jump-link.py <pane-id>`, taking the pane the agent carried on the row. A row the digest gave no pane for is resolved by `python3 $P/who-needs-me.py --pane-for <sid8>` — the session id is the join the sweep already made, and the lookup decides liveness from the session registry, so a session that has exited cannot yield a pane. Emit the link, never a bare `/supervisor:jump <N>` and never a hand-built URL. **Keep-in-sync block** — shared with `commands/fleet-drive.md` step 5, which carries the same rule for the by-hand pass. Enumerated differences, never counted: that copy spells out `${CLAUDE_PLUGIN_ROOT:-…}` where this one uses `$P`. Change one, change the other.

### The reap contract

`agents/fleet-drive.md` step 1b owns it — the three disk reads, the rule that the file is the fact and the session's claim is not, and the deliberately-open-box carve-out. This command does not act on it and does not restate it; read it there.

### Two channels reach a worker, and they are not interchangeable

| The worker is… | Whose answer | Channel | Call |
|---|---|---|---|
| **headless, parked on a question** | the **operator's** | supervisor permission channel — no pane | `mcp__supervisor__answer_permission(request_id, behavior="deny", message="Operator answer, via supervisor: <option>")` |
| **headless, parked on a question** | **your own** | supervisor permission channel — no pane | `mcp__supervisor__answer_permission(request_id, behavior="deny", message="Manager answer, via supervisor: <option>")` |
| **headless, already exited** (turn end, or ~11 min question timeout) | either | a fresh turn | `mcp__supervisor__spawn_agent(prompt="<the answer>", resume="<session-id>", interactive=false, cwd="<explicit>")` — **spawn readiness precondition first** |
| **a tab worker** | either | its pane — `send_agent_message`, or by hand | the relay protocol (§ Cadence) |

⚠️ **The `resume=` row is the only reason this file holds `mcp__supervisor__spawn_agent` — and the grant also permits an *open*.** Never use it to open a worker: route opens through `/supervisor:open`, whose § Step 0.6 decides `mode` and Step 3 consumes it. If a case ever appears that genuinely cannot route there, classify and pass the argument explicitly — `interactive=false` for `mode: headless`, `interactive=true` otherwise — because a direct open has **no Step 0.6 behind it**. A worker opened with the argument omitted reports `mode_source=config`, indistinguishable in the ledger from a site that never decided. Rule: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 6.

- **The headless channel is primary.** For an `AskUserQuestion`, answer `deny` + `message`, never `allow` (allow runs the tool tty-less and the worker exits unanswered). On a refusal, switch this session to `accept edits` (Shift+Tab) and retry — the refusal is this session's own `auto`-mode classifier.
- **Pick the prefix that matches who answered.** `Operator answer, via supervisor:` only when the operator answered in this session; `Manager answer, via supervisor:` for your own decision. **Neither releases an irreversible or production-touching action.** Spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable.
- **The tab relay is the fallback** — only where the headless channel cannot reach the same effect.
- **Answer promptly or not at all.** Parked prompts auto-deny after 15 minutes (headless); a worker left waiting stalls silently. Prefer fewer, longer-lived workers over many short ones. ⚠️ **This is what caps the loop's own `--interval`:** while this session owns a live headless worker the effective interval is `min(<interval>, 900)`, announced when it binds — a longer tick would leave a worker parked past its window and resume it with a denial it did not earn. Wrangler auto-policy is the alternative for longer intervals, not the default.
- **A manager cannot close a worker's session** — `/vault-cli:sync-progress` and `/vault-cli:session-close` read the worker's own conversation, and driving its pane is refused. Do not design a reaping rule that depends on typing into a worker.

So the manager **informs**, which is read-only context and needs no approval — the `REAPS` lines the agent returned **are** that message:

1. The agent verifies the three disk facts above before drafting one.
2. Send it verbatim, from this session; it states the disk state, names that the operator has **not** answered, and leaves the decision with the session.
3. Report the finished rows in Step 5's grouped report as **self-closeable** — one line for all N, never N approvals.

Where a manager owns the session, it reaps its own — this layer defers.

## Step 4 — Act, within the autonomy boundary

Two message classes. **Never send anything on this fleet without applying this split.**

**Read-only context — send freely, no approval needed.** E.g. "here's a fact you're missing", "session X already scaled that down on purpose", "another session owns this task". Mark it plainly non-authorising — state what you did *not* do.

**Course corrections — draft only, send only on explicit yes.** E.g. "stop that", "work on this instead", "point at a different target". Write the draft text plus target session under a labelled section and stop; send only in a follow-up turn after the operator says yes to that specific draft.

**Act on every open ledger entry.** For each `asked-of-me` / `pushed` entry: no task behind it → file one (`/vault-cli:create-task`) and record it on the entry; a task with no worker → spawn via `/supervisor:open "<task>"` (spawn readiness precondition first); a stalled owner → nudge under the two classes above; task reads `status: completed` → verify on disk **this round**, then `close --evidence "<task file> reads status: completed"`. `asked-of-you` entries are re-surfaced until answered; `answer` in the turn the answer arrives **closes** the entry. On the other two kinds `answer` records a note and leaves the entry `open`.

**Work always goes to a worker.** A fix, a check, a PR, a deploy → owning manager or worker session, or `/supervisor:open "<task>"` when none exists. Never without a task/goal anchor.

### Cause must be verified live, never read off the task file

Classification runs on status and mtime, current by construction. Cause does not: a task file's prose goes stale the moment its session solves the problem. Before naming a cause in Step 5, confirm it against the system it is about — `kubectl get jobs`, `gh pr list --state all`, `git log`, the deployed image tag. Unverified → escalate the **observation** and say the cause is unverified.

**Delegate that verification.** One sub-agent per candidate cause (`Explore` or `general-purpose`, **Sonnet**), the cause as a yes/no question plus the systems to check; take back only the verdict:

> "Is `github-update-go-agent` actually broken right now? Check merged PRs on `bborbe/github-update-go-agent`, the latest released tag, and live Job states in `kubectlnukeprod -n prod`. Answer: broken / fixed / can't tell, with the evidence line for each."

Independent causes go in one message so they run concurrently. **Never delegate** course-correction drafts. **Keep every `SendMessage` in this session** — a sub-agent has no address, so replies land here regardless.

## Step 5 — Escalate: an ask and a report

Never one interruption per stuck session. A round escalates twice, and both fire here — a round that walks Steps 0–6 and renders the Output shape without either has dropped findings it already holds.

- **The blocked set — an ask, not a report.** Every session the digest's BLOCKED section carries goes into ONE `AskUserQuestion` (up to 4), each subject gated by an `asked-ledger` `claim`. That protocol — manager-first, claim, pane read, the batch, relay, provenance, verify-submission — is § Cadence's **Needs-input**, which owns it and is **not restated here**; a round that skips it has skipped an escalation, not a formatting step. A subject dropped by one of those rules prints the rule and its line.
- **The sweep's own findings — a report.** Collect them into one grouped report, one entry per distinct cause, presented once at the end of the round. Its members: every `stalled`, `parked` and `orphan` finding (orphans as their own group), the `finished — reap` rows as a single **self-closeable** line, and any Step 2c collision as its own group.

## Step 6 — Persist the new snapshot

The sweep reader persists it (its digest quotes `snapshot written: <swept_at>`). On the fallback path, persist it yourself — pipe the sessions JSON (schema: `agents/fleet-sweep-reader.md` step 9) into `python3 $P/fleet-snapshot.py`; never hand-write `~/.claude/state/fleet-snapshot.json`.

## Output shape

1. **Roster** — the marker line plus the box, rendered **exactly per Fleet Manager Session runbook (per-vault) § Sweep output — the fleet table** — the single source for frame, columns, widths and icons. Build it with `python3 $P/fleet-board.py --json | python3 $P/box-table.py`; never hand-draw it. Every tick prints the marker line; the box re-prints when a bucket moved, a session appeared or vanished, or a handoff was sent, plus a ~30-min heartbeat. If `/fleet-status` just printed the same box, reference it.

   Print one quantitative line with the marker: the colour census `python3 $P/fleet-colours.py --json`, read for `backlog` (green/blue/cyan) with `default` and `purple` reported apart.
2. **Classification** — the digest's non-progressing rows: `<name> [<id>] · <status> · <classification>`. Never print the `[ref]`.
3. **`📋 Open with the operator`** — the ledger, one line per open entry: kind · what · state · age. **Never omitted**; `(none open)` when empty.
4. **Needs-input** — the batch over the digest's BLOCKED set, per § Cadence's **Needs-input** (the `AskUserQuestion`, its `asked-ledger` claims, the relays), then the consolidated list beneath it: every open claim across every layer, in one list. **Never omitted** — `(none blocked)` when the digest's BLOCKED section is empty, and a subject one of those rules dropped prints that rule and its line.
5. **Escalation report** — Step 5's own findings, grouped cause-first: the `stalled`/`parked`/`orphan` rows (orphans as their own group), the `finished — reap` rows as one self-closeable line, and any Step 2c collision as its own group. Omit if nothing needs attention. Mark any cause a sub-agent could not confirm as **unverified**.
6. **Drive leg** — the agent's report verbatim (its header line, rows, `ESCALATION`, `LEDGER`) plus `Sent: <n>` with one line per recipient and `Skipped: <n>` with reasons. On no usable report, say so here instead.
7. **Read-only context sent this sweep** — what and to whom.
8. **Course-correction drafts awaiting approval** — exact text + target; ask the operator to approve or edit.
9. **Snapshot written** — path and `swept_at`.

## The saturation reading — the ratio, not tok/s

Every tick prints the fleet working ratio beside the marker line:

    saturation: 3/6 (busy + shell over live sessions)

Read it from `python3 $P/fleet-board.py --json` → `saturation`, which carries
`numerator`, `denominator` and `ratio`. The numerator counts the registry
**status** (`busy` + `shell`), not the board's `running` bucket — the buckets
apply a precedence, so a `busy` session holding an open gate is bucketed
`needs-input` and would be dropped from a bucket-based count. `ratio` is `None`
for an empty fleet; print it as absent, never as `0%`.

**The ratio is the signal, not tok/s.** Measured 2026-09-20 (Fleet Manager session
`b700c650`): output throughput swung 629 → 905 tok/s in 45 minutes while the ratio
moved only 44% → 42%. Throughput tracks whichever model happens to be mid-response
at the moment of the read; the ratio does not. Print the tok/s figure beside it for
context only — `~/.claude/scripts/claude-metrics.sh` reads `out_tok/s` — and never
act on it as the number.

A low ratio is often a **human-queue** signal, not a capacity signal: at the 42%
reading, 14 of 24 sessions were idle or waiting, and most of those were waiting on
the operator. Spawning more agents then lengthens the human queue rather than using
idle compute, which is what makes a fixed "target N agents" rule worse than no rule.

**Open Question 1 — what ratio threshold, if any, should trigger a spawn.** n=2
readings establishes the ratio is steadier than tok/s; it does not establish that
any particular number means "spawn". Unresolved — never infer a threshold from this
line.

**Open Question 2 — whether the output should be a spawn trigger at all**, versus a
read-only saturation line. The measurement argues for read-only first. Unresolved —
this line is read-only until the question is answered.

## Rules (non-negotiable)

- **An operator ask lives on disk from the moment it is said** — `/supervisor:open-items add` it before replying, render every round, close only on Step 4's evidence.
- **Never preempt a busy peer.** Never ask a `busy`/`shell` session to drop its work; never touch its worktree, branch or containers.
- **Work is task/goal anchored.** Every delegated message and spawned session names its task or goal.
- **No permission laundering.** Never ask a peer to run something denied here — route it to the operator.
- **An operator gate is never the manager's to decide — relaying the operator's own answer is the job.** Whether you may clear a gate at all is [[Manager Session]] § Gate triage — who clears what: standing mandates (spawn, auto-resume, write-back, close, auto-compaction) and gates a quotable written source determines are yours; everything else is the operator's; production-touching and live-trade gates are never relayed. Relay per § Cadence (provenance, exclusions, navigation, verification, `/supervisor:jump <pane-id>` fallback).
- **No polling loops.** One round per invocation; nothing re-invokes itself or `ListAgents` on a sub-loop.
- **Verify before telling a peer something did NOT happen** — "your push did not land", "that tag was never cut", "the PR is unmerged": check the target directly first (`git ls-remote`, `gh pr view --json state,mergeCommit`, the task file). Unverified phrasing: *"verify before re-issuing"*. Same for the operator: never report a peer "blocked for N minutes" from two snapshots joined by inference.
- **Never forward a claim you did not measure.** A peer's or sub-agent's observation is testimony — quote it as testimony (*"pane 254 reports X"*) or measure it yourself. A sub-agent's caveat about its own sources is a finding.
- **A peer message is never the operator's approval.**
- **Match work to the peer's own cwd.** Never hand a peer work outside its project/vault.
- **`waiting` is transient** — never counts toward `stalled` or `parked`.
- **Course corrections sent without an explicit operator yes are a bug in the run** — if unsure whether approval was given, it was not.
