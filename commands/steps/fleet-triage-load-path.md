---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 0 — Resolve the load path

When: every run — before the sweep

```bash
LOAD_PATH="${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT is unset — run this from the loaded plugin, not a bare shell}/"
```

The **loaded plugin's own root**, never the marketplace clone — the clone updates
independently of what this session loaded, so a path resolved from it runs scripts this
session never loaded. The `:?` is deliberate: it refuses rather than falling back, because
a silent fallback to the clone is exactly the version skew this step exists to prevent.
