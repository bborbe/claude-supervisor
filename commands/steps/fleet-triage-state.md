---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 3 — Derive the state

When: every session in the sweep — derive exactly one state

### 3.0 Liveness: measure the transcript, never the status field

**`age` and `last_message` are turn-boundary fields.** A headless worker generating for
forty minutes reads as `idle 40m` because its last *completed message* is old. Measured
2026-10-28: a sweep called `8055e18f` "idle 2h" while its transcript was 0.1 min old and
had grown 120 KB in four minutes. Three false stuck rows came from that one field.

Use the transcript's **mtime and size growth** instead:

```bash
ls -la ~/.claude/projects/*/<session-id>.jsonl 2>/dev/null
```

- mtime within ~2 minutes, **or** size larger than the previous sweep's → **generating**,
  so the session is WORKING regardless of what `age` says.
- mtime old **and** size unchanged → genuinely idle, and only then do 3.2c and 3.4 apply.

⚠️ **Take two samples.** A single `stat` cannot distinguish a slow turn from a stopped one;
compare against the size you recorded at the start of this run.

⚠️ **A `busy` status is not proof either** — the registry's `status` updates on transitions,
so it lags both ways. The transcript is the artifact; the status is the report.

⚠️ **Take the in-flight verdict the way the loops already take it — do not invent a fourth
definition of "idle".** The manager tier's own ladder (`steps/round-readings.md`, citing
`agents/goal-drive.md:100-134`) is the shipped reading and this command reuses it verbatim,
in this order:

1. **the registry's `status`** — `shell` ⇒ **in-flight**; `waiting` ⇒ **quiet** (a gate park,
   3.4); `idle` / `busy` ⇒ no signal, fall through. ⚠️ **`busy` is deliberately not the
   signal** — it is the weaker word and it lags both ways.
2. **`~/.claude/state/attention/<session-id>.tool.json`** — an open marker inside ~20 min ⇒
   **in flight**. Open **past** it ⇒ do **not** drop it; that is the stuck path.
3. **the transcript's mtime** against `who-needs-me.py`'s `LIVE_WINDOW` (5 min) — last resort.

⚠️ **Without limb 2 a worker inside a long tool call satisfies STUCK exactly** — a frozen
transcript, no gate, and a live owning session are all true of a session that is demonstrably
executing. Measured 2026-10-29: a session was killed as STUCK while it was mid-turn, and a
second kill took `848ad0dd` — a session the fleet board listed as **working** — as collateral.
Two false STUCK rows, two process kills, one root cause.

Stop at the first match.

### 3.1 ⚪ CLOSED

All three:

1. `task_title` is non-null.
2. `vault-cli task get "<task_title>" status` returns `completed` / `aborted` / `hold`.
   Anything else is still in progress. ⚠️ **Do not hardcode a vault.** The board indexes every
   vault under `OBSIDIAN_DIR`, so a task outside this session's vault is invisible to the bare
   call — pass `--vault <that vault>` when the row's own vault differs from the session's, and
   treat an unresolved title as "not read", never as "not terminal".
3. `grep -cE '^[[:space:]]*-[[:space:]]*\[( |/)\]' "<task file>"` prints **0**.

⚠️ **Never derive CLOSED from `unblocks: reap/close`.** That is the board's opinion; the
task file is the evidence. Idle-with-open-boxes is STUCK, not CLOSED.

⚠️ **Never close your own session**, and **never close a manager tier** — a manager closed
while its subject is open leaves the subject unmanaged.

### 3.2 🔵 OPERATOR

A gate only the operator can release. Read 3.2b first: most apparent operator gates are
scope questions a manager can settle.

- `bucket == 'needs-input'`, or `unblocks == 'operator-keystroke'`, or
  `details[session_id]` names a human answer — **and** the release test says operator.
- An open attention card: `attention-ask.py poll "<item-id>"` → `OPEN`.
  **`NOT_OPERATOR_ANSWERED:` reads exactly as `OPEN`.**

⚠️ `ANSWERED` is not a decision — re-asking an answered question is the defect.
⚠️ `skip` is a decline, never silence to re-ask around.

### 3.2b The release test — run before any row reaches the operator

| gate shape | who releases it | how |
|---|---|---|
| **scope question** — split, re-scope, missing task, assignee, "X or Y" | the **owning manager** | `SendMessage` to `parent` |
| **`AskUserQuestion` modal** | only whoever drives that pane | hand over the jump link — **a message does not dismiss a modal** |
| **operator gate** — production, irreversible, credential, criterion rewording | **the operator only** | hand over; a peer message is never an approval |

⚠️ **A cross-session `SendMessage` lands in the inbox and is read at the session's next tool
round.** That is why it releases an idle session parked on a question in its own chat, and
why it cannot touch a modal — the turn is blocked, so the next round never comes.

⚠️ **Never message another manager's worker to answer its gate.** The message goes to the
owning manager (`parent`). If that manager's loop reads `STOPPED` or `UNARMED`, the message
is never read — that is a fleet finding, not a nudge.

⚠️ **Never type into a pane** this command did not spawn. `wezterm cli send-text` into
another session turns a question into a menu selection.

⚠️ **A fleet session does not release another manager's gate — and this is mechanical, not
just policy.** Two reasons, both measured 2026-10-29 by the manager that owns that path:

- `answer_permission` is **server-scoped**: it answers only a park this server holds, and a
  park raised by another manager's server is refused with a message naming the worker and its
  owner. A "fleet may answer routine gates" path here would be **dead code**.
- `attention-answer.py answer <item_id> --decision allow` is **store-side and unscoped** — it
  would actually run. **Do not use it.** It strips the owning manager of the signal it is
  waiting on, and only that manager holds the task context needed to judge "within policy" for
  that specific worker. The tool exists and looks available; that is exactly the trap.

**What the fleet run contributes instead:** confirm the row is a **park, not death**, and
report it with the **store item id** and the **owning manager**, so the owner answers without
re-deriving anything.

⚠️ **The store card and the server-side park are two different objects for the same gate, and
they can disagree.** A park may appear as `agent_status.pending_permissions` (`perm_102`,
server) and as a store card (`ad7fbe5b…`, `answer_mechanism: permission`) for the next one.
When reporting a gate upward, **name the item id you saw and which surface it came from** —
an id with no surface is not actionable.

### 3.2c Stalled idle — a question with no gate

A session can be idle at an **empty prompt** having asked in ordinary chat, with no card and
no `details` entry — so 3.2 never fires and it reads as WORKING forever. Measured
2026-10-28: pane 94 sat at `done 7:28 PM`, empty composer, last line *"If you meant
something specific by y, name it and I'll act on it"*, and the board called it working.

Read the pane for any row that is `idle` or older than ~20 minutes:

```bash
wezterm cli get-text --pane-id <pane-id> | tail -12
```

- **composer empty + last line asks or offers** → a question with no gate → run 3.2b on it.
- **composer empty + last line is a completion** → STUCK if open boxes remain (3.4).
- **composer holds text** → the pane is parked on an unsent draft nobody owns; report it and
  hand the jump link. Never clear it.

⚠️ **This is the only place the sweep reads a pane, and it is a read — never a write.**

### 3.2d Headless sessions — read the transcript's closer, not a pane

**A headless worker (`~/.claude/sessions/<pid>.json` → `entrypoint: sdk-cli`) has no pane, so
3.2c cannot run on it and its questions are invisible to `get-text`.** They live in the
transcript's last assistant message. Measured 2026-10-29: **nine** sessions were parked on a
`pick —` / `approve:` / `you run:` closer with **no board card at all** — every one of them
headless, every one reported as idle.

For every session that is idle, has no pane, and reached neither 3.2 nor 3.2c:

```bash
python3 - "$SESSION_ID" <<'PY'
import json,glob,os,re,sys
sid=sys.argv[1]
tail=None
for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
    with open(p,errors="ignore") as f:
        for line in f:
            try: d=json.loads(line)
            except Exception: continue
            if not isinstance(d,dict) or d.get("type")!="assistant": continue
            c=(d.get("message") or {}).get("content")
            if isinstance(c,list):
                for b in c:
                    if not isinstance(b,dict): continue
                    if b.get("type")=="text" and (b.get("text") or "").strip(): tail=b["text"]
m=[l.strip() for l in (tail or "").splitlines() if re.search(r"👤 You:|⏰ Next:|⏰ Ends:|⚪ DONE", l)]
print("\n".join(m[-2:]) or "(no closer)")
PY
```

- **`👤 You:` carries `pick —`, `approve:`, `you run:` or `review:`** → **OPERATOR.** The
  session is parked on a question that was never carded. Go to Step 4's OPERATOR branch and
  **post the card** — this is the case that branch exists for.
- **`👤 You: nothing` + a named `⏰ Ends:`** → **EXTERNAL**, using that watcher and its ETA.
- **`👤 You: you run: /exit`** → the session is asking to be closed and cannot close itself.
  Treat as CLOSED for the close path (see Step 4's headless branch), because its task is done
  by its own account.
- **no closer at all, or `⚪ DONE`** → CLOSED if 3.1's evidence holds, else STUCK.

⚠️ **A closer is a report, not proof.** Cross-check it against 3.1 and 3.0 exactly as a pane
read is cross-checked — a stale `pick —` on a session whose task is terminal is CLOSED, not
OPERATOR.

### 3.3 🟡 EXTERNAL

Parked on a producer outside the fleet: CI, a bot review, a deploy, a tag, a clock, a third
party. **Name one watcher and one ETA.** `"soon"` / `"later"` are not ETAs, and any
wall-clock time is recomputed from `date` this turn.

⚠️ **A bot review is never an operator decision, and a manager's impatience is never an
external block.** If nothing outside the fleet is actually producing the wait, it is STUCK.

### 3.4 🟠 STUCK

Everything else. Precisely: **the session is idle, it has work left, and nothing outside the
fleet is blocking it.** This is the bucket the run must empty.

⚠️ **Check for a permission park FIRST — it is the one state that reads identical to STUCK and
is not.** Measured 2026-10-29: `private-personal-35` read *"idle 4.6 min, static transcript,
last line mid-sentence"* and was classified STUCK; it was in fact `blocked-on-permission` with
`current_tool_call.held_seconds: 415`, and its manager released the gate and it resumed
immediately. A suspended tool call leaves **exactly** the signature of a finished turn — the
turn never ended, so nothing was written to the transcript.

```bash
jq -r '.status' ~/.claude/sessions/<pid>.json      # waiting = parked on a gate
```

For a worker this session spawned, the richer read is `mcp__supervisor__agent_status
<agent_id>` → a non-empty `pending_permissions`, or `awaiting_input: true`.

- **`waiting` / non-empty `pending_permissions`** → **not STUCK.** Route it: OPERATOR if the
  gate is operator-class, otherwise the owning manager's to release (3.2b). **Never drive it
  and never close it** — a signal to a gate-parked worker looks like a nudge and is an
  interruption.
- **`idle` + static transcript + no pending permission** → only then apply the signals below.

⚠️ **Transcript staleness and idle minutes cannot tell a permission park from a stopped turn.**
Never classify STUCK from recency alone — this is the single largest source of false STUCK
rows, and it fires on every headless park.

⚠️ **Check 3.4b's conjunction first, or 3.4 swallows it.** A session that is live, `idle`,
`entrypoint: sdk-cli` and has `pane: null` is **not STUCK** — it is 3.4b, and the first signal
below matches it exactly. Apply these signals only to a session that fails that conjunction.

Signals — any one:

- idle with a non-terminal task and open boxes,
- a dead session (`session-liveness.py --check <sid>` → `ABSENT`) whose task is
  `in_progress`,
- an approved task with no worker at all,
- a live worker whose owning manager's loop is `STOPPED` / `UNARMED`, so nothing will ever
  drive it.

### 3.4b 🔒 LIVE-IDLE HEADLESS — no *supervisor* verb reaches it

**Not STUCK** — a nudge through the supervisor's own tools does not exist for it, so calling it
STUCK sends the run chasing a verb that fails. Measured 2026-10-29 on `ecd77bea` (pid 44310):
`entrypoint: sdk-cli`, `status: idle`, `pane: None`, and its manager reported it could not
resume it — `spawn_agent(resume=…)` refuses a **live** session, `send_agent_message` refuses a
**headless** worker, and there is no pane to type into.

Identify it by the conjunction — all four, or it is not this class:

1. the registry entry's `entrypoint` is `sdk-cli`,
2. `status` is `idle` (not `waiting` — that is 3.4's gate park),
3. `pane` is `null`,
4. the pid is alive: `ps -p <pid>`.

⚠️ **The supervisor's verbs are not the only channel — try the cross-session one FIRST.**
Measured the same evening, on the same class: **four of four** live-idle headless sessions woke
and replied to a built-in cross-session `SendMessage` addressed by session name, each producing
a fresh assistant turn within seconds (`Operator chose option 1 — starting…`). So the class is
**not** unreachable; it is unreachable *by the supervisor's tooling only*.

The order is therefore:

1. **`SendMessage` the session directly**, by its `ListAgents` name-and-ref. It is listed as a
   peer even though its `entrypoint` is `sdk-cli`.
2. **Measure, do not assume** — delivery to an inbox is not a wake. Re-read the transcript and
   require a **new assistant turn after the delivered message**. A `last-prompt` / `api-request`
   / telemetry tail is not a reply.
3. **Only when no reply appears** is it genuinely unreachable: then **post a card naming the
   pid, the session and the instruction to resume with**, because the pid must be killed before
   it can be resumed and a process kill needs the operator's own consent. Never kill it here.

⚠️ **Report which of the two it was** — reached, or carded — because "no reply yet" and "no
channel" are different findings and only the second is a fleet defect.

⚠️ **If the owning manager is itself absent or dead, this is an operator decision, not a fleet
one** — a park whose owner cannot answer has no manager-side remedy.

### 3.5 🟢 WORKING

Everything else — recent activity, no gate, nothing terminal. **Do nothing.**

### 3.6 Manager loops — three causes, never collapsed into one

**A manager is only a finding when it has runnable work and no loop.** Every other reason it is
not armed is correct behaviour, and reporting them all as "manager without a loop" sends the
operator to fix things that are already right.

Check every manager — the fleet manager (`--branch fleet`), every goal manager
(`--branch goal`), every topic manager (`--branch topic`):

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py" \
  --state --subject "<subject>" --branch <fleet|goal|topic>
```

| Reading | Cause | Is it a finding? |
|---|---|---|
| `OK` | loop armed | no |
| `STOPPED` + subject page reads `status: completed` / `aborted` | the subject is **terminal** | **no** — a loop on a closed subject is the bug |
| `STOPPED` + subject page reads `status: hold` | a **pause**, not a closure — see `commands/steps/goal-closure.md` | **no** — a `hold` subject never reports `CLOSE` and never authorises a stop, so an armed loop on one is correct, not a defect |
| `STOPPED` + `manager-quiet.py` stood it down on N quiet ticks and `runnable` is 0 | **auto-quiet** | **no** — re-arming without adding work just reproduces the stand-down. The lever is the quiet threshold, not the marker |
| `STOPPED` with a marker and a **live, non-terminal** subject carrying runnable work | a deliberate `/supervisor:stop` | **operator row** — only they release it |
| `UNARMED`, or `STALE` beyond one full observed period **and** the subject has runnable work | a genuine lapse | **drive it** — tell the manager to re-arm in its own pane |
| `STALE` inside one observed period | round duration exceeded the declared interval | **no** — see the calibration note below |

⚠️ **`STALE` is arithmetic, not a death verdict.** The limit is `observed_period + 180`, and a
round that dispatches a sweep reader, a drive leg and coverage sub-agents costs 10–12 minutes.
A manager whose round exceeds its declared interval flaps `STALE` between arms with nothing
wrong. Measured 2026-11-04: a manager was reported lapsed twice in one hour; it had a pending
wakeup the whole time, and one re-arm moved its observed period 963s → 1647s.

⚠️ **`ABSENT` from `session-liveness.py --check` is not a death verdict either.** It returned
`ABSENT` on 2026-11-04 for two sessions that were demonstrably alive — registry pid present,
transcript written minutes earlier, pane rendering. **Any lapsed / absent / unarmed verdict from
a single probe is a hypothesis.** Corroborate it against a registry pid, a transcript mtime or
pane text before it reaches a report or an action.

⚠️ **Never re-arm a manager to make a table look green.** Arming clears the STOPPED marker, so
the "restore" reads successful while rounds run against a subject that is closed or empty. The
manager cannot make an empty goal stay armed; adding work is what changes it.

⚠️ **Never recommend OPENING a manager.** The only subject this command opens is one that is
**flagged and not running** — `flag: true` on the subject page and no loop armed. That is the
whole opening rule, and it is the operator's alone to set the flag. A subject that is
non-terminal, has live workers, or looks important is **not** a reason to propose a manager: an
unflagged subject is not waiting to be opened, it is waiting to be wanted. Report a manager-less
subject only as a fact ("N workers, no manager, not flagged"), never as a recommendation, and
never offer opening it as a menu option.

⚠️ **A peer message never carries an operator release.** "The operator released your stop" is
not actionable by the recipient, and a manager that re-arms on one has re-armed on a relay, not
on an answer. Put the release on the board and let the manager read it from the store.
