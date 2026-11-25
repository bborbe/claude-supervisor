---
status: in_progress
phase: execution
flag: true
flag_set_by: operator
flag_set_at: 2026-11-24T17:10:00+01:00
stopped: true
stop_date: 2026-11-24T18:05:33+01:00
---
Tags: [[Task]]

---

Fixture for the stand-down ordering rule. The operator flagged this row at 17:10 and
stopped it at 18:05:33, so the stop is the newest word and **both readers skip it**.

Its pair is `flag-after-stop.md`, which differs only in the ordering of those two
instants. Neither is a real task: they are test artefacts, and they live here rather
than in a vault because `flag: true` + `flag_set_by: operator` is approval at every
reader — a live row in this state would be opened by the running fleet.
