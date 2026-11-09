---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 5 — Render

When: every run — after the act phase

```
# Fleet triage — <date> — <n> sessions

  state      tier           session                       what it waits on
  ⚪ CLOSED   worker         <label>                       completed, 0 open boxes
  🔵 OPERATOR goal-manager   <label>                       <the question, verbatim>
  🟡 EXTERNAL worker         <label>                       bot review on <PR> — ETA ~HH:MM
  🔒 LIVE-IDLE HEADLESS worker <label>                     idle, no pane — SendMessage sent, no reply
  🟠 STUCK    worker         <label>                       idle 41m, 3 open boxes, no gate
  🟢 WORKING  worker         <label>                       —
```

Under it, and only for rows that are not WORKING:

**Closed** — `sid-prefix · task · closed` or `· refused: <token>`.
**Drove** — `action | what it targeted | wake path | observed result`. The **wake path** is
one of:

- `pane:<target-resolution>` — `pane-wake.py --send` returned `delivered`. The resolution is
  `manager-idle-tab`, `worker:no-manager`, `worker:manager-paneless` or `worker:manager-absent`.
- `sendmessage` — only a cross-session `SendMessage` went out. That is a delivered nudge for a
  **headless** session (§ 4b). For a **tab** session it is not one, because the message waits for
  a turn that never comes.
- `none:<reason>` — nothing reached the session. The reason is the dry run's `skip:*` verbatim
  (`skip:operator-ask`, `skip:open-card`, `skip:spinner`, a modal skip, …), or `unverified`, or
  `failed:<why>`, so a refusal reads as a refusal and never as a silent failure.

Print a one-line tally of the wake paths under the table, plus the share that fell through to a
worker. If most wakes fell through to `worker:*`, say that manager resolution looks broken.
**Needs you** — one row per OPERATOR: **jump link** for a tab session, **card handle** for a
pane-less one, the question in the session's own words, your recommended answer.

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py" <pane-id>
```

⚠️ **`jump-link.py` takes an integer pane id only.** Resolve it by joining `label` against
`wezterm cli list`.

⚠️ **A pane-less OPERATOR row has no pane, so no jump link can exist for it — hand over the card
instead.** This is the headless session § 3.2d classifies OPERATOR (a `👤 You:` closer with no
gate), and **both** jump forms fail on it: `jump-link.py` needs an integer pane id, and
`/supervisor:jump <sid-prefix>` resolves a session id through tab titles, so a session with no tab
never matches. Give the operator the **card's `item_id`** — posted by Step 4's OPERATOR branch —
plus `/supervisor:attention-next`, which reads the board rather than a pane. ⚠️ **Never hand over
a command that cannot work for the row** — a jump link for a pane-less session is a dead end the
operator cannot diagnose from the board. The same substitution applies to a tab session whose
`label` matches no live pane.
