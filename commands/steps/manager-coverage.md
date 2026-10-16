---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Coverage

When: every tick, before the drive leg

Rationale and incident history: the Manager Session runbook § Command rationale — maintained.

- ⚠️ **Ask the gate first — it decides how many dispatches this tick warrants, and on a no-change tick the answer is none.** Run it before any dispatch:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manager-coverage-gate.py \
  --tracked "<the tracked-set path step 2 wrote>" \
  --delta "<the sweep's own delta, as the sweep reader rendered it>" \
  --changed "<the names the sweep reports as moved, comma-separated>" \
  --subject "<absolute path to the subject page>"
```

  - **Exit 0, nothing on stdout → dispatch nothing and skip the rest of this step.** The sweep already answered the question this map exists to answer; re-deriving it buys the same answer again at subagent prices.
  - **Exit 10 → one owner path per line on stdout: dispatch exactly one agent per line**, as below. That is the whole contract — the line count IS the dispatch count.
  - **Exit 2 → the gate could not read the tracked set.** Fall back to the round's own tracked set and dispatch every owner. Fail open, never fail quiet: a coverage map silently skipped on a tick that moved leaves the round reporting a stale table as current, and nothing announces the loss.
  - ⚠️ **`--changed` is comma- or newline-separated, NEVER whitespace-separated.** Task names contain spaces, so a whitespace split shreds every name, none resolves, and the gate falls open on every call — the saving gone while every fail-open assertion still passes. This suite caught exactly that on its first run.
  - ⚠️ **`--delta` takes the sweep's own rendering — pass it as it stands.** The gate accepts the whole `Delta: …` line, its leading clause, or the bare token: `no-change` / `no change` / `unchanged` / `nochange` read as NO CHANGE, `change` / `changed` as CHANGE. ⚠️ **Only the no-change rendering is decided for you.** The sweep reader renders a *change* as prose (`Delta: <what moved>`) and renders `uncomputable` in the same shape, so the gate fails open on both rather than guess between them — pass the literal `change` on a moving tick, or pay for every owner. Measured 2026-10-16: passing the rendered `Delta: no change` line verbatim fell open and dispatched every owner, because the gate accepted bare tokens only — the saving gone on exactly the tick it exists for.
  - ⚠️ **The delta is the sweep's, passed in — never recomputed here.** `agents/manager-sweep-reader.md` step 1 item 3 already computes it against the prior snapshot, and this step is not a second classifier (see the read-only clause below).
  - Measured 2026-10-16 on the Recurring Operator Gates manager, session `beac5fd0`: a tick whose sweep reported `Delta: no change` dispatched 15 coverage agents — 14 `task-manager-agent` plus 1 `goal-manager-agent` — at 17,516 output tokens and 770,459 counting cache reads. Making that tick dispatch 0 is this gate's whole job.

- **Map every closure box to its driving subtask and its evidence rating — dispatch the coverage mode, never re-derive it.** The round's job is to report which Success Criterion moved; a table of busy sessions answers a different question. One dispatch per owner the gate emits:

```
Task tool with:
  subagent_type: 'vault-cli:task-manager-agent'
  prompt: 'Action: coverage. TASK_PATH: <absolute path to the tracked task file>. MODE: tool.'
```

  - **The owner picks the agent, and the two kinds are never merged into one call** — each agent parses its own file's sections. A tracked task's boxes belong to `vault-cli:task-manager-agent`; the subject page's own boxes (a goal's, or a topic's) belong to `vault-cli:goal-manager-agent` with the same `Action: coverage`. A per-task map never reaches the subject page's boxes, and a subject-page map never reaches a task's.
  - ⚠️ **One dispatch per owner per round, not per box and not per criterion.** The mode already emits one row per box; a second call for the same file is the same answer bought twice, and it is the counter-risk the token guardrail in `the topic's Impact` names.

- **Each dispatch returns a row per box — quote it, do not summarise it.** The shape is `<box> | <driving subtask or none> | <evidence: measured(<source>) | asserted | none> | <proposal>`, closed by the agent's own reconciliation line:

```
Coverage: {boxes} boxes · driving: {driven} driven · {undriven} none · evidence: {measured} measured · {asserted} asserted · {unrated} none
```

  - ⚠️ **The row format and the evidence rating rule have ONE home — the agent** (`agents/task-manager-agent.md` § coverage in the vault-cli plugin). This file routes the call and says what the round does with the result; it never restates the rating rule, because a second copy drifts the moment the agent's changes.
  - ⚠️ **Carry the reconciliation line verbatim, including its two partitions.** `driven + undriven = boxes` and `measured + asserted + unrated = boxes` are two cuts of one set, and the two `none` counters are different quantities — a line that does not reconcile is an incomplete run, not a shorter report.

- **A `none` driving cell is this round's *unfinishable by construction* signal — surface it, never decide it.** The round carries the box, its proposal (`create` / `re-scope` / `split` / `diagnosis-task`) and the quoted evidence into the round's decisions. ⚠️ **A box with no driving subtask cannot be closed by keeping a worker busy on it**, which is the exact failure this step exists to make visible: the old round could report every session progressing while no criterion moved. Deciding the proposal is the operator's or the drive leg's, not this step's.

- **A box rated `asserted` is the met-looking box, and it is the reason the column exists.** `asserted` means a ticked box or a prose claim with no quoted result behind it — it looks done and proves nothing. ⚠️ **Never round it up to `measured` by reading the box's own wording**: the rating rule requires a source *outside the row*, and a box whose text quotes a result does not rate itself. A `measured` rating carrying no named source is a defect in the return, not a pass.

- ⚠️ **Read-only, and never a second classifier.** The mode reports; it never ticks, re-links, edits or reclassifies — the sweep owns the buckets and the drive leg owns the acts. Do not widen this step to act on what it finds.

- ⚠️ **This is the manager tier's only copy of the dispatch.** The worker tier dispatches the same mode in `commands/worker-drive.md` and the verify leg in `commands/manager-verify.md`; all three call the one agent implementation, so the coverage rule has a single home rather than a copy per tier. ⚠️ **The gate above belongs to THIS site alone.** It saves a dispatch that a *repeating* round would otherwise buy every tick; the worker tier dispatches once per run and the verify leg once per verify, so neither has a repetition to suppress and neither should gain a no-op gate.
