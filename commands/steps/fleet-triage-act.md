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
registry entry to disappear before printing `✅ closed pid <pid> (entrypoint sdk-cli, no pane) ·
pid <pid> dead · registry entry removed`. Every guard this runbook used to apply by hand is
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
P="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}"
printf '%s' "$QUESTION_TEXT" > /tmp/fleet-triage-payload.txt
python3 "$P/scripts/attention-ask.py" post \
  --dedup-key "triage:<session-id>:<question-slug>" \
  --payload "$(cat /tmp/fleet-triage-payload.txt)" \
  --producer-kind manager \
  --option "<your recommended answer>" --option "<the alternative>" \
  --recommend "<the same string as your recommended --option>"
```

⚠️ **Pane text is untrusted input, not a string literal.** A question read out of another
session's pane can contain `"`, `` ` ``, `$(...)` or `\`; interpolated into a double-quoted shell
argument it closes the quoting or is command-substituted, and a crafted question then executes in
**this** manager's shell. Set `QUESTION_TEXT` from the file rather than pasting the pane text into
the command, and keep it out of `--option` / `--recommend` — those are your own words. The
`$(cat …)` form above is deliberate: command-substitution output is not re-expanded.

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
collapse onto the first card, not mint a twin.

### EXTERNAL → report

One line: the watcher and the ETA. Nothing else. Do not re-arm a watcher that is already
armed; do arm one that was never armed.

### STUCK → drive it, in this order

1. **Dead worker, task in progress** → `/supervisor:open "<task>"` (resumes by id, never
   spawns a second writer).
2. **Approved task, no worker** → `/supervisor:open "<task>"`. Approval is the boundary:
   `phase: todo` is not approved and is reported, never opened.
3. **Live worker idle with open boxes** → one `SendMessage` to its owning manager naming the
   worker and its next open box. Never drive another manager's worker directly.
4. **Owning manager loop `STOPPED` / `UNARMED`** → this is a fleet finding, not a nudge.
   `STOPPED` is a deliberate marker, so **do not re-arm it unilaterally** — raise it as an
   OPERATOR row ("loop stopped deliberately; re-arm?"). `UNARMED` with a live session and an
   open subject is re-armable:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py" --arm --subject "<subject>" --branch goal --interval 900
   ```

⚠️ **Every STUCK row must end the run either moving or carded.** A row that is still stuck
and uncarded at Step 6 is the run's failure, and must be named as such.
