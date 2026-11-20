---
description: Open the right thing for a name — resolves it to a task, goal or topic, then hands the row to the wrapper's open endpoint, which jumps to a live session, resumes a recorded one, or starts a new one. Batch mode opens every task flagged for today. Name via $ARGUMENTS; optional --task/--goal/--topic, --vault, --color, --flagged, --first-step.
argument-hint: "<name> [--task|--goal|--topic] [--vault <v>] [--color <c>] [--flagged] [--first-step <text>]"
allowed-tools:
  - Bash(vault-cli:*)
  - Bash(python3:*)
  - Bash(grep:*)
  - Bash(find:*)
  - Bash(awk:*)
  - Read
  - TodoWrite
  - Agent
  - mcp__supervisor__open_rows
---

Open whatever a name refers to, in one verb: **resolve the name, send rows, render verdicts.**

This command is a **thin caller**. It resolves *which page* a name means and hands the row to the
wrapper's open endpoint. Every routing decision is the endpoint's:

- role → mode resolution, and the write-back of a defaulted `role` / absent `mode`;
- the answer-path probe on headless rows;
- liveness, the jump / live / resume / create branch;
- headless resume and create (the in-process spawn), the spawn caps, the start rate, the
  terminal-task guard;
- tab rows: the `Direct` window for `human`, `Managers` for `manager`, the chip, the launcher;
- the topic ceiling, the manager recipe, the start commands and their ready waits;
- every refusal.

The contract — request shape, verdict fields, decision order, refusal tokens — is
`claude-wrapper/docs/open-endpoint.md`. It is authoritative; this file never restates it.

```
/supervisor:open <name>
  ├─ resolve: which vault, which kind (task | goal | topic)  ← this command's whole job
  ├─ one row  → POST /api/1.0/open  (or MCP open_rows)
  └─ render the verdict: jump · live · resume · create · yours · refused

/supervisor:open --flagged → select the operator-flagged rows for today (all vaults),
                             apply the pre-call holds, send them as ONE request,
                             render one verdict per row.
```

⚠️ **Never spawn, never probe liveness, never resolve a window or a chip, never run the
answer-path check, never classify `role`/`mode` from a task body, never call a launcher or
ceiling script.** The endpoint owns all of it and re-decides it on every call; a second
implementation here is the bug this command was rewritten to remove. If a verdict disagrees
with what you expected, the endpoint is the authority — report the verdict, do not re-derive it.

## Arguments

**Parse `$ARGUMENTS` yourself — never use positional argument references** — a title is
multi-word, so a positional resolves to one word of it.

- **name (required):** the task / goal / topic title or identifier — everything that is not a
  recognised flag. May contain spaces; never word-split it, and quote it when passing it on.
- **`--task` / `--goal` / `--topic` (optional):** force the kind, skipping resolution (Step 1).
- **`--vault <name>` (optional):** target vault. Overrides cwd detection (Step 0). With
  `--flagged` it *narrows* the scan to one vault — the default there is **every** vault
  declaring a `tasks_dir`.
- **`--color <name>` (optional, accepted, NOT forwarded):** the chip is resolved by the endpoint
  from the row's role and the vault family. The request contract has no colour field, so a
  `--color` value is reported back to the operator as ignored, never sent.
- **`--first-step <text>` (optional):** forwarded verbatim as the row's `first_step`. The
  endpoint types it in front of the work command, separated by a blank line.
- **`--flagged` (optional):** batch mode — select every operator-flagged task due today, apply
  the pre-call holds (Step B), and send **all eligible rows in ONE request**. Takes no name;
  skip Steps 1–3 and run Step B. There is no `--confirm`: opening is the default.

If no name survives parsing **and `--flagged` was not given**, print
`❌ Pass a task, goal or topic name.` and STOP.

## Step 0 — Resolve the vault

⚠️ **Single-name path only. `--flagged` does NOT use it** — the batch scans every vault with a
`tasks_dir` (Step B), so resolving one vault from cwd would narrow the scan.

**Never hardcode a vault.** Precedence: `--vault` → the vault whose `path` contains the cwd →
`default_vault` from config. **If both resolver arms fail, STOP.**

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

**The `cwd==p` arm is load-bearing, not defensive** — a vault-root session sits *at* the root,
where the `+os.sep` form matches nothing.

**`is_default` is not a field.** `default_vault` lives at the config's top level, not inside a
vault entry — `config list --output json` returns entries with no such key. `vault-cli config`
exposes only `current-user` and `list`; there is no `config get`.

The vault name is **not validated here** — an unconfigured name comes back from the endpoint as
`row-unreadable` or `missing-row` for that row, which is the refusal the operator sees.

## Step 1 — Resolve the kind

Skip when `--task` / `--goal` / `--topic` was given. Otherwise probe all three and report which
matched:

```bash
TOPICS_DIR=$(vault-cli config list --output json | python3 -c '
import json, sys
data = json.load(sys.stdin)
vaults = data if isinstance(data, list) else data.get("vaults", [])
match = [v for v in vaults if v.get("name") == "<vault>"]
print((match[0].get("topics_dir") if match else "") or "23 Topics")')
vault-cli --vault "<vault>" task get "<name>" status  2>/dev/null
vault-cli --vault "<vault>" goal get "<name>" status  2>/dev/null
NAME_PAT=$(python3 -c 'import sys;n=sys.argv[1];print("".join("\\"+c if c in "*?[]" else c for c in n))' "<name>")
find "$TOPICS_DIR" -maxdepth 1 -iname "*$NAME_PAT*.md"
```

`TOPICS_DIR` is the vault's configured value, defaulting to `23 Topics` — read from the config
rather than assumed, because a vault that keeps its topics elsewhere would otherwise probe a
directory that does not exist and report the name as matching no kind. For the topic probe,
confirm `page_type: topic` **inside the frontmatter block**, not anywhere in the file:

```bash
awk '/^---$/{n++; next} n==1' "<page>" | grep -q '^page_type: topic'
```

**Escape the glob metacharacters in the name first.** `find -iname` takes a *pattern*, so a
title containing `*`, `?` or `[` changes the match set — `Fix [bug]` reads `[bug]` as a
character class, so the intended page silently fails to match and the probe reports the name
as matching no kind. `NAME_PAT` above backslash-escapes those three characters.

**Use `find -iname`, never a shell glob.** `ls "<topics_dir>/"*"<name>"*.md` is case-sensitive
under zsh, so a lowercase argument matches nothing against Title Case filenames.

Then:

- **exactly one match** → that is the row's `kind`. Say which type matched.
- **more than one** → print each match with its type and STOP; tell the operator to re-run with
  `--task` / `--goal` / `--topic`. Goal and task titles are not namespaced in these vaults, so
  collisions are real — never guess, and never silently prefer one type.
- **no match** → print the closest candidates (`vault-cli task search` / `goal search`) and STOP.
  Never create anything from an unmatched name.

## Step 2 — Send the row

One row for the single-name path. Prefer the MCP tool when `mcp__supervisor__*` is loaded;
otherwise the REST call. Both take the same rows and return the same verdicts.

```
open_rows(rows=[{kind: <task|goal|topic>, vault: "<vault>", name: "<name>", first_step: "<text>"}])
```

```bash
python3 - "$VAULT" "$KIND" "$NAME" "$FIRST_STEP" <<'PY'
import json, os, sys, urllib.error, urllib.request
vault, kind, name, first = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
row = {"kind": kind, "vault": vault, "name": name}
if first:
    row["first_step"] = first
rows = [row]
req = urllib.request.Request(
    "http://127.0.0.1:18090/api/1.0/open",
    data=json.dumps({"rows": rows}).encode(),
    headers={
        "Content-Type": "application/json",
        "X-Claude-Code-Session-Id": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
    },
)
# A create/resume verdict returns only once the tab exists and its ready-gated start
# commands are sent (a goal manager's second send waits up to 300 s). The socket timeout is
# a backstop only: the Bash harness kills the call at 600 s regardless, so a batch that would
# outlast that is chunked by the caller (see the note below), never run in one request.
timeout = 600
try:
    print(urllib.request.urlopen(req, timeout=timeout).read().decode())
except urllib.error.HTTPError as exc:
    body = exc.read().decode(errors="replace")
    sys.exit("open refused: HTTP %s — %s" % (exc.code, body))
except urllib.error.URLError as exc:
    sys.exit("open unreachable at http://127.0.0.1:18090 — %s" % exc.reason)
PY
```

⚠️ **A failure here is a reported refusal, never a silent empty result.** The wrapper answers a
rejected request with a 4xx and a body naming the reason; print it. An unreachable endpoint means
the daemon is down, which is a different problem from a refused row — say which one you hit
rather than reporting "nothing happened".

⚠️ **Build the JSON in code, never by string interpolation.** A title may contain a quote, a
backslash or a newline; the endpoint refuses a name carrying a path separator or a control
character at request validation, and a hand-built body turns a legal title into a `400`.

⚠️ **Allow minutes, not seconds.** A `create` or `resume` verdict does not return until the tab
exists and its ready-gated start commands have been sent (a goal manager's second send waits up
to 300 s). Use a generous timeout; a short one abandons an open that is still in progress and
reports nothing.

⚠️ **Pass the timeout on the Bash tool call itself, not only inside the script.** The Bash
tool's default is 120 s and its maximum is **600 s**, so a `create` on the goal path fails at
the default exactly where the wait matters. Set the call's timeout to at least 300 s — the
in-script `timeout=` above cannot rescue a call the harness has already killed.

⚠️ **A batch must fit in 600 s, and that is a hard ceiling, not a per-row budget.** The
harness cap is absolute, so a `--flagged` batch whose rows could each take 300 s cannot be
opened in one call: split it into chunks that each fit, and send them as separate requests.
Scaling the socket timeout past 600 does nothing — the harness kills the process first, and
the operator sees a partial batch with no verdicts printed.

⚠️ **`X-Claude-Code-Session-Id` is what the answer-path probe checks** — it must be *this*
session's id, so a headless worker's prompts park with the manager that opened it. Omitting the
header refuses every headless row (`answer-path`).

⚠️ **One call per invocation on the single-name path.** A row is one open; a request with many
rows is a batch and is processed in request order.

## Step 3 — Render the verdicts

One line per verdict, in request order. The `branch` names what happened; never invent a verb
the contract does not have.

| `branch` | Render |
|---|---|
| `jump` | `↪️ jumped — <vault>/<kind> "<name>" · window <window_id> · pane <pane_id>` |
| `live` | `🔒 LIVE — <vault>/<kind> "<name>" · <session_id> · live, nothing opened` |
| `resume` | `♻️ resumed <session_id-prefix> — <vault>/<kind> "<name>" · path <headless\|tab>` |
| `create` | `🆕 created <session_id-prefix> — <vault>/<kind> "<name>" · path <headless\|tab>` |
| `yours` | `👤 YOURS — <name> · role: manager, nothing opened` |
| `refused` | `⛔ REFUSED — <name> · <reason> · <message> · <remedy>` |

Also print, per row: `role` with `role_source` when it is not `on-disk`, `mode`, and
`mode_note` when set (a stored `interactive` on an agent row comes back
`normalised interactive->headless` — say so, it is a change to disk the endpoint made).
`window_key` / `window_id` are set only when `path` is `tab`.

⚠️ **A verdict's `message` on an opened tab is not a failure** — it is the best-effort note about
a start command that timed out. The tab is open; the branch stays `create`/`resume`. Report the
message and let the operator re-arm the loop.

⚠️ **A short verdict list is a bug, not a filter.** The contract returns exactly one verdict per
request row. If the counts differ, say so and stop — never open a row to "fix" it.

## Refusals

The caller may see any of these `reason` tokens. Read the row's `message` and `remedy` from the
verdict and pass them on; the decision order, the exact meaning and the remedy wording live in
`claude-wrapper/docs/open-endpoint.md` — read it there, do not restate it.

| Token | What to tell the operator |
|---|---|
| `missing-row` | the page does not exist in that vault — check the name and the `--vault` |
| `row-unreadable` | the page exists but could not be read — fix the vault/task argument |
| `undecidable-role` | the task's `role:` holds a value the endpoint does not recognise |
| `manager-mode-headless` | a goal/topic page declares `mode: headless` — the manager stays down visibly |
| `truncated-id` | `claude_session_id` is not a full 36-character uuid — re-stamp it |
| `liveness-unreadable` | liveness could not be read, so nothing was opened |
| `answer-path` | no manager is registered to answer this session's prompts — arm this session's manager loop and re-run |
| `role-map-key-missing` | the role map lacks the key the row's role needs |
| `hold-window` | the resolved window is the operator's hold window |
| `goal-loop-unsupported` | the row carries `goal_loop: true`, which the endpoint does not port |
| `target-unsupported` | the row's `target:` is not absent/`local`, which the endpoint does not port |
| `in-flight` | a create for this row is already in flight in this daemon |
| `spawn-refused` | a cap, the start rate, the terminal-task guard or a launcher/ceiling script refused — the `message` is that component's own |

⚠️ **Never re-spell a refusal into another open.** A refusal is a verdict; the remedy is the
operator's or the manager's move, not a second call with the reason worked around.

A malformed request is `400` on REST and `isError` on `open_rows`, with nothing opened — fix the
request, never retry it unchanged.

## Preconditions the caller owns

These run **before** the call and are the only decisions left here. Everything the endpoint
already owns it re-decides itself on every call, so nothing in this list may duplicate it — and
it owns none of the four below, so a row sent without them is opened without them.

### P1 — kind ambiguity

Step 1's rule: one match or an explicit `--task` / `--goal` / `--topic`. Never guess.

### P2 — approval (`phase: todo` refuses unless the operator flagged it)

Read the resolved row's phase and flag provenance from the CLI, never from a frontmatter grep:

```bash
vault-cli --vault "<vault>" task get "<task>" phase
vault-cli --vault "<vault>" task get "<task>" flag
vault-cli --vault "<vault>" task get "<task>" flag_set_by
```

**An operator-set flag IS the approval** (ruling 2026-11-15). A row is approved when it carries
`approved_at` **or** `flag: true` with `flag_set_by` in `operator` / `legacy` — the predicate is
`scripts/flag_provenance.py` `is_approved()`, the one home; never restate it. Nothing writes
`approved_at` / `approved_by`: the worker's entry path moves a flagged `todo` row on itself.

`phase: todo` on a row that is **not** operator-flagged is **not approved**. Print and STOP —
send no row:

```
⛔ NOT APPROVED — <task> is at phase: todo.
   The operator approves it first:  vault-cli task approve "<task>"   (or flags it)
   Then re-run:                     /supervisor:open "<task>"
```

⚠️ **Never add the approval yourself, and never read an agent-set or unattributed flag as one.**
An agent-set `flag_set_by` (anything outside `operator` / `legacy`, absent included) approves
nothing.

Goals and topics: an absent `phase` is exempt; `phase: todo` alone is gated — print
`⌛ waiting-approval — <subject> is at phase: todo` and send no row.

### P3 — readiness (task rows that would be created)

**Scope: a row that will `create` a headless worker or a tab.** A row that will `jump` or
`resume` is not gated, and a `role: human` row is not gated. **`task_type:` frontmatter rows are
exempt**; show `— (pipeline contract)`.

⚠️ **The decision is the readiness ladder, whose single home is `docs/fleet-surface.md` § Spawn a
worker item 2** — read the branches there; the thresholds are never restated as a second counter.
Orientation only: **≥ 9 opens; below 9 runs one improvement pass and re-audits; ≥ 7 after that
pass opens; below 7 is held with its gaps.**

**The probe stays read-only by construction — the improvement pass is the caller's act.** Do NOT
call `/vault-cli:plan-task --non-interactive` here — its entry contract mutates the rows it
inspects. Dispatch one sub-agent per task:

```
Agent(subagent_type='vault-cli:task-auditor', description='Readiness: <task>',
  prompt='Audit <absolute task path>. READ-ONLY readiness probe for a spawn gate: never edit, never ask. Return your normal report, then additionally check these hard gates and list each failure: (1) # Success Criteria with ≥2 binary checkboxes; (2) # Tasks lists concrete steps that reach the SC; (3) shipping-class tasks carry an e2e verify subtask naming a procedure AND an expected result; (4) every # Tasks item maps to an SC or is the verify subtask; (5) any push/deploy/credential names its target system and account. Score the row and name every hard-gate failure; do NOT apply an open bar yourself — the open decision is the readiness ladder's, and its single home is `docs/fleet-surface.md` § Spawn a worker item 2. Then, before you score, check for the one class of gap that is not yours to close: a gap whose resolution needs a decision only the operator can make — a Success Criterion depending on a page, artifact or credential that does not exist, a scope question the task file does not answer, a target system or account named nowhere. If you find one, end with UNFIXABLE: <reason> as your ONLY final line, naming the gap and why only the operator can resolve it — no score, no READINESS line, and do not repair it yourself. Otherwise end with exactly one line: READINESS: <score>/10 — <gap>; <gap>. Report the score and the gaps and stop there: do NOT add a ready/not-ready word, and do NOT apply the ladder yourself — the ladder decides whether it opens, and a verdict word you add is never read. Never run a filesystem-wide find.')
```

**A sub-agent that cannot close a gap because its resolution needs an operator decision returns
`UNFIXABLE: <reason>` instead of a `READINESS:` line.** The improvement pass acts on it first:
rewrite the criterion into a falsifiable form, or split it into its own follow-up task, then
re-audit. Holding the row is the fallback.

**A missing or unparseable `READINESS:` line is not-ready.** The probe emits no `ready` word —
decide from the score alone.

A held row is **not sent**. Report `⛔ NOT READY <score>/10 — <task>` with its gap bullets.

⚠️ **One override, and it is the operator's own answer: in a `--flagged` batch a below-bar row
opens anyway.** The flag *is* the operator's answer to the question the card would ask, so
holding it would ask twice and defeat the "open all flagged" intent. The row is sent, and its
gap bullets travel as its `first_step` so the worker starts *improved* rather than raw. The
ladder still runs first — the row opens after the improvement pass, not instead of it. This
override is P3's alone: it does not apply to a single-name open, where an operator naming one
task gets the honest NOT READY instead.

### P4 — the operator's session hold

A hold is operator **policy about a session**, not the `hold` task status and not the `HOLD`
marker for a `blocked` row. The endpoint does not know about it — its refusal set has
`hold-window`, which is the operator's *window*, not a session hold — so this caller is the
only place it can be enforced, and dropping it makes a session the operator explicitly parked
resumable. Rule: `skills/hold/SKILL.md`.

Read the row's recorded session id and refuse **before** sending it:

```bash
SID=$(vault-cli --vault "<vault>" task get "<name>" claude_session_id 2>/dev/null)
if [ -n "$SID" ]; then
  python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/session-holds.py" is-held "$SID"
  # exit 0 → held, reason on stdout · exit 1 → not held · exit 2 → bad target
fi
```

⚠️ **Exit 0 refuses the row and nothing is sent for it.** Print
`⏸️ HELD — <reason> · <age> · <task> · refused; only the operator releases it` and STOP for
that row: no request row, no verdict to render. On the `--flagged` path drop the row from the
batch and count it held rather than silently omitting it — a dropped row that leaves no trace
is the accounting defect the tick already fixed once.

### P5 — nothing else

The fleet cap, the operator-named exemption, the start rate, the terminal-task guard, the
answer-path probe, the topic ceiling, the launcher choice and the window are **the endpoint's**.
Do not pass a marker for any of them and do not pre-check them.

## Step B — the `--flagged` batch

A batch has no single name: select the rows, apply the pre-call holds, send them **in one
request**, render one verdict per row in request order.

⚠️ **The readiness ladder (§ P3) runs on every task row in the batch, before the request.** Its
below-bar hold is overridden here by the flag — a flagged row opens with its gap bullets as its
`first_step` — but the improvement pass still runs first. A batch that skips the ladder opens
every row raw and loses the whole point of the flag.

**Selector — ALL VAULTS by default, and an absent `defer_date` does NOT exclude.**

`flag: true` AND `status: in_progress` AND `defer_date` absent or due
(`scripts/defer-date.py`, time of day kept; invalid withheld), read from the CLI per vault —
blocked state has the same single source, never a frontmatter grep:

```bash
for v in $(vault-cli config list --output json | python3 -c "
import json,sys
for x in json.load(sys.stdin):
    if x.get('tasks_dir'): print(x['name'])"); do
  vault-cli --vault "$v" task list --all --output json 2>/dev/null | VAULT="$v" DD="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/defer-date.py" python3 -c "
import json,sys,os,importlib.util as iu
raw=sys.stdin.read().strip()
if not raw: sys.exit(0)
try: d=json.loads(raw)
except Exception: sys.exit(0)
if d is None: sys.exit(0)
tasks=d if isinstance(d,list) else d.get('tasks',d.get('items',[]))
try: s=iu.spec_from_file_location('dd',os.environ['DD']);dd=iu.module_from_spec(s);s.loader.exec_module(dd)
except Exception as e: print('ERROR |',os.environ['VAULT'],'| defer-date.py not loaded, nothing selected:',e); sys.exit(1)
def due(t): return dd.defer_state(t.get('defer_date'))[0] in ('none','due')
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

⚠️ **The provenance filter is a gate, not a tidy-up; it is what makes every admitted row an
approved one (an operator flag is the approval).** A refused row prints
`REFUSED | <vault> | <task> | flag_set_by=<who>` and is excluded. **Never drop a row silently.**
`legacy` opens a row — a migration marker, not a credential. Rule home:
`docs/fleet-surface.md` § Approval provenance.

**Scan every vault that declares a `tasks_dir`; `--vault <name>` narrows to one.** An
unanswerable vault is skipped and named, never fatal. **Print the vault on every row** — titles
are not namespaced across vaults, so a table without a vault column opens the wrong task.

**The count is a property of the day — never hardcode it and never treat a row count as a check.**

**No approve pass.** Every admitted row is operator-flagged, which makes it approved by
`is_approved()`. Write no `approved_at` / `approved_by` / `phase`, and leave `status` as found.

### B1 — holds, applied before the request

- **`blocked`** → `⛔ HOLD`, print its blockers, exclude the row.
- **`owner_live`** — a flagged task whose goal (or, absent one, topic) already has a live manager
  is **held**, never opened: two sessions on one subject. The fact is already computed by
  `scripts/flagged-rows.py` — read it, never re-derive it:

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/flagged-rows.py" --json --vault "$v"
  ```

  `owner_live: true` with a `goal`/`topic` owner → `⛔ HOLD — owner <kind> "<name>" has a live
  manager`. `owner_live: false` → send. `owner_live: null` (the probe could not answer) → send,
  noting `❔ owner liveness unverified`. The `fleet` sentinel owner → send. No row in the payload
  → send. An owner **opened earlier in this run** → treat as live: ask the ledger,
  `opened-recently.py --subject "<owner> Manager" --minutes <window>` (exit 0 → held).

  ⚠️ **The verdict is the owner's, not the row's.** `🔒 LIVE` reads the task's own session;
  `held-owner-live` reads the owning subject's. Never reuse one label for the other.

- **`opened-recently`** — skip a row this batch opened inside the hour, read from the record:

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/opened-recently.py" --subject "<task>" --minutes 60
  # exit 0 → opened inside the window (stdout: <spawned_at>\t<session_id>\t<label>)
  # exit 1 → not opened inside the window; send it
  # exit 2 → the ledger could not be read
  ```

  ⚠️ **Exit 2 is not exit 1.** An unreadable ledger is the absence of an answer, not a "no" —
  report `❓ UNVERIFIED — <task> · the spawn ledger could not be read · skipped` and send nothing
  for that row. **Print the matched `spawned_at` beside the skip.**

### B2 — one request, many rows

Build the eligible rows in the printed stable order and send them in **ONE** call — a request
with many rows is the batch, processed in request order. Same tool/endpoint as Step 2, with
`first_step` set only on a row the readiness ladder opened below its bar (its gap bullets).

```json
{"rows": [
  {"kind": "task", "vault": "private-personal", "name": "<task 1>"},
  {"kind": "task", "vault": "private-personal", "name": "<task 2>", "first_step": "<gap bullets>"}
]}
```

⚠️ **One POST for the batch, not one per row.** The endpoint processes rows in request order and
is the only pacing the batch needs — do not add a counter, a sleep or a per-row invocation.

⚠️ **Do not pre-check what the endpoint owns** — no liveness probe, no answer-path probe, no cap
read, no `role`/`mode` write, no spawn. A `role: manager` row comes back `yours` and opens
nothing; a `role: human` row opens its own `Direct` tab; an agent row goes headless. The verdict
says which.

**Print the table before the call, then the verdicts after it:**

```
FLAGGED BATCH — <vault> — <date> — <n> rows sent

  #  vault               task                                   role    mode
  1  private-personal    a monitoring task - 2026W38-sun        agent   headless
  2  private-personal    a routine task - 2026W39               human   interactive
 10  private-personal    a routine task - 2026W38-sun           agent   headless   ⛔ HOLD — blocked
 11  private-personal    a routine task - 2026W44-sun           agent   headless   ⛔ HOLD — owner goal "Some Goal" has a live manager

Opened N · jumped J · live L · resumed R · yours Y · refused X · held K (blocked · owner live) · skipped S already opened · not ready NR
```

`role` / `mode` in the table are what the endpoint **decided**, so read them from the verdicts —
never classify them here. Before the call the column may show `—`; after it, the verdict's value.

**Write the batch's checklist into TodoWrite before the call, and close every item after the
verdicts render.** One item per sent row plus one for the report. Mark an item completed only
when its verdict is settled: **jumped**, **live**, **resumed**, **created**, **yours**,
**refused**, or **held** with its stated reason. The batch cannot end while any item is
`pending`/`in_progress`; if one is, the run stopped early — say so.

## Notes

- Managers (`/supervisor:goal-loop`, `/supervisor:fleet-loop`) *recommend* opening sessions; this
  command is the executor — run it when the human approves.
- The open decision itself (spec `open-endpoint`, `open-tick-reroute`) is the wrapper's; this
  file is the caller named in `claude-wrapper/docs/open-endpoint.md` and holds only name
  resolution and the pre-call holds.
- Invoked as `/supervisor:open`.
