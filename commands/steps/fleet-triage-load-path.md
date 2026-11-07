---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 0 — Resolve the load path

When: every run — before the sweep

```bash
echo "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}"
```

The **loaded plugin's own root** when the client substitutes it, and the marketplace clone only
when it does not.

⚠️ **The `:-` fallback is load-bearing, not style.** `CLAUDE_PLUGIN_ROOT` is **unset** in a
command's Bash — the client substitutes the exact `${CLAUDE_PLUGIN_ROOT}` token into the command
text, it does not export the variable to the shell — so a bare form resolves to
`/scripts/<name>.py` and fails, and a `:?` form aborts the whole run with its own error message.
Measured and recorded at `docs/session-tiers.md:200-202` (verified 2026-09-23). This is why the
fallback is the repo-wide convention rather than a belt-and-braces default.

⚠️ **There is no variable to carry forward.** Shell state does not survive between `Bash` calls,
so every script call in every step inlines this same
`${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}` form. This step
prints the resolved root for the run's record; it exports nothing.
