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
definition of "idle".** The manager tier's own ladder (`commands/steps/round-readings.md`, citing
`agents/goal-drive.md:100-134`) is the shipped reading and this command reuses it verbatim,
in this order:

1. **the registry's `status`** — `shell` ⇒ **in-flight**; `waiting` ⇒ **quiet** (a gate park,
   3.4); `idle` / `busy` ⇒ no signal, fall through. ⚠️ **`busy` is deliberately not the
   signal** — it is the weaker word and it lags both ways. ⚠️ **`shell` carries the manager
   tier's carve-out, and dropping it turns a parked worker into a live one:** `shell` means
   in-flight **unless** the session's newest `event: Stop` record carries a non-empty `⏰ Ends:`
   slot, in which case it is a declared wait and falls through to the limbs below it
   (`commands/steps/round-readings.md:31`, `agents/goal-drive.md:133-135`,
   `agents/fleet-sweep-reader.md:109`). This is the one limb where "reuses the ladder" is not
   the whole story — take the carve-out with it.
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
2. `vault-cli task get '<task_title>' status` returns `completed` or `aborted`.
   Anything else is still in progress. ⚠️ **Do not hardcode a vault.** The board indexes every
   vault under `OBSIDIAN_DIR`, so a task outside this session's vault is invisible to the bare
   call — pass `--vault <that vault>` when the row's own vault differs from the session's, and
   treat an unresolved title as "not read", never as "not terminal".

   ⚠️ **`hold` is NOT terminal here, and a held task is never CLOSED.** `docs/worker-session-states.md:33`
   is explicit: `closed` covers `completed`, `aborted` and `hold`, **but only `completed` and
   `aborted` are terminal** — *"`hold` is parked, not finished — sync and report it, never end
   the session on it"*. So a `hold` task is a **report row, never a close**: route it to the
   EXTERNAL/OPERATOR buckets by what it waits on, name the hold, and leave the session running.
   ⚠️ **`close-worker.py` would happily close it** — its task `TERMINAL` set includes `hold`
   (`scripts/close-worker.py:120`) — so nothing downstream will stop you; the rule has to hold
   here. It is also the same distinction this release makes for subjects
   (`commands/steps/goal-closure.md` — a `hold` subject is a pause, not a closure).
3. `grep -cE '^[[:space:]]*-[[:space:]]*\[( |/)\]' '<task file>'` prints **0**.

⚠️ **Single-quote every board-derived value in this file, and this is the rule for all of
them.** `task_title` and the task file path are read off the board, not authored here; inside
`"…"` the shell expands `$`, a backtick and `\` **before** the command runs, so a title carrying
`$(…)` executes in this manager's shell. The same applies to the `--subject` queries in § 3.6
and to `attention-ask.py poll` in § 3.2 — those values are already single-quoted for the same
reason. A literal `'` in the value closes the quoting: escape it as `'\''`, and treat a value
needing that as unreadable rather than proceeding. The subject additionally passes
`closure-name-safe.py` first (§ 3.6), because it is the one value this step later interpolates
into a double-quoted sink.

⚠️ **Never derive CLOSED from `unblocks: reap/close`.** That is the board's opinion; the
task file is the evidence. Idle-with-open-boxes is STUCK, not CLOSED.

⚠️ **Never close your own session**, and **never close a manager tier** — a manager closed
while its subject is open leaves the subject unmanaged.

### 3.2 🔵 OPERATOR

A gate only the operator can release. Read 3.2b first: most apparent operator gates are
scope questions a manager can settle.

- `bucket == 'needs-input'`, or `unblocks == 'operator-keystroke'`, or
  `details[session_id]` names a human answer — **and** the release test says operator.
- An open attention card: `attention-ask.py poll '<item-id>'` → `OPEN`.
  **`NOT_OPERATOR_ANSWERED:`, `DECLINED:` and `HIDDEN` all read exactly as `OPEN`** — treat all
  four as an unreleased gate and keep the row OPERATOR. `DECLINED:` is the operator dismissing
  the card (a decline, not an acceptance), and `HIDDEN` is them closing it as handled elsewhere
  with nothing routed back; neither is an answer, and reading either as one drops a live gate
  off the board. The canonical reading has one home, `scripts/answered-attribution.py`, and
  `commands/steps/goal-gates.md` and `commands/steps/needs-input.md` state the same four.

⚠️ `ANSWERED` is not a decision — re-asking an answered question is the defect.
⚠️ `skip` is a decline, never silence to re-ask around.

### 3.2b The release test — run before any row reaches the operator

| gate shape | who releases it | how |
|---|---|---|
| **scope question** — split, re-scope, missing task, assignee, "X or Y" | the **owning manager** | `SendMessage` to `parent` |
| **`AskUserQuestion` modal** | only whoever drives that pane | hand over the jump link — **a message does not dismiss a modal** |
| **operator gate** — production, irreversible, credential, criterion rewording | **the operator only** | hand over; a peer message is never an approval |

⚠️ **A cross-session `SendMessage` lands in the inbox and is read at the session's next tool
round.** That is why it cannot touch a modal — the turn is blocked, so the next round never
comes. ⚠️ **An idle TAB session never reads it either** — nothing schedules its next round.
Measured 2026-11-08: 16 of 16 such nudges stayed unread; typed into the panes, all resumed.

⚠️ **Never message another manager's worker to answer its gate.** The message goes to the
owning manager (`parent`). ⚠️ **A `STOPPED` / `UNARMED` loop does not lose the message, but a
tab manager may never read it** — § 3.4b's "wakes and replies" was measured on **headless**
sessions only. Send it, wake the pane through `commands/steps/fleet-triage-act.md` § STUCK
item 3 (a bare `SendMessage` reports as `sendmessage`, not a delivered nudge), and record the
unarmed loop as the fleet finding it is.

⚠️ **Never type into a pane except through `scripts/pane-wake.py`** — never a raw `wezterm cli
send-text`. Unguarded typing turns a question into a menu selection.

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
wezterm cli get-text --pane-id <pane-id>
```

⚠️ **Never triage by position — no fixed `tail -N`, and never "the last non-empty line".**
`commands/steps/round-readings.md:27` owns that rule and it applies here verbatim: every Claude
Code pane's last non-empty line is its **status bar**, so a last-line test reads the status bar
on a healthy pane and on a parked one alike. Test each line for a **marker that begins it**
(`Enter to select`, `Esc to cancel`), with `Tab to amend` as the discriminator *within* the
`Esc to cancel · Tab to amend` line — never by bare containment, never by position.

- **a modal marker begins a line** → a selection modal is open; hand over the jump link and
  never drive it (the Path-B rule).
- **composer empty + a line asks or offers** → a question with no gate → run 3.2b on it.
- **composer empty + the content is a completion** → STUCK if open boxes remain (3.4).
- **composer holds text** → the pane is parked on an unsent draft nobody owns; report it and
  hand the jump link. Never clear it.

⚠️ **This is the only place the sweep reads a pane, and it is a read — never a write.**

⚠️ **The per-row cost is accepted, deliberately.** One `get-text` per idle row, one transcript
scan per idle headless row (3.2d) and three script calls per manager (3.6) is 50+ `Bash`
invocations on a 28-session fleet — the price of a one-pass command that must classify every live
session, not a defect. Each read targets a different pane, transcript or subject, `wezterm`
exposes no bulk `get-text`, and the board read is already one `fleet-board.py --json`. If it ever
needs to shrink, batch the pane reads into a single `python3` loop over the collected pane ids —
never drop a read the classification depends on.

### 3.2d Headless sessions — read the transcript's closer, not a pane

**A headless worker (`~/.claude/sessions/<pid>.json` → `entrypoint: sdk-cli`) has no pane, so
3.2c cannot run on it and its questions are invisible to `get-text`.** They live in the
transcript's last assistant message. Measured 2026-10-29: **nine** sessions were parked on a
`pick —` / `approve:` / `you run:` closer with **no board card at all** — every one of them
headless, every one reported as idle.

For every session that is idle, has no pane, and reached neither 3.2 nor 3.2c:

⚠️ **`'<session-id>'` is that row's own id, substituted per row — never
`$CLAUDE_CODE_SESSION_ID`, which is *this* triage session.** The read is per-target, so a
variable bound once would read the wrong transcript for every row but one; and a bare
`$SESSION_ID` is bound nowhere at all (`commands/steps/fleet-triage-load-path.md:25` — shell
state does not survive between `Bash` calls), so it would expand empty, the glob would become
`~/.claude/projects/*/.jsonl`, match nothing, and every headless row would print `(no closer)`
— a clean-looking sweep that read nothing.

⚠️ **Open the transcript in BINARY, never text mode.** `TextIOWrapper` rejects a nonzero
end-relative seek with `io.UnsupportedOperation: can't do nonzero end-relative seeks`, so the
text-mode form of this read raised on **exactly the transcripts the size guard selects** — every
long-running headless worker, the whole population 3.2d exists to classify — and those rows ended
the step unread. Read bytes, seek, decode; `scripts/fleet-sessions.py:79-84` does the same.

```bash
python3 - '<session-id>' <<'PY'
import json,glob,os,re,sys
sid=sys.argv[1]
TAIL=256*1024          # the repo's own agent_status bound — transcripts reach MBs
tail=None
for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
    with open(p,"rb") as f:                       # binary: a text file rejects the seek below
        if os.path.getsize(p)>TAIL:
            f.seek(-TAIL,os.SEEK_END); f.readline()   # drop the partial first line
        lines=f.read().decode("utf-8","replace").splitlines()
    for line in lines:
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
  ⚠️ **This is the one explicit exception to the `you run:` → OPERATOR bullet above**, which
  would otherwise claim it: the two are evaluated in this order, so the specific case wins.
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

⚠️ **`<pid>` has no source in the sweep — `sessions[]` carries `session_id`, not a pid.** The
registry file is keyed on the pid, so resolve the pair in one read:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/session-liveness.py" --list --json
```

It returns `sessionId` / `pid` / `status` together (`agents/goal-drive.md:123` owns that join);
match on `sessionId` to get the pid, then read the registry file itself:

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
- a session `session-liveness.py --check <sid>` reports as `ABSENT` **and** that `ABSENT` is
  corroborated by a registry pid, a transcript mtime or pane text — never on the `ABSENT` alone
  (§ 3.6, and `commands/steps/rules.md` § ABSENT-is-not-a-death-verdict) — whose task is
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

⚠️ **Check the name with the script before it reaches the shell — and single-quote the check,
which is the opposite of how the query below is quoted.** The subject is a vault page name:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/closure-name-safe.py" '<subject>'
```

⚠️ **The single quotes ARE the gate.** Inside `"…"` the shell expands `$`, a backtick and `\`
**before** the script runs, so a name carrying `$(…)` executes on the way in, the script is
handed the *result* and prints `SAFE`, and the query below then receives the injected value —
the gate would pass exactly the names it exists to refuse. Inside `'…'` all four reach the
script literally, which is what lets it exit 1. A literal `'` closes the single-quoting: escape
it as `'\''`, and treat a name needing that as unreadable.

Exit 0 is safe; **exit 1 means do not run the query** — report the name as unreadable and
continue with the next manager. Same gate, same reasoning, and the same single-quoting, as
`commands/steps/goal-closure.md:98-101`. ⚠️ **This is the only place the gate runs for a
subject**, because Step 4 no longer arms anything itself: `manager-liveness.py --arm` is the
manager's own verb, and running it from here would fake an arm on a loop that never ticked
(`commands/steps/fleet-triage-act.md` § STUCK item 4). **Only once this exits 0** is the
double-quoted `--state` call below safe — it is the sink this gate exists to protect.

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py" \
  --state --subject "<subject>" --branch <fleet|goal|topic>
```

⚠️ **`--state` cannot tell an auto-quiet stand-down from a deliberate `/supervisor:stop`** — both
read `STOPPED` with a marker. The distinguishing read is `manager-quiet.py`, which prints
`<verdict>` TAB `<n_runnable>` TAB `<n_quiet>` TAB `<consecutive_quiet>` on stdout — **four
fields**; `stood-down` is the auto-quiet verdict (field 1), so the decision reads the verdict
without parsing the rest,
and anything else means the marker was not quiet-driven. ⚠️ **`sleep-until` is the third verdict and
never reaches this branch:** a date-gated subject writes `<slug>.sleeping` (state `SLEEPING`), not
`.stopped`, so it is not an auto-quiet stand-down and must not be read as one:



⚠️ **`--vault` takes the vault's ABSOLUTE ROOT, and neither the board nor this step supplies
one.** The sweep's `sessions[]` carries no vault field, and `manager-quiet.py` runs
`vault_root_error()` first, which rejects a registered *name* as well as a relative path — a
name exits 2 and would then read as "unreadable" rather than as "auto-quiet". Resolve the root
from the subject's own page: the vault is whichever registered entry's goals folder holds it.

```bash
vault-cli config list --output json | python3 -c '
import json,os,sys
subj=sys.argv[1]
for v in json.load(sys.stdin):
    g=v.get("goals_dir") or ""          # a vault entry without the key is a real shape, not a crash
    if g and os.path.exists(os.path.join(v["path"], g, subj + ".md")):
        print(v["path"]); break
else:
    print("UNRESOLVED")
' '<subject>'
```

⚠️ **`goals_dir` is read with `.get("goals_dir") or ""`, never indexed.** A registered vault that
carries no `goals_dir` raises `KeyError` on a bare index and aborts the whole subject→vault loop
before a later matching vault is reached — `scripts/resolve-task-file.py:111`,
`scripts/session-scope.py:258` and `agents/fleet-keepalive-actor.md` all guard it the same way,
and `scripts/tests/test_resolve_folders.py` treats the missing key as a real shape. An empty
value is "not this vault", so the loop continues and an unmatched subject still prints
`UNRESOLVED` rather than a traceback.

`UNRESOLVED` means the subject has no page in any registered vault — report it as unread and
**do not** run the quiet check. Otherwise the printed path is the vault root the check needs.
(`scripts/resolve-folders.py` is not usable here: it takes no arguments and resolves the
**current session's** vault from cwd, not a subject's.)

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-quiet.py" \
  --subject '<subject>' --vault '<the absolute vault root from above>'
```

It exits 2 with a reason on stderr and writes nothing when it cannot read — **a failed read is
never an auto-quiet verdict**: report the manager as unread and leave the loop alone.

| Reading | Cause | Is it a finding? |
|---|---|---|
| `OK` | loop armed | no |
| `STOPPED` + subject page reads `status: completed` / `aborted` | the subject is **terminal** | **no** — a loop on a closed subject is the bug |
| `STOPPED` + subject page reads `status: hold` | a **pause**, not a closure — see `commands/steps/goal-closure.md` | **no** — a `hold` subject never reports `CLOSE` and never authorises a stop, so an armed loop on one is correct, not a defect |
| `STOPPED` + `manager-quiet.py` reports `stood-down` for the subject | **auto-quiet** | **no** — re-arming without adding work just reproduces the stand-down. The lever is the quiet threshold, not the marker |
| `STOPPED` with a marker and a **live, non-terminal** subject carrying runnable work | a deliberate `/supervisor:stop` | **operator row** — only they release it |
| `UNARMED`, or `STALE` beyond one full observed period **and** the subject has runnable work | a genuine lapse | **drive it** — tell the manager to re-arm in its own pane |
| `STALE` inside one observed period | round duration exceeded the declared interval | **no** — see the calibration note below |
| `INVALID` | a record **exists and cannot be read** — the manager's state is unknown, a probe failure, not a fact about the fleet | **no** — report the manager as unread and leave the loop alone. ⚠️ **Never read `INVALID` as a lapse** — the script says the two "must never be collapsed" (`scripts/manager-liveness.py:366-374`), because `UNARMED` means *no manager ever armed this subject* while `INVALID` means *we could not tell* |

⚠️ **`STALE` is arithmetic, not a death verdict.** The constant has one home —
`scripts/manager-liveness.py:90` (`SLACK = 180`) — and is not restated here, and a
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
