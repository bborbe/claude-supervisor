---
description: Open the right thing for a name — resolves it to a task, goal or topic, then jumps to its live session, resumes its recorded session, or starts a new one. Batch mode opens every task flagged for today. Name via $ARGUMENTS; optional --task/--goal/--topic, --vault, --color, --flagged, --first-step.
argument-hint: "<name> [--task|--goal|--topic] [--vault <v>] [--color <c>] [--flagged] [--first-step <text>]"
allowed-tools:
  - Bash(vault-cli:*)
  - Bash(wezterm cli spawn:*)
  - Bash(wezterm cli list:*)
  - Bash(wezterm cli send-text:*)
  - Bash(grep:*)
  - Bash(ls:*)
  - Bash(find:*)
  - Bash(pgrep:*)
  - Bash(python3:*)
  - Bash(awk:*)
  - Read
  - TodoWrite
  - Agent
---

Open whatever a name refers to, in one verb. Replaces `/open-session` (task-only, and it stopped instead of jumping when a session already existed).

The command **resolves** and **dispatches**. It never does the work, never runs the planning turn itself, and never absorbs the manager loop — it launches them.

```
/supervisor:open <name>
  ├─ task   → live pane? jump · live but no pane (cluster worker)? refuse · has claude_session_id? resume · else spawn gates (Step 1.5: approval, then readiness) → spawn
  ├─ goal   → resolve to its active task, then the task branch
  └─ topic  → live goal-loop? jump · else spawn /supervisor:goal-loop

/supervisor:open --flagged → batch: select flag:true · in_progress · due today,
                             classify + WRITE any missing role: and mode: from the
                             task body, print the table, then open each eligible
                             row in stable order. role: human is SKIPPED; a body
                             with no signal is REFUSED and left unwritten.
                             mode: by § Spawn a worker item 6, never ad hoc.
```

## Arguments

**Parse `$ARGUMENTS` yourself — never use positional argument references** — a title is multi-word, so a positional resolves to one word of it.

From `$ARGUMENTS` take:

- **name (required):** the task / goal / topic title or identifier — everything that is not a recognised flag. May contain spaces; never word-split it, and quote it when passing to `vault-cli`.
- **`--task` / `--goal` / `--topic` (optional):** force the type, skipping resolution. Use when a name matches more than one kind.
- **`--vault <name>` (optional):** target vault. **Overrides cwd detection** (Step 0). With `--flagged` it *narrows* the scan to one vault — the default there is **every** vault declaring a `tasks_dir`, not the cwd's.
- **`--color <name>` (optional):** colour for the spawned session. **Default: resolved from the role map (§ 3.0)** — `pink` for an agent, `orange` for a manager, `cyan` for a human. Pass this flag only to override that resolved value. One of `red, blue, green, yellow, purple, orange, pink, cyan, default`.
- **`--flagged` (optional):** batch mode — select every task flagged for today, fill in any missing `role:` and `mode:` by reading each task's body, print the table, and **open every eligible row**. Takes no name; skip Steps 1–4 and go to Step 0.5, then Step 0.6. There is no `--confirm`: opening is the default.

If no name survives parsing **and `--flagged` was not given**, print `❌ Pass a task, goal or topic name.` and STOP.

## Step 0 — Resolve the vault

⚠️ **This step is for the single-name path only. `--flagged` does NOT use it** — the batch scans every vault with a `tasks_dir` (Step 0.5), so resolving one vault from cwd would narrow the scan.

**Never hardcode a vault.** Precedence: `--vault` → the vault whose `path` contains the cwd → `default_vault` from config. **If both resolver arms fail, STOP.** ⚠️ **`--vault` is deliberately NOT validated here** — an unconfigured name fails *downstream*.

```bash
VAULT="${VAULT_FLAG:-$(python3 -c "
import json,subprocess,os,yaml
d=json.loads(subprocess.check_output(['vault-cli','config','list','--output','json']))
cwd=os.path.realpath(os.getcwd())
def inside(p):
    p=os.path.realpath(p)
    return cwd==p or cwd.startswith(p+os.sep)
hit=[v['name'] for v in d if v.get('path') and inside(v['path'])]
if hit:
    print(hit[0])
else:
    cfg=os.path.expanduser('~/.config/vault-cli/config.yaml')
    try:
        d2=yaml.safe_load(open(cfg))
    except (OSError, yaml.YAMLError):
        d2=None
    v=d2.get('default_vault') if isinstance(d2, dict) else None
    if not isinstance(v, str) or not v or v not in {x.get('name') for x in d}:
        raise SystemExit('cwd is not inside any configured vault and ' + cfg + ' carries no usable default_vault - pass --vault <name>')
    print(v)
")}"
[ -n "$VAULT" ] || { echo '❌ no vault resolved — stopping' >&2; exit 1; }
```

**The `cwd==p` arm is load-bearing, not defensive** — a vault-root session sits *at* the root, where the `+os.sep` form matches nothing.

**`is_default` is not a field.** `default_vault` lives at the config's top level, not inside a vault entry — `config list --output json` returns entries with no such key (all 11 `None`). `vault-cli config` exposes only `current-user` and `list`; there is no `config get`.

Then resolve the rest from that vault's own config entry — **never assume**:

```bash
vault-cli config list --output json    # → claude_script, worker_launcher, manager_launcher, session_project_dir, topics_dir
```

- **`topics_dir`** — use the vault's value, defaulting to `23 Topics`. A vault without a topics folder (another vault) simply never resolves a topic; that is correct, not a failure — say so if a topic was asked for.
- **`worker_launcher` / `manager_launcher` / `claude_script`** — the launcher rungs: `private-personal` → `cc-private-deepseek` / `cc-private-claude`, `brogrammers` → `cc-seibert-deepseek` / `cc-seibert-claude`. A task's own `launcher:` beats the worker default; a goal's never counts. Never carry one vault's script into another.
- **`session_project_dir`** — the new tab's start dir. It is **absent** for `private-personal` and `brogrammers`, and points at `my-vault` for several sibling vaults; the `cc-*` launchers `cd` into their own vault themselves, so an absent value is fine — pass no `cd`.

## Step 0.5 — Flagged batch branch (`--flagged`)

Skip Steps 1–4 when `--flagged` was given: a batch has no single name to resolve. Runs after Step 0, which supplied `<vault>`.

**Selector — ALL VAULTS by default, and an absent `defer_date` does NOT exclude.**

`flag: true` AND `status: in_progress` AND (`defer_date` absent OR `defer_date <= today`), read from the CLI per vault — blocked state has the same single source, never a frontmatter grep:

```bash
for v in $(vault-cli config list --output json | python3 -c "
import json,sys
for x in json.load(sys.stdin):
    if x.get('tasks_dir'): print(x['name'])"); do
  vault-cli --vault "$v" task list --all --output json 2>/dev/null | VAULT="$v" python3 -c "
import json,sys,os,datetime
raw=sys.stdin.read().strip()
if not raw: sys.exit(0)                      # vault returned nothing — skip, never crash
try: d=json.loads(raw)
except Exception: sys.exit(0)
if d is None: sys.exit(0)                    # measured: one vault returns literal null
tasks=d if isinstance(d,list) else d.get('tasks',d.get('items',[]))
today=datetime.date.today().isoformat()
def due(t):
    dd=str(t.get('defer_date') or '')[:10]
    return (not dd) or dd <= today           # ABSENT means not deferred, i.e. available now
# An agent-set flag is never an approval (ruled 2026-09-28). Only these two actors open a row:
#   operator — the operator's own surface wrote it (Vault UI toggle, a hand-typed --by operator)
#   legacy   — one of the 270 rows whose flag predates the field, dispositioned 2026-09-30
# Everything else refuses, INCLUDING an absent flag_set_by: after the disposition no row should
# carry one, so an absent value means a writer that did not declare itself, which is the bypass.
OPENING_ACTORS=('operator','legacy')
cand=[t for t in tasks if t.get('flag') is True and t.get('status')=='in_progress' and due(t)]
sel=[t for t in cand if str(t.get('flag_set_by') or '').strip() in OPENING_ACTORS]
for t in sorted([x for x in cand if x not in sel], key=lambda x: x.get('name','')):
    who=str(t.get('flag_set_by') or '').strip() or '<absent>'
    print('REFUSED |', os.environ['VAULT'], '|', t.get('name'), '| flag_set_by=' + who, '| not the operator — not opened, not approved')
for t in sorted(sel, key=lambda x: x.get('name','')):
    print(os.environ['VAULT'], '|', t.get('name'), '|', 'HOLD' if t.get('blocked') else '', '|', ','.join(t.get('blocked_by') or []))
"
done
```

⚠️ **The provenance filter is a gate, not a tidy-up; it must stay ahead of the approve pass.**

⚠️ **Refuse loudly; never drop a row silently.** A refused row prints `REFUSED | <vault> | <task> | flag_set_by=<who>` and is excluded from the batch.

⚠️ **`legacy` opens a row — a migration marker, not a credential.**

**Scan every vault that declares a `tasks_dir`; `--vault <name>` narrows to one.** Of 13 configured vaults, 8 declare one. A vault-scoped run is the exception now, not the default — the operator manages one fleet, not one vault, and the same complaint already stood against the fleet manager's vault argument.

**An unanswerable vault is skipped and named, never fatal** — the loop exits quietly per vault.

⚠️ **The absent-`defer_date` clause is the half that surfaces other vaults — do not drop it while keeping the loop.** An absent `defer_date` means available now, not *never surface*.

**Print the vault on every row.** Task titles are not namespaced across vaults, so a table without a vault column collapses two different tasks into one row and opens the wrong one.

The count is a property of the day, not of the command — **never hardcode it and never treat a row count as a check**. A shrinking set is the selector working, not the selector failing.

**`mode` is derived per row and written, and the table carries a `mode` column.**

✅ **Per-task mode is its own `mode:` field, never folded into `role:`**, with the fallback rule that makes it safe: *unclear → interactive*. Step 0.6 decides and writes it.

⚠️ **Two fields, not one: `role` cannot answer the mode question.** The mapping is not restated here — owned by item 6 of § Spawn a worker.

**The asymmetry that justifies the fleet default staying `interactive` is owned by item 6 of § Spawn a worker**, together with the measurement behind it — stated once there rather than restated here.

**No ordering is computed — but blocked rows are surfaced.** It **holds** every row the CLI reports `blocked=True`: mark it `⛔ HOLD`, print its blockers, skip it with a stated reason.

**Opening is the default — there is no dry run and no `--confirm`.** Print the table, then open every eligible row.

## Step 0.6 — Classify and fill the missing `role` and `mode`

⚠️ **This step runs on EVERY open — the batch and the single-task path alike.** On the single-task path, every "selected row" below is the one resolved task.

**A missing `role:` is a question to answer, not a row to drop** — classify it from the body.

Resolve the task in this order — each selected row on the batch path, the single resolved task otherwise:

1. **`role:` present** → use it. Never re-derive over a value already on disk; the field exists so the decision is made once. **`role: manager` is never a spawn target** — render `👤 YOURS` and skip the row (see the manager guard below).
2. **`role:` absent** → classify it now from the task's **body**, then write the result.
3. **Body gives no basis to decide** → refuse this row, write nothing, and say so. This is the one case that still stops — see the undecidable guard below.

**The classifying question is `role`'s own, not the mode question.** Ask: *does finishing this task need the operator's own hands, their presence, or a judgement only they can make?* Yes → `human`. No → `agent`.

Three signals mean `human`, and each has to come from reading the body:

- **the physical world** — a tank to drain, a machine to power off, a device to hold
- **a GUI-only surface with no agent interface** — an app this session cannot drive at all
- **the operator's own approval IS the work** — a live trade, a sign-off, a decision reserved to them

Everything else is `agent`. A task naming `kubectl`, `ssh` or a deploy is still `agent`: operationally weighty is not the same as human-only, and conflating those two is precisely the error the keyword derivation made.

⚠️ **Classify from the BODY, never from the title or the frontmatter.**

**A `role: manager` row is never opened — and the classifier never derives `manager`.** `manager` is **declared** by whoever files the row (the write command accepts it for exactly that), never inferred: a prose-derived value measured at ~1.1% false positives against 4/19 recall was rejected. Open a `manager` row as **nothing at all** — render `👤 YOURS` (the vocabulary `agents/goal-sweep-reader.md` step 4 and `agents/goal-drive.md` clause (4) already use), spawn no session, open no window, and write nothing; the declared value is the record.

### Then decide `mode` — the rule lives in one place, and this is not it

**The classifier is not restated here.** It has its single home in `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker **item 6** — the classifying question, the `role` → `mode` mapping, what positive evidence looks like, and the asymmetry that makes `interactive` a floor rather than a tie-break. Read it there.

What this file owns is the **storage and the write**: resolve `mode` per that rule, write the field, and read it back at Step 3.

```bash
vault-cli --vault "<vault>" task set "<task>" mode <interactive|headless>
```

⚠️ **A restated copy of the rule is not a harmless comment — it is a second counter.** Keep it in one place.

**Write both values before opening anything:**

```bash
vault-cli --vault "<vault>" task set "<task>" role <human|agent|manager>
vault-cli --vault "<vault>" task set "<task>" mode <interactive|headless>
```

Writing them is the point, not a side effect. The next batch reads the fields instead of re-deciding, so one task cannot be classified one way today and the other way tomorrow. It also makes the call reviewable — a wrong value is visible on disk and the operator corrects it with that same one-line command.

**`mode` is consumed, not merely recorded** — Step 3 passes `interactive=false` to `spawn_agent` only when the field reads `headless` (absent field → the argument is omitted and the fleet config decides; see § 535).

**`shipping:` is read here and passed at Step 3 — never inferred.** Pass `shipping: true` to `spawn_agent` only when the field reads `true`; omit it otherwise. A derived value would hand unattended commit rights to a row nobody marked. Rule: § Spawn a worker item 9.

⚠️ **A machine-written role is indistinguishable from an operator-written one on disk.** Nothing in the frontmatter records who decided. So **print every auto-filled row distinctly** (`🏷️ filled now`) instead of folding it into the opened count. That report line is the only provenance this decision has, and it exists for exactly one read — the first one, while objecting is still cheap.

⚠️ **The undecidable guard still refuses; never soften it into a default.** When the body genuinely does not say, do **not** guess and do **not** write the field — print `⛔ REFUSED — role: undecidable (body carries no signal; not written)`. **This applies to `role` only.** `mode` has a safe floor and never blocks — an undecidable mode is `interactive`.

**A `role: human` row is never opened as a WORKER — it is opened into the Direct window instead.**

Open a `human` row as **the operator's own session**, not a delegate:

- window → `Direct` (`~/.cache/wezterm-role-map.json` → `Direct.window_id`), never `Agents`
- chip → `cyan`, never `pink` — the colour is the fleet's only cue for which role a session plays
- prompt → the same `/vault-cli:work-on-task "<task>"`, because anchoring the task is useful to a human too

Print it as `→ DIRECT` rather than `⛔ SKIPPED`. The distinction that matters is **who drives the session**, not whether a session exists: a cyan tab in `Direct` with the task loaded is a workspace, and spawning a pink agent worker onto the same row would be the failure. One of those is a window assignment; the other is a delegation.

⚠️ **Some `human` rows are useful in a tab and some are not** — a water change gets nothing from a terminal. Do **not** invent a third category to sort them: open both, and let the operator close the one that does not help. A fourth bucket guessed from prose would be the keyword derivation again, wearing yet another hat.

```
FLAGGED BATCH — <vault> — <date> — <n> tasks

approve  a monitoring task - 2026W38-sun
approve  a routine task (my-vault) - 2026-09-20
approve  a routine task - 2026W39
         (a row already approved, or one not at `phase: todo`, prints nothing)

  #  task                                          role    mode
  1  a monitoring task - 2026W38-sun                  agent   interactive   🏷️ filled now
  2  a routine task (my-vault) - 2026-09-20   agent   interactive   (on disk)
  3  a routine task - 2026W39                       human   interactive   🏷️ filled now
     → DIRECT — role: human, opened as the operator's own session, never as a worker  4  Untitled Stub - 2026-09-21                    —       —
     ⛔ REFUSED — role: undecidable (body carries no signal; not written)
 10  a routine task - 2026W38-sun                   agent   interactive   (on disk)
     ⛔ HOLD — blocked by two other in-flight tasks
  ...
  Rows with no `mode:` on disk open per fleet config (`spawn.mode`); only an
  explicit `headless` overrides it.

⚠️  Ordering is NOT auto-detected. Every ⛔ row is skipped;
    the rest open in the stable order printed above.

Opened N · approved A · filled F fields · skipped H (human) · yours Y (manager) · refused U (undecidable) · held K (blocked).
```

**Approve every eligible row first — before the readiness dispatch.** The batch's opening move is `vault-cli --vault "<vault>" task approve "<task>"` for each Step 0.5 selector row that is **not** JUMP/RESUME-bound. `role: human` rows are approved too. A row not at `phase: todo` is left alone.

⚠️ **`task approve` also moves `status` to `next`; the pass must put it back.** Every row here came from the Step 0.5 selector (`status: in_progress`). Each approved row takes one more write:

```bash
vault-cli --vault "<vault>" task set "<task>" status in_progress
```

⚠️ **This is what makes the pass's two branches agree: `in_progress` is correct** — a row holding a live worker is not queued.

⚠️ **One exception: a row whose `phase` is empty.** `task approve` cannot accept it — it refuses with `task is at phase "(none)", not "todo"` — so the pass writes the approval itself, in this order:

```bash
vault-cli --vault "<vault>" task set "<task>" approved_by operator
vault-cli --vault "<vault>" task set "<task>" approved_at "<RFC3339 timestamp>"
vault-cli --vault "<vault>" task set "<task>" phase planning
```

⚠️ **The order is load-bearing, not cosmetic.** A row moved to `planning` *before* the approval is recorded can never be approved afterwards — *the flag is the approval*: approval first, phase second. **A row at neither `todo` nor empty is left alone.**

⚠️ **The pass runs before the readiness dispatch, never after** — a row is approved whether or not it clears Gate 2; a held row that was approved opens on the next run.

⚠️ **Print one `approve <task>` line per approved row.** The run's output is the only record that the ordering held, and it is what a reader checks to tell a real approve from a line-printing no-op.

**Readiness-gate every CREATE-bound row first, concurrently.** For each CREATE-bound row (`role: agent`, no live pane, no `claude_session_id`), dispatch Step 1.5's readiness sub-agent — **all in ONE message**, one `Agent` call per row. Resume/jump-bound, `role: human` (→ DIRECT) and `task_type:` pipeline rows are not gated. `readiness` column: `✅ ready <score>/10`; `⚠️ opened <score>/10` (operator-flagged, below threshold — gap bullets under the row); `⛔ NOT READY` (still below after the improvement pass); `— (resume/jump/direct)`. A not-ready row is **held, never opened** — **except an operator-flagged row below the ladder's open threshold, which opens with its gaps** (§ Step 1.5 Gate 2); held rows go in the footer (`… · not ready R (readiness)`). ⚠️ **Gate 1 does not refuse this batch** (`flag: true` on a `todo` row **is** the approval for `--flagged`). The batch runs **Gate 2 only** — ⚠️ **Gate 3 still runs at the spawn.**

**Loop the eligible rows in the printed stable order**, delegating each to this command's own single-task path. Per row, invoke `/supervisor:open "<task>"` — with `--first-step "<gap bullets>"` only when the row opened below the readiness ladder's open threshold. Never reimplement jump / resume / spawn here; never pass a mode override to the fleet. ⚠️ **The per-row delegation IS the guard — no shortcut keeps it.** ⚠️ **The batch is paced by delegation, never by a counter of its own** (rule home: `docs/fleet-surface.md` § Spawn a worker item 5).

**Skip every `⛔ HOLD` row and report the skip with its blockers** — a blocked task is one whose prerequisites have not finished, so opening it is the failure the batch exists to prevent. Never open a blocked row because it appeared in the table: appearing in the selector is not the same as having its prerequisites met. Report each row's outcome, and every skip with its stated reason.

**Write the batch's checklist into TodoWrite before the loop, and close every item before the run ends.** One item per selected row plus one for the report. Mark an item completed only when its outcome is settled: **opened**, **live** (JUMP/RESUME), **refused**, or **held** with its stated reason. The batch cannot end while any item is `pending`/`in_progress`; if one is, the run stopped early — say so. ⚠️ **The item text names the outcome, not just the row.**

## Step 1 — Resolve the type

Skip when `--task` / `--goal` / `--topic` was given. Otherwise probe all three and report which matched:

```bash
vault-cli --vault "<vault>" task get "<name>" status  2>/dev/null
vault-cli --vault "<vault>" goal get "<name>" status  2>/dev/null
find "<topics_dir>" -maxdepth 1 -iname "*<name>*.md"
```

For the topic probe, confirm `page_type: topic` **inside the frontmatter block**, not anywhere in the file:

```bash
awk '/^---$/{n++; next} n==1' "<page>" | grep -q '^page_type: topic'
```

**Use `find -iname`, never a shell glob.** `ls "<topics_dir>/"*"<name>"*.md` is case-sensitive under zsh, so a lowercase argument matches nothing against Title Case filenames.

Then:

- **exactly one match** → act on it, and say which type matched
- **more than one** → print each match with its type and STOP; tell the operator to re-run with `--task` / `--goal` / `--topic`. Goal and task titles are not namespaced in these vaults, so collisions are real — never guess, and never silently prefer one type
- **no match** → print the closest candidates (`vault-cli task search` / `goal search`) and STOP. Never create anything from an unmatched name

## Step 1.5 — Spawn gates (CREATE branch only)

**The single home of `/supervisor:open`'s spawn preconditions.** Step 2A's CREATE branch and the `--flagged` batch (Step 0.6) both reference this section; neither restates it.

Two gates run here, in this order. The first asks whether the row may be opened at all; the second asks whether its plan is good enough to hand to a worker. **A row that fails the first is never probed by the second** — auditing an unapproved row spends work on a decision the operator has not made.

### Gate 1 — approval (`phase: todo` refuses)

Read the resolved row's phase from the CLI, never from a frontmatter grep:

```bash
vault-cli --vault "<vault>" task get "<task>" phase
```

`phase: todo` means **the operator has not approved this row.** `vault-cli task approve` is the only thing that moves `todo → planning` and writes `approved_by` / `approved_at` in the same storage call; without that record the row is the operator's to approve, not ours to open. Print and STOP — **spawn nothing**, do not run Gate 2, do not write to the task:

```
⛔ NOT APPROVED — <task> is at phase: todo.
   The operator approves it first:  vault-cli task approve "<task>"
   Then re-run:                     /supervisor:open "<task>"
```

⚠️ **Never add the approval yourself, and never read a `flag: true` as one here.** This gate exists because a worker spawned on an unapproved row reaches the approval question itself and hands the operator a pane-bound `you run: vault-cli task approve …` — the pane-bound approval the attention board exists to remove.

⚠️ **The `--flagged` batch does not merely bypass Gate 1 — it performs the approval.** `flag: true` on a `todo` row **is** the approval for `/supervisor:open --flagged` only. A flagged `todo` row opens approved (`phase: planning`, `approved_by` set).

⚠️ **The ruling is load-bearing, not decorative** — a `flag: true` **is** task content, and `vault-cli`'s `docs/task-writing.md` § *Phase transitions* permits approval only on the operator's "own explicit words" — which the ruling is.

The approval does **not** extend to the four sweeps (`goal-loop`, `goal-drive`, `fleet-loop`, `fleet-drive`), which report `todo` rows instead of acting on them. ⚠️ **Enforcement of the flag's provenance sits upstream of this gate**, in the § Step 0.5 selector.
### Gate 2 — readiness

**Scope: CREATE only.** JUMP and RESUME are never gated. ⚠️ **The decision here is the readiness ladder, whose single home is `docs/fleet-surface.md` § Spawn a worker item 2** — read the branches there; the thresholds are never restated as a second counter. Orientation only: **≥ 9 opens directly; below 9 runs one improvement pass and re-audits; ≥ 7 after that pass opens; below 7 is carded and held.** ⚠️ **In the `--flagged` batch the card branch is overridden for operator-flagged rows**: a row the Step 0.5 selector admitted that lands below the ladder's open bar **opens** anyway, its gap bullets carried into the worker's prompt as its first step (§ Step 0.6 owns the transport). **Gate 1 is untouched.** `role: human` rows into Direct are not gated. **`task_type:` frontmatter rows are exempt**; show `— (pipeline contract)`.

**The probe stays read-only by construction — the improvement pass is the caller's act, never the probe's.** Do NOT call `/vault-cli:plan-task --non-interactive` here — its entry contract mutates the rows it inspects. ⚠️ **That ban does not reach the ladder's improvement pass, which *is* a write** (it edits the task file between two probes). Dispatch one sub-agent per task:

```
Agent(subagent_type='vault-cli:task-auditor', description='Readiness: <task>',
  prompt='Audit <absolute task path>. READ-ONLY readiness probe for a spawn gate: never edit, never ask. Return your normal report, then additionally check these hard gates and list each failure: (1) # Success Criteria with ≥2 binary checkboxes; (2) # Tasks lists concrete steps that reach the SC; (3) shipping-class tasks carry an e2e verify subtask naming a procedure AND an expected result; (4) every # Tasks item maps to an SC or is the verify subtask; (5) any push/deploy/credential names its target system and account. Score the row and name every hard-gate failure; do NOT apply an open bar yourself — the open decision is the readiness ladder's, and its single home is `docs/fleet-surface.md` § Spawn a worker item 2. Then, before you score, check for the one class of gap that is not yours to close: a gap whose resolution needs a decision only the operator can make — a Success Criterion depending on a page, artifact or credential that does not exist, a scope question the task file does not answer, a target system or account named nowhere. If you find one, end with UNFIXABLE: <reason> as your ONLY final line, naming the gap and why only the operator can resolve it — no score, no READINESS line, and do not repair it yourself. Otherwise end with exactly one line: READINESS: <score>/10 — <gap>; <gap>. Report the score and the gaps and stop there: do NOT add a ready/not-ready word, and do NOT apply the ladder yourself — the ladder decides whether it opens, and a verdict word you add is never read. Never run a filesystem-wide find.')
```

**A sub-agent that cannot close a gap because its resolution needs an operator decision returns `UNFIXABLE: <reason>` instead of a `READINESS:` line.** ⚠️ **`UNFIXABLE:` no longer stops the row — the improvement pass acts on it first:** the pass **rewrites the unmeetable criterion into a falsifiable form, or splits it into its own follow-up task**, then re-audits. The card is the **fallback**, fired only when neither works or the criterion is operator-only. Reader: `agents/goal-drive.md` clause (1) **step 0**.

**The ladder, as this gate runs it — dispatch the probe, read the score, then branch.** The thresholds' single home is `docs/fleet-surface.md` § Spawn a worker item 2; this is the gate's own execution of them:

1. **Score ≥ 9** → ready. Spawn.
2. **Score < 9** → run **one improvement pass**: edit the task file to fix the audit's named gaps (for an `UNFIXABLE:`, rewrite the criterion falsifiable or split it into a follow-up task). Then re-dispatch the probe. ⚠️ **One run of `agents/goal-drive.md` clause (1)'s repair loop — this gate adds no second loop and no round cap.** ⚠️ **A hard-gate failure is never waived by the ladder.**
3. **Post-pass score ≥ 7** → ready. Spawn. Nothing is carried into the worker's prompt: the row clears the ladder on its own.
4. **Post-pass score < 7** → print `⛔ NOT READY <score>/10 — <task>` with the gap bullets, **post the attention-board card** per `docs/fleet-surface.md` § Spawn a worker item 2, and hold the row. ⚠️ **In the `--flagged` batch this branch is overridden** — the row opens with its gap bullets carried into the worker's prompt (see § Scope above, and § Step 0.6 for the transport).

**A missing or unparseable `READINESS:` line is not-ready** — a probe that did not answer has not cleared the gate, and it is not a licence to run the improvement pass blind. **Parse the score**: the gate decides from `<score>` plus the hard-gate list itself.

⚠️ **The probe emits no `ready`/`not-ready` word — decide from the score alone.**



⚠️ **A `phase: todo` row is untouched by the ladder** — it is refused at Gate 1 above, before any probe runs, and the improvement pass never writes its `phase`.

⚠️ **A readiness string in this file is not a gate.** A file-level grep for the reference satisfies a naive check while individual sites stay ungated. Each site (CREATE branch, batch dispatch) links here by name — check each one's own window.

### Gate 3 — the fleet cap, and the operator-named exemption

**Runs last, at the spawn itself, because it is the only gate whose answer depends on the fleet's live count.** The thresholds, their defaults and the marker's definition have **one home** — `docs/fleet-surface.md` § Spawn a worker item 5 — so read them there and **never restate a number here**. Orientation only: **below the soft cap everything opens; between the soft and hard caps only an operator-named task opens; at the hard cap nothing opens and a card is posted.**

**Resolve the marker before you spawn.** Read the row's own flag provenance — the same two fields § Step 0.5 already filters on:

```bash
vault-cli --vault "<vault>" task get "<task>" flag
vault-cli --vault "<vault>" task get "<task>" flag_set_by
```

⚠️ **Two calls, never one — `task get` takes exactly ONE key** (`vault-cli task get <task-name> <key> [flags]`); a second field is not a second key.

A row is **operator-named** when `flag` is true **and** `flag_set_by` is an opening actor (`operator`, or the `legacy` rows § Step 0.5 admits). Pass `operator_named: true` for such a row; omit it for every other row. ⚠️ **Never infer it; never write it into the row's prose.** ⚠️ **An agent-set flag is not operator-named** — test the *pair*, never `flag` alone.

**On a refusal, read WHICH threshold refused — the server's message names the cap and its source.**

- **The soft cap refused an ordinary row** → print the refusal and STOP. This is the intended state, not a defect: the row is picked up next sweep, and `⏸️ CONCURRENCY LIMIT` is the line a sweep already knows.
- **The hard cap refused — including an operator-named row** → the fleet is FULL (including the **manager** hard cap, `MANAGER_FLEET_FULL` in `server/spawn-mode.mjs`, carded the same way, keyed on the manager-fleet-full state). **Post one attention-board card naming the fleet-full condition** via `attention-ask.py post`, keyed on the fleet-full state (so a second attempt while still full does not ask twice), then print the refusal and STOP. ⚠️ **The card is the operator's to answer, never the caller's to clear.**

⚠️ **This gate is advisory to the server, not a substitute for it.** `spawn_agent` re-resolves both thresholds and re-reads the live count itself and refuses on its own — so a stale count here costs a refused spawn the next sweep picks up, never an overrun. Never describe this gate as what keeps the fleet inside its limits.

## Step 2A — Task branch

**Probe for a live session first.**

```bash
PRIOR_SID="$(vault-cli --vault "<vault>" task get "<task>" claude_session_id 2>/dev/null)"
PRIOR_SID_RC=$?
wezterm cli list
```

**Refuse if the id could not be read — before any branch, the hold check included.** ⚠️ **`2>/dev/null` makes a FAILED read indistinguishable from a task that has no session, and that difference decides CREATE.** A non-zero `PRIOR_SID_RC` means `vault-cli` itself failed; the first refuses, only the second falls through to CREATE.

Print `❓ SESSION UNREADABLE — <task> · vault-cli exited <rc> reading claude_session_id · refused; fix the vault/task argument and re-run.` and STOP: no JUMP, no RESUME, no CREATE.

⚠️ **`--vault` takes a name, not a path** — a path exits non-zero, and under `2>/dev/null` an empty id matches every session.

**Refuse first if that session carries an operator hold — before any branch, JUMP included.** A hold is operator *policy* about a **session**; it is **not** the `hold` *task status*, nor the `HOLD` marker for a `blocked` row — only the first is read here:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/session-holds.py is-held "<PRIOR_SID>"
# exit 0 → held, reason on stdout · exit 1 → not held · exit 2 → bad target
```

⚠️ **Refuse loudly and name the release path — never skip the row silently.** Print `⏸️ HELD — <reason> · <age> · <task> · refused; only the operator releases it` and STOP: no JUMP, no RESUME, no CREATE. Rule: `skills/hold/SKILL.md`.

**Read the full pane list — never `| tail -N` or `| head -N`.** Match the task title against the TAB TITLE column.

⚠️ **Identify your own pane first — `$WEZTERM_PANE` — before attributing any title to a sibling.**

**Also probe for a headless turn — a pane list cannot see one.** `vault-cli task work-on --mode headless` holds a `claude_session_id` while appearing in **no pane at all**, so the branches below read "no live session" and spawn a second writer onto a session a `--print` turn is mid-write on. Check before choosing a branch:

```bash
pgrep -fl "<PRIOR_SID>" || echo "no process holding this session"
```

A live pid → **wait for it to exit, then resume** (Step 3). JUMP is unavailable — there is no pane to activate, and the branch logic below would otherwise spawn onto it, putting two writers on one conversation.

**Then refuse if the session is live but reachable from nowhere here — this is the cluster worker.** Probe via the plugin's single reader; **do not open the registry here**:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/session-liveness.py --check "<PRIOR_SID>"
# exit 0 LIVE · 1 ABSENT · 2 UNKNOWN · 3 AMBIGUOUS — an 8-char prefix is a legal argument
```

**When `PRIOR_SID` is non-empty and neither a live pane nor a live pid was found, refuse — no JUMP, no RESUME, no CREATE — unless the verdict is `ABSENT`:**

- **`LIVE` (exit 0)** → `🔒 LIVE ELSEWHERE — <task> · <PRIOR_SID> · live but holds no pane and no local process; a cluster worker, or a headless turn whose id is not in its argv · refused; do not spawn.`
- **`UNKNOWN` (exit 2)** → `❓ UNVERIFIED — <task> · <PRIOR_SID> · liveness could not be read, so a resume is not authorised · refused.` ⚠️ **`UNKNOWN` is not `ABSENT`** — an unreadable source never authorises a second writer.
- **`AMBIGUOUS` (exit 3)** → `❓ UNVERIFIED — <task> · <PRIOR_SID> · the prefix matches more than one session, so it resumes neither · refused.`

⚠️ **The guard is bounded by `PRIOR_SID`** — with no session id there is nothing to probe, so a task that has never been worked falls straight through to branch 3 (CREATE) and its spawn gates. Probing an empty id would match every session and refuse the whole CREATE path.

**`ABSENT` (exit 1) falls through to the branches below unchanged.** ⚠️ **A blanket refusal does not satisfy this** — this guard fires only on *live, and reachable from nowhere here*.

Then take exactly one branch:

1. **Live pane exists → JUMP.** Do not spawn; a second tab on one session is two views of one conversation, not two workers.

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump.py pane:<pane-id>
   ```

   Report `↪️ jumped to <task> — pane <pane-id>, window <window-id>`.

**Jump via `/supervisor:jump`, never a raw `wezterm cli activate-tab` line**. `activate-tab` **cannot cross windows**: called from another window it succeeds and nothing visibly moves. `/supervisor:jump` resolves the coordinate at run time from `wezterm cli list`, prints the window id, and takes a **pane id**. Neither namespace survives a WezTerm restart — re-resolve after one.
2. **No live pane, `PRIOR_SID` present → RESUME** in a new tab (Step 3).
3. **Neither → CREATE**, then resume in a new tab — **only after Step 1.5's spawn gates clear: Gate 1 (approval), Gate 2 (readiness), Gate 3 (the fleet cap and the operator-named marker)** (the `--flagged` batch pre-clears Gates 1–2 only — ⚠️ **Gate 3 still runs here**). A `phase: todo` row stops at Gate 1 (`⛔ NOT APPROVED`); a Gate-2 failure prints its gaps; either way STOP, no spawn. ⚠️ **Gate 3 is the only site passing `operator_named: true`** — a spawn without it is refused at the soft cap.

**The caller mints nothing.** Spawn via Step 3's create branch, which hands the work prompt to the worker as an *argument* — the **worker runs its own planning turn and creates its own session**. The caller returns as soon as the spawn returns.

   Step 3 carries the recipe and its rationale — read it there rather than restating it here. Once the spawn returns, continue with Step 4 (verify + report) and Step 4.1 (bind the task explicitly).

## Step 2B — Goal branch

The goal branch **resolves the goal's manager and delegates**; it reimplements nothing. A worker tab is titled with a *task*, never a goal, so there is no pane to probe at goal level — the manager is found in the session registry.

0. **Approval gate — before resolving anything.** `vault-cli goal get "<goal>" phase`. A goal at `phase: todo` is in the operator's approval inbox and is NOT approved for management: print `⌛ waiting-approval — <goal> is at phase: todo; approve it with vault-cli goal approve "<goal>"` and STOP — spawn nothing, list nothing, jump nowhere. An absent `phase` is **exempt**, as is every other value; `todo` alone is gated.

1. Resolve the goal's manager from the **session registry**, exactly as Step 2C item 1 does — read it through its single reader, never by opening the directory:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/session-liveness.py" --list --json
   ```

   ⚠️ **A non-zero exit is "cannot tell", never "no manager" — do not spawn on it.** **Only exit 0 with no match means "none is running now".** A non-zero exit, or unparseable JSON, means the question could not be answered: report it and stop; never fall through to item 4.

   Then match `<goal> Manager` case-insensitively against `name` and against every entry in `formerNames`.

   ⚠️ **The goal page's `manager_session_id` is a lead, never the answer.** When the registry matches nothing, read it and **run `session-liveness.py --check <id>` on it before doing anything with it**, then take the action that exit code maps to in `docs/fleet-surface.md` § *Which manager owns a subject*.

2. **Live → JUMP** to it, same `/supervisor:jump pane:<pane-id>` + window-id reporting as Step 2A — this covers both a registry hit and a page-field `LIVE`.

3. **`ABSENT` → RESUME it by id** (Step 3). The conversation is what is durable, not the process, so an `ABSENT` id is a resume, never a reason to spawn. ⚠️ **Do not reach item 4 on an `ABSENT` verdict** — a second manager on a live goal is precisely the outcome this step exists to prevent.

4. **None, `UNKNOWN` or `AMBIGUOUS` → spawn a goal manager** on `<MGR>` from `python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-launcher.py" "<manager_launcher>" "<goal page>" --default-source manager-default` (prints `<MGR>\t<source>`; non-zero → stop; rule: `docs/fleet-surface.md` § Which launcher a manager opens on). `<manager_launcher>` is the vault's configured value **as the absolute path `config list` emits** — a bare name fails the script's file check. `<goal page>` is the vault's goals folder plus `<goal>.md` (`24 Goals/` in the operator's vault). Resolve the `Managers` window and the `orange` chip from the role map (§ 3.0, `role: manager`), then:

   ```bash
   wezterm cli spawn ${WINDOW_ID:+--window-id "$WINDOW_ID"} -- bash -lc 'unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; exec "<MGR>" -n "<goal> Manager" "/color orange"'
   ```

   Same `unset` prefix as Step 3.1 — without it the manager comes up named after the session that spawned it. Then tell the operator to run, in that tab, `/vault-cli:work-on-goal "<goal>"` and `/supervisor:goal-loop "<goal>"`. `/supervisor:open` starts the session; the manager arms its own loop.

   **The colour and the window are role signals, not decoration** — a manager comes up `orange` in `Managers` so it can never be read as a worker, which stays `pink` in `Agents` (§ 3.0).

5. **Only when the manager branch cannot run** — `manager-launcher.py` exited non-zero, or the spawn failed — fall back to the goal's tasks:
   - List them — every task whose `goals:` frontmatter names this goal. **Frontmatter only.** A body-text `[[Goal]]` mention is not membership: grepping `[[goal]]` file-wide over one topic's five member goals returned 39 tasks where frontmatter returns 21, and two of the extras were `in_progress` under entirely different goals.
   - **one non-terminal task** → run the task branch (Step 2A) on it
   - **several** → list them with status/phase and ask which; never pick for the operator
   - **none** → the goal has nothing executable. Report that and suggest `/vault-cli:work-on-goal "<goal>"`, which is the planning path that creates tasks. Do not spawn anything

⚠️ **A live goal manager owns the goal's tasks** — do not open a worker from this branch on a goal whose manager is live. Item 5 exists for a goal that has no manager and could not get one, never as a shortcut past the manager.

## Step 2C — Topic branch

0. **Approval gate — before resolving anything.** `vault-cli topic get "<topic>" phase`. A topic at `phase: todo` is in the operator's approval inbox and is NOT approved for management: print `⌛ waiting-approval — <topic> is at phase: todo; approve it with vault-cli topic approve "<topic>"` and STOP — spawn nothing, jump nowhere. An absent `phase` is **exempt**, as is every other value; `todo` alone is gated.

1. Resolve the topic's manager from the **session registry**, not from `ListAgents` — but read it through its single reader rather than opening the directory here. `session-liveness.py --list --json` returns one record per live session carrying `sessionId`, a live `status`, the current `name`, **`formerNames`** (the names it has held since it started) and `cwd`:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/session-liveness.py" --list --json
   ```

⚠️ **A non-zero exit is "cannot tell", never "no manager" — do not spawn on it.** **Only exit 0 with no match means "none is running now".** A non-zero exit, or unparseable JSON, means the question could not be answered: report it and stop; never fall through to step 3.

⚠️ **The precondition covers the spawn ledger's workers, NOT every live session** — the guard is `registry_live ∩ spawn ledger`: **a topic manager started by hand holds no ledger record**, so a live manager whose row is missing does not withhold the list; the `formerNames` match above covers that. If this step matches nothing while the operator believes a manager runs, re-read that manager's endpoint directly with `session-liveness.py --check <id>` before spawning.

   Then match `<topic> Manager` case-insensitively against `name` and against every entry in `formerNames`.

⚠️ **Do not re-open `~/.claude/sessions/*.json` here.** The reader returns `formerNames` and `cwd` precisely so this call site does not need the directory, and so a cluster worker's registration reaches this step too.

**Match the topic against `name` AND `formerNames`.** Matching only the current name is the hole this closes. `sessionId` is the identity, and `status` is live, so a hit can never resume a dead conversation.

   ⚠️ **The registry lists live sessions only**, like `ListAgents`: an exited session's entry is removed and its `formerNames` with it. That is the right scope here (the failure being prevented is a duplicate spawn against a *running* manager), but do not read an empty result as "this topic never had a manager" — only as "none is running now".

⚠️ **The topic page's `manager_session_id` is a lead, never the answer.** The registry above answers *who is running now*. When it matches nothing, read the topic page's `manager_session_id` and **run `session-liveness.py --check <id>` on it before doing anything with it**, then take the action that exit code maps to in `docs/fleet-surface.md` § *Which manager owns a subject*.

⚠️ **The ban this replaces was right about the hazard and wrong about the remedy.** ⚠️ **The order by which an owner is resolved is stated once, in `docs/fleet-surface.md` § *Which manager owns a subject* — not restated here.**
2. **Live → JUMP** to it, same `/supervisor:jump pane:<pane-id>` + window-id reporting as Step 2A — this covers both a registry hit and a page-field `LIVE`.
3. **`ABSENT` → RESUME it by id.** The conversation is what is durable, not the process, so an `ABSENT` id is a resume, never a reason to spawn. ⚠️ **Do not reach item 4 on an `ABSENT` verdict** — a second manager on a live topic is precisely the outcome this step exists to prevent, and it is the failure the superseded ban above was trying to avoid by refusing the field altogether.
4. **None, `UNKNOWN` or `AMBIGUOUS` → spawn a manager** on `<MGR>` from `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-launcher.py" "<manager_launcher>" "<topic page>" --default-source manager-default` (prints `<MGR>\t<source>`; non-zero → stop; rule: `docs/fleet-surface.md`). `<manager_launcher>` is the vault's configured value **as the absolute path `config list` emits** — a bare name fails the script's file check — and `--default-source manager-default` tells it the value came from config rather than from `claude_script`. The script applies the subject page's override and reports `manager-default` or `subject`:

   ```bash
   wezterm cli spawn -- bash -lc 'unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; exec "<MGR>" -n "<topic> Manager" "/color orange"'
   ```

   Same `unset` prefix as Step 3.1 — without it the manager comes up named after the session that spawned it. The colour rides as the spawn prompt, per the recipe. Then tell the operator to run `/supervisor:goal-loop "<topic>"` in it. The manager loop is stateful and this command is not; `/supervisor:open` starts the session, the manager arms its own loop.

**The colour is a role signal, not decoration** — a topic manager comes up `orange` so it can never be read as a worker, which stays `pink` (Step 3.1 and the `--color` default). Keep the two distinct.

## Step 3 — Spawn the tab (resume/create branches)

Resolve the vault's launcher and dir from config — **never hardcode them**:

```bash
vault-cli config list --output json      # → claude_script, worker_launcher, manager_launcher, session_project_dir
```

Effective dir = the vault's `session_project_dir`, else no `cd`.

⚠️ **Pass `role`, not `window_id`.** `spawn_agent` takes an optional `role` (`manager` / `agent` / `human`, default `agent`) and resolves **both** the colour and the window from the published map **in-process**, so pass § 3.0's resolved role here. The older `window_id` argument still works but is no longer how a role is routed. The response reports the resolved `role` and `window_id` — **check those fields**.

**Create branch — `spawn_agent`, which takes the work prompt as an *argument*.** Call shape: [`docs/fleet-surface.md` § Spawn a worker](../docs/fleet-surface.md#spawn-a-worker) — prompt `/vault-cli:work-on-task "<task>"`, `vault <vault>`, **`task <task>`**, label `<task>`, role `<role>` (§ 3.0), `[target <target>]`, `[shipping]` (Step 0.6's read). ⚠️ **`task` binds the worker to the vault; `label` is NOT a substitute.** ⚠️ **Pass `vault`, not `cwd`** — one of the two is **required**; rule and refusals: `§ Spawn a worker` **item 8**. **With `--first-step <text>`, the prompt is `<text>`, a blank line, then `/vault-cli:work-on-task "<task>"`**; otherwise the bare work command.

**Read `target:` and pass `target="<value>"` when the field is present, omitting it when absent** (absent = `local`; `docs/fleet-surface.md` § Spawn a worker item 7 is the single home for the field, its default and the `docker` / `k8s` argument contract). Pass the value **verbatim** — an unknown value reaches the server and is refused there, never defaulted or lower-cased here. ⚠️ **Then check ELIGIBILITY — a separate question from which target, read from the same item.** When the row carries a disqualifier, **print a warning naming it** before spawning (`⚠️ target "<t>" is not eligible for "<task>": needs <disqualifier>`). ⚠️ **Advisory, never a refusal** — print it and leave the decision here; never silently drop the `target`, never rewrite the row's frontmatter, never refuse the spawn on this ground. The disqualifier list has its single home in item 7 — **read it there and never restate it here.** ⚠️ **On a `docker` / `k8s` row the argument contract changes and the `role` above is dropped** — item 7 carries that branch; never hand-build it from this line.

**Do NOT pass `interactive` here — unless the task file itself declares `mode:`; otherwise the fleet default decides, and it lives in a file.** The server resolves `SUPERVISOR_SPAWN_MODE`, then `spawn.mode` in `~/.config/claude-supervisor/config.json`, then its built-in `interactive`. The response reports `mode_source` (`argument`/`env`/`config`/`default`).

**The carve-out is both-ways: pass the argument whenever the task's frontmatter declares a `mode:`, and omit it only when the field is absent.** `mode: headless` → `interactive=false`; `mode: interactive` → `interactive=true`. An absent `mode:` still means *omit the argument entirely*, not *pass interactive*. ⚠️ **Local rows only** — on a `docker` / `k8s` row this carve-out does not apply; `§ Spawn a worker` item 7 carries that branch.

⚠️ **The interactive half is load-bearing, not a saving.** A `mode:` the spawn honours in only one direction is inert in the other — the field must be consumed both ways.

The rule that *produces* the value is not restated here — it has its single home in `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 6; this line is only the consumption.

⚠️ **Writing `mode: headless` today opens a worker into a mechanism with two known, measured defects** — its gates park with the spawning session's server and no one else can answer them, and the resume guard does not guard. Until prompts and questions can be answered remotely, `headless` should be rare and never a guess.

**Parity is real, not a compromise** — a headless worker inherits the same MCP servers a tab does.

**Do NOT add `policy` while `defaultMode` is `auto`.** `auto` is in `POLICY_UNREACHABLE_MODES`, so `spawn_agent` **refuses** a named policy (*"policy would never be consulted"*) and the spawn fails. Omit it; the overlay is usable only under `acceptEdits`.

**`interactive` is the debug escape hatch, and it works in both directions.** Pass `interactive=true` when a worker's own screen must be watched live. Pass `interactive=false` to force one headless worker while the fleet runs in tabs. Either way it overrides the fleet default for that one spawn only. The colour applies only on the tab path. ⚠️ **Local rows only** (§ Spawn a worker item 7).

**The fleet's current mode is not recorded here — read the file.** `cat ~/.config/claude-supervisor/config.json` is the answer; the standing mode is `interactive`.

Requires `mcp__supervisor__*` in **this** session — see the recipe's precondition, which fails silently.

**Fallback when `mcp__supervisor__*` is unavailable.** Every `cc-*` launcher passes `--strict-mcp-config`, which turns its config file into an allowlist — plugin-provided servers are never loaded, so a vault whose `~/.claude/mcp-obsidian-<vault>.json` does not itself name `supervisor` carries no supervisor tools at all. Then spawn the wezterm way and send the work command afterwards.

⚠️ **The fallback cannot produce a headless worker — a wezterm spawn is a tab by construction.** `supervisor` is named in the **private-personal** and **brogrammers** launcher configs only. Opening into a vault whose workers must be managed, add the entry there first.

### 3.0 Resolve the session's role → window + colour

**Before either spawn path**, resolve the role from the task's frontmatter and read the window/chip pair from the role map. This stops a human-only task coming up as a pink agent tab in whatever window the caller happened to be in.

```bash
ROLE=$(vault-cli task get "<task>" role 2>/dev/null || echo "")
MAP="$HOME/.cache/wezterm-role-map.json"
WINDOW_ID=$(jq -r --arg r "${ROLE:-agent}" '
  ({"manager":"Managers","agent":"Agents","human":"Direct"}[$r] // "Agents") as $p
  | .[$p].window_id // empty' "$MAP" 2>/dev/null)
CHIP=$(jq -r --arg r "${ROLE:-agent}" '
  ({"manager":"Managers","agent":"Agents","human":"Direct"}[$r] // "Agents") as $p
  | .[$p].chip // empty' "$MAP" 2>/dev/null)
```

| `role:` value | window | chip |
|---|---|---|
| *(absent)* — an ordinary agent worker | Agents | `pink` |
| `manager` | Managers | `orange` |
| `human` | Direct | `cyan` |

**`role:` absent means agent, and that is the correct default** — it preserves today's behaviour for every task that has not declared a role, which is all but `Start Day`.

⚠️ **If `$MAP` is missing or `WINDOW_ID` comes back empty, fall back to today's behaviour and SAY SO in the report** — spawn into the caller's window with `pink`. Do **not** silently proceed as if routing worked.

Then pass the pair to whichever spawn path runs:

- **wezterm path (§ 3.1 / resume):** add `--window-id "$WINDOW_ID"` to the `wezterm cli spawn` call when it is non-empty, and replace the hardcoded `"/color pink"` with `"/color $CHIP"`.
- **`spawn_agent` path (§ 3):** pass `role` — the server resolves the window **and** the colour from the published map in-process, so this path needs no `--window-id` and no separate colour step. It reports the resolved `role` and `window_id` back, so check those rather than the landing window alone.

### 3.1 Spawn — the wezterm path (fallback, and always the resume branch)

```bash
# Pace the start FIRST, and never skip this line on this branch. The resume branch bypasses
# the server, so this is the ONLY thing that throttles it — a batch of resumes issued without
# it is exactly the 2026-10-10 burst (22 starts in 2.8 min). It reserves a slot and sleeps
# until it, so no timing logic belongs here; exit 1 means the rate refused the start and the
# reason is on stderr. The rule's single home is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md`
# § Spawn a worker item 5 — this is the call site, not the recipe.
#
# ⚠️ RUN THIS WITH A TIMEOUT OF AT LEAST 300s — never under the Bash tool's 120s default. The
# script may sleep to MAX_START_DELAY_MS (300s) and it writes the reservation BEFORE sleeping,
# so a caller that kills it mid-sleep has already spent a slot without spawning anything, and
# this `|| exit 1` then aborts the row. At the default rate a 22-row batch reaches waits of
# 4-5 minutes, so the default timeout fails the batch exactly where the pacing matters. Pass
# an explicit timeout on the call (the Bash tool's maximum is 600s).
node "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/start-rate.mjs" || exit 1

wezterm cli spawn ${WINDOW_ID:+--window-id "$WINDOW_ID"} --cwd "<cwd>" -- bash -lc 'unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; exec "<claude_script>" --resume <session_id> -n "<title>" "/color '"$CHIP"'"'
```

⚠️ **For a task, `<claude_script>` is the task's resolved launcher, not the vault default** — this path bypasses the server, so apply the rule yourself: `vault-cli task get "<task>" launcher --vault <vault>`, and when that is empty use the vault's `worker_launcher`. ⚠️ **Do NOT read the goal's launcher** — a goal states how its manager runs, never its workers (rule home: [`docs/fleet-surface.md` § Spawn a worker](../docs/fleet-surface.md#spawn-a-worker) item 8).

**`--window-id` is what puts the tab in the right window** — without it the new tab inherits `WEZTERM_PANE` from the calling session. `${WINDOW_ID:+...}` keeps the flag off entirely when the map is unavailable.

**`--cwd` is the other half of the same trap, and it is not inherited either.** Without it the tab inherits *wezterm's* working directory. Read the value from the shared reader (`session-liveness.py --list --json`, field `cwd`) and resolve it **before** the spawn; never guess, and refuse rather than spawn into the wrong tree.

**Order matters here: pass `/color` as the spawn prompt → send the work command after.** The colour is the *only* thing that belongs in this spawn's prompt argument. Do NOT pass the work prompt there — a session spawned with it is busy from its first instant.

The trailing `"/color $CHIP"` is the initial prompt — Claude executes it at startup and reports `Session color set to: <chip>`, then idles. `$CHIP` is § 3.0's role-resolved value, so the colour follows the role rather than a fixed default. Override the colour with `--color <name>`; valid: `red, blue, green, yellow, purple, orange, pink, cyan, default`.

**The resume branch always uses this path, never `spawn_agent`.** `spawn_agent` with a `resume` argument refuses a session whose liveness **cannot be determined** — and `/supervisor:open` resumes precisely the sessions its own probe could not classify as live (Step 2A). That refusal is correct behaviour, but it would block the resume rather than route it.

**This shape's single home is `claude-supervisor/docs/fleet-surface.md` § Spawn a worker**. This block is the call site, not the recipe. Read that section before changing the shape; every call site must match it.

⚠️ **Two things about this shape are load-bearing, and both are explained only in the recipe:**
- the **`unset` prefix** — kept, but its peer-registration *reason* is **refuted**; the recipe records the measurement. Do not restate the socket story as its justification.
- the **`/color`-as-spawn-prompt split** — Claude Code parses one submitted message as one command and `/color` takes the entire trimmed argument, so the colour cannot ride inside a larger prompt.

### 3.2 Send the work command — **wezterm fallback path only**

**Skip this section entirely on the `spawn_agent` path** — there the work prompt *is* the spawn argument, so nothing needs sending. This applies only when 3.1's wezterm shape was used.

The colour already ran as the spawn prompt, so the session is idle and the work command submits on the first try:

```bash
sleep 8                                   # let the session reach its prompt
wezterm cli send-text --pane-id <new-pane-id> --no-paste $'/vault-cli:work-on-task "<task>"'
sleep 1
wezterm cli send-text --pane-id <new-pane-id> --no-paste $'\r'   # submit as a separate step
```

**Send the text and the submit as two steps.** A trailing `\r` appended to the text is not sufficient. Send the text, then a bare `\r` (`wezterm cli send-text --pane-id <N> --no-paste $'\r'`), and **repeat until the composer clears**. **Read the pane back after sending** — a prompt is not delivered until the composer is empty *and* the session is visibly working. ⚠️ **The `sleep` is a race** — the supervisor waits for the `❯` glyph instead.

**Why the colour goes in the spawn and the work command does not.** Confirm `Session color set to: <name>` appears before you send the work command.

⚠️ **Only ever send this into a pane THIS command just spawned, while it is still idle.** This is not a licence to drive other sessions by `send-text` — answering another session's operator gate that way is still forbidden.

The `cc-*` launchers `cd` into their own vault before starting Claude, so a vault with no `session_project_dir` needs no `cd` at all. The new tab inherits `WEZTERM_PANE` from the calling session, so it spawns into the current window.

## Step 4 — Verify and report

- `wezterm cli list` shows the new tab (spawn branches), or the jump target was activated (jump branch)
- the task's `claude_session_id` matches the session now running
- **the spawned session owns its name** — check the record, do not trust the tab title alone:

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/session-liveness.py" --list --json \
    | python3 -c "import json,sys; [print(r['name'], r['nameSource']) for r in json.load(sys.stdin) if r['sessionId'].startswith('<sid-prefix>')]"
  ```

  `nameSource: user` → correct. `nameSource: peer` → the `unset` prefix was missing or ineffective; the session is named after its spawner and the title-match session-connect cannot resolve it (see below).
- state the branch taken in one line: `↪️ jumped` / `♻️ resumed <sid-prefix>` / `🆕 created <sid-prefix>` — plus the resolved type **and the resolved vault**, so a mis-resolution is visible immediately
- **the worker runs where you asked, under that vault's launcher** — read `vault`, `launcher` and `model` back from the spawn response: `launcher` must equal the launcher the task resolves to (its `launcher:` frontmatter, else the vault `worker_launcher`, else the vault `claude_script` — never its goal's; `launcher_source` names which — rule home: [`docs/fleet-surface.md` § Spawn a worker](../docs/fleet-surface.md#spawn-a-worker) item 8); the returned `cwd` must **not** be the `claude-supervisor` repo, and `model` must be the one that launcher sets.

### 4.1 Bind the task explicitly — `-n` does not self-link

**`-n` sets the session record's `name` but writes NO `custom-title` line into the transcript.** `work-on-task`'s session-connect resolves by scanning transcripts for `customTitle`, so it finds nothing and the task stays unlinked.

⚠️ **On the tab path the server already wrote it — verify the `bind` field, and do NOT write it yourself.** Because the spawn above passes `task`, `spawn_agent` binds the session through `task-binding.mjs`'s ownership rule and reports the outcome as `bind` on its response:

- `bind.action == "stamped"` — the task's `claude_session_id` was EMPTY and now names this session.
- `bind.action == "appended"` — the task was already owned; this session was appended to `metrics_sessions` and the owner was left intact.
- `bind.error` — the session is live but the vault record failed. Reconcile by hand; see below.
- `bind == null` — no `task` was passed, so nothing was bound. On this path that means the spawn was malformed.
- **the key is ABSENT entirely** — the spawn took the local **headless** path, which does not bind. Step 3 sends `interactive=false` for every row whose `mode:` reads `headless`, so this is ordinary for those rows. Bind it by hand if the row must be resolvable.

⚠️ **A manual `vault-cli task set "<task>" claude_session_id "<uuid>"` is a SECOND writer with no ownership rule.** Use it **only** when `bind.error` is set or the headless path was taken, then reconcile rather than overwrite: read the current owner first, and prefer `vault-cli task append-metrics-session "<task>" "<uuid>"` when the field already holds an owner.

⚠️ **Never reconstruct a uuid from a prefix.** Session listings are often truncated to 8 chars; the remaining 28 are not guessable and a fabricated uuid writes a link to a session that does not exist. Read the full value from the record every time.

⚠️ **A wrong name silently breaks task↔session linkage.** `work-on-task`'s session-connect writes `claude_session_id` only when **exactly one** uuid matches the task name; several sessions sharing one inherited name match zero-or-many, so it refuses to guess and the task stays unlinked.

## Notes

- Managers (`/supervisor:goal-loop`, `/supervisor:fleet-loop`) *recommend* opening sessions; this command is the executor — run it when the human approves.

- Replaces `.claude/commands/open-session.md`. The jump branch and the goal/topic resolution are the additions.

  ```bash
  grep -rln "<old-name>" \
    ~/Documents/Obsidian/my-vault/.claude \
    ~/Documents/Obsidian/my-vault/65\ Runbooks \
    ~/.claude/commands ~/.claude/plugins/cache
  ```

  The mirror-image failure is just as easy: reading the generated `CLAUDE.md` shows *that* a string is stale but never *where it lives* — the text is in `.claude/claude-md-rules/`, and a regeneration carries stale sources forward unchanged while looking like a fix. Two sibling sessions hit that side of it the same day.
- **Invoked as `/supervisor:open`.** The spawn call shape is referenced from `docs/fleet-surface.md` § Spawn a worker, not restated.
