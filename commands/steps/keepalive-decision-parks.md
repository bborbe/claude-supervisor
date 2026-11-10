---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps/<name>.
user-invocable: false
---

# Resume a decision-parked worker whose wait cleared

The clock leg (`agents/fleet-keepalive-actor.md` § 2) reads a wait that wrote itself a
`defer_date`. Three waits write none and no reader resumes them: an **approval** that landed
after its worker was closed, an **answered attention card** whose worker exited, and a
**`blocked_by` set** that has gone terminal. The reap (`scripts/reap-idle-headless.py`) closes
such a worker at 15 minutes idle and deliberately leaves the transcript and the task's
`claude_session_id` intact, so the row is resumable — and until this half runs, nothing resumes
it. The clock leg is not a substitute: it reads `park-event.py due`, which holds only waits that
registered a watch.

## Population — the reap's residue, read from the task files

A row is a candidate when its `claude_session_id` names a session `session-liveness.py` does
**not** report live and its `status` is still live (`next` / `todo` / `in_progress`). Do not
re-derive that index: `approved-not-started.py` already walks it and splits the two classes —
`never-started` (no id; the lever is open, not resume) and **`orphaned`** (id not live; the lever
is resume). This half takes `orphaned` only.

⚠️ **That index is a starting point, not the whole population, and its own predicate says so.**
`approved-not-started.py` selects rows carrying `approved_at`, so a reaped worker whose task was
never formally approved is absent from its `orphaned` class — and every row it does return carries
`approved_at` by construction, which is why kind (a) below cannot be read as "the field is present".
Where the index and a direct task-file read disagree, the task file wins: the population this half
owes is *reaped workers still owed a resume*, and an approval record is one wait among three, not
the entry condition.

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/approved-not-started.py" --tasks-dir "<vault>/<tasks-dir>" --json
```

⚠️ **`--tasks-dir` is required and the path is the vault's own** — resolve it from `vault-cli
config`, never hardcode a directory name: a renamed tasks directory would glob nothing and this
half would report a clean round, the exact silent failure the topicless-goal leg exists to
remove. ⚠️ **An `unknown` reading is not `0`** — an unreadable registry prints `unknown` and
exits non-zero; report it as it comes back and resume nothing, because "no candidates" and "I
could not ask" must not render the same.

## Read the wait, per kind, and act only when it cleared

A candidate whose wait has not cleared is left alone and is not counted as resumed — the point
of this half is that a closed worker is resumed by an event, never by a clock, so an unchanged
wait produces no act on any round.

| Kind | The read | Resume |
|---|---|---|
| (a) approval | `approved_at` is **later than the session's last transcript message** — the timestamp `fleet-sessions.py:last_message_ts()` reads, the one `LAST-ACTIVE` renders, never the file mtime | `/supervisor:open --task` |
| (b) answered card | the session's attention event log `~/.claude/state/attention/<sid>.events.jsonl`, folded as `who-needs-me.py` folds it: an `open` line closed by a later `close` line is an answered card, and `scripts/attention-card-lookup.py` resolves it to its item — **never** `GET /api/1.0/attention`, which returns open items only and so can never show an answered one | **`spawn_agent`, with the answer as the prompt** |
| (d) `blocked_by` | `blocked_by` is **non-empty** AND every name resolves to a task whose status is terminal | `/supervisor:open --task` |

⚠️ **The non-empty guard on kind (d) is load-bearing, not defensive.** "Every name is terminal" is
satisfied **vacuously** by an empty `blocked_by` — the common case — so without the guard the leg
resumes every orphaned row on every round and kinds (a) and (b) become moot. This repo already
names the trap: `scripts/manager-predispatch.py:496` — *"An empty blocked_by is unblocked — and that
is exactly why an EMPTY read is not neutral: it satisfies the ready-to-start clause vacuously"* —
and `blocked_by_verdict` asks whether the set is non-empty before it asks whether it is terminal.
An empty set is **not a wait that cleared**; it is a row with no wait of this kind, and this half
resumes it on no round.

⚠️ **Kind (b)'s "answered" excludes a decline and excludes an empty answer.** A card record
reading `skip` is the operator declining, not a question answered — it is neither a cleared wait
nor an unread one, and resuming on it would hand the worker an "answer" that answers nothing. An
answer that is present but **empty** is the same case: there is no prompt to deliver, and
`spawn_agent` with an empty prompt is precisely the bare resume this section forbids. Both are
counted separately and resume nothing.

⚠️ **Kind (a) is the read most easily written wrong, because the population makes it look
already-true.** `approved-not-started.py` selects rows that **carry `approved_at`**, so inside its
`orphaned` class every candidate has one — a read of "the row carries `approved_at`" is satisfied by
100% of candidates and resumes rows whose approval is months old. The event is not the field's
presence, it is its **ordering**: the approval must be later than the session's last transcript
message, which is what "the approval landed and nothing resumed it" means. A row whose approval
predates its last turn was still running after it was approved, and its wait is something else.

⚠️ **Kind (b) is the one resume that carries a prompt, and it is the only reason this leg holds
`spawn_agent`.** An exited worker still holds its unanswered question: resuming it with an empty
prompt, or with "continue", leaves the question unanswered and re-parks it at the same gate
(`docs/fleet-surface.md` § *A headless worker exits at turn end*). So kind (b) resumes through the
fresh-turn channel and **not** through `/supervisor:open`, which takes a task name and no prompt:

```text
mcp__supervisor__spawn_agent(prompt=<the card's answer, verbatim>, resume=<the session id>,
                             interactive=false, cwd=<explicit>)
```

The answer is the prompt — a resume that restores the process without delivering it produces the
exact loop this half exists to end. ⚠️ **When the card's answer cannot be read, the row is
`unread`, never "no wait"** — the three-valued rule `round-readings.py` applies, and for the same
reason: folding a failed read into a negative resumes a worker whose question nobody answered, or
leaves one waiting on an answer that did land.

Kinds (a) and (d) resume each named task through `/supervisor:open --task "<name>"` — the same act
as § 2, never a fresh session and never a bare `claude`, which brings a session back on a reduced
MCP surface.

## Consumption: these waits never un-clear, so the cap is what bounds the leg

⚠️ **Kinds (b) and (d) have no natural consumption.** An answered card stays answered and a
terminal `blocked_by` stays terminal, so a row resumed on either wait is still eligible on the
next round — and the round after. A worker that exits without clearing its task would be
re-resumed every fleet round forever, with nothing in the output to say so. Kind (a) has the same
shape once `approved_at` is in the past.

So **every resume this half performs goes through § 4's machinery, not around it**:

```bash
python3 "$P/worker-keepalive.py" --vault "<vault>" --task "<name>" --record-resume "<id>"
```

⚠️ **`<id>` is the id the resume actually produced, never the task's stored one.** A kind (b)
resume goes through `spawn_agent(resume=…)`, and whether that continues the recorded session id or
returns a fresh one is a property of the transport, not of this step — so take the id from the
call's own return and record *that*. Recording the stored id instead is the silent failure this
whole paragraph exists to prevent: `--record-resume` refuses an id the liveness instrument does not
read `LIVE`, so a stale id leaves the cap unarmed and the row is re-resumed on every round with a
`crash-loop` that never fires. If the resume returns no id at all, the row is `unread` and nothing
is recorded.

`RESUME_CAP` is 3 and `--record-resume` refuses an id the liveness instrument does not read
`LIVE`, so a resume that never landed does not arm the cap against a retry. Once the cap is spent
the verdict is `crash-loop`: **stop resuming that task and escalate once** with the
`⚠️ WORKER CRASH-LOOP` line — that line is the consumption marker. It is deliberately not a
"consumed" flag on the card or the `blocked_by` set: those are facts about the past, not about
whether the resume worked, and a marker that suppresses the second resume also suppresses the
escalation the third one owes.

⚠️ **This is also what keeps the leg inside the ownership and cap gates § 4 already enforces.**
Resuming a decision-parked row through a bare `/supervisor:open` would skip the `worker--<slug>`
cap the dead-worker path applies.

⚠️ **The three-valued rule covers kinds (a) and (d) too, not only (b).** An unreadable task file,
an `approved_at` that cannot be compared against the transcript timestamp, and a `blocked_by` name
that resolves to no task are each `unread` — never "wait not cleared" and never "cleared". The
count is `decision-unread`; a row counted there is resumed on no round and is named in the
round's report.

## The hand-back

**Three counts, and the third exists so an unread answer is never folded into a not-cleared
wait:** `decision-candidates` is the population this half considered, `decision-resumed` is what it
resumed, and `decision-unread` is the rows whose wait it could not read. A round that saw rows and
resumed none is thereby distinguishable from one that saw none, and both are distinguishable from
one that could not read them. None is a digest key.
