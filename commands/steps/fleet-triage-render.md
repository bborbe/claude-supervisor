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
  🟠 STUCK    worker         <label>                       idle 41m, 3 open boxes, no gate
  🟢 WORKING  worker         <label>                       —
```

Under it, and only for rows that are not WORKING:

**Closed** — `sid-prefix · task · closed` or `· refused: <token>`.
**Drove** — `action | what it targeted | observed result`.
**Needs you** — one row per OPERATOR: **jump link**, the question in the session's own
words, your recommended answer.

```bash
python3 "${CLAUDE_PLUGIN_ROOT:?set by the loaded plugin}/scripts/jump-link.py" <pane-id>
```

⚠️ **`jump-link.py` takes an integer pane id only.** Resolve it by joining `label` against
`wezterm cli list`. No match → hand over `/supervisor:jump <sid-prefix>` and say so.
