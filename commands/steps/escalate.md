---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Escalate

When: every tick

  - **Routing:** on `stalled`/crashed-looking, `SendMessage` the owning manager first (*"`<session>` looks stalled — your topic"*); TTS the human only if it persists — voice-gated.
## Step 5 — Escalate: an ask and a report

⚠️ **This step runs in `supervisor:fleet-round` (router row 10); the ask it describes belongs to the manager's row 11.** Anything here that needs the operator's answer is **routed**, not put — see the blocked-set bullet below.

Never one interruption per stuck session. A round escalates twice; both fire here — a round that walks Steps 0–6 and renders the Output shape without either has dropped findings it already holds.

- **The blocked set — ROUTED to row 11, never asked here.** Every session the payload's `BLOCKED` section carries is **rendered as text and routed to the manager's row 11**, which puts the ONE `AskUserQuestion` (up to 4), each subject gated by an `asked-ledger` `claim`. ⚠️ **A headless delegate cannot put an `AskUserQuestion`** — it would park with nobody to answer — so this step's job is the material and the route, not the ask. Before 2026-11-19 this bullet said the round asks directly, which was satisfiable only while the manager ran this row. That protocol — manager-first, claim, pane read, the batch, relay, provenance, verify-submission — is **Needs-input** in `${CLAUDE_PLUGIN_ROOT}/commands/steps/needs-input.md`, which owns it and is **not restated here**. A subject dropped by one of those rules prints the rule and its line.
- **The sweep's own findings — a report.** Collect them into one grouped report, one entry per distinct cause, presented once at round end. Its members: every `stalled`, `parked` and `orphan` finding (orphans as their own group), the `finished — reap` rows as a single **self-closeable** line, and any Step 2c collision as its own group. Omit if nothing needs attention. Mark any cause a sub-agent could not confirm as **unverified**.
- **Recurring causes — file once, never re-report.** For every cause class `who-needs-my-help.py`'s `Root cause` block lists (seen on **≥2 distinct calendar days**), look for an open task covering it: `grep -lF "<class>" "<vault>/<tasks-dir>"/*.md`. None → file one by dispatching `Task(subagent_type: 'vault-cli:task-creator', …)` — the round holds `Task`, not `Skill`, so a slash command is not available here — then name the filed task and its path in the report; it lands at `phase: todo` for the operator's approval, which is where readiness and opening belong. ⚠️ **One task per class** — a class that already has an open task is reported as `covered by [[<task>]]`, never filed again; the second round of the same day must find the first round's task.
