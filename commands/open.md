---
description: Open the right thing for a name — resolves it to a task, goal or topic, then jumps to its live session, resumes its recorded session, or starts a new one. Batch mode opens every task flagged for today. Name via $ARGUMENTS; optional --task/--goal/--topic, --vault, --dir, --dry-run, --flagged.
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
  - Agent
---

Open whatever a name refers to, in one verb. Replaces `/open-session` (task-only, and it stopped instead of jumping when a session already existed).

The command **resolves** and **dispatches**. It never does the work, never runs the planning turn itself, and never absorbs the manager loop — it launches them.

```
/supervisor:open <name>
  ├─ task   → live pane? jump · has claude_session_id? resume · else readiness gate (Step 1.5) → spawn
  ├─ goal   → resolve to its active task, then the task branch
  └─ topic  → live manager-loop? jump · else spawn /supervisor:manager-loop

/supervisor:open --flagged → batch: select flag:true · in_progress · due today,
                             classify + WRITE any missing role: and mode: from the
                             task body, print the table, then open each eligible
                             row in stable order. role: human is SKIPPED; a body
                             with no signal is REFUSED and left unwritten.
                             mode falls back to interactive whenever unclear.
```

## Arguments

**Parse `$ARGUMENTS` yourself — never use positional argument references.** A title is multi-word and space-containing, and a positional reference resolves to a single *word* of it: invoked as `/supervisor:open "Speak a Filler When Audio Is Slow to Start"`, the first positional renders as `a` and the second as `Filler`, not as the title. Every other command that takes a positional takes a single token — an owner, a scope, a mailbox — so word-splitting never bites them. A title does. `audit-page.md` is the reference form. (This paragraph deliberately spells no positional reference, because the harness would substitute it here too.)

From `$ARGUMENTS` take:

- **name (required):** the task / goal / topic title or identifier — everything that is not a recognised flag. May contain spaces; never word-split it, and quote it when passing to `vault-cli`.
- **`--task` / `--goal` / `--topic` (optional):** force the type, skipping resolution. Use when a name matches more than one kind.
- **`--vault <name>` (optional):** target vault. **Overrides cwd detection** (Step 0). With `--flagged` it *narrows* the scan to one vault — the default there is **every** vault declaring a `tasks_dir`, not the cwd's.
- **`--dir <path>` (optional):** directory the new tab starts in. Overrides the vault's `session_project_dir`.
- **`--color <name>` (optional):** colour for the spawned session. **Default: resolved from the role map (§ 3.0)** — `pink` for an agent, `orange` for a manager, `cyan` for a human. Pass this flag only to override that resolved value. One of `red, blue, green, yellow, purple, orange, pink, cyan, default`.
- **`--dry-run` (optional):** print the resolved type, target, branch and the exact commands, then stop. No session created, no tab spawned, no jump.
- **`--flagged` (optional):** batch mode — select every task flagged for today, fill in any missing `role:` and `mode:` by reading each task's body, print the table, and **open every eligible row**. Takes no name; skip Steps 1–4 and go to Step 0.5, then Step 0.6. There is no `--confirm`: opening is the default.

If no name survives parsing **and `--flagged` was not given**, print `❌ Pass a task, goal or topic name.` and STOP.

## Step 0 — Resolve the vault

⚠️ **This step is for the single-name path only. `--flagged` does NOT use it** — the batch scans every vault with a `tasks_dir` (Step 0.5), so resolving one vault from cwd would be exactly the narrowing that hid 8 `brogrammers` candidates and 2 `octopusagent` ones on 2026-09-21.

**Never hardcode a vault.** Precedence: `--vault` flag → the vault whose `path` contains the cwd → `default_vault` from the config.

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
    print(yaml.safe_load(open(cfg)).get('default_vault','personal'))
")}"
```

**The `cwd==p` arm is load-bearing, not defensive.** A session started in a vault is normally *at* the vault root, where `startswith(p + '/')` is false — measured 2026-09-15: from `/Users/bborbe/Documents/Obsidian/Brogrammers`, the equality held and the `+os.sep` form matched nothing, so every vault-root session would have silently fallen through to `default_vault` (personal) and run the whole command against the wrong vault.

**`is_default` is not a field.** `default_vault` lives at the config's top level, not inside a vault entry — `config list --output json` returns entries with no such key (all 11 `None`). `vault-cli config` exposes only `current-user` and `list`; there is no `config get`.

Then resolve the rest from that vault's own config entry — **never assume**:

```bash
vault-cli config list --output json    # → claude_script, session_project_dir, topics_dir
```

- **`topics_dir`** — use the vault's value, defaulting to `23 Topics`. A vault without a topics folder (Brogrammers) simply never resolves a topic; that is correct, not a failure — say so if a topic was asked for.
- **`claude_script`** — the vault's own launcher. `personal` → `cc-personal-deepseek`, `brogrammers` → `cc-brogrammers-deepseek`. Never carry one vault's script into another.
- **`session_project_dir`** — the new tab's start dir. It is **absent** for `personal` and `brogrammers`, and points at `Personal` for several sibling vaults; the `cc-*` launchers `cd` into their own vault themselves, so an absent value is fine — pass no `cd`, and let `--dir` be the override when the session needs a repo.

## Step 0.5 — Flagged batch branch (`--flagged`)

Skip Steps 1–4 when `--flagged` was given: a batch has no single name to resolve. Runs after Step 0, which supplied `<vault>`.

**Selector — ALL VAULTS by default, and an absent `defer_date` does NOT exclude.** Both halves changed 2026-09-21 on the operator's instruction (*"default should be … all vaults"*), after a run against `personal` alone opened 8 rows and silently missed every other vault.

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
sel=[t for t in tasks if t.get('flag') is True and t.get('status')=='in_progress' and due(t)]
for t in sorted(sel, key=lambda x: x.get('name','')):
    print(os.environ['VAULT'], '|', t.get('name'), '|', 'HOLD' if t.get('blocked') else '', '|', ','.join(t.get('blocked_by') or []))
"
done
```

**Scan every vault that declares a `tasks_dir`; `--vault <name>` narrows to one.** Of 13 configured vaults, 8 declare one. A vault-scoped run is the exception now, not the default — the operator manages one fleet, not one vault, and the same complaint already stood against the fleet manager's vault argument.

⚠️ **Two guards the loop must keep, both measured 2026-09-21 by the run that motivated this.** One vault (`dataassistant`) returns **literal `null`** rather than a task list, which crashed the selector with `'NoneType' object has no attribute 'get'` and aborted the scan mid-way — so one bad vault silently truncated every vault after it in the loop order. Empty stdin does the same. Both exit quietly per-vault: **a vault that cannot answer is skipped and named, never fatal.**

⚠️ **The absent-`defer_date` clause is the half that actually surfaces other vaults — do not drop it while keeping the loop.** Measured: `brogrammers` holds 856 tasks and 46 `flag: true`, of which **8 are `flag: true` AND `in_progress`** — genuine candidates — and **0 carry a `defer_date`**, because that vault does not use the field. Under the old three-way AND, every one of those 8 was excluded. So widening the scan **alone** would still have returned zero for `brogrammers`: the fix would have looked delivered and changed nothing. `defer_date` means *do not surface before this date*; reading its absence as *never surface* inverts it.

**Print the vault on every row.** Task titles are not namespaced across vaults — `Check Unassigned Tasks - 2026-09-21` exists in both `personal` and `octopusagent` as two different tasks, measured the same day. A table without a vault column collapses those into one row and opens the wrong one.


The count is a property of the day, not of the command — **never hardcode it and never treat a row count as a check**. Measured 2026-09-20: 79 tasks in `25 Tasks/` carried `flag: true` and the filter returned 12 at 10:45, then 11, 10 and 7 across the next two hours as workers completed their tasks. A shrinking set is the selector working, not the selector failing.

**No mode is derived, and no mode column is printed.** Every row opens in whatever `~/.config/claude-supervisor/config.json` `spawn.mode` says — `interactive` as of 2026-09-21, verified live. Print one line under the table: `All rows open <mode> — fleet config, not per-task.`

**Removed 2026-09-21 on the operator's instruction, with the reasoning kept because it generalises.** This command used to grep each task for `ssh`, `kubectl`, `make buca`, `make apply`, `gh pr merge`, `update-all.sh`, derive `interactive` on a match and `headless` otherwise, print the verdict with its reason — **and then discard it**, because `spawn_agent` is never passed a mode and the fleet config decides. A column showing a computation nobody consumes is the same defect as the dry run deleted in the section below: **a display shaped like a control.** It cost a reader's attention on every batch and decided nothing.

**The derivation was also weak on its own terms**, which is why it is not worth repairing in place. The keyword set detects *operationally dangerous*, which is a proxy for “worth watching” — not for “can run unattended”, which is the actual question headless asks. `Cleanup Email Inbox` scored `headless` because it names no infrastructure commands, not because it can run without a human. The real signal is whether the task raises questions mid-flight, and nothing here measured that.

✅ **Per-task mode came back on 2026-09-21 — as its own `mode:` field, never folded into `role:`.** The operator reinstated it that morning with the fallback rule that makes it safe: *unclear → interactive*, because a session that needs interaction and is headless is hard to manage. Step 0.6 decides and writes it.

⚠️ **Two fields, not one, and the reason is that `role` cannot answer the mode question.** Mode is *mechanism* (wezterm tab vs headless SDK); role is *who the session is for* (human / agent / manager). `SPAWN_MODES` contains no `human`. Check the values: `human` and `manager` both force `interactive` — a person needs a screen, a manager is a tab you jump to — while `agent` leaves the question genuinely open. So role **constrains** mode without **determining** it: a partial function, not the same axis. One field that looked like both is how they got conflated the first time.

**The asymmetry that justifies the fleet default staying `interactive`:** a wrongly-interactive task costs one idle tab; a wrongly-headless one burns a whole session on gates nobody answers — measured 2026-09-20, two workers stranded and four gates expired across three workers in ~90 minutes.

**No ordering is computed — but blocked rows are surfaced, and that is not the same thing.** The command never derives a sequence: it prints the stable sorted order and states plainly that ordering is not auto-detected. What it *does* read is the `blocked` / `blocked_by` pair the CLI already computes, and it **holds** every row the CLI reports `blocked=True` — mark the row `⛔ HOLD`, print its blockers, and skip it with a stated reason. Surfacing a field the CLI computed is display; inferring an order from prose would be the deferred work. Do not collapse the two.

Measured 2026-09-20: `Rebuild Nuke Cluster - 2026W38-sun` and `Complete Rsync Backups - 2026W38-sun` both report `blocked_by = None` — neither is blocked — so that pair has **no** machine-readable dependency and the command cannot and must not order them. The three shutdown tasks (`Turn off fire/hell/sun - 2026W38-sun`) DO carry `blocked_by`, and are held on that basis. The field is real and 19 tasks in `25 Tasks/` use it; it was absent data on the one pair, never missing support.

**Opening is the default — there is no dry run and no `--confirm`.** Print the table, then open every eligible row. Removed 2026-09-21 on the operator's instruction, and the reasoning is worth keeping: **a dry run is a warning, not a control.** It prevented nothing — it asked the reader to read a table and type the command a second time, and a skimmed table passed it silently. What replaces it is an actual gate.

## Step 0.6 — Classify and fill the missing `role` and `mode`

**A missing `role:` is a question to answer, not a row to drop.** Refusing every unroled row is what this section replaced on 2026-09-21. The strict default was right in principle and unsatisfiable in practice — measured that morning, **1 of 4281 task files in `25 Tasks/` carried a `role:` field at all**, and its value was `human`. Zero said `agent`. So the gate refused all ten flagged rows and the batch opened nothing — and would have done so every day, for every task, until the field appeared by some other means. A criterion nothing can satisfy is broken, not strict.

Resolve each selected row in this order:

1. **`role:` present** → use it. Never re-derive over a value already on disk; the field exists so the decision is made once.
2. **`role:` absent** → classify it now from the task's **body**, then write the result.
3. **Body gives no basis to decide** → refuse this row, write nothing, and say so. This is the one case that still stops — see the undecidable guard below.

**The classifying question is `role`'s own, and it is not the mode question.** Ask: *does finishing this task need the operator's own hands, their presence, or a judgement only they can make?* Yes → `human`. No → `agent`. It is **not** “is this dangerous”, which is what the deleted keyword grep measured, and **not** “can this run unattended”, which is the mode question this file refuses to fold in two sections above.

Three signals mean `human`, and each has to come from reading the body:

- **the physical world** — a tank to drain, a machine to power off, a device to hold
- **a GUI-only surface with no agent interface** — an app this session cannot drive at all
- **the operator's own approval IS the work** — a live trade, a sign-off, a decision reserved to them

Everything else is `agent`. A task naming `kubectl`, `ssh` or a deploy is still `agent`: operationally weighty is not the same as human-only, and conflating those two is precisely the error the keyword derivation made.

⚠️ **Classify from the BODY, never from the title or the frontmatter.** `Aquascape PWC` reaches this selector as flagged + `in_progress` + due today with no live session — the exact signature of unowned work the spawn mandate acts on — and its title, its frontmatter and its status all read like any other row. Measured 2026-09-20: it was caught **only** because a manager read the body and saw an aquarium water change. The title carries none of that; the first line of the body carries all of it.

### Then decide `mode` — and fall back to `interactive` whenever it is unclear

**`role` is resolved first, because it settles most of the mode question by itself:**

| resolved `role` | `mode` |
|---|---|
| `human` | **`interactive`, always.** A person needs a screen; there is nothing to decide. |
| `manager` | **`interactive`, always.** A manager is a long-lived tab the operator jumps to. |
| `agent` | the only open case — decide it below |

**For an `agent` row, write `headless` only on positive evidence that the task never needs a human mid-flight.** Everything else is `interactive`. This is not a tie-break, it is a floor: *unclear* is not a category that resolves to headless, and neither is *probably fine*.

⚠️ **The fallback is asymmetric on purpose, and the asymmetry is measured.** A wrongly-`interactive` task costs one idle tab the operator closes. A wrongly-`headless` one burns a whole session on gates nobody can answer — 2026-09-20: two workers stranded, four gates expired across three workers in ~90 minutes. The operator's own framing, 2026-09-21: *a session that needs interaction and is headless is hard to manage.* So the cost of guessing wrong is not symmetric, and the default must lean to the cheap failure.

**What positive evidence actually looks like** — the task runs a fixed procedure end to end, decides nothing a human would want to weigh in on, and has no approval step, no triage judgement, and no “ask if unsure” in its own body. **Expect this to be rare.** Most recurring work in this vault triages, files, or decides something: an inbox sweep judges what is actionable, an alert check judges what to silence. Those are `interactive`, and a classifier that finds many headless rows is mis-reading the bodies, not finding an optimisation.

⚠️ **Never derive mode from operational weight.** `kubectl`, `ssh`, `make apply` and `gh pr merge` say a task is *worth watching*, not that it *cannot run unattended* — and they say nothing at all about the reverse case, which is the dangerous one: `Cleanup Email Inbox` names no infrastructure command and still needs a human. That inverted proxy is exactly why the keyword derivation was deleted; do not rebuild it under a new field name.

**Write both values before opening anything:**

```bash
vault-cli --vault "<vault>" task set "<task>" role <human|agent>
vault-cli --vault "<vault>" task set "<task>" mode <interactive|headless>
```

Writing them is the point, not a side effect. The next batch reads the fields instead of re-deciding, so one task cannot be classified one way today and the other way tomorrow. It also makes the call reviewable — a wrong value is visible on disk and the operator corrects it with that same one-line command.

**`mode` is consumed, not merely recorded.** Step 3 passes `interactive=false` to `spawn_agent` when and only when the field reads `headless`; an absent field means the argument is omitted and the fleet config decides. A field this command writes and the spawn ignores would be the deleted mode column wearing a different hat.

⚠️ **A machine-written role is indistinguishable from an operator-written one on disk.** Nothing in the frontmatter records who decided. So **print every auto-filled row distinctly** (`🏷️ filled now`) instead of folding it into the opened count. That report line is the only provenance this decision has, and it exists for exactly one read — the first one, while objecting is still cheap.

⚠️ **The undecidable guard still refuses, and must not be softened into a default.** When the body genuinely does not say — an empty task, a title-only stub, a body that is nothing but links — do **not** guess and do **not** write the field. Print `⛔ REFUSED — role: undecidable (body carries no signal; not written)` and move on. **This applies to `role` only.** `mode` has a safe floor and therefore never blocks — an undecidable mode is `interactive`, which is the whole point of the fallback. Role has no such floor: there is no value that is safe to assume, because assuming `agent` is what spawns a worker onto an aquarium. Guessing here would reintroduce the Aquascape hazard with an extra step: the wrong guess would be *written to disk* and then trusted by every later run as though a human had made it. An absent field is honestly unknown; a wrong field is confidently wrong, and costs more to detect.

**A `role: human` row is never opened as a WORKER — it is opened into the Direct window instead.** Amended 2026-09-21, same session that wrote the skip: refusing to spawn an agent onto an aquarium water change is right, but *therefore open nothing* does not follow from it. Those two claims got conflated, and the result was a `Direct` window sitting empty while the only two rows it exists to hold were dropped on the floor.

Open a `human` row as **the operator's own session**, not a delegate:

- window → `Direct` (`~/.cache/wezterm-role-map.json` → `Direct.window_id`), never `Agents`
- chip → `cyan`, never `pink` — the colour is the fleet's only cue for which role a session plays
- prompt → the same `/vault-cli:work-on-task "<task>"`, because anchoring the task is useful to a human too

Print it as `→ DIRECT` rather than `⛔ SKIPPED`. The distinction that matters is **who drives the session**, not whether a session exists: a cyan tab in `Direct` with the task loaded is a workspace, and spawning a pink agent worker onto the same row would be the failure. One of those is a window assignment; the other is a delegation.

⚠️ **Some `human` rows are useful in a tab and some are not** — a water change gets nothing from a terminal. Do **not** invent a third category to sort them: open both, and let the operator close the one that does not help. A fourth bucket guessed from prose would be the keyword derivation again, wearing yet another hat.

```
FLAGGED BATCH — <vault> — <date> — <n> tasks
  #  task                                          role    mode
  1  Bot is Healthy - 2026W38-sun                  agent   interactive   🏷️ filled now
  2  Cleanup Email Inbox (Personal) - 2026-09-20   agent   interactive   (on disk)
  3  Aquascape PWC - 2026W39                       human   interactive   🏷️ filled now
     ⛔ SKIPPED — role: human, never opened as a worker
  4  Untitled Stub - 2026-09-21                    —       —
     ⛔ REFUSED — role: undecidable (body carries no signal; not written)
 10  Turn off fire - 2026W38-sun                   agent   interactive   (on disk)
     ⛔ HOLD — blocked by [[Complete Rsync Backups - 2026W38-sun]], [[Run update-all.sh - 2026W38-sun]]
  ...
  Rows with no `mode:` on disk open per fleet config (`spawn.mode`); only an
  explicit `headless` overrides it.

⚠️  Ordering is NOT auto-detected. Every ⛔ row is skipped;
    the rest open in the stable order printed above.

Opened N · filled F fields · skipped H (human) · refused U (undecidable) · held K (blocked).
```

**Readiness-gate every CREATE-bound row first, concurrently.** Before the loop, find the rows that would take Step 2A's CREATE branch — `role: agent`, no live pane, no `claude_session_id` — and dispatch Step 1.5's readiness sub-agent for **all of them in ONE message** (one `Agent` call per row, parallel). Resume/jump-bound rows, `role: human` (→ DIRECT) rows, and `task_type:` pipeline rows (§ Step 1.5) are not gated. Add a `readiness` column to the table: `✅ ready 9/10`, `⛔ NOT READY 7/10` with the gap bullets under the row, or `— (resume/jump/direct)`. A not-ready row is **held, never opened**, and counted in the footer: `… · not ready R (readiness)`. The per-row `/supervisor:open` below reuses this verdict — it does not re-gate a row the batch already gated in this run.

**Loop the eligible rows in the printed stable order** and delegate each to this command's own single-task path — the exact Step 2A resolution (live pane → JUMP, `claude_session_id` → RESUME, else CREATE + spawn). Invoke `/supervisor:open "<task>"` per row; never reimplement jump / resume / spawn here, and never pass a mode override to the fleet. The row's `role` is now on disk, so § 3.0 reads it back from frontmatter like any other task — the batch does not hand it forward in memory, which is what makes the write in Step 0.6 load-bearing rather than bookkeeping.

**Skip every `⛔ HOLD` row and report the skip with its blockers** — a blocked task is one whose prerequisites have not finished, so opening it is the failure the batch exists to prevent. Never open a blocked row because it appeared in the table: appearing in the selector is not the same as having its prerequisites met. Report each row's outcome, and every skip with its stated reason.

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

**Use `find -iname`, never a shell glob.** `ls "<topics_dir>/"*"<name>"*.md` is case-sensitive under zsh, so a lowercase argument matches nothing against Title Case filenames — verified 2026-09-12: `*"discord"*.md` returned `no matches found` while `*"Discord"*.md` returned 5 files.

Then:

- **exactly one match** → act on it, and say which type matched
- **more than one** → print each match with its type and STOP; tell the operator to re-run with `--task` / `--goal` / `--topic`. Goal and task titles are not namespaced in these vaults, so collisions are real — never guess, and never silently prefer one type
- **no match** → print the closest candidates (`vault-cli task search` / `goal search`) and STOP. Never create anything from an unmatched name

## Step 1.5 — Readiness gate (CREATE branch only)

**The single home of `/supervisor:open`'s spawn precondition.** Step 2A's CREATE branch and the `--flagged` batch (Step 0.6) both reference this section; neither restates it. Added 2026-09-23 after `/supervisor:open --flagged` spawned 10 workers with no pre-spawn audit and 6 of 10 sat on a planning question within 6 minutes — the fleet manager already gated its own spawn sites at task-auditor 9/10 ([[Every Spawn Site Carries the Readiness Precondition at One Bar]]); `/supervisor:open` was the uncovered site.

**Scope: CREATE only.** JUMP and RESUME are never gated — a live or resumable session already exists, and gating it would strand work mid-flight. `role: human` rows opened into Direct are not gated either — the operator drives them. **Rows carrying a `task_type:` frontmatter field are exempt** — pipeline-emitted tasks (e.g. `sentry-issue-analyzer`) take their agent contract from `task_type`, not from page sections, so the auditor refuses the whole class for lacking `# Success Criteria` / `# Tasks` (measured 2026-09-23: 3/10). Show `— (pipeline contract)` in the readiness column. Decided by the operator 2026-09-23.

**The probe is read-only by construction.** Do NOT call `/vault-cli:plan-task --non-interactive` here: its entry contract flips `next`/`backlog` → `in_progress`+`planning` and it can write template-verdict files, so a gate calling it would mutate every row it inspects. Dispatch one sub-agent per task:

```
Agent(subagent_type='vault-cli:task-auditor', description='Readiness: <task>',
  prompt='Audit <absolute task path>. READ-ONLY readiness probe for a spawn gate: never edit, never ask. Return your normal report, then additionally check these hard gates and list each failure: (1) # Success Criteria with ≥2 binary checkboxes; (2) # Tasks lists concrete steps that reach the SC; (3) shipping-class tasks carry an e2e verify subtask naming a procedure AND an expected result; (4) every # Tasks item maps to an SC or is the verify subtask; (5) any push/deploy/credential names its target system and account. The ready bar is score ≥ 9/10 AND zero hard-gate failures — NOT plan-task's 8. End with exactly one line: READINESS: ready <score>/10 | READINESS: not-ready <score>/10 — <gap>; <gap> (a score below 9 is always not-ready; name "score <n> below 9" as a gap). Never run a filesystem-wide find.')
```

**Bar: ready = auditor score ≥ 9 AND no hard-gate failure.** 9/10 is the fleet manager's bar, not plan-task's current 8 — raising plan-task to match is [[Raise Plan-Task's Plan-Ready Gate From 8 to the 9-10 Readiness Bar]]. A missing or unparseable `READINESS:` line is **not-ready** (a probe that did not answer has not cleared the gate). **Parse the score, never trust the label**: the gate decides `ready` from `<score> ≥ 9` plus the hard-gate list itself. Measured 2026-09-23 on the first real run: both probes ended `READINESS: ready 8/10` — the auditor applied plan-task's 8 — and a label-trusting gate would have spawned both.

**Not ready → do not spawn.** Print `⛔ NOT READY <score>/10 — <task>` and the gap bullets, and point at `/vault-cli:plan-task "<task>"` to fix them. Nothing is written to the task.

⚠️ **A readiness string in this file is not a gate.** The 2026-09-21 predecessor recorded the trap: a file-level grep for the reference satisfies a naive check while individual sites stay ungated. Each site (CREATE branch, batch dispatch) links here by name — check each one's own window.

## Step 2A — Task branch

**Probe for a live session first.**

```bash
PRIOR_SID="$(vault-cli --vault "<vault>" task get "<task>" claude_session_id 2>/dev/null)"
wezterm cli list
```

**Read the full pane list — never `| tail -N` or `| head -N`.** Panes are unordered, so a truncated view drops live sessions silently and a clean result is indistinguishable from a missed one. Observed 2026-09-11: `wezterm cli list | tail -8` hid pane `1247`, already running the target task, and a duplicate tab was spawned onto the same session. Match the task title against the TAB TITLE column.

⚠️ **Identify your own pane first — `$WEZTERM_PANE` — before attributing any title to a sibling.** `wezterm cli list` marks no pane as yours, so your own tab's title reads exactly like a peer's — and the jump branch below carries the same trap: if your own tab carries the task title, you jump to yourself. Observed 2026-09-20: a session read its own pane's title off this listing and concluded a third-party session owned a goal page; the false premise drove a recommendation the operator then acted on, and only a peer's interruption caught it.

**Also probe for a headless turn — a pane list cannot see one.** `vault-cli task work-on --mode headless` holds a `claude_session_id` while appearing in **no pane at all**, so the branches below read "no live session" and spawn a second writer onto a session a `--print` turn is mid-write on. Check before choosing a branch:

```bash
pgrep -fl "<PRIOR_SID>" || echo "no process holding this session"
```

A live pid → **wait for it to exit, then resume** (Step 3). JUMP is unavailable — there is no pane to activate. Observed 2026-09-13: `wezterm cli list` matched **0** panes for the task while pids `33052`/`33055` ran `cc-personal-deepseek --print … --session-id <sid>`; the branch logic below would have spawned onto it, putting two writers on one conversation.

Then take exactly one branch:

1. **Live pane exists → JUMP.** Do not spawn; a second tab on one session is two views of one conversation, not two workers.

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump.py pane:<pane-id>
   ```

   Report `↪️ jumped to <task> — pane <pane-id>, window <window-id>`.

   **Jump via `/supervisor:jump`, never a raw `wezterm cli activate-tab` line** — same rule every manager command now carries. `activate-tab` **cannot cross windows**: called from another window it succeeds and nothing visibly moves, so a silent no-op reads as a successful jump. A tab id is also **renumbered when its tab moves windows**, so a handed-over `--tab-id` goes dead within the hour (2026-09-18: three spawned workers routed as tabs 158/159/160 in window 0 became 163/164/165 in window 2, and `activate-tab --tab-id 159` failed outright with *"could not determine which pane should be active"* while `activate-pane --pane-id 239` worked immediately). `/supervisor:jump` resolves the coordinate at run time from `wezterm cli list`, prints the window id so a cross-window no-op is legible, and takes a **pane id** — the more stable of the two handles. Neither namespace survives a WezTerm restart, so re-resolve after one.
2. **No live pane, `PRIOR_SID` present → RESUME** in a new tab (Step 3).
3. **Neither → CREATE**, then resume in a new tab — **only after Step 1.5's readiness gate returns ready** (or the `--flagged` batch already gated this row ready in this run). Not ready → print the gaps and STOP; no spawn.

   **The caller mints nothing.** Spawn via Step 3's create branch — which hands the work prompt to the worker as an *argument*, so the **worker runs its own planning turn and creates its own session**. The caller returns as soon as the spawn returns; it never runs a planning turn, never waits on one, and never reads a `session_id` out of it. A session id is not needed to start the worker: the worker is the thing that creates it.

   That is the whole difference from this branch's former shape, which pre-minted the session with an inline headless work-on call in the *caller*. Two writers on one conversation was the mild failure; the measured one was a caller-side planning turn that outlived its own timeout (exit 124, empty output, orphaned pids, ~7 min blocked) with the `session_id` it was supposed to return never arriving.

   Step 3 carries the recipe and its rationale — read it there rather than restating it here. Once the spawn returns, continue with Step 4 (verify + report) and Step 4.1 (bind the task explicitly).

## Step 2B — Goal branch

The goal branch **resolves and delegates**; it reimplements nothing. A worker tab is titled with a *task*, never a goal, so there is no pane to probe at goal level.

1. List the goal's tasks — every task whose `goals:` frontmatter names this goal. **Frontmatter only.** A body-text `[[Goal]]` mention is not membership: grepping `[[goal]]` file-wide over one topic's five member goals returned 39 tasks where frontmatter returns 21, and two of the extras were `in_progress` under entirely different goals.
2. Then:
   - **one non-terminal task** → run the task branch (Step 2A) on it
   - **several** → list them with status/phase and ask which; never pick for the operator
   - **none** → the goal has nothing executable. Report that and suggest `/vault-cli:work-on-goal "<goal>"`, which is the planning path that creates tasks. Do not spawn anything

## Step 2C — Topic branch

1. Resolve the topic's manager from the **session registry**, not from `ListAgents` — `~/.claude/sessions/<pid>.json` carries `sessionId`, a live `status`, the current `name`, and **`formerNames`**, the names the session has held since it started.

   ```bash
   python3 - <<'EOF'
   import json, glob, os
   TOPIC = "<topic>"
   want = (TOPIC + " Manager").lower()
   for p in glob.glob(os.path.expanduser('~/.claude/sessions/*.json')):
       d = json.load(open(p))
       names = [d.get('name') or ''] + [f.get('name') or '' for f in d.get('formerNames') or []]
       if any(n.lower() == want for n in names):
           print(d['sessionId'], '|', d.get('status'), '|', d.get('name'))
   EOF
   ```

   **Match the topic against `name` AND `formerNames`.** Matching only the current name is the hole this closes. A topic manager is long-lived and gets renamed; on a name-only match `/supervisor:open` concludes there is none and spawns a **second** manager onto a live topic, neither aware of the other — observed 2026-09-18 (duplicate Sentry manager, killed after ~2 min), reasoning from a colour signal that had itself gone stale. The registry keeps every name the session has held, so a renamed manager stays findable under the name it was spawned with. `sessionId` is the identity — stable across `/rename`, verified 2026-09-18 on three sessions whose `formerNames` recorded superseded names while `sessionId` stayed constant through all of them — and `status` is live, so a hit cannot resume a dead conversation.

   ⚠️ **The registry lists live sessions only**, like `ListAgents`: an exited session's entry is removed and its `formerNames` with it. That is the right scope here (the failure being prevented is a duplicate spawn against a *running* manager), but do not read an empty result as "this topic never had a manager" — only as "none is running now".

   ⚠️ **Do not key this on a `claude_session_id` stamp on the topic page.** Topic pages carry no such field today (0/6 in Personal) and adding one would be worse than the gap: a topic manager restarts constantly, so a written-once stamp goes stale, and a stale stamp is authoritative-looking — `/supervisor:open` would confidently resume a dead conversation, where the current name-match at least fails loudly by spawning.
2. **Live → JUMP** to it, same `/supervisor:jump pane:<pane-id>` + window-id reporting as Step 2A.
3. **None → spawn a manager** in a new tab:

   ```bash
   wezterm cli spawn -- bash -lc 'unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; exec "<claude_script>" -n "<topic> Manager" "/color orange"'
   ```

   Same `unset` prefix as Step 3.1 — without it the manager comes up named after the session that spawned it. The colour rides as the spawn prompt, per the recipe. Then tell the operator to run `/supervisor:manager-loop "<topic>"` in it. The manager loop is stateful and this command is not; `/supervisor:open` starts the session, the manager arms its own loop.

   **The colour is a role signal, not decoration** — a topic manager comes up `orange` so it can never be read as a worker, which stays `pink` (Step 3.1 and the `--color` default). On 2026-09-18 the two were both `pink`, and an operator instruction to "relaunch every pink worker" nearly relaunched a manager as a worker; reasoning from the same signal, the Fleet Manager then drafted a duplicate manager onto a live topic. The colour is the only fleet-wide cue for which role a session plays — keep the two distinct.

## Step 3 — Spawn the tab (resume/create branches)

Resolve the vault's launcher and dir from config — **never hardcode them**:

```bash
vault-cli config list --output json      # → claude_script, session_project_dir
```

Effective dir = `--dir` when given, else the vault's `session_project_dir`, else no `cd`.

⚠️ **Pass `role`, not `window_id`.** `spawn_agent` takes an optional `role` (`manager` / `agent` / `human`, defaulting to `agent`) and resolves **both** the colour and the window from the published map **in-process** (`bborbe/claude-supervisor#67`, released `v0.22.0`), so pass § 3.0's resolved role here. The older `window_id` argument still works and still wins when passed, but it is no longer how a role is routed: a window id has to cross the MCP tool boundary, and `window_id: 0` did not survive that crossing reliably — measured 2026-09-20, it reached the server 4 times in 6 and silently inherited the caller's window the rest, with both failures a run's first spawn and no reproducible trigger found. A `role` is a word, so it cannot be dropped that way, and the id is looked up at the moment of spawn. **Verified 2026-09-20 after release, each from a real spawn passing a role and NO window id:** `manager` → window 0 / orange, `agent` → window 2 / pink, `human` → window 1 / cyan. The response reports the resolved `role` and `window_id`, so **check those fields** rather than inferring routing from where the tab landed.

**Create branch — `spawn_agent`, which takes the work prompt as an *argument*.** The task never goes over keystrokes. The call shape lives once, in [`docs/fleet-surface.md` § Spawn a worker](../docs/fleet-surface.md#spawn-a-worker) (path A) — follow it, with these values: prompt `/vault-cli:work-on-task "<task>"`, cwd `<dir>`, label `<task>`, role `<role>` (§ 3.0).

**Do NOT pass `interactive` here — unless the task file itself declares `mode:`. Otherwise the fleet default decides, and it lives in a file.** The server resolves `SUPERVISOR_SPAWN_MODE`, then `spawn.mode` in `~/.config/claude-supervisor/config.json`, then its built-in `interactive`. Passing the argument in this file is exactly what made the config unreachable before: on 2026-09-18 every manager hardcoded `interactive=false`, so changing the fleet's mode meant editing N command files and course-correcting every manager already running — three kills, seven more workers found under two other managers, and a WezTerm restart. Omit it and the operator changes the whole fleet with one edit. The spawn response reports `mode_source` (`argument`/`env`/`config`/`default`) if you need to know which source decided.

**The one carve-out: pass `interactive=false` when, and only when, the task's own frontmatter says `mode: headless`.** An absent `mode:` still means *omit the argument entirely* — not *pass interactive* — so the config keeps deciding for every task that has not opted out. That is what preserves the one-place-to-change property while letting a single task override it. A `mode:` the spawn never reads would be the same defect as the deleted column: **a display shaped like a control.** If this file writes the field, this file must also honour it.

⚠️ **Writing `mode: headless` today opens a worker into a mechanism with two known, measured defects** — its gates park with the spawning session's server and no one else can answer them, and the resume guard does not guard (a mid-turn headless worker can be resumed, putting two writers on one conversation). That is the standing argument for the interactive fallback being aggressive rather than polite. The operator's own position, recorded in the open-items ledger: headless becomes useful *at the point that permission prompts and questions can be answered remotely*. Until then, `headless` should be rare and never a guess.

**Parity is real, not a compromise — measured 2026-09-18.** A headless worker spawned into the Brogrammers vault had `mcp__semantic-search__search_related` and `mcp__obsidian__obsidian_get_file_contents` available and returned a real vault hit (`70 Runbooks/Manager Session.md`). The code is built for it: `settingSources: ['user','project','local']` — the same an interactive session gets — and `resolveMcpServers(launcher)` parses the launcher's `--mcp-config`, so a headless worker inherits the same MCP servers a tab does. (The `spawn_agent` tool description used to claim headless meant "accepting the narrower toolchain"; that was stale and is corrected as of the config-file change.)

**Do NOT add `policy` while `defaultMode` is `auto`.** The per-vault overlays (`~/.claude/supervisor-policy-<vault>.json`) exist, but `auto` is in `POLICY_UNREACHABLE_MODES`, so `spawn_agent` **refuses** a named policy outright — *"policy would never be consulted"* — and the spawn fails. Omit it. The overlay becomes usable only if `defaultMode` moves to `acceptEdits`, the quietest mode in which the policy stays reachable: [[Supervisor - Install and Operate]] § Troubleshooting.

**`interactive` is the debug escape hatch, and it works in both directions.** Pass `interactive=true` when a worker's own screen must be watched live — a `/color pink` tab, prompts answered in that tab, this session unable to help. Pass `interactive=false` to force one headless worker while the fleet runs in tabs. Either way it overrides the fleet default for that one spawn only. The colour applies only on the tab path; a headless spawn returns no `color` field because it has no tab.

**The fleet's current mode is not recorded here — read the file.** This paragraph flipped twice on 2026-09-18 (tabs → headless → tabs), which is the argument for the config: a decision that changes lives in one place the operator edits, not in prose that every manager must re-read. `cat ~/.config/claude-supervisor/config.json` is the answer; the operator's standing decision as of 2026-09-18 is `interactive`.

**Why interactive, in the operator's own words** (2026-09-18, relayed via the Fleet Manager session rather than heard in this one — treat the wording as reported, the substance as settled): *"Currently we start all sessions in interactive mode to have better debugability. In the long run we want to run them in the background but for this we have to work floors"* — "floors" is the transcript verbatim; reading it as *workflows* is an inference, not the operator's word. So headless is the **destination, not the current default**, and what gates it is that groundwork — not a doubt about parity, which was measured. Do not re-argue the default from first principles here; change the file when the workflows are ready.

Requires `mcp__supervisor__*` in **this** session — see the recipe's precondition, which fails silently.

**Fallback when `mcp__supervisor__*` is unavailable.** Every `cc-*` launcher passes `--strict-mcp-config`, which turns its config file into an allowlist — plugin-provided servers are never loaded, so a vault whose `~/.claude/mcp-obsidian-<vault>.json` does not itself name `supervisor` carries no supervisor tools at all. Then spawn the wezterm way and send the work command afterwards.

⚠️ **The fallback cannot produce a headless worker — a wezterm spawn is a tab by construction.** On such a vault the worker's prompts go to its own pane and this session cannot answer them, so "the operator never visits a worker tab" is not achievable there. `supervisor` is named in the **personal** and **brogrammers** launcher configs only; the other vault configs do not have it. If you are opening into a vault whose workers must be managed, add the entry there first — [[Supervisor - Install and Operate]] § Step 2.

### 3.0 Resolve the session's role → window + colour

**Before either spawn path**, resolve the role from the task's frontmatter and read the window/chip pair from the role map. This is what stops a human-only task coming up as a pink agent tab in whatever window the caller happened to be in — the mis-signal measured 2026-09-20 on `Start Day`.

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

⚠️ **If `$MAP` is missing or `WINDOW_ID` comes back empty, fall back to today's behaviour and SAY SO in the report** — spawn into the caller's window with `pink`, and note that the role map is not deployed (`dotfiles` PR #8 ships the export; it publishes on WezTerm's reconcile tick). Do **not** silently proceed as if routing worked: a spawn that looks routed and is not is worse than one that plainly is not.

Then pass the pair to whichever spawn path runs:

- **wezterm path (§ 3.1 / resume):** add `--window-id "$WINDOW_ID"` to the `wezterm cli spawn` call when it is non-empty, and replace the hardcoded `"/color pink"` with `"/color $CHIP"`.
- **`spawn_agent` path (§ 3):** pass `role` — the server resolves the window **and** the colour from the published map in-process, so this path needs no `--window-id` and no separate colour step. It reports the resolved `role` and `window_id` back, so check those rather than the landing window alone.

### 3.1 Spawn — the wezterm path (fallback, and always the resume branch)

```bash
wezterm cli spawn ${WINDOW_ID:+--window-id "$WINDOW_ID"} -- bash -lc 'unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; exec "<claude_script>" --resume <session_id> -n "<title>" "/color '"$CHIP"'"'
```

**`--window-id` is what puts the tab in the right window** — without it the new tab inherits `WEZTERM_PANE` from the calling session (see the note at the end of this step), which is exactly how a human-only task landed in the Agents window. `${WINDOW_ID:+...}` keeps the flag off entirely when the map is unavailable, so the fallback degrades to the old behaviour rather than passing an empty `--window-id`.

**Order matters here: pass `/color` as the spawn prompt → send the work command after.** The colour is the *only* thing that belongs in this spawn's prompt argument: it runs in ~1s and leaves the session idle at its prompt, ready for the real command. Do NOT pass the work prompt there — a session spawned with it is busy from its first instant, and anything sent afterwards queues behind it instead of submitting.

The trailing `"/color $CHIP"` is the initial prompt — Claude executes it at startup and reports `Session color set to: <chip>`, then idles. `$CHIP` is § 3.0's role-resolved value, so the colour follows the role rather than a fixed default. Override the colour with `--color <name>`; valid: `red, blue, green, yellow, purple, orange, pink, cyan, default`.

**The resume branch always uses this path, never `spawn_agent`.** `spawn_agent` with a `resume` argument refuses a session whose liveness **cannot be determined** — and `/supervisor:open` resumes precisely the sessions its own probe could not classify as live (Step 2A). That refusal is correct behaviour, but it would block the resume rather than route it.

**This shape's single home is `claude-supervisor/docs/fleet-surface.md` § Spawn a worker** — which [[Supervisor - Install and Operate]] § Step 4 itself names as the recipe's home, and which does not restate the shape. This block is the call site, not the recipe. Read that section before changing the shape or its rationale; every call site (here, and both `/supervisor:manager-loop` copies) must match it.

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

**Send the text and the submit as two steps.** A trailing `\r` appended to the text is not sufficient — measured 2026-09-16 on two panes, the message wrapped to two lines and the `\r` was swallowed, leaving the text sitting **unsubmitted** in the input box, which reads exactly like a delivered prompt. Send the text, then send a bare `\r` (`wezterm cli send-text --pane-id <N> --no-paste $'\r'`) and **repeat it until the composer clears** — measured twice, the second relay needed **three** Enters; extra Enters on an empty composer are harmless. **Read the pane back after sending** — a prompt is not delivered until the composer is empty *and* the session is visibly working. (A bare `\n` alone likewise leaves the text unsubmitted.) ⚠️ **The `sleep` is a race** — on a loaded machine the session may not have reached its prompt, and a fixed delay cannot tell. The supervisor avoids it by waiting for the `❯` glyph instead. Hardening this path (glyph-wait + a submission check rather than an assumed `\r`) is `[[Topic Manager Spawn Shapes Omit the Mandatory unset Prefix]]`'s SC4, not this file's.

**Why the colour goes in the spawn and the work command does not.** Measured 2026-09-15: `<launcher> -n "test" "/color pink"` executed at startup and returned `Session color set to: pink` with the session then idle — one argument, no send-text, no timing risk. The reverse pairing fails: a pane spawned with `/vault-cli:work-on-task …` as its prompt was busy immediately, and a colour line sent afterwards sat unsubmitted until a follow-up bare `\r`. Confirm `Session color set to: <name>` is on screen before sending the work command.

⚠️ **Only ever send this into a pane THIS command just spawned, while it is still idle.** Typing into a pane that is mid-turn lands the text in whatever prompt or menu is open (observed: a `/rename` went into a selection menu and had to be escaped out). This is not a licence to drive other sessions by `send-text` — answering another session's operator gate that way is still forbidden (see `/supervisor:manager-loop` guardrails); configuring a session you just created is not the same act.

The `cc-*` launchers `cd` into their own vault before starting Claude, so a vault with no `session_project_dir` needs no `cd` at all. The new tab inherits `WEZTERM_PANE` from the calling session, so it spawns into the current window.

## Step 4 — Verify and report

- `wezterm cli list` shows the new tab (spawn branches), or the jump target was activated (jump branch)
- the task's `claude_session_id` matches the session now running
- **the spawned session owns its name** — check the record, do not trust the tab title alone:

  ```bash
  python3 -c "
  import json,glob
  for f in glob.glob('$HOME/.claude/sessions/*.json'):
      d=json.load(open(f))
      if str(d.get('sessionId','')).startswith('<sid-prefix>'):
          print(d.get('name'), d.get('nameSource'))"
  ```

  `nameSource: user` → correct. `nameSource: peer` → the `unset` prefix was missing or ineffective; the session is named after its spawner and the title-match session-connect cannot resolve it (see below).
- state the branch taken in one line: `↪️ jumped` / `♻️ resumed <sid-prefix>` / `🆕 created <sid-prefix>` — plus the resolved type **and the resolved vault**, so a mis-resolution is visible immediately

### 4.1 Bind the task explicitly — `-n` does not self-link

**`-n` sets the session record's `name` but writes NO `custom-title` line into the transcript.** Verified 2026-09-15: a session spawned with `-n "Complete Kafka Restore"` had `name=Complete Kafka Restore nameSource=user` in `~/.claude/sessions/<pid>.json` and **zero** `"type":"custom-title"` lines in its `.jsonl`. Since `work-on-task`'s session-connect resolves by scanning transcripts for `customTitle`, it finds nothing, reports `not connected — 0 matching session(s)`, and the task stays unlinked.

So after spawning, read the uuid from the session record and write it yourself:

```bash
python3 -c "
import json,glob
for f in glob.glob('$HOME/.claude/sessions/*.json'):
    d=json.load(open(f))
    if d.get('name')=='<task>' and d.get('status') in ('busy','idle'):
        print(d['sessionId'])"
vault-cli task set "<task>" claude_session_id "<uuid>" --vault <vault>
```

⚠️ **Never reconstruct a uuid from a prefix.** Session listings are often truncated to 8 chars; the remaining 28 are not guessable and a fabricated uuid writes a link to a session that does not exist. Read the full value from the record every time.

⚠️ **A wrong name silently breaks task↔session linkage.** `work-on-task`'s session-connect resolves the current session by matching the task name against each transcript's `customTitle`, and writes `claude_session_id` only when **exactly one** uuid matches. Several sessions sharing one inherited name match zero-or-many, so it refuses to guess and the task is left unlinked — or worse, still pointing at a dead predecessor. Observed 2026-09-15: two of three spawned workers ran correctly but their tasks kept stale ids from a killed job, and had to be rebound by hand with `vault-cli task set "<task>" claude_session_id "<uuid>"`. The naming defect and the linkage defect are the same defect.

## Notes

- Managers (`/supervisor:manager-loop`, `/supervisor:fleet-loop`) *recommend* opening sessions; this command is the executor — run it when the human approves.
- **Global since 2026-09-15** (moved from `Personal/.claude/commands/open.md`). The vault is now resolved at runtime instead of defaulting to `personal`; `topics_dir`, `claude_script` and `session_project_dir` all come from that vault's config entry. The Personal vault keeps its own topic pages, so `/supervisor:open <topic>` still works there — and now works in Brogrammers for task/goal, which the vault-local copy could not reach.
- **Readiness gate measured 2026-09-23** (first real `/supervisor:open --flagged`, run from the Fleet Manager session at ~14:20): 4 rows; 2 live → JUMP, ungated; 2 CREATE-bound Personal rows gated concurrently, both HELD at 8/10 with all 5 hard gates passing. The probes' own label read `ready 8/10`, so § Step 1.5 now pins the bar in the prompt and parses the score. No recurring row was CREATE-bound in that run, so the recurring-passes claim is still unmeasured.
- Replaces `.claude/commands/open-session.md`, removed 2026-09-13. The old command was task-only and its pane probe ended in STOP; the jump branch and the goal/topic resolution are the additions.
- **Renaming a command? Grep all four roots, not just the vault.** The 2026-09-13 rename swept the vault tree and missed `~/.claude/commands/fleet-loop.md`, which kept telling the fleet manager to spawn via a name that no longer existed — the exact breakage the rename was meant to prevent, one directory outside where the grep looked. Caught by the Fleet Manager session, not by the sweep. The full path list:

  ```bash
  grep -rln "<old-name>" \
    ~/Documents/Obsidian/Personal/.claude \
    ~/Documents/Obsidian/Personal/65\ Runbooks \
    ~/.claude/commands ~/.claude/plugins/cache
  ```

  The mirror-image failure is just as easy: reading the generated `CLAUDE.md` shows *that* a string is stale but never *where it lives* — the text is in `.claude/claude-md-rules/`, and a regeneration carries stale sources forward unchanged while looking like a fix. Two sibling sessions hit that side of it the same day.
- **Moved into the supervisor plugin 2026-09-23** (from `~/.claude/commands/open.md`); invoked as `/supervisor:open`. The spawn call shape is referenced from `docs/fleet-surface.md` § Spawn a worker, not restated.
