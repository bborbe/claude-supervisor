---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Wake misses: the subjects a session-based check never enumerates

Read by `/supervisor:fleet-triage` § 3.6 (`commands/steps/fleet-triage-state.md`) and by the
fleet keep-alive leg (`agents/fleet-keepalive-actor.md` § 4c). Both used to start from live
manager sessions. So a subject whose manager is dead, or running headless, was never checked,
and the sweep gate kept waking nothing for it every hour while both read green. Measured
2026-11-14/15: 58 of 126 wakes read `NO-PANE`, and six subjects had no `WOKEN` in more than
20 h.

The gate records each wake's outcome in the subject's ledger as one column,
`wake=<result> session=<sid8> live=<yes|no|unknown>` (`wake=none` when no wake ran). Read it
once per run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/wake-misses.py"
```

Output: one line per finding, then a summary line.

```
MISS   <vault> <subject> <ts> <result> <session> <live>
STALE  <vault> <subject> <ts> <result> <session> <live>
wake-misses=<n> stale=<n> measured=<n> unmeasured=<n>
```

| Reading | Meaning | fleet-triage | keep-alive leg |
|---|---|---|---|
| `MISS` | the subject's last actionable wake was `NO-MANAGER` (every registration dead), `NO-PANE` (a live manager the pane lookup missed), `HEADLESS` (a live manager with no pane), or a wake run that failed | **`NO-LOOP`**. Render subject, session, `live=` and result, counting a subject once even if § 3.6's table also flagged it | `wake-miss:` line in the hand-back |
| `STALE` | the same, but older than the window (48 h by default), so the subject may have stopped producing actionable ticks | listed under the managers line, never counted | not reported |
| `WOKEN` / `SKIP:<reason>` | the wake path worked: the prompt was typed, or the guard deliberately held it back | nothing | nothing |
| `unmeasured=<n>` | ledgers written before the gate recorded wake outcomes, which cannot show a miss | printed beside the result. `measured=0` means no wake was checked, and the line says so rather than reading green | `wake-unmeasured=<n>` |
| exit **2** | the state directory could not be read | the wake check is **unread**, never zero misses | `wake-misses=not-run` |

**Keep-alive hand-back** — append these lines to the leg's hand-back (`agents/fleet-keepalive-actor.md` `<output_format>`):

```text
wake-misses=<n>|not-run · wake-measured=<n> · wake-unmeasured=<n>
wake-miss: <subject> · <result> · <sid8> · live=<yes|no|unknown>
```

⚠️ **Report, never act.** A `NO-MANAGER` subject has no manager to wake. Whether one is opened
is the opening rule's call (§ 3.6: only a flagged, not-running subject is opened), never this
check's. A `HEADLESS` manager is a separate defect: its resume path started it without a pane.
