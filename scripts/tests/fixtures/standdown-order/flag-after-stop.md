---
status: in_progress
phase: execution
flag: true
flag_set_by: operator
flag_set_at: 2026-11-24T19:36:58.408271+01:00
stopped: true
stop_date: 2026-11-24T18:05:33+01:00
---
Tags: [[Task]]

---

Fixture for the stand-down ordering rule. The operator stopped this row at 18:05:33 and
deliberately re-flagged it at 19:36:58.408271, so the flag is the newest word and
**both readers open it** — a presence-only guard would still call it stopped.

Its pair is `stopped-after-flag.md`, which differs only in the ordering of those two
instants. This is the measured 2026-11-24 sequence from
`Classify the Heat-Pump Penetration Seal`, whose live row cannot be replayed: its flag
was cleared again at 19:47:58, leaving only `stopped` + `stop_date` on the page.
