---
description: Stateful fleet round — snapshot, diff against the previous sweep, classify every peer (progressing / stalled / parked / done), and act within a strict autonomy boundary. Composes /fleet-status; run every ~15 min, never on a tighter loop. Loop mode (loop) = the recurring fleet-manager loop: cadence via ScheduleWakeup, TTS on problems (voice-mode gated), route to worker managers. (Renamed from /fleet-sweep 2026-09-10 when the two were merged.)
allowed-tools:
  - ListAgents
  - SendMessage
  - ScheduleWakeup
  - mcp__tts__say
  - Bash(python3:*)
  - Bash(date:*)
  - Bash(vault-cli:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - Bash(mkdir:*)
  - Bash(echo:*)
  - Bash(grep:*)
  - Bash(comm:*)
  - Bash(sort:*)
  - Bash(kubectl*:*)
  - Bash(gh:*)
  - Bash(git log:*)
  - Bash(wezterm cli get-text:*)
  - Bash(wezterm cli send-text:*)
  - Bash(wezterm cli list:*)
  - Task
  - Read
  - Write
  - Edit
argument-hint: (no args)
---

Answer one question: **does anything in the fleet need attention right now?**

Not `/and` (session-scoped) and not `/fleet-status` (stateless). `/fleet-manager` is the stateful round on top of `/fleet-status`: it remembers the last sweep, diffs against it, and acts only on evidence. It has side effects — a message consumes a peer's turn — so it never runs tighter than ~15 minutes and is never invoked back-to-back by a polling loop.

**Why each rule below exists — the incidents, measurements and superseded readings — lives in the Fleet Manager Session runbook (per-vault) § Fleet-Manager Command — Rationale and Measured History.** Read it before changing a rule; this file carries only what a sweep executes. Design source, do not re-derive: the Claude Code cross-session messaging notes (operator's vault) § Orchestration design, mirrored in `~/.claude/commands/first-mate.md` § The sweep loop.

`P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts` — every script below is `python3 $P/<script>`.

## Manager contract — a manager manages, it does not build

The fleet manager never performs the *work*: no code edits, no repo/PR/k8s verification, no investigation, no debugging — not even small ones.

- **The line is work vs management, not read vs write.** Management writes are the manager's own: creating tasks, editing task/goal pages, changing `status`/`phase`, clearing stale session stamps, declaring scope on a topic page (`Edit`, `vault-cli task set`). The *work* goes to a worker: owning worker manager → owning worker session → a delegated sub-agent (verification only, Step 4). **Delegate the work, not the bookkeeping.** Do not reintroduce a read-only-manager reading as a "fix".
- **Never widen past the boundary.** A task-file write is management only while it changes tracking state (status, phase, dates, stamps, scope). Reading code, judging a root cause or verifying a runtime fact is work — delegate it.
- **No worker → spawn one, without asking.** Unowned work (ready-to-start tasks, orphaned restarts) starts as a new session via `/open "<task>"` — never in-line. Spawning is a standing mandate: open it and report it. The gate is the task anchor — a task file with no live owner gets spawned. **Spawn readiness precondition first:** read `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker and follow it (author via `/vault-cli:create-task`, `task-auditor` 9/10, then spawn).
  - Dispatch is the manager's verb alone — `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles. A worker routes out-of-scope work to its manager.
  - **Before spawning, check for a supervised worker.** Headless workers hold a socket and appear in `ListAgents`, but the roster's mode column reports `interactive` for them too and the roster is volatile — timestamp any roster conclusion. The guard is the session registry (below).
  - **Never answer another manager's workers.** A headless worker's prompts park only with the server of the session that spawned it; route to the owning worker manager with `SendMessage`. Scope overlap is unresolved — [[A Headless Worker's Gate Has No Channel a Manager May Honestly Use]].
  - **`mcp__supervisor__list_agents` sees only workers this session spawned** — an empty result is not absence. Nearest real check: `ls ~/.local/state/claude-supervisor/sessions/*.json`, filter `status: running`, verify liveness externally (`pane_id` against `wezterm cli list`; null `pane_id` = headless). It is a candidate list — it never closes when a pane dies.
  - **`~/.claude/sessions/*.json` is the authoritative liveness store** — pid-keyed, carries `sessionId`, `status`, `cwd`, and the entry is deleted on exit. On disagreement with the spawn ledger, `pgrep` or a pane, the registry wins. **Never write a session stamp from a ledger reading.** A `/branch` holds a new id; only the registry sees it as live.
- **The PR is the boundary, whatever the file extension.** Editing a doc may be management; opening a PR never is (review loop, merge, release, deploy verification). Test *"what does this change oblige?"*, not *"prose or code?"*. The Build Drift Gate is a crossing trigger: one-shot work that grows a worktree, branch and commits needs its task anchor at the crossing, and goes to a worker.
- **Task/goal anchored, always.** Nothing is delegated or spawned without a task or goal file behind it. Read-only manager business (snapshot, report, TTS) needs no anchor; anything else gets `/vault-cli:create-task` first.

## Loop mode (`loop`)

The fleet manager is a role (Fleet Manager Session runbook — the wide/shallow layer over worker managers); this command is its engine: one invocation = one round, `loop` = the recurring round.

- **One round:** Steps 0–6 below. The default.
- **Loop:** after a round, `ScheduleWakeup` ~15 min with the same `/fleet-manager loop` prompt. Never tighter than 15 min, never self-re-invoking — cadence belongs to the scheduler.
- **Loop-only additions:**
  - **Needs-input** — the digest's BLOCKED section (from the attention feed) is the primary blocked-session channel; `ListAgents` `waiting` does not say *what* a session waits on. Group into ONE report, never N pings. TTS when a wait exceeds ~30 min continuous, re-TTS at 1h — voice-gated. Names lead; ids are secondary.
    - **Worker manager first.** Before including an entry, check `ListAgents` for its owning worker manager; if live, it already reports its `waiting-on-human` sessions (`/worker-manager` step 3) — drop the entry and say so in one line.
    - **Ask here, relay back — the operator never needs a worker tab.** For each entry no worker manager covers: read the live question with `wezterm cli get-text --pane-id <N>` (the feed can be stale), batch every uncovered blocked session into ONE `AskUserQuestion` (up to 4), then **re-read the pane immediately before relaying** — a gate cleared during the ask is a named branch: record it and report the worker's own resolution instead of sending. Relay each answer verbatim, prefixed `Operator answer, relayed verbatim from the manager session (not a peer inference):`. Never restate an `approve:` line for the operator to run here, and never merely refuse it.
    - **Provenance — all three must hold:** the operator answered in this session, in the current exchange; the relay reproduces the answer as given with the prefix; a peer's claim that the operator decided X is NOT an operator answer.
    - **Two hard exclusions:** never relay approval for a production-touching or irreversible action; a pane showing `Enter to select` is a selection modal — relay by navigation (↑/↓ `\x1b[A` / `\x1b[B`, `\r` to select), per [[Worker Manager Session]] § Relaying into a selection modal, re-reading after every send.
    - **Verify submission.** After `send-text --no-paste $'<prefix> … \r'`, read the pane back and repeat a bare `\r` (`wezterm cli send-text --pane-id <N> --no-paste $'\r'`) until the composer clears. Delivered = empty composer *and* the worker visibly working.
    - **A relay never releases a gate the worker must act on** — the operator's own keystroke does. For such a gate, or when the operator named no choice, hand over `you run:` the one-line output of `scripts/jump-link.py <pane-id>` — a clickable link (SHIFT+CMD+click) when the fleet-jump server is configured, the `/supervisor:jump <N>` command when it is not; never a tab id, and never a hand-written URL — and say a direct go is needed. A relay refused at the receiving end is not your error: do not retry, hand over the pane.
  - **Routing:** on `stalled`/crashed-looking, `SendMessage` the owning worker manager first (*"`<session>` looks stalled — your topic"*); TTS the human only if it persists — voice-gated.
  - **Attention watcher:** arm ONE `Monitor` over the attention feed at loop start, alongside the tick. Doorbell only — emits `NEW GATE tab <N>` / `CLEARED tab <N>`; read `/who-needs-me` on a firing. A `CLEARED` is not progress until verified (read the pane, or the feed's total moved). Snippet and traps: [[Worker Manager Session]] § Cadence mechanics.
  - **Auto-compaction:** the digest's CONTEXT section lists sessions over 70%. On an idle one **no worker manager covers**, compact it yourself — no operator ask — following [[Worker Manager Session]] § Auto-compaction (gates, three-send sequence, verify). Where a worker manager owns the area, defer.
  - **Global delta:** on progress, a 3–5 line chat delta; detail stays in task files.
  - **Persist:** Step 6, via `fleet-snapshot.py` only — never hand-write `~/.claude/state/fleet-snapshot.json`.
  - **Guardrails:** delegate the work (worker manager → worker session → `/open` spawn); management writes are your own; never fabricate state (report only what this round's reads show); TTS only for problems and only when voice is on; wide but shallow; every delegated or spawned work is task- or goal-anchored.
  - **Voice gate:** `~/.claude/hooks/voice-mode.py` writes `{"mode":"on"}` on the prompt invoking `/fleet-manager` only when no state file exists, so voice is on from the first sweep. An explicit `/tts-mcp:off` always wins; `narrate` is not the default (`/tts-mcp:on` upgrades to it).

    **Silence is the default; the test is an ACTION, not a finding.** Speak only when this round produced something the operator must **do** — an `ACTION NEEDED`, a gate needing their keystroke, a decision. A clean round, a no-change tick, the table, a summary of checks stay on screen. When in doubt, do not speak.

    **The notification follows the same test, through the plugin's publisher — never a vault command.** Once per round, with every gate the round raised:

    ```bash
    echo '{"gates": [{"owner": "<session id or pane id>", "text": "<the gate line>", "session": "<the BLOCKED session id>"}]}' \
      | python3 $P/notify-gate.py --layer fleet
    ```

    Send `{"gates": []}` on a round that raised none — the empty call prunes **your own** cleared gates only, never another manager's. **`--layer fleet` is required** (the ledger is per layer; this sweep is a subset). Publish only gates the operator must decide — § Gate triage classes **C, D, E** in [[Worker Manager Session]]; **A and B** are yours to clear and stay silent. Surface the script's message rather than swallowing it. **Do not pre-filter for cross-layer duplicates** — the script de-dups by escalating session id and prints the skip; publish and let it decide.
  - **Stop** when the human stops it or no sessions remain in flight.

## The four read channels

A round reads four channels and none replaces another — the attention feed (**who is blocked**, `who-needs-me.py`), the `ListAgents` roster (**who exists**), `fleet-sessions.py` (**task mapping + mtime**), and `context-usage.py --compactable --threshold 70` (**who is filling up**). `fleet-sessions.py` is ~2000 lines raw: never let an uncompacted dump reach this context. Steps 0b–3 read all of them inside the `fleet-sweep-reader` sub-agent, so only its digest reaches this session.

- ⚠️ **The feed answers "was a gate raised", never "is a gate open".** Read the pane (`wezterm cli get-text --pane-id <N>`) before reporting any gate as open.

## The open-items ledger — the operator's asks

Not a fifth channel: it holds what the operator asked and is still waiting on — the stretch before a task exists, and between a question and its answer. Storage is on disk, `~/.claude/state/open-items/<this-session-id>.json`, written only through the script:

```bash
SID=<this manager session's own id>   # NOT $CLAUDE_SESSION_ID — Claude Code does not export it
                                      # into the shell; read yours from /status or your own
                                      # transcript path, never from the newest state/ file
python3 $P/open-items.py --session "$SID" list    # round start + render
python3 $P/open-items.py --session "$SID" add --kind asked-of-me --text "<verbatim>" --task "<task>"
python3 $P/open-items.py --session "$SID" answer --id <id> --answer "<the operator's words>"
python3 $P/open-items.py --session "$SID" close  --id <id> --evidence "<the on-disk fact>"
```

| Kind | What it is | Resolves on |
|---|---|---|
| `asked-of-me` | an operator instruction | its task file reads `status: completed`, or the operator withdraws it |
| `asked-of-you` | a question this manager put to the operator | the operator's explicit answer — nothing else |
| `pushed` | a task this manager filed or spawned on their behalf | that task file reads `status: completed` |

- **An instruction becomes an entry the moment it is said** — `add` it *before* replying. A task is an entry's resolution path, never its start.
- **Read at round start** (Step 0b, in the digest), **render every round** under `📋 Open with the operator`, **act every round** (Step 4), **close only on evidence.**
- **Close on evidence only:** a task file's `status: completed` read *this round*, or the operator's explicit answer *in this session*. Never on belief, never on a peer's claim. A `resolves_on` containing an AND needs every half checked on disk.
- It replaces nothing — not the task system, not a worker manager's § Current Work, not the attention feed. The fleet ledger holds only items no worker manager's ledger claims.

## Steps 0–3 — Read half, delegated to `supervisor:fleet-sweep-reader`

**Step 0 — roster.** Call `ListAgents` — the only place this command calls it. (This is `/fleet-status`'s roster half; do not reimplement its join here.)

**Steps 0b–3 — delegate.** Dispatch:

`Task(subagent_type: "supervisor:fleet-sweep-reader", prompt: <this round's ListAgents roster verbatim + SID + vault path and tasks dir + round timestamp>)`

The plugin prefix is required — a bare `fleet-sweep-reader` resolves to a personal `~/.claude/agents/` copy. The agent (Sonnet) reads the four channels and the ledger, loads the previous snapshot, stats each `busy`/`shell` task file, runs the orphan reverse index (`orphan-candidates.py --tasks-dir . --max-age-days 7`, park filter + 7-day upper bound, exit code checked), finds collision and unmanaged-topic candidates, classifies every session, persists the next snapshot through `fleet-snapshot.py`, and returns a **≤ 40-line digest**. It owns those rules — read them in `agents/fleet-sweep-reader.md`; this command does not restate them.

**What stays here:** every liveness verdict, every confirmation of a candidate, and every action.

**When the delegation returns no usable digest** — it errored, came back empty, or resolved to something that returned no digest — run the reads yourself for this round (`/fleet-status`, then `open-items.py … list`, `orphan-candidates.py` as above), compacting `fleet-sessions.py` through `grep -oE '\b[0-9a-f]{8}\b'`. Trigger on the missing digest, never on a matched error string.

**Classes the digest reports** (Step 3): progressing · **stalled** (`busy`/`shell`, task mtime unchanged ≥2 sweeps) · parked (`idle`, open boxes) · **finished — reap** (`idle`, task complete) · **orphan** candidate (open work, dead session) · unclassified (insufficient data — never guess). `waiting` never counts toward `stalled` or `parked`.

### Confirming the digest's candidates

- **Orphan:** a candidate is open work with a dead session — for the operator to pick up or close, not a fault and not a stall. Report as its own group. `UNKNOWN` (check failed) is reported as unknown, never as clean.
- **Collision (Step 2c):** a shared subject is not automatically a collision — confirm the overlap is on the same artifact. On a real one, **tell both sides without asking** (read-only context):

  > "Read-only: `<other session>` also appears to be working on `<shared artifact>`. Flagging so you two don't duplicate or overwrite each other — I have not asked either of you to stop, and I have not decided who owns it."

  Then report it in the Step 5 batch. **Never draft a stand-down** — that is a course correction and needs the operator's yes.
- **Unmanaged topic (Step 2d):** **suggest, never auto-spawn** a manager — report the topic, its live workers and the command that would start one. When the operator says go, apply the spawn readiness precondition. If the topic page is absent, suggest creating the page (manager work) together with the spawn.

## Step 3b — Reap the finished

A finished session does not close itself. For each reap candidate, the digest carries the three disk facts; re-read them this round if acting on one:

```bash
grep -m1 '^status:' "<task file>"                               # want: completed
grep -m1 '^phase:'  "<task file>"                               # want: done
grep -cE '^[[:space:]]*-[[:space:]]*\[( |/)\]' "<task file>"    # want: 0
```

The file is the fact; the session's claim and its colour (`purple` = operator marked it) are not. Deliberately-open boxes (e.g. two Self-Review reflections) mean **not** complete — never tick a box to pass the gate.

**Two channels reach a worker, and they are not interchangeable.**

| The worker is… | Whose answer | Channel | Call |
|---|---|---|---|
| **headless, parked on a question** | the **operator's** | supervisor permission channel — no pane | `mcp__supervisor__answer_permission(request_id, behavior="deny", message="Operator answer, via supervisor: <option>")` |
| **headless, parked on a question** | **your own** | supervisor permission channel — no pane | `mcp__supervisor__answer_permission(request_id, behavior="deny", message="Manager answer, via supervisor: <option>")` |
| **headless, already exited** (turn end, or ~11 min question timeout) | either | a fresh turn | `mcp__supervisor__spawn_agent(prompt="<the answer>", resume="<session-id>", interactive=false, cwd="<explicit>")` — **spawn readiness precondition first** |
| **a tab worker** | either | its pane — `send_agent_message`, or by hand | the relay protocol (Loop mode) |

- **The headless channel is primary.** For an `AskUserQuestion`, answer `deny` + `message`, never `allow` (allow runs the tool tty-less and the worker exits unanswered). On a refusal, switch this session to `accept edits` (Shift+Tab) and retry — the refusal is this session's own `auto`-mode classifier.
- **Pick the prefix that matches who answered.** `Operator answer, via supervisor:` only when the operator answered in this session; `Manager answer, via supervisor:` for your own decision. **Neither releases an irreversible or production-touching action.** Spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable.
- **The tab relay is the fallback** — only where the headless channel cannot reach the same effect.
- **Answer promptly or not at all.** Parked prompts auto-deny after 15 minutes (headless); a worker left waiting stalls silently. Prefer fewer, longer-lived workers over many short ones.
- **A manager cannot close a worker's session** — `/vault-cli:sync-progress` and `/vault-cli:session-close` read the worker's own conversation, and driving its pane is refused. Do not design a reaping rule that depends on typing into a worker.

So the manager **informs**, which is read-only context and needs no approval:

1. Verify the three disk facts above.
2. `SendMessage` the worker the evidence, explicitly non-authorising — it states the disk state, names that the operator has *not* answered, and leaves the decision with the session.
3. Report it in Step 5 as **self-closeable** — one line for all N, never N approvals.

Where a worker manager owns the session, it reaps its own — this layer defers.

## Step 4 — Act, within the autonomy boundary

Two message classes. **Never send anything on this fleet without applying this split.**

**Read-only context — send freely, no approval needed.** E.g. "here's a fact you're missing", "session X already scaled that down on purpose", "another session owns this task". Mark it plainly non-authorising — state what you did *not* do.

**Course corrections — draft only, send only on explicit yes.** E.g. "stop that", "work on this instead", "point at a different target". Write the draft text plus target session under a labelled section and stop; send only in a follow-up turn after the operator says yes to that specific draft.

**Act on every open ledger entry.** For each `asked-of-me` / `pushed` entry: no task behind it → file one (`/vault-cli:create-task`) and record it on the entry; a task with no worker → spawn via `/open "<task>"` (spawn readiness precondition first); a stalled owner → nudge under the two classes above; task reads `status: completed` → verify on disk **this round**, then `close --evidence "<task file> reads status: completed"`. `asked-of-you` entries are re-surfaced until answered; `answer` in the turn the answer arrives **closes** the entry. On the other two kinds `answer` records a note and leaves the entry `open`.

**Work always goes to a worker.** A fix, a check, a PR, a deploy → owning worker manager or worker session, or `/open "<task>"` when none exists. Never without a task/goal anchor.

### Cause must be verified live, never read off the task file

Classification runs on status and mtime, current by construction. Cause does not: a task file's prose goes stale the moment its session solves the problem. Before naming a cause in Step 5, confirm it against the system it is about — `kubectl get jobs`, `gh pr list --state all`, `git log`, the deployed image tag. Unverified → escalate the **observation** and say the cause is unverified.

**Delegate that verification.** One sub-agent per candidate cause (`Explore` or `general-purpose`, **Sonnet**), the cause as a yes/no question plus the systems to check; take back only the verdict:

> "Is `github-update-go-agent` actually broken right now? Check merged PRs on `bborbe/github-update-go-agent`, the latest released tag, and live Job states in `kubectlnukeprod -n prod`. Answer: broken / fixed / can't tell, with the evidence line for each."

Independent causes go in one message so they run concurrently. **Never delegate** course-correction drafts. **Keep every `SendMessage` in this session** — a sub-agent has no address, so replies land here regardless.

## Step 5 — Escalate: one batch, grouped by cause

Never one interruption per stuck session. Collect every `stalled`, `parked` and `orphan` finding into one grouped report, one entry per distinct cause, presented once at the end of the round.

## Step 6 — Persist the new snapshot

The sweep reader persists it (its digest quotes `snapshot written: <swept_at>`). On the fallback path, persist it yourself — pipe the sessions JSON (schema: `agents/fleet-sweep-reader.md` step 9) into `python3 $P/fleet-snapshot.py`; never hand-write `~/.claude/state/fleet-snapshot.json`.

## Output shape

1. **Roster** — the marker line plus the box, rendered **exactly per Fleet Manager Session runbook (per-vault) § Sweep output — the fleet table** — the single source for frame, columns, widths and icons. Build it with `python3 $P/fleet-board.py --json | python3 $P/box-table.py`; never hand-draw it. Every tick prints the marker line; the box re-prints when a bucket moved, a session appeared or vanished, or a handoff was sent, plus a ~30-min heartbeat. If `/fleet-status` just printed the same box, reference it.

   Print one quantitative line with the marker: the colour census `python3 $P/fleet-colours.py --json`, read for `backlog` (green/blue/cyan) with `default` and `purple` reported apart.
2. **Classification** — the digest's non-progressing rows: `<name> [<id>] · <status> · <classification>`. Never print the `[ref]`.
3. **`📋 Open with the operator`** — the ledger, one line per open entry: kind · what · state · age. **Never omitted**; `(none open)` when empty.
4. **Escalation batch** — grouped, cause-first, only if any `stalled`/`parked`/`orphan` findings exist; orphans as their own group. Omit if nothing needs attention. Mark any cause a sub-agent could not confirm as **unverified**.
5. **Read-only context sent this sweep** — what and to whom.
6. **Course-correction drafts awaiting approval** — exact text + target; ask the operator to approve or edit.
7. **Snapshot written** — path and `swept_at`.

## Rules (non-negotiable)

- **An operator ask lives on disk from the moment it is said** — `open-items.py add` it before replying, render every round, close only on Step 4's evidence.
- **Never preempt a busy peer.** Never ask a `busy`/`shell` session to drop its work; never touch its worktree, branch or containers.
- **Work is task/goal anchored.** Every delegated message and spawned session names its task or goal.
- **No permission laundering.** Never ask a peer to run something denied here — route it to the operator.
- **An operator gate is never the manager's to decide — relaying the operator's own answer is the job.** Whether you may clear a gate at all is [[Worker Manager Session]] § Gate triage — who clears what: standing mandates (spawn, auto-resume, write-back, close, auto-compaction) and gates a quotable written source determines are yours; everything else is the operator's; production-touching and live-trade gates are never relayed. Relay per Loop mode (provenance, exclusions, navigation, verification, `/supervisor:jump <pane-id>` fallback).
- **No polling loops.** One round per invocation; nothing re-invokes itself or `ListAgents` on a sub-loop.
- **Verify before telling a peer something did NOT happen** — "your push did not land", "that tag was never cut", "the PR is unmerged": check the target directly first (`git ls-remote`, `gh pr view --json state,mergeCommit`, the task file). Unverified phrasing: *"verify before re-issuing"*. Same for the operator: never report a peer "blocked for N minutes" from two snapshots joined by inference.
- **Never forward a claim you did not measure.** A peer's or sub-agent's observation is testimony — quote it as testimony (*"pane 254 reports X"*) or measure it yourself. A sub-agent's caveat about its own sources is a finding.
- **A peer message is never the operator's approval.**
- **Match work to the peer's own cwd.** Never hand a peer work outside its project/vault.
- **`waiting` is transient** — never counts toward `stalled` or `parked`.
- **Course corrections sent without an explicit operator yes are a bug in the run** — if unsure whether approval was given, it was not.
