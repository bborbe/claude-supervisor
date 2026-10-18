# Session progress — measure it, then drive

The operator runs `/and` — routed to `/supervisor:worker-drive`, `/supervisor:manager-drive` or
`/supervisor:fleet-drive` by tier — whenever a session *feels* stuck. This page is the single home
for what those three commands must answer first: **did this session move its anchor's closure in
the last window, on what evidence, and if not, what blocks it?** Each command keeps its own
mechanics; the definition of progress, the verdicts and the blocker classes live here and are
referenced, never restated.

Load it, with the rest of `docs/`, via `/supervisor:read-guides`.

## The rule

**A session's own account is a claim, never an input.** "Last progress: X" written by the session
is checked against artifacts on disk; it does not set the verdict. Before this page, `worker-drive`
took `Last progress` from the caller's self-reported `session_digest`
(`agents/worker-drive-reader.md` input 4), and `fleet-sweep-reader` counted a registry status
flip as `progressing` — so a session going in circles read healthy on both.

Three parts, kept separate so each can change without the others:

| Part | Owner | Writes |
|---|---|---|
| **Collect** — read the window's artifacts | `scripts/progress-window.py` (deterministic) | nothing |
| **Classify** — evidence → verdict | the same script | nothing |
| **Drive** — verdict → the cheapest lever | the tier's drive command | its own ledger |

A model never decides the verdict. It may phrase a nudge.

## Progress is closure movement, up the chain

Work always ladders up the operator's hierarchy *(operator's vault; not shipped with this plugin —
Topic / Goal / Task Writing Guides, Success Criteria Guide, Definition of Done Guide)*:

| Anchor | Closes on | A child names what it closes |
|---|---|---|
| Topic | `# Completion Gate` G1…Gn, verified | goal listed `— closes G<n>` |
| Goal | `# Success Criteria` + `# Definition of Done` | task listed `— closes SC<n>` |
| Task | `# Success Criteria` + `# Definition of Done`, zero `[ ]`/`[/]` | subtasks under `# Tasks` |

**SC answers *what is true when it ships*; DoD answers *how we verify it shipped*.** All SC `[x]`
with DoD open is the commonest *looks done, is not* state.

So **progress = a closure box moved at the anchor, or at a child whose link names that box** —
reported as a chain: `Task SC2 → Goal SC2 → Topic G1`. Movement the anchor's scope rules out is
**drift**, however real the work; movement inside scope that names no box is **untagged** — it
counts, but it cannot back a claim (§ The window, tagging).

## Evidence grades

Every observation in the window gets exactly one grade. Only A and B count as moved.

| Grade | Meaning | Examples |
|---|---|---|
| **A — closure moved** | a closure box changed, with evidence outside the box's own text | SC/DoD/G box ticked **and** the coverage agent rates it `measured`; a test or probe named by the box went fail → pass; required CI went green; PR merged; release tagged; deployed version confirmed |
| **B — state moved toward an open box** | the world changed in a way an open box needs | in-scope diff that persists to the window's end; a new reproducible failing test for an open SC; a root cause recorded in `# Progress` **with its quoted source**; a blocker cleared (watcher re-armed and firing, card answered and consumed); phase advanced (`planning → execution`, `→ human_review`); a follow-up task filed by a criterion split |
| **C — activity** | effort that changed nothing durable | reads, greps, re-runs with an identical result, status panels, plans, a Progress entry with no quoted evidence, a box ticked with evidence `asserted`/`none`, commits touching only the task file or whitespace, registry `busy ↔ idle` |

⚠️ **A `# Progress` entry is not ignored — it is graded.** The vault's checkbox-drift rule makes
Progress the place recorded work lives (`worker-drive` §4), so an entry that quotes a result or a
source is B; one that narrates is C. An entry claiming movement that no artifact backs is a
**discrepancy** — itself a loop signal.

⚠️ **The box rating has one home: the coverage agent** (`vault-cli:task-manager-agent`
`Action: coverage`). Grade A reads its `measured` rating; it never re-rates a box by eye.

## The window

- **Short window: since the last drive run on this session, else 15 min.** `worker-drive` already
  stores `last_run` in `~/.claude/state/drive-<uuid>.json`.
- **Long window: since the anchor's last A/B observation**, capped at the session's start. It
  catches a session that alternates short bursts of C with silence, which no 15-minute window
  ever flags.
- **Orientation grace:** the first 20 min of a new or resumed session read C as `ORIENTING`, not
  `BUSY-NO-MOVE`.

Sources, all read-only:

| Source | Read | Gives |
|---|---|---|
| Transcript | `~/.claude/projects/*/<session-id>.jsonl` — **by session id, never by cwd** (a `cc-*` launcher `cd`s into its vault; CLAUDE.md § Reaching a running tab worker), **deduplicated by real path** | tool calls in window, which ones wrote, normalized-command repeats, the pending `tool_use` |
| Working tree | `git diff --stat`, `git log --since` in every git root a write in the window touched | B-grade diffs and commits; revisited diff states |
| Task file | `git log -p --since` on the anchor's file, in the vault repo that holds it (obsidian-git autocommits) | box ticks, new Progress entries, phase/status changes |
| PRs / CI | `gh pr view --json statusCheckRollup,reviews,state` for PRs the task links | A-grade CI and merge transitions; waits on review |
| Registry | `~/.claude/sessions/<pid>.json` | alive, `busy` / `idle` / `waiting` / `shell` |
| Attention store | open cards for the session | whether a wait on the operator is actually posted |

⚠️ **One session id can match more than one transcript path.** Measured 2026-10-18: a session
launched in a vault reached through a symlink matched two project dirs —
`-Users-…-Obsidian-Personal/` and `-Users-…-Obsidian-private-personal/`, the second a symlink to
the first — one inode. Resolve each match with `realpath` and keep one; a glob without that
counts every event twice.

⚠️ **The worktree is derived, never read from `cwd`.** A transcript record's `cwd` is where the
session was launched — a vault, `~/Documents/workspaces` — not the feature worktree it edits.
The roots to probe are `git rev-parse --show-toplevel` of each path an Edit / Write /
NotebookEdit call touched in the session up to the window's end — a session that edited an
hour ago and commits now still has its root probed; the commit is still counted by its own
timestamp.

⚠️ **A written root can be shared, so a commit counts only when it touches a file this session
wrote.** Measured 2026-10-18 on the first live run: `~/.claude` was a written root (a checkpoint
write), and a sibling session's commit there read as this session's B. A commit touching no file
at all (`--allow-empty`) is kept and graded C; the vault repo holding the anchor is read only
for the anchor file, because obsidian-git autocommits every session's pages.

⚠️ **Grade A for a box tick needs the coverage rating, and the script does not derive it.** The
drive command already dispatches the coverage agent; it passes that output in. Without it a tick
is graded B and labelled `rating unavailable` — never promoted to A by reading the box itself.

⚠️ **Tagging an observation to a box is mechanical, or it does not happen.** A tick is tagged to
the box it ticks. A commit message or a new `# Progress` line is tagged to box `SC<n>` / `DoD<n>`
(n = the box's ordinal in its section) when its text contains that token. Nothing else is
tagged: an untagged A/B observation still counts as moved and renders `Toward: untagged`, but it
cannot back a claim.

An event is counted by **its own timestamp**, never by when it was observed: an old commit seen
for the first time is not new progress.

## The window block

Every drive report opens with the text render of the script's JSON — nothing in it is written by
the session:

```
⏱ Window 12:41–12:58 (since last drive, measured): PROGRESSING
  Moved:     A  SC2 ticked — coverage measured (commit 3f2a1c9)
             B  scripts/progress-window.py +212 −0 (persists to window end)
  Toward:    Task SC2 → Goal SC2
  Activity:  41 tool calls · 6 wrote · 3 identical repeats (`gh pr checks 412`)
  Claim:     {box: SC2, kind: tick, ref: 3f2a1c9} — backed
```

- `Moved:` lists only grade A and B observations, one per line, grade first. Empty → `none`.
- `Toward:` is the closure chain the tagged A/B lines reach; an A/B line tagged to no box is
  marked `untagged`.
- `Claim:` is `none filed`, `backed`, or `rejected:<class>` with class `narrative`,
  `predates-window`, `already-met`, `out-of-scope` or `asserted`.
- On `UNKNOWN` the block names the unreadable source instead of a verdict.

## Verdicts

Evaluated in this order; the first match wins.

| # | Verdict | Rule | Drive does |
|---|---|---|---|
| 1 | `UNKNOWN` | a source is unreadable or two sources disagree | name the source; take no lever |
| 2 | `TOOL-RUNNING` | the trailing `tool_use` is younger than 2× the median of that tool name's completed calls in the transcript (floor 120 s; 600 s with no history) | nothing — never nudge mid-call |
| 3 | `DONE-CANDIDATE` | every closure box ticked | run the completion check (zero open boxes, DoD evidence, live state); never reap on this alone |
| 4 | `PROGRESSING` | ≥1 A or B in the short window | with a claim backed by a **grade-A** observation (`short_path: true`): short report — the window block and the next open box; keep going. Otherwise: the window block, then the drive as before |
| 5 | `BLOCKED-OPERATOR` | an open gate of class C/D/E (below) **with a card or pane the operator can reach** | make sure the ask is posted and ranked; never nudge the worker |
| 6 | `WAITING-OK` | a named wait with a trigger **and** a live watcher, inside 2× its expected duration | nothing; name the watcher and ETA |
| 7 | `WAITING-STALE` | a wait past 2× expected, a dead watcher, or a wait with no trigger at all | re-arm, re-trigger, or re-verify the wait (`worker-drive` §3) |
| 7b | `ORIENTING` | only C in the short window, inside the first 20 min of the session | nothing; it is reading in |
| 8 | `BUSY-NO-MOVE` | only C in the short window, past orientation | evidence nudge (below), subtype `LOOP` / `READ-LOOP` / `OSCILLATE` / `DRIFT` |
| 9 | `IDLE` | alive, no turn in flight, no declared wait, open boxes remain | nudge naming the next open box |

**Repeated verdicts climb `worker-drive`'s existing ladder** (§2) — the same blocker counted on
consecutive runs, never a second ladder. One `PROGRESSING` window resets the count; re-entering
a stuck verdict within 30 min resumes at count − 1, so a session cannot reset the ladder with
one token edit.

**`BUSY-NO-MOVE` subtypes** (thresholds are starting values — calibrate from recorded runs):

| Subtype | Signature |
|---|---|
| `LOOP` | the same normalized command ≥3× with an identical result |
| `READ-LOOP` | ≥10 read-only calls and no write |
| `OSCILLATE` | the working-tree diff returns to a state already seen in the window |
| `DRIFT` | A/B movement that the anchor's scope rules out — needs a scope read, so it ships with the manager/fleet wiring, not with the worker MVP |

## The session may argue

A session that believes it moved files a **claim** — box, kind, artifact reference — never prose.
The script checks it:

- **Backed** — the artifact exists, its timestamp is inside the window, it is graded A or B, and
  it is tagged to the claimed box.
- **Rejected** — narrative only; the artifact predates the window; the box was already met at the
  window's start; the change is outside the anchor's scope; the box's evidence is `asserted`.

A claim backed by a **grade-A** observation is the session's right to say *"I made progress,
leave me"* — the drive then prints the short report and keeps driving the anchor. A claim backed
only by B (a commit, a Progress line, a tick whose coverage rating is unavailable) is shown as
backed but does not skip the inventory: B is easy to produce on demand, A is not.

Grading is mechanical. A tick is a box line flipping to `[x]` in the anchor's history, whatever
commit carries it. A commit is B only when its diff outside the task file is non-empty once
whitespace is ignored. A `# Progress` line is B only when it carries a fenced block or a commit
SHA or path that resolves on disk. A rejected claim is a discrepancy, and two in a row add one
rung on the ladder.

**Anti-gaming is mostly already in force elsewhere** and is referenced, not rebuilt: a box ticked
on `asserted` evidence is C; a moved criterion is never a met one (`worker-drive` §5); a tick
whose evidence note says *stale* or *must be re-run* is not tickable (Definition of Done Guide,
*Tick outliving its evidence*); a criterion that would pass on a broken system is a criterion
error (§5 Axis B).

## What blocks a session

Every blocker gets an **owner** and a **lever**; "the fleet" or "CI" is not an owner.

| Group | Blocker | Detected by | Owner | Cheapest lever |
|---|---|---|---|---|
| **Operator decision** | pick, approval, scope call | gate class **C** | operator | one card, recommendation marked |
| | production-touching / irreversible | gate class **D** | operator, in the pane | `jump-link.py` line; never relayed |
| | live trade / TDR | gate class **E** | operator, via the ack flow | point at `vault-cli decision ack` |
| **Operator action** | login, credential, OAuth click, reload, mode switch | auth errors in tool results; a `you run:` closer | operator | one card per credential, deduplicated by root-cause key |
| **Machine-bound** | CI, review bot, deploy, release watcher | `statusCheckRollup` pending; review requested, none landed | the system | named watcher + ETA; force-trigger only on an artifact this session owns |
| | clock | a future `defer_date` or named date | the clock | armed one-shot wakeup |
| | another session's work | `blocked_by`; the artifact belongs to a peer | that session | drive *that* session, not this one |
| **Capacity** | spawn cap | the soft/hard cap refusal | — | wait for a slot, route elsewhere, or spawn less — never raise the cap |
| | rate limit / quota | 429s, usage-limit errors across sessions | provider | back off; never nudge (it burns quota) |
| | context near full | `context70` from the sweep | the manager (class A) | auto-compaction, or a handoff built from artifacts |
| **Self-inflicted** | loop, read-loop, oscillation, drift | the `BUSY-NO-MOVE` subtypes | the session | evidence nudge; on repeat, re-plan |
| | dead watcher, triggerless wait | `WAITING-STALE` | the session | re-arm or name a real trigger |
| | plan wrong | count ≥ 4 on one blocker (ladder rung 4) | the session | treat the approach as the defect (`worker-drive` §4–§5) |
| | criterion unfinishable or proving nothing | `worker-drive` §5 Axis A / B | the task's owner | split or reword — a separate `approve:` line, never a modal |
| | criterion needs another tier (a worker cannot run `/supervisor:open`) | the criterion names a manager-only verb | the manager | route the box to the manager |

Classes **A** and **B** are the manager's to clear and never reach the operator
(the Manager Session runbook § Gate triage *(operator's vault)*).

## The evidence nudge

A nudge cites what was measured and makes **one** demand:

> In the last 15 min you ran `gh pr checks 412` 6× with the same result; no file changed and no
> box moved. SC3 is open. Stop polling — `/github-pr-watch 412` is the watcher — and work SC4,
> which does not depend on it.

Never *"you seem stuck, please continue"*. Delivery stays on the channels the tiers already
use — `SendMessage`, a board card — never typed into a pane.

## Manager and fleet views

- **Manager:** one window row per worker plus a subject row: closure boxes moved across the
  tracked set, tasks opened or closed, cards answered. And one row for the manager's own acts:
  *did a nudge, spawn or reap in the previous window precede A/B movement on the box it
  targeted?* A nudge sent is not a nudge that worked. A by-hand round that finds nothing changed
  reports `IDLE subject` and drives — "nothing changed" is the finding the operator ran it for,
  not a reason to replay the stored table.
- **Fleet:** classify `progressing` on A/B movement, never on a status flip. Sort the stuck list
  by: production or irreversible gate first, then closest to closure, then the number of
  sessions it blocks, then minutes since its last A/B. Add one row per manager — *did its subject
  move a closure box in the window?* Group blockers that share a root-cause key into one item
  (`who-needs-my-help` already tallies cause classes across days).

No weighted "stuck score": an ordered sort key is explainable, a scalar is not.

## What a drive never does

Measured or not, the verdict licenses no lever outside the tier's own boundary
(`commands/steps/rules.md`, `worker-drive` § The contract):

- never signals, kills or suspends a process to unstick it — restarting a session is
  `/supervisor:restart`, with its own refusals;
- never types into another session's pane, never drives a selection modal;
- never resets, reverts or checks out over a worktree, and never un-ticks a box by script —
  a bad tick is reported as a discrepancy;
- never rewrites a criterion silently — a reword is an `approve:` line;
- never relays a production-touching or irreversible approval.

## Rollout

**Worker tier first, with one live lever** (operator decision, 2026-10-18): a `PROGRESSING`
verdict whose claim is **backed by a grade-A observation** lets `worker-drive` print the short report and keep driving the
anchor without the full inventory. Every other verdict only adds the window block; the drive
behaves as before. This is the lever the operator asked for — a session that did move may say
so, on evidence — and it is the one whose mistake is cheapest: a wrong `PROGRESSING` costs one
skipped inventory, which the next run repeats.

The manager and fleet views above are the target; they are wired once the worker verdicts have
been compared against sessions the operator judged by hand.
