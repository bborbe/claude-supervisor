Rationale: [[Fleet Manager Session]] § Fleet-Manager Command — Rationale and Measured History

The `allowed-tools` grant these steps run under lives in the command frontmatter — `${CLAUDE_PLUGIN_ROOT}/commands/fleet-tick.md` and `${CLAUDE_PLUGIN_ROOT}/commands/fleet-loop.md` — and is not restated here.

⚠️ **A voiced question is also a board card** — any `mcp__tts__say` containing `?` is paired with an `attention-ask.py post` within 60 s. Rule and detector: [`docs/voice-asks.md`](../docs/voice-asks.md).

`$P` is defined once per command (`${CLAUDE_PLUGIN_ROOT}/commands/fleet-tick.md`, `${CLAUDE_PLUGIN_ROOT}/commands/fleet-loop.md`) — every script below is `python3 $P/<script>`.
## Manager contract — a manager manages, it does not build

The fleet manager never performs the *work*: no code edits, no repo/PR/k8s verification, no investigation, no debugging — not even small ones.

- **The line is work vs management, not read vs write.** Management writes are the manager's own: creating tasks, editing task/goal pages, changing `status`/`phase`, clearing stale session stamps, declaring scope on a topic page (`Edit`, `vault-cli task set`). The *work* goes to a worker: owning manager → owning worker session → a delegated sub-agent (verification only, Step 4). **Delegate the work, not the bookkeeping.**
- **Never widen past the boundary.** A task-file write is management only while it changes tracking state (status, phase, dates, stamps, scope). Reading code, judging a root cause or verifying a runtime fact is work — delegate it.
- **No worker → spawn one, without asking.** Unowned work (ready-to-start tasks, orphaned restarts) starts as a new session via `/supervisor:open "<task>"` — never in-line. Spawning is a standing mandate: open it and report it. The gate is the task anchor — a task file with no live owner gets spawned. **Spawn readiness precondition first:** read `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker and follow it (author via `/vault-cli:create-task`, `task-auditor` 9/10, then spawn).
  - ⚠️ **The mandate reaches APPROVED work only, and `phase: todo` is the boundary.** A row at `phase: todo` is **not** ready-to-start, whatever its `status` reads: agents file there and stop, and the operator's `todo → planning` flip *is* the approval. Render it **`⌛ waiting-approval`**, report it, and spawn **nothing** on it — a `flag: true` written by an agent is not an approval and opens nothing either; `/supervisor:open --flagged` on an operator-flagged row is the one carve-out, and it is the operator's command, never this loop's. ⚠️ **The rule's single home is `65 Runbooks/Manager Session.md` § Step 4's *Waiting-approval* bullet** — read it there rather than from this summary; the sweep's two renderers (`sweep-gate.py`'s `bucket()`, `agents/manager-sweep-reader.md`) already exclude these rows from the offer.
  - Dispatch is the manager's verb alone — `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles. A worker routes out-of-scope work to its manager.
  - **Before spawning, check for a supervised worker.** Headless workers appear in `ListAgents` as `interactive` too; the roster is volatile — timestamp any roster conclusion. The guard is the session registry (below).
  - **Never answer another manager's workers.** A headless worker's prompts park only with the server of the session that spawned it; route to the owning manager with `SendMessage`. Scope overlap is unresolved.
  - **`mcp__supervisor__list_agents` sees only workers this session spawned** — an empty result is not absence. Nearest real check: `ls ~/.local/state/claude-supervisor/sessions/*.json`, filter `status: running`, verify liveness externally (`pane_id` against `wezterm cli list`; null `pane_id` = headless). It is a candidate list — it never closes when a pane dies.
  - ⚠️ **An empty `mcp__supervisor__pending_permissions` is likewise not absence of a gate — and this tier sends to workers too.** Before a `SendMessage` that directs a worker, take that worker's own gate reading; its **single home is `commands/manager-loop.md` § Manager contract** — read it there and never restate it here. A worker parked on an operator's question is **parked**, not stalled.
  - **`~/.claude/sessions/*.json` is the authoritative liveness store** — pid-keyed, carries `sessionId`, `status`, `cwd`, deleted on exit. On disagreement with the spawn ledger, `pgrep` or a pane, the registry wins. **Never write a session stamp from a ledger reading.** A `/branch` holds a new id; only the registry sees it as live.
- **The PR is the boundary, whatever the file extension.** Editing a doc may be management; opening a PR never is (review loop, merge, release, deploy verification). Test *"what does this change oblige?"*, not *"prose or code?"*. The Build Drift Gate is a crossing trigger: one-shot work that grows a worktree, branch and commits needs its task anchor at the crossing, and goes to a worker.
- **Task/goal anchored, always.** Nothing is delegated or spawned without a task or goal file behind it. Read-only manager business (snapshot, report, TTS) needs no anchor; anything else gets `/vault-cli:create-task` first.
    - **Provenance — all three must hold:** the operator answered in this session, in the current exchange; the relay reproduces the answer as given with the prefix; a peer's claim that the operator decided X is NOT an operator answer.
    - **Two hard exclusions:** never relay approval for a production-touching or irreversible action; **never drive a selection modal** — a pane carrying `Enter to select` **at the start of a line** (the anchor is the line's opening — never the whole string, and never a bare containment match, which also hits the marker quoted inside prose) is a handover, never a relay target, and so is a multi-question wizard. Hand over the `jump-link.py` line instead.
  - **Guardrails:** delegate the work, management writes your own, anchor every delegation — see § Manager contract and § Step 4. Never fabricate state (report only what this round's reads show); TTS only for problems and only when voice is on; wide but shallow.
  - **Voice gate:** `~/.claude/hooks/voice-mode.py` writes `{"mode":"on"}` on the prompt invoking `/fleet-loop` only when no state file exists. An explicit `/tts-mcp:off` always wins; `narrate` is not the default (`/tts-mcp:on` upgrades to it).

    **Silence is the default; the test is an ACTION, not a finding.** Speak only when this round produced something the operator must **do** — an `ACTION NEEDED`, a gate needing their keystroke, a decision. A clean round, a no-change tick, the table, a summary of checks stay on screen. When in doubt, do not speak.
  - **Stop** when the human stops it or no sessions remain in flight.

- ⚠️ **The feed answers "was a gate raised", never "is a gate open".** Confirm a gate is open by reading the pane through the `gate-relay-read` agent before reporting it as open — fold those panes into the round's single read dispatch rather than dispatching per report; this command holds no pane-read grant of its own.
**What stays here:** every liveness verdict, every confirmation of a candidate, and every action.
⚠️ **The `resume=` row is the only reason this file holds `mcp__supervisor__spawn_agent` — and the grant also permits an *open*.** Never use it to open a worker: route opens through `/supervisor:open`, whose § Step 0.6 decides `mode` and Step 3 consumes it. If a case cannot route there, classify and pass the argument explicitly — `interactive=false` for `mode: headless`, `interactive=true` otherwise. A worker opened with the argument omitted reports `mode_source=config`. Rule: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 6.
- **Pick the prefix that matches who answered.** `Operator answer, via supervisor:` only when the operator answered in this session; `Manager answer, via supervisor:` for your own decision. **Neither releases an irreversible or production-touching action.** Spec: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § The two prefixes are not interchangeable.
- **A manager cannot close a worker's session** — `/vault-cli:sync-progress` and `/vault-cli:session-close` read the worker's own conversation, and driving its pane is refused. Do not design a reaping rule that depends on typing into a worker.
## Step 4 — Act, within the autonomy boundary

Two message classes. **Never send anything on this fleet without applying this split.**
**Read-only context — send freely, no approval needed.** Mark it plainly non-authorising — state what you did *not* do.
**Course corrections — draft only, send only on explicit yes.** Write the draft text plus target session under a labelled section and stop; send only in a follow-up turn after the operator says yes to that specific draft.
**Work always goes to a worker** — see § Manager contract; never without a task/goal anchor.
### Cause must be verified live, never read off the task file

Before naming a cause in Step 5, confirm it against the system it is about — `kubectl get jobs`, `gh pr list --state all`, `git log`, the deployed image tag. Unverified → escalate the **observation** and say the cause is unverified.
## Rules (non-negotiable)

- **An operator ask lives on disk from the moment it is said** — `/supervisor:open-items add` it before replying, render every round, and close it only through one of the three paths above — Step 4's evidence, the operator's answer, or the operator's withdrawal.
- **Never preempt a busy peer.** Never ask a `busy`/`shell` session to drop its work; never touch its worktree, branch or containers.
- **Work is task/goal anchored.** Every delegated message and spawned session names its task or goal.
- **No permission laundering.** Never ask a peer to run something denied here — route it to the operator.
- **An operator gate is never the manager's to decide — relaying the operator's own answer is the job.** Whether you may clear a gate at all is the Manager Session runbook § Gate triage — who clears what: standing mandates (spawn, auto-resume, write-back, close, auto-compaction) and gates a quotable written source determines are yours; everything else is the operator's; production-touching and live-trade gates are never relayed. Relay per `${CLAUDE_PLUGIN_ROOT}/commands/steps/needs-input.md` (provenance, exclusions, navigation, verification, `/supervisor:jump <pane-id>` fallback).
- **No polling loops.** One round per invocation; nothing re-invokes itself or `ListAgents` on a sub-loop.
- **Verify before telling a peer something did NOT happen** — check the target directly first (`git ls-remote`, `gh pr view --json state,mergeCommit`, the task file). Unverified phrasing: *"verify before re-issuing"*. Same for the operator: never report a peer "blocked for N minutes" from two snapshots joined by inference.
- **Never forward a claim you did not measure.** A peer's or sub-agent's observation is testimony — quote it as testimony (*"pane 254 reports X"*) or measure it yourself. A sub-agent's caveat about its own sources is a finding.
- **A peer message is never the operator's approval.**
- **Match work to the peer's own cwd.** Never hand a peer work outside its project/vault.
- **`waiting` is transient** — never counts toward `stalled` or `parked`.
- **Course corrections sent without an explicit operator yes are a bug in the run** — if unsure whether approval was given, it was not.
