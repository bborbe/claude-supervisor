---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 4 — Act

When: every row whose state is not WORKING, unless `--dry-run`

`--dry-run` → classify, print, act on nothing.

### CLOSED → close

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/close-worker.py" <session-id>
```

**One close path for both tiers.** `close-worker.py` kills the pane of a tab session and sends
SIGTERM to the registry pid of a headless one, waiting `EXIT_DEADLINE` for the pid and its
registry entry to disappear before printing `✅ closed pane <N> · pid <pid> dead · registry entry
removed` for a tab session, and `✅ closed headless session (sdk-cli) · pid <pid> dead · registry entry
removed` for a headless one (`commands/close.md:102-105` documents both shapes). Every guard this runbook used to apply by hand is
inside it — `busy-target`, `held-session`, `dirty-worktree`, `gate-held`, `task-unresolved` and
`task-not-terminal` — so the "is it finished, is it idle, is it gated" evidence 3.1 and 3.0
already gathered is re-checked against the same sources rather than trusted from this table.

**The refusal token is the first line of stdout.** Branch on it:

| Token | Then |
|---|---|
| `task-not-terminal` | re-check 3.1; the file and the script disagreed |
| `gate-held` | it is OPERATOR, not CLOSED — hand over the jump link |
| `busy-target` | it is WORKING — skip |
| `held-session` | operator hold; report and skip |
| `dirty-worktree` | report; the operator chooses commit / stash / another target |
| `headless-target` | **retired** — `close-worker.py` now closes a headless worker itself. A token means the deployed plugin predates the change; update it, never fall back to `kill` |
| `task-unresolved` | report and skip |
| `unknown-session-id` / `ambiguous-pid` | stop that row, continue |

⚠️ **A refusal is terminal for that row — never fall back to `wezterm cli kill-pane`.** That
is the same act with none of the guards.

⚠️ **`headless-target` no longer exists.** A `sdk-cli` entrypoint is a delivery detail, not a
refusal — the close is a pane kill for a tab session and a SIGTERM for a headless one. If you
still see that token, the deployed plugin predates this change; re-run
`claude plugin update supervisor@claude-supervisor` rather than falling back to `kill`.

⚠️ **Never signal a pid by hand here.** The hand-rolled `kill` / `kill -9` block this branch
used to carry had none of the guards — it could not tell a finished worker from a parked one,
and a headless worker that ended its turn is **parked**, waiting for a continuation. A refusal
from `close-worker.py` is terminal for that row: report the token and move on.

⚠️ **Report every close with its session label and the token the script printed.** A silent
close is indistinguishable from a worker that crashed.

### OPERATOR → one row each, and a card for **every** one

Build the Step 5 table. **Every OPERATOR row must carry an open card when the run ends.**
Post no new card for a gate that already has one — a second post asks the same question twice
— but "already has one" must be **proven**, not assumed.

**Prove it mechanically, for every row, before posting.** A session that already holds an open
gate leaves a record beside its id:

```bash
ls ~/.claude/state/attention/<session-id>*.open.json 2>/dev/null
```

- **A file exists** → read its `item_id` and poll it. `OPEN` or `ANSWERED` → **do not post**;
  the session already has a card and a second one asks the operator the same question twice.
  Reuse that `item_id` in the report.
- **No file** → post with the command below.

⚠️ **This check is not optional and not inferable.** Measured 2026-11-04: a run posted fourteen
triage cards from a sub-agent's pane reads without checking, and produced three defects — a
duplicate card on a session that already had one, a card on a gate whose own card the worker
was already parked on (so the worker raced the operator's answer and certified a task 14
seconds before a "do not certify yet" landed), and a card attributed to the wrong session
entirely, whose answer the operator then gave against a question that was never that worker's.
Every one of the three is caught by the `ls` above.

⚠️ **Attribute the pane to the session, never the reverse.** A pane read must be tied to the
session by a field on that session's own record — not by a title match and not by a sub-agent's
judgement about which pane "looks like" the row. A mis-attributed read produces a confident,
wrong card, and the operator has no way to see the error from the board.

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/attention-ask.py" post \
  --producer-kind manager \
  --liveness-ref owner:<session-id> \
  --dedup-key 'triage:<session-id>:<question-slug>' \
  --payload '<the session, its task, and the question in its own words>' \
  --option '<your recommended answer>' --option '<the alternative>' \
  --recommend '<the same string as your recommended --option>'
```

⚠️ **`--liveness-ref owner:<session-id>` is not optional, and omitting it reintroduces the defect this whole card path exists to avoid.** `attention-ask.py post` defaults the ref to `owner:<producer-id>` — **this** manager — and `attention-controller`'s `pkg/provenance.go` `sessionIDFromItem` resolves the jump from `liveness_ref` **before** `producer_id`. So a card about another session, posted without the ref, sends the operator straight back to the fleet manager's own pane, where there is nothing to answer. That is the complaint the operator raised three times (2026-09-30 ×2, 2026-11-04) and the one `commands/steps/goal-gates.md` and `commands/steps/needs-input.md` fix in this same release; this tier's post path is a third caller and must pass it too. `owner:` is the right model precisely because it probes nothing (`OwnerLivenessModel` returns live unconditionally), so the card outlives the target's exit and is bounded only by `expires_at` — and every OPERATOR row is a live sweep entry by construction.

⚠️ **Single-quote every value that carries pane text, and escape a literal `'` as `'\''`.**
This is the repo's convention for card values assembled from a source you did not author
(`commands/steps/covered-subject-invariant.md` names the same rule). A question read out of
another session's pane can contain `"`, `` ` ``, `$(...)` or `\`; inside **double** quotes those
close the quoting or are command-substituted, and a crafted question then executes in **this**
manager's shell. Inside single quotes they are inert. Keep `--option` / `--recommend` in your own
words — they are your judgement, never a quote from the session.

⚠️ **No card is the default state, not the exception.** Measured 2026-10-29: **nine** sessions
sat parked on `pick —` / `approve:` closers and **not one had a card** — so the operator's
board rendered *"Nothing needs attention"* while nine questions waited unanswered. A run that
classifies those as OPERATOR and posts nothing reproduces exactly that failure, and it is the
failure this branch exists to end.

⚠️ **The card carries the question in the session's own words**, with your recommended answer
as option 1. A card reading *"session X needs you"* without the question is a pointer, not a
decision, and it costs the operator a jump to learn nothing.

⚠️ **Post nothing for a CLOSED or EXTERNAL row.** Those need no decision, and carding them is
the `ack`-only noise that makes the board unreadable.

⚠️ **The `--dedup-key` is what makes re-runs safe.** Derive it from the session id plus a slug
of the question, never from a timestamp — a second triage of the same unchanged question must
collapse onto the first card, not mint a twin. ⚠️ **`<question-slug>` is `[a-z0-9-]+`** — derive
it by lowercasing and replacing every run of other characters with `-`, never by quoting pane
text into the key. The key is a single-quoted word, so a slug carrying a literal `'` closes the
quoting; constraining it to that alphabet removes the case instead of relying on the
single-quote/escape rule stated above the card command.

### EXTERNAL → report

One line: the watcher and the ETA. Nothing else.

⚠️ **This command arms no watcher, and the router grants no `Monitor` — deliberately.** A run is
one pass; a watcher armed here would outlive the run that created it and fire into a session that
is no longer triaging, which is noise dressed as a guard. So the branch's whole job on a row whose
wait has **no watcher at all** is to *report* that, not to fix it:

```
EXTERNAL — <label> (<sid-prefix>): waiting on <named producer>, ETA <when> · ⚠️ no watcher armed
```

…and then hand the arming to whoever owns the wait: one `SendMessage` to the row's owning manager
naming the session, the producer and the missing watcher. For a manager row, the missing watcher is
its own loop, and the same message names that. **Never re-arm a watcher that is already armed, and
never invent a second one from here** — a duplicate watch is worse than none, because it doubles
every notification and hides which one is authoritative.

### 4b LIVE-IDLE HEADLESS → `SendMessage` first

**This row is not STUCK, and it must not fall through to the STUCK branch below.** Its drive
verb does not exist: no *supervisor* command reaches a session with `pane: null`, so
`/supervisor:open`, a pane nudge and a `wezterm` keystroke all have nothing to act on. The one
channel that does reach it is a cross-session `SendMessage`.

1. **Read the nudge record first — this is what makes the expiry real.** The count lives on
   disk, not in a report. ⚠️ **Read it through `python3`, not `cat`** — `cat` is not in this
   command's grant list, and a read that parks on a permission prompt is the same non-drive
   this branch exists to end:
   ```bash
   python3 - '<session-id>' <<'PY'
   import json,os,sys
   p=os.path.expanduser(f"~/.claude/state/fleet-triage/nudges/{sys.argv[1]}.json")
   try: print(json.dumps(json.load(open(p))))
   except Exception: print(json.dumps({"session_id":sys.argv[1],"open_box":None,
                                       "nudges":[],"carded_at":None}))
   PY
   ```
   Shape: `{"session_id": "<target>", "open_box": "<the box it was nudged about>", "nudges":
   [{"at": "<ISO8601Z>", "run": "<this triage session's id>"}], "carded_at": "<ISO8601Z>" | null}`.
   ⚠️ **A record is the only thing that survives the run** — the report is chat, and a
   compaction or a different session sees none of it, so without this file every run looks like
   a first nudge and the two-run expiry below can never fire.
2. **Decide from the record, then act — exactly one of three:**
   - **`carded_at` is set** → already escalated. Report the row and **do not nudge again**.
   - **`open_box` matches AND the last entry's `run` is a different session id** → this is the
     second-or-later run on an unchanged box: **post the OPERATOR card now** (step 5), then
     **write the record back with `--carded`** and stop.
   - **otherwise** (no record, a different `open_box`, or the only entry is this run) →
     **`SendMessage` the session directly**, naming its next open box, then **write the record
     back** without `--carded`.

   ⚠️ **One write path for both branches — the difference is the flag, not a second copy of the
   code.** `--carded` appends the current run's nudge **and** stamps `carded_at`, which is what
   makes branch 1 reachable and what lets step 5 read *both* nudges' timestamps back from the
   record:
   ```bash
   python3 - '<session-id>' '<open box>' [--carded] <<'PY'
   import json,os,sys,datetime
   sid,box=sys.argv[1],sys.argv[2]
   carded="--carded" in sys.argv[3:]
   run=(os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_PID")
        or str(os.getpid()))                            # never empty: the expiry compares run ids
   p=os.path.expanduser(f"~/.claude/state/fleet-triage/nudges/{sid}.json")
   try: d=json.load(open(p))
   except Exception: d={"session_id":sid,"open_box":None,"nudges":[],"carded_at":None}
   if d.get("open_box")!=box:                           # the worker moved: count AND escalation restart
       d["nudges"]=[]; d["carded_at"]=None
   d["open_box"]=box
   d["nudges"].append({"at":datetime.datetime.now(datetime.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),"run":run})
   if carded: d["carded_at"]=d["nudges"][-1]["at"]
   os.makedirs(os.path.dirname(p),exist_ok=True)
   tmp=f"{p}.{os.getpid()}.tmp"                         # tmp + os.replace: never a truncating write
   with open(tmp,"w") as f: json.dump(d,f,indent=2)
   os.replace(tmp,p)
   PY
   ```
   ⚠️ **Write it only after the send (or the card post) succeeded** — a record for a nudge that
   never left is a false count that escalates a worker nobody ever contacted.
   ⚠️ **`<open box>` is task-file checkbox text, so it needs the single-quote escape.** A box
   reading `Fix the user's login` closes the quoting and the write fails **after** the nudge was
   delivered — the record is lost and Step 6 then counts the row as STUCK. Escape a literal `'`
   as `'\''`, the same rule every other text-carrying value in this file follows.
   ⚠️ **The write is tmp + `os.replace`, and the readers' `except` is not a licence to truncate.**
   A kill mid-write on a plain `open(p,"w")` leaves a partial file that the next run reads as
   "no record", silently resetting the count — the exact collapse `check-drive-ledger-writes.py`
   was written after (`scripts/drive-ledger.py:219-228` is the repo's form).
   ⚠️ **The run id is never empty.** `CLAUDE_CODE_SESSION_ID` is absent in some spawned children
   (`commands/worker-drive.md:428`), and an empty-string fallback would make branch 2 compare
   `""` with `""` and never fire — so the fallback chain ends at the python pid, which is
   distinct for every run. `commands/steps/fleet-triage-sweep.md:25-27` takes the same
   `${…:-$CLAUDE_PID}` view of the variable.
   **`SendMessage` is the only drive verb for this state** — never the owning manager (that is
   STUCK item 3, a different row), and never a pane write.
3. **Re-read its status once, after the send.** Still `idle` with the same open box and no
   reply → it is parked, not stuck.
4. **Card a kill only if the send produced no reply and the task is finished** — an OPERATOR row
   carrying the session id, the question, and the `close-worker.py` command you propose. Never
   signal a headless pid by hand, and never close a live worker that still has open work.
5. ⚠️ **A nudge unanswered on two consecutive runs is a card either way, finished task or not**
   — that is step 2's second branch, and it is the reason the record exists. The question changes
   from *"shall I close it"* to *"this worker has ignored two nudges — close it, or take the
   pane?"*, and it goes to the operator as an OPERATOR row with the session id, the open box it
   was nudged about, and **both nudges' timestamps read from the record**. A third identical
   `SendMessage` is not a drive action: it is the same input against the same state, and the
   run's own `NUDGED` outcome (`commands/fleet-triage.md` § The invariant) is only legitimate
   because it expires. Left uncarded, a worker that ignores every nudge is exactly the
   "idle with work left" the invariant calls a failure — this is the branch that catches it.

⚠️ **The distinction is the conjunction, not the symptom.** Live, `idle`, `entrypoint: sdk-cli`,
`pane: null` — all four, and `commands/steps/fleet-triage-state.md` § 3.4b owns the test. A
session failing any limb is some other state and belongs to its own branch.

### STUCK → drive it, in this order

1. **Dead worker, task in progress** → `/supervisor:open "<task>"` (resumes by id, never
   spawns a second writer).
2. **Approved task, no worker** → `/supervisor:open "<task>"`. Approval is the boundary:
   `phase: todo` is not approved and is reported, never opened.
3. **Live worker idle with open boxes** → one `SendMessage` to its owning manager naming the
   worker and its next open box. Never drive another manager's worker directly.

   ⚠️ **A `SendMessage` alone does not wake an idle TAB session** — it waits in the inbox for a
   turn that never comes (16 of 16 no-ops, 2026-11-08). So after the message, **wake the
   session item 3 already names**, through its pane:

   - **Target.** The owning manager (`parent`) when that manager is live and is a tab session.
     The worker itself **only** when no live manager owns it, and that is measured as exactly
     one of `worker:no-manager` (no `parent`), `worker:manager-paneless` (the parent is live but
     `pane-wake.py` reports `skip:no-pane` for it), or `worker:manager-absent` (the parent is not
     in the live session list). Record which. ⚠️ If most rows in a run fall through to the
     worker, that is a broken manager resolution, not the design working — say so in the report.
   - **Dry run first, every time:**
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/pane-wake.py" --session '<target-id>'
     ```
     It prints the pid→tty→pane join and the guard verdict and **types nothing**. Only a
     `wake` / `wake:suggestion` row proceeds. Every `skip:*` is reported as the row's reason,
     never retried by another route.
   - **Then send, for that target only:**
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/pane-wake.py" --session '<target-id>' --send --message '<one line naming the worker and its next open box>'
     ```
     `delivered` (composer cleared **and** the pane shows a spinner) is the only success.
     `unverified` and `failed:*` are reported as such, never as a nudge.
   - **What it refuses by itself:** this session's own pane, the `Fleet Manager`, any
     `Fleet Pusher…` session, a session with an open gate, and every pane the guard does not
     clear (spinner, selection modal, `❯ 1.` option row, a `👤 You:` ask, typed text, a machine
     wait, an unclassifiable read). Never work around a refusal with a raw `wezterm cli send-text`.
4. **Owning manager loop `STOPPED` / `UNARMED`** → this is a fleet finding, not a nudge.
   `STOPPED` is a deliberate marker, so **do not re-arm it unilaterally** — raise it as an
   OPERATOR row ("loop stopped deliberately; re-arm?"). `UNARMED` with a live session and an
   open subject is a lapse, and the drive is **one `SendMessage` to that manager telling it to
   re-arm in its own pane** — `commands/steps/fleet-triage-state.md` § 3.6 carries the same row
   and the same mechanism, and this step must not invent a second one. ⚠️ **`WRONG-BRANCH` and a
   `--state` refusal (exit 3, empty stdout) are never this row** — both are misreads of the
   subject, not a missing loop, and a re-arm sent on one lands on a manager that is running or
   stood down on purpose.

   ⚠️ **Never run `manager-liveness.py --arm` from here.** `--arm` is documented as *"run by the
   manager at EVERY re-arm"*: it writes the cadence record and clears `.stopped`, so calling it
   from the triage session **fakes an arm on a manager that never armed itself** — the record
   then reads armed while nothing ticks, and the next sweep reports a healthy loop that does not
   exist. It also cannot be made correct from here: `--arm` takes `--branch <fleet|goal|topic>`
   and a triage row may be any of the three, so a hardcoded `--branch goal` writes the wrong
   namespace for a fleet or topic manager. The manager owns its own arm; this command owns the
   finding.

⚠️ **Every STUCK row must end the run either moving or carded.** A row that is still stuck
and uncarded at Step 6 is the run's failure, and must be named as such.
