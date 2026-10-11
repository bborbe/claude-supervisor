---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Render the output shape

When: every tick

  - **Global delta:** on progress, a 3–5 line chat delta; detail stays in task files.
## Output shape

1. **Roster** — the marker line plus the box, rendered **exactly per Fleet Manager Session runbook (per-vault) § Sweep output — the fleet table** — the single source for frame, columns, widths and icons. Build it with `python3 $P/fleet-board.py --json | python3 $P/box-table.py`; never hand-draw it. Every tick prints the marker line; the box re-prints when a bucket moved, a session appeared or vanished, or a handoff was sent, plus a ~30-min heartbeat. If `/fleet-status` just printed the same box, reference it.

   Print one quantitative line with the marker: the colour census `python3 $P/fleet-colours.py --json`, read for `backlog` (green/blue/cyan) with `default` and `purple` reported apart.

   ⚠️ **Also print `workers: <live>/<target>` on the marker line, every round.** Read the live count with `python3 $P/worker-sessions.py --count` — live **worker sessions**: the spawn ledger joined to the registry **or** the heartbeat store (a worker with no registry entry — headless in-process, or cluster — answers through the heartbeat). ⚠️ **Not `live-workers.py`** (reads the headless heartbeat store, answers `0` while interactive workers are live), and not `--list | wc -l` on either (counts the empty-store sentence, answers `1`). A non-zero exit means the store could not be read; print `workers: unknown`, never `0`. ⚠️ **The registry alone is not the answer either** — `~/.claude/sessions/*.json` also holds the operator's own sessions and the managers, so it is read **only through the ledger join that excludes them**, never on its own; the `ListAgents` roster is not a count at all. The target is `spawn.maxConcurrent` in `~/.config/claude-supervisor/config.json`, whose single home is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 5 — **read the number there and never restate it here.** ⚠️ **Read it with its own instrument, every round — never from memory:** `python3 $P/worker-target.py` prints the resolved target and nothing else, and `--source` adds `default|config|env`. Print `workers: <live>/unlimited` when the key is `0`, and `workers: unknown` when the store cannot be read — never `0`. ⚠️ **The SOFT cap is the target; the HARD cap above it is NOT this leg's business.** `python3 $P/worker-target.py --hard` reads it, and this round must never use it as the target: the band between the two is the operator-named exemption, and **a fleet sweep never passes `operator_named`**, so its ceiling is the soft cap on every path it has.
2. **Classification** — the digest's non-progressing rows: `<name> [<id>] · <status> · <classification>`. Never print the `[ref]`.
3. **`📋 Open with the operator`** — the ledger, one line per open entry: kind · what · state · age. **Never omitted**; `(none open)` when empty.
4. **Needs-input** — the batch over the digest's BLOCKED set, per **Needs-input** in `${CLAUDE_PLUGIN_ROOT}/commands/steps/needs-input.md` (the `AskUserQuestion`, its `asked-ledger` claims, the relays), then the consolidated list beneath it: every open claim across every layer, in one list. **Never omitted** — `(none blocked)` when the digest's BLOCKED section is empty, and a subject one of those rules dropped prints that rule and its line.
5. **Escalation report** — Step 5's own findings, grouped cause-first, rendered exactly per `${CLAUDE_PLUGIN_ROOT}/commands/steps/escalate.md` (its member list, its omit-if-nothing rule, its **unverified** marking).
6. **Adoption** — Step 3a's lines verbatim, rendered exactly per `${CLAUDE_PLUGIN_ROOT}/commands/steps/adopt.md` (the per-manager lines, the `adopted by <sid8>` line, the census line and the claim-failure lines).
7. **Drive leg** — the agent's report verbatim (its header line, rows, `ESCALATION`, the `Waiting on your keystroke` list, `LEDGER`) plus `Sent: <n>` with one line per recipient and `Skipped: <n>` with reasons, rendered exactly per `${CLAUDE_PLUGIN_ROOT}/commands/steps/drive.md` — including its rule that the `Waiting on your keystroke` list still prints on a round whose drive leg returned no usable report, so the section is never omitted on any round.
8. **Read-only context sent this sweep** — what and to whom.
9. **Course-correction drafts awaiting approval** — exact text + target; ask the operator to approve or edit.
10. **Snapshot written** — path and `swept_at`.
