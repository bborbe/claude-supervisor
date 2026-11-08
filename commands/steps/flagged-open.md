---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Flagged open

When: every tick

⚠️ **This leg acts, and a read-only execution is not a run of it.** It opens each admitted row; a
run that only reports `unactioned: <n> operator-flagged` without invoking the command has measured
nothing — `opened=0` because the leg was told to print it is a failure, not a quiet round. It is one
of the round's two acting legs, so the round's delegate rule carves it out — a sub-agent may run it
only with write tools (`commands/fleet-drive.md`, the delegate rule's carve-out).

⚠️ **Gate this leg on the round's debt — chain it, do not merely read it:**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-receipt.py" --debt --subject "Fleet Manager" --branch fleet \
  && <run this leg>
```

Exit **12** = a step is still owed: print the `debt:` lines, skip this leg, and **carry on with the round — the tail still runs**, because the tail's `--check` is what reports the round's verdict to the operator. Exit **0** = no debt, run it. ⚠️ **Exit 2 = the gate could not read or write the debt file** — it prints nothing to stdout, only a reason to stderr; skip the leg and print that reason, never read 2 as "no debt". ⚠️ **`--debt` judges the round in progress**, re-reading the receipt, so a round that already swept is not blocked by a debt it has paid. ⚠️ **The exit code is the gate, never the printed line** — a leg that reads only the text is satisfied by an empty read. ⚠️ **An operator's flag is not a licence to run ahead of the sweep** — the 13:27 round on 2026-10-31 opened three flagged rows and never swept, and nothing in its render said so. Opening the row is one round's delay; sweeping is what makes the next round's opening decision correct.

## Step 5c — Open what the operator already marked

`/supervisor:open --flagged` has been specified since before the `flag_set_by` field existed and
had **no caller**: `commands/open.md` defines the batch, `commands/steps/goal-spawn.md`
excluded the very rows it selects, and three rule files said the loop must never invoke it.
This leg is the caller. The operator's mark is their decision; opening it executes that
decision and decides nothing new (`docs/session-tiers.md` § The four tiers, rule 2).

**Invoke the command. Do not re-implement its selector** — `commands/open.md` § the
`--flagged` batch owns the vault scan, the due-date rule, the `OPENING_ACTORS` gate and the
`REFUSED` line, and a second copy here would drift from it:

```
/supervisor:open --flagged
```

**Act on what it prints, and nothing else.** It opens each admitted row and prints the rest as
`REFUSED | <vault> | <name> | flag_set_by=<who> | not the operator — not opened, not approved`.
**Print every `REFUSED` line verbatim and open nothing it names.**

⚠️ **`flag_set_by` is the safety rule, not a detail.** Only `operator` and `legacy` are
admitted; an agent-set value is refused, and so is an **absent** one — after the disposition no
row should carry one, so an absent value means a writer that did not declare itself, which is
the bypass. Never widen the admitted set to make a row openable, and never "fix" a refusal by
writing `flag_set_by` on the row.

⚠️ **This leg removes the human from the loop, and that is a real change in blast radius.**
Before it, the batch ran only when the operator typed the command; now it runs every tick
unattended. `legacy` is a plain frontmatter value with no enforcement behind it, so under the
fleet's cooperative-agent trust model an agent that writes `flag: true` +
`flag_set_by: legacy` can get its own task opened without the operator. The same assumption
underlies `keepalive.md` resuming a page-supplied `claude_session_id`. **That is the accepted
model, not a defect — but it is named here so nobody reads the `flag_set_by` check as an
authentication control.** If the model ever changes, this leg and the keep-alive are the two
places that must change with it.

## Step 5c.1 — Open the flagged managers

⚠️ **`/supervisor:open --flagged` selects tasks only** (`commands/open.md` § the batch), so the
goal and topic tiers have their own caller here. A flagged **goal** or **topic** with no live
manager is opened by this round too — the operator's ruling is that a flag means *open me* at
every tier, not only at the task tier.

Run the selector and act on what it prints:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/flagged-subjects.py"
```

**It owns the predicate, the ordering, the registry match, the free-slot gate and the
attention-card check** — none of that is restated here, and none may be re-derived at this
call site. Read its header for what qualifies a subject and why the gate reads the cap it
reads. `--json` prints the whole computation and opens nothing, for a diagnosis.

**Act on `OPEN` lines only, in the order printed.** The order is the selector's — topics
before goals — and it is not this leg's to re-sort: a topic manager owns its goals the moment
it starts, and the selector already reports those goals as `COVERED` rather than opening
both. For each `OPEN | <vault> | <kind> | <name>`:

```
/supervisor:open --topic "<name>" --vault "<vault>"      # kind: topic
/supervisor:open --goal "<name>" --vault "<vault>"       # kind: goal
```

That command resolves the manager, jumps to a live one, resumes an `ABSENT` one and spawns
only when neither exists — so this leg never decides *how* to open, only *whether*.

**Everything else is reported, never opened.** `MANAGED` (a live manager already owns it),
`DEFERRED | … | manager cap` (a free slot ran out — **name it in the round's render**),
`COVERED` (its topic is being opened this round), `WAITING | … | phase: todo` (the operator's
approval inbox — `commands/open.md` § Step 2B refuses it), `CARDED` (an open
`fleet-propose-manager` / `fleet-approve-manager` card already asks the operator) and
`UNKNOWN | … | <reason>` (a page session id the liveness probe could not read) each print
their line verbatim.

⚠️ **Exit 2 is REFUSED, never "nothing to open".** The selector exits 2 when the vault set,
the session registry, the manager cap, the live-manager count, the attention store or a
vault's goals/topics directory could not be read; it prints the reason to stderr and no
`SUMMARY`. Open nothing this round, print the reason, and record this half of the leg as
not-run (below).

⚠️ **This leg opens a manager, and that is rule 2, not rule 4.** Rule 4 forbids opening a
manager *on the operator's behalf*; a flagged page is the operator's own mark, so opening it
executes their decision and decides nothing new (`docs/session-tiers.md` § The four tiers,
rule 2). `flag_set_by` is **not** read here — operator ruling 2026-10-30, *"it should not
matter how set the flag"* — so the cap is the only bound after the flag. The `--flagged`
task batch keeps its provenance gate because it writes `approved_by: operator`; that split
has one home in `docs/fleet-surface.md` § Approval provenance.

**Record the count — in the digest, not only in chat.** `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet-legs.py" --digest "<this round's digest>" --leg operator-flagged --count <n>`, with `<n>` the flagged subjects this leg **declined to act on**: the tasks the `--flagged` batch refused, held or skipped, plus every goal or topic the selector reported as `DEFERRED`, `WAITING`, `CARDED` or `UNKNOWN`. ⚠️ **`MANAGED` and `COVERED` are excluded** — a live manager already owning the subject, and a goal whose topic opened this round, are both the leg working correctly, and counting them would render a healthy round as a regression in the `# Regression metric`. The same logic already excludes a JUMP/RESUME-bound task, which counts as opened rather than unactioned. The line's format and its not-run form have their **single home in `commands/steps/output-shape.md` item 14** — read it there and never restate it here.

⚠️ **A leg that does not execute records WHY.** When the debt gate skips this leg (exit 12 = a step is still owed, exit 2 = the gate could not read the debt file), write `fleet-legs.py --leg flagged-open --not-run "<reason>"` rather than leaving the sweep's seeded `not run — no reading recorded this round` standing — `--check` reads that seed as "nobody has spoken for this key". ⚠️ **Never render `unactioned: 0` for a leg that did not run** — a suppressed leg and a leg that opened nothing must never print the same, which is the whole reason the count is written to the digest instead of spoken into chat.

⚠️ **A failed write is reported, never swallowed.** If `fleet-legs.py` exits 2 the digest could not be read or written (the sweep may not have written it yet); render `legs: UNKNOWN — <reason>` for this round and retry the record once the digest exists. A reading that never landed must not read as a leg that never ran.
