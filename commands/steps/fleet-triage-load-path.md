---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 0 — Assert the load path

When: every run — before the sweep

```bash
: "${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT is unset — run this from the loaded plugin, not a bare shell}"
echo "$CLAUDE_PLUGIN_ROOT"
```

The **loaded plugin's own root**, never the marketplace clone — the clone updates
independently of what this session loaded, so a path resolved from it runs scripts this
session never loaded. The `:?` is deliberate: it refuses rather than falling back, because
a silent fallback to the clone is exactly the version skew this step exists to prevent.

⚠️ **Every script call in every step inlines the same `${CLAUDE_PLUGIN_ROOT:?...}` form.**
There is no `LOAD_PATH` variable to carry forward: shell state does not survive between
`Bash` calls, so a variable set here would be empty by the time a later step used it. This
step therefore *proves* the variable is set and stops the run early if it is not; it exports
nothing.
