---
# Not a slash command — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Closure

When: every tick, after the classify

Applies to every subject whose every `# Success Criteria` box **and** every `# Definition of
Done` box reads `[x]` — that is the only qualifying state, and the sections below define it.

**A page whose evidence is complete closes itself. Nobody certifies it.**

The rule and its rationale have one home — `commands/goal-loop.md` § Manager contract
(*"You may close a goal; you may never certify one"*). This step owns only the execution.

## What to do

For each tracked goal (and, when the topic branch is running, each declared topic):

1. **Read both checkbox sections from the file, each counted on its own**, never from a status
   field and never with a whole-page count:
   ```bash
   python3 - "<page>" <<'PY'
   import re,sys
   txt=open(sys.argv[1],encoding="utf-8").read()
   def tally(name):
       m=re.search(rf"^# {re.escape(name)}\s*$(.*?)(?=^# |\Z)", txt, re.S|re.M)
       if not m: return None
       b=m.group(1)
       return (len(re.findall(r"^\s*-\s*\[x\]", b, re.M)),
               len(re.findall(r"^\s*-\s*\[[ /]\]", b, re.M)))
   for n in ("Success Criteria","Definition of Done"):
       t=tally(n)
       print(f"{n}: MISSING" if t is None else f"{n}: {t[0]} done, {t[1]} open")
   PY
   ```
   ⚠️ **A whole-page `grep -c` is not this check.** It counts the `# Tasks` boxes too, so it can
   neither report the SC/DoD split the Reporting block below prints nor tell a page whose DoD
   section is absent from one whose DoD section is empty — and the second is the failure this
   rule exists to refuse. **Both sections must be present, and both must show 0 open.** An
   absent section is **unmeasurable**, not complete: report it as a gap and close nothing.

2. **Run the tier's own verb and quote it verbatim:**
   ```bash
   vault-cli goal complete "<goal>"      # goal tier
   vault-cli topic complete "<topic>"    # topic tier
   ```
   The verb re-checks its own gate. **Its verdict is the authority, never this step's read** —
   when the two disagree, the verb wins and the disagreement is reported, not resolved by hand.

3. **Never pass `--force`.** A close that needs it is a close this step does not make.

## What this step must never do

- **Never tick a box** in either section, for any reason. That is certification and it is
  forbidden at every tier.
- **Never set `status: completed` directly.** The verb writes it; nothing else may.
- **Never close on a partial read** — a page whose DoD section is missing is *not* complete,
  it is unmeasurable. Report it as a gap rather than treating an absent section as empty.
- **Never close a page whose open boxes are another task's work.** Report which boxes are open
  and whose they are; closure is not a way to dispose of them.

## Reporting

One line per page examined, and one for the round:

```
closure: <page> — <n> open (SC <a>, DoD <b>)            → not a candidate
closure: <page> — 0 open → goal complete → <verdict>    → closed
closure: <page> — 0 open → goal complete → REFUSED: <the verb's own line>
```

⚠️ **A refusal is reported verbatim and leaves the page open.** It is not smoothed into
"closed", and it is not retried with `--force`.
