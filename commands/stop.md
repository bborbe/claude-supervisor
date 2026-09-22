---
description: Stand this session's manager loop down — disarm the model-waking cadence, leave the model-free gate loop running, keep the session (and its asks ledger) alive. The disarm contract is the vault's Worker Manager Session runbook § Guardrails item 6 — Session end.
allowed-tools:
  - CronList
  - CronDelete
  - ScheduleWakeup
  - TaskStop
  - Bash(date:*)
  - Bash(pgrep:*)
  - Bash(ps:*)
  - Bash(python3:*)
argument-hint: "(no argument)"
---

Stand the manager loop **this session** is running down. This is the operator's verb; it is not a close.

**The disarm contract — what `stop` must disarm, what it must leave alone, and why — is the vault's `Worker Manager Session` runbook § Guardrails item 6 — Session end. That section is the single normative copy. Read it, and do not restate it here or anywhere else.**

## Arguments

None. The loop is session-scoped, so there is nothing to name. `stop` acts on this session and only this session.

## Procedure

### 1. Say which loop is being stood down

Read this session's recorded subject — read-only, never write it here:

```bash
python3 -c "
import json,os
sid=os.environ.get('CLAUDE_CODE_SESSION_ID','')
p=os.path.expanduser('~/.claude/state/worker-manager/'+sid+'.json')
try: d=json.load(open(p))
except Exception: d={}
print('session',sid[:8],'| subject:',d.get('subject','(none recorded)'),'| branch:',d.get('branch','—'),'| vault:',d.get('vault','—'))
"
```

A missing file is not an error — the loop may have been armed before the subject was recorded. Print the session id and carry on.

### 2. Disarm the cadence drivers this command can reach

Call the harness tools directly. Do not shell out for these, and do not improvise a driver list — the reachable set is fixed and small:

| Driver | Reach it with |
|---|---|
| a `CronCreate` job held by this session | `CronList` → `CronDelete` per job id |
| a `ScheduleWakeup` loop | `ScheduleWakeup` with `stop: true` |
| a `Monitor` / background task this session armed | `TaskStop` with its task id |

- **`CronList` first, and print every job before deleting it** — id, schedule, prompt prefix. A deletion the operator cannot see is indistinguishable from a job that was never armed. Delete **every** job this session holds: a session-scoped cron job in a manager session is part of the loop by construction, and if one is not, the printed line is how the operator notices.
- Then `ScheduleWakeup` with `stop: true`, and no other field.
- Then `TaskStop` for each background task **you can name**. ⚠️ The harness exposes no enumeration of in-session background tasks, so a task id is reachable only when you armed it and it is still in this conversation. One you cannot name cannot be stopped here — report it as still standing rather than implying it was cleared.

### 3. Probe the model-free gate loop and print it — signal nothing

```bash
pgrep -f '[s]weep-gate' | while read -r pid; do ps -o pid=,etime=,command= -p "$pid"; done
```

Print the pid(s) and uptime. **Do not signal it** — the runbook's § Guardrails item 6 states what must survive `stop` and why. The bracket in `[s]weep-gate` is load-bearing: it stops the probe matching its own shell.

If the probe returns nothing, print `⚠️ no gate loop found` and say plainly that the loop may never have been armed. That is a fact to report, not to repair.

### 4. Report the tick file and the ledger — both untouched

```bash
python3 -c "
import json,os,re,datetime
sid=os.environ.get('CLAUDE_CODE_SESSION_ID','')
sp=os.path.expanduser('~/.claude/state/worker-manager/'+sid+'.json')
subj=json.load(open(sp)).get('subject','') if os.path.exists(sp) else ''
slug=re.sub(r'[^a-z0-9]+','-',subj.lower()).strip('-')
t=os.path.expanduser('~/.claude/state/sweep-gate/'+slug+'.tick.txt')
print('tick  :',t,'|',datetime.datetime.fromtimestamp(os.path.getmtime(t)).isoformat(timespec='seconds') if os.path.exists(t) else 'ABSENT')
lp=os.path.expanduser('~/.claude/state/open-items/'+sid+'.json')
d=json.load(open(lp)) if os.path.exists(lp) else {}
items=d.get('items',[])
print('ledger:',lp,'| open',sum(1 for i in items if i.get('state')=='open'),'of',len(items))
"
```

### 5. Print the report

```
⏹️ STOP — <subject> (<branch>) · session <sid8>
  Disarmed
    ✓ CronDelete <id>  <schedule>  "<prompt prefix>"
    ✓ ScheduleWakeup — dynamic loop ended
    ✓ TaskStop <id> — <what it was>
  Left running — the runbook's § Guardrails item 6 owns this contract
    ● gate loop  pid <pid>, up <etime>
    ● tick file  <path>  mtime <iso>
  Session alive, not closed
    ● ledger <path> — <n> open of <m>, unchanged by this command
  Restart: /supervisor:worker-manager "<subject>"
```

Print `·` for a driver that was **not** armed rather than dropping the line — a missing line reads as "checked and clean" when the truth may be "never looked".

⚠️ **Write nothing.** No task, topic or goal page; no state file; no `open-items.py` mutation. `stop` is a disarm, not a bookkeeping act, and a write here is the one thing that would make it visible to a sweep that is no longer running.

⚠️ **Never end with `/vault-cli:session-close`, and never offer it** — § Guardrails item 6 states what must survive `stop`. This report is not a substitute for the Async State Closer in `~/.claude/CLAUDE.md`, which is unconditional: end the turn normally, with the closer.

⚠️ **If a driver refuses to disarm** — `CronDelete` errors, `ScheduleWakeup` rejects the stop — print the failure verbatim together with the driver still standing, and say the loop is **not** fully stood down. A `stop` that reports success over a live driver is worse than one that reports the failure.
