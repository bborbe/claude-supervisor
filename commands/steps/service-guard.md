---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Service guard

When: every tick, as the fifth reading of `steps/readings.md` (router row 15) — run: `python3 $P/service-guard.py --apply`

A standing resource guard for the long-running local services the fleet depends on. Each round reads every guarded service's memory footprint and CPU against its bar, and on a breach reopens the task row that owns the service — so a service that drifts back is caught by the round, not by the operator reading Activity Monitor.

## What it reads

The config is `~/.claude/state/service-guard.json` — one entry per service: its launchd `label`, `max_footprint_mb`, optional `max_cpu_pct`, and the owning `task` + `vault`. The bars are the owning tasks' own success criteria; change them there first, then here. No config prints `service-guard: no config` — the machine is unguarded, which is a statement, not an error.

Memory is `footprint -p <pid>` → `phys_footprint`, **never `ps -o rss`**: on macOS the two diverge by more than an order of magnitude (measured 2026-11-02 — 18 MB RSS against 5,522 MB footprint), and every earlier reading that used RSS was wrong. CPU is `ps -o pcpu=`.

## What it does

- `OK` — nothing.
- `BREACH` — with `--apply`, a terminal owning row (`completed` / `aborted`) is set back to `status: in_progress`, `phase: execution`, and the line reads `REOPENED`. An open row is left as it is. Print every `BREACH` and `REOPENED` line in the round's report; a reopened row is approved work and enters the open leg (router row 16) like any other.
- `UNKNOWN` — the pid or footprint could not be read; the script exits non-zero. Print it as it comes back and never round it to `OK`: "within bounds" and "could not measure" must not render the same.

⚠️ **The guard reopens; it never restarts.** A restart returns a process to its baseline and proves nothing about the leak, and it is production-touching besides. Restarting a guarded service stays the owning row's decision, under its own approval.
