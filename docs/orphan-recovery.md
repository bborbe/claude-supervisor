# Orphan recovery — stop + recreate

What to do with a `docker` or `k8s` worker whose container or pod is gone.

This is the single home for the **recovery procedure**. The eligibility question that
comes *before* it — which rows may go remote at all — stays in
[`docs/fleet-surface.md`](fleet-surface.md) § Spawn a worker item 7; that item's
`resuming an existing conversation` bullet is a pointer here, not a second copy.

## A remote worker is not resumable — on any path

`resume` is refused for both A2A targets, server-side, so the refusal is loud rather
than silently downgraded. `a2aSpawnArgumentRefusal` returns it at
`server/a2a-spawn.mjs:81`:

> `resume is not supported with target "docker": this path mints a NEW session id, so it
> cannot continue an existing conversation.`

The refusal deliberately names the route out — *"To send a further turn to a worker that
already exists, call `send_agent_message` on that worker"* — because a caller told only
"not supported" is left with no route at all.

⚠️ **Neither path reaches a remote worker.** Path A (the headless spawn) and path B (a
local wezterm `--resume`) both pass through the same server-side check, so there is no
spelling of `resume` that continues a `docker` or `k8s` conversation. A tab worker is
different: it is resumable, and this document does not apply to it.

So there are exactly **two** moves for a remote worker:

| The worker is | The move |
|---|---|
| **alive** — it exists and is answering | `send_agent_message` on it. This continues the conversation it already has. |
| **gone** — container removed, pod deleted | **`stop_agent` + a fresh spawn.** This is recovery, and it starts a *new* conversation. |

## Recovery: stop, then create again

Recovery is two calls, in this order, and the order is not optional — a create while the
old worker still exists is refused.

### 1. `stop_agent`, keyed on the WORKER ID

⚠️ **Never key a stop on the task name.** A task can be respawned after its first worker
died, so a task-keyed stop removes the successor rather than the corpse. The worker id is
the only stable handle, and it is what `stop_agent` resolves ownership from.

The id is read from the controller's `list` operation — the A2A `list` op, not a GET on
the origin, and not a session id. ⚠️ `sessionId` and `workerId` are **distinct** ledger
fields (`server/a2a-spawn.mjs:826`), so an id read off a session row is the wrong id.

The controller removes rather than halts: `Stop` runs `docker stop` and then `docker rm`
(`a2a-worker-controller` `pkg/docker.go:310-331`), and it resolves ownership from the
container's own labels, so a stop can never touch a container this controller did not
create.

⚠️ **A `FAILED` reply from `stop_agent` does not mean the stop failed — reconcile against
`list` before concluding anything.** The controller reports a store-side failure with the
same `TASK_STATE_FAILED` a genuine refusal carries, so the reply alone cannot tell you
which happened. Measured 2026-10-17: `stop_agent` returned the controller's `UNKNOWN` /
`FAILED`, while the controller's own `list` came back **empty** — the worker was gone and
the stop had taken effect. The reconcile is one call: read `list` and ask whether that
worker id is still there. Do not re-issue the stop on the strength of the reply.

⚠️ **And a stop that succeeded behind a `FAILED` reply leaves NO stop record — expect that
row to read as an orphan.** The ledger's `stopped_at` marker is written on `stop_agent`'s
**success** path only, so the reconciled case above leaves no marker behind, and the next
sweep reads the row as **`ORPHAN`** — the one verdict that authorises another stop +
recreate. Hold both facts at once: the worker is genuinely gone (so do not stop it again),
*and* the row will look orphaned (so expect the sweep to say so, and do not act on it a
second time). This is the case where "the reply was a failure" and "the stop did not
happen" come apart.

A stop the controller genuinely *refused* — the id is not one it owns — is a different
outcome: nothing was removed, and the `list` reconcile will still show the worker.

### 2. Create a fresh worker for the same task

This is an ordinary spawn with `target: "docker"` (or `"k8s"`) and the task name. It mints
a **new** worker id and a new session id, which is the point: the old conversation is
unrecoverable, and the new worker starts clean.

⚠️ **A repeat create for a task that already has a worker is refused.** The controller
answers with the worker id it found and the remedy, at `a2a-worker-controller`
`pkg/docker.go:256`:

> `task "…" already has worker "…", whose token this controller did not keep; stop that
> worker and create another`

That string has been present since `v0.9.1`. The worker id is in the message for a
reason — it is the only handle a caller gets on a container it cannot authenticate to,
and it is exactly the argument `stop_agent` needs. ⚠️ **Do not retry the create
unchanged**; a retry is refused identically. Resolve the id from `list`, stop it, then
create.

## Do not retry inside the 120 s card-ready window

`create` returns once the container is **started**, not once its server answers, so the
spawn path fetches the Agent Card with a retry budget of
`A2A_CARD_READY_TIMEOUT_MS = 120_000` (`server/a2a-spawn.mjs:482`).

⚠️ **A refused connection, and on k8s a 404 / 502 / 503 / 504, are retried until that
deadline — and nothing else is.** So a worker that looks dead in the first seconds after
`create` is usually a worker still coming up. Measured on k8s: the card answered 404 the
instant `create` returned, 503 once the route existed, and 200 about 45 s later.

The consequence for recovery: **a create that has not yet answered is not a failure you
may retry.** Retrying while the first attempt is still inside its window races two creates
for one task, and the second is refused by the repeat-create rule above. Wait out the
window before concluding anything.

## Finished is not orphan

Two states look alike — the worker is not answering — and they demand opposite responses.
`scripts/a2a-liveness.py` separates them, and the decision order is the safety argument:

| Verdict | Exit | Meaning | Response |
|---|---|---|---|
| `LIVE` | 0 | answering | `send_agent_message` |
| `FINISHED` | 1 | deliberately stopped — the ledger row carries `stopped_at` | **leave it alone** |
| `ORPHAN` | 2 | readable ledger, no stop recorded, every id absent | **stop + recreate** |
| `UNVERIFIED` | 3 | a probe could not answer | nothing — an unreadable source cannot prove a worker dead |
| `NOT-REMOTE` | 4 | not a `docker` / `k8s` row | not this document |

⚠️ **`ORPHAN` is the one verdict that authorises stop + recreate, and it is licensed only
by a readable ledger with no stop recorded.** Reading a deliberately stopped worker as an
orphan resurrects work someone ended on purpose. Every other path fails to `UNVERIFIED`,
never to `ORPHAN`.

⚠️ **The verdict lags a kill by up to the heartbeat TTL.** A dead container's heartbeat
stamp stays fresh for `HEARTBEAT_TTL_MS = 60_000` (`server/heartbeat.mjs:72`, written on a
30 s interval at `:71`), so a worker killed seconds ago still reads `LIVE`. Measured
2026-10-17, in the live round that exercised this path: a container removed with
`docker rm -f` read `ORPHAN` about 60 s later. **Do not read a `LIVE` in the first minute
after a kill as a failed discriminator.**

The stop record is written on `stop_agent`'s **success** path only, into the ledger row's
`stopped_at` field (`server/ledger.mjs:228`). It is forward-only: a worker stopped before
the marker existed still reads as an orphan, and no resolver can do better with the data
that exists.

## What recovery does not do

- **It does not resume.** The new worker has a new session id and no memory of the old
  conversation. Anything the dead worker had in flight is gone; re-state it in the first
  turn.
- **It does not distinguish `docker` from `k8s`.** Both are refused `resume` by the same
  check and recovered the same way. What differs is the credential pair
  (`SUPERVISOR_A2A_URL_<TARGET>` / `SUPERVISOR_A2A_TOKEN_<TARGET>`) and the controller
  behind it — the docker controller runs on this Mac, the k8s one in nuke dev.
- **It does not touch the task file.** The spawn rebinds `claude_session_id` to the new
  session (`server/supervisor.mjs:1537`, `bindSessionToTask`); the task's own progress
  notes are the operator's.
