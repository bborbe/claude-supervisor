# Confirm the digest's candidates

When: the digest carries an orphan, collision or unmanaged-topic candidate

### Confirming the digest's candidates

- **Orphan:** a candidate is open work with a dead session — for the operator to pick up or close, not a fault and not a stall. Report as its own group. `UNKNOWN` (check failed) is reported as unknown, never as clean.
- **Collision (Step 2c):** a shared subject is not automatically a collision — confirm the overlap is on the same artifact. On a real one, **tell both sides without asking** (read-only context):

  > "Read-only: `<other session>` also appears to be working on `<shared artifact>`. Flagging so you two don't duplicate or overwrite each other — I have not asked either of you to stop, and I have not decided who owns it."

  Then report it in Step 5's grouped report, as its own group. **Never draft a stand-down** — that is a course correction and needs the operator's yes.
- **Unmanaged topic (Step 2d):** **suggest, never auto-spawn** a manager — report the topic, its live workers and the command that would start one. When the operator says go, apply the spawn readiness precondition. If the topic page is absent, suggest creating the page (manager work) together with the spawn.
