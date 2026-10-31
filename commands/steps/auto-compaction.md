---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Auto-compaction

When: the hand-back reports `context70` > 0

  - **Auto-compaction:** the payload's `CONTEXT ≥70% (idle, not in tool)` section lists the sessions the sweep found over the bound — **500k tokens**, because the token bound is a precondition and the percentage cannot fire below it; the section header keeps its older `≥70%` name. Read it at the `payload=` path the sweep hand-back named. On an idle one **no manager covers**, compact it yourself — no operator ask — following the Manager Session runbook § Auto-compaction (gates, three-send sequence, verify). Where a manager owns the area, defer.
