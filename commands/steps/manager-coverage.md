---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Coverage

When: every tick, before the drive leg

Rationale and incident history: the Manager Session runbook § Command rationale — maintained.

- **Map every closure box to its driving subtask and its evidence rating — dispatch the coverage mode, never re-derive it.** The round's job is to report which Success Criterion moved; a table of busy sessions answers a different question. One dispatch per closure-box owner:

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

- ⚠️ **This is the manager tier's only copy of the dispatch.** The worker tier dispatches the same mode in `commands/worker-drive.md` and the verify leg in `commands/manager-verify.md`; all three call the one agent implementation, so the coverage rule has a single home rather than a copy per tier.
