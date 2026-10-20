---
description: DEPRECATED alias for `/supervisor:topic-status` (topic) and `/supervisor:goal-status` (goal) — read-only snapshot of ONE subject. Kept working so an armed loop's wakeup does not die on the rename; new callers use the tier's own command.
argument-hint: "[topic|goal]"
allowed-tools:
  - Read
  - Skill
---

⚠️ **Deprecated — use `/supervisor:topic-status` for a topic, `/supervisor:goal-status` for a goal.** This file is an alias, kept so anything already invoking `/supervisor:manager-status` keeps working. It is not a second implementation and must not become one.

⚠️ **Why this alias exists at all, and it is not politeness.** The manager tier split into a topic tier and a goal tier on 2026-10-20, and **every armed manager loop wakes by calling `/supervisor:manager-status <subject>`** — the prompt its own `ScheduleWakeup` carries. A rename with no alias therefore stops every live loop on the first `/reload-plugins` after the release, **silently**: the wakeup fires, the command is gone, and nothing reports a lapsed loop until `manager-liveness.py` calls it stale two intervals later. The alias is what turns that into a working tick.

**Do this:** resolve the subject and its branch exactly as `commands/topic-status.md` § Arguments does — source 1 of `docs/subject-resolution.md` first — then invoke the matching command through the `Skill` tool with the **same arguments**, and follow it exactly as written:

- a **topic** page resolves → `Skill: supervisor:topic-status`
- a **goal** page resolves → `Skill: supervisor:goal-status`

That file is the single home of the procedure. Nothing about the behaviour differs, and **nothing may be reimplemented here** — the grants, the router table and every rule live in the tier's own command, which is why this alias holds `Skill` and `Read` and nothing else.

⚠️ **The tier is resolved, never guessed.** A name matching both a topic page and a goal page is the ambiguity `docs/subject-resolution.md` § The page test already forbids resolving: print both candidates and stop.
