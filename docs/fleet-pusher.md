# The Fleet Pusher — the cheap wake cadence

Two layers keep the fleet moving, and they cost very different amounts:

| Layer | What it does | Cost | When |
|---|---|---|---|
| **Fleet Pusher** (`com.bborbe.fleet-pusher`) | `pane-wake.py` in send mode over every live session — types one line into an idle composer | **no model turn** | every 15 min, launchd |
| **Fleet triage** (`/supervisor:fleet-triage`) | one pass that classifies, closes, drives and cards | one model turn | on demand |

The pusher is the layer that reaches an idle session without the operator. It is **not** a triage
pass: "the cadence is running" and "triage is running" are different answers, and a pusher that
wakes panes has done its whole job.

⚠️ **Never put `fleet-triage` on the tick.** Each run is a model turn, and a session that runs it
repeatedly fills its own context until it can no longer do the job.

## Install

```bash
cp launchd/com.bborbe.fleet-pusher.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.bborbe.fleet-pusher.plist

cp launchd/com.bborbe.fleet-pusher-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.bborbe.fleet-pusher-watch.plist
```

Reload after an upgrade — the plists point at the **marketplace clone**, so the scripts follow
`make update` without a reload; the reload is only needed if the plist itself changed:

```bash
launchctl kickstart -k gui/$UID/com.bborbe.fleet-pusher
```

Remove:

```bash
launchctl bootout gui/$UID/com.bborbe.fleet-pusher
launchctl bootout gui/$UID/com.bborbe.fleet-pusher-watch
```

## Why two agents

The tick **cannot report its own death**: if the tick is the thing that stopped, nothing inside it
runs. `com.bborbe.fleet-pusher-watch` (`StartInterval` 300) reads
`~/.claude/state/fleet-pusher/heartbeat` and, past 1800 s (2× the tick interval, so one slow tick
raises nothing), posts one `ack` card. It is a **condition, not a question**, deduped on a fixed
key so a long outage yields one card rather than one per check.

Three dedup keys, one per failure mode:

| Key | Means |
|---|---|
| `fleet-pusher-heartbeat-missing` | the heartbeat file does not exist — the cadence never ran on this machine |
| `fleet-pusher-heartbeat-unparseable` | the file holds something that is not an ISO-8601 UTC stamp |
| `fleet-pusher-heartbeat-stale` | the stamp is older than 1800 s — the cadence has stopped |

## Two things the watchdog needs to actually work

Both were found in review, and either one alone makes the watchdog decorative.

**`--producer-id` is mandatory.** `attention-ask.py` defaults the producer to
`$CLAUDE_CODE_SESSION_ID`, and launchd provides no such variable, so a post without the flag is
**refused** — the watchdog would detect a dead cadence and report it to nobody. It passes a fixed
slug, `--producer-id fleet-pusher-watch`, which also makes every card carry the same `owner:`
liveness ref (never pruned).

**A boot is not a dead cadence.** Right after a reboot the tick has had no chance to run, so an
absent or ageing heartbeat is expected. The watchdog stays quiet while the machine has been up for
less than 1800 s. Residual, stated rather than hidden: a long **sleep** is not covered by boot
time — launchd runs the missed tick promptly on wake, but a watchdog pass landing first can still
post one card. That direction is deliberate: one false card is cheaper than a silent dead cadence.

## Five things that look healthy and are not

Every one of these was measured on this machine. Items 1, 2, 4 and 5 are each pinned by a named
test in `scripts/tests/test_fleet_pusher.py`; item 3 is not a defect a test can catch — it is a
rule the tick enforces at runtime.

1. **`launchctl list` showing exit `0` does not mean the cadence works.** Measured 2026-11-12:
   with `wezterm` missing from the launchd PATH, every tick exited **0** and woke nothing for ~35
   minutes. `pane-wake.py` shells out to `wezterm cli list`, which ships at
   `/Applications/WezTerm.app/Contents/MacOS/wezterm` — a path no system directory contains. The
   plist appends that directory **last**, and the tick treats a pane-wake `{"error": …}` line as a
   broken **environment** (exit 1, `TERMINAL:`) rather than one more skipped session. A green
   agent that wakes nothing is worse than a red one.

2. **A fresh heartbeat is not proof of a delivery.** The tick writes it *before* any work, and
   exits `0` when `session-liveness.py` returns no sessions. A dead cadence and a cadence with
   nothing to do produce the same heartbeat. Read the log's `delivered=N` line instead.

3. **A non-zero exit is normal on a healthy fleet.** `pane-wake.py` exits `0` when every session
   ended `dry-run`, `delivered` or a `skip:*`; `1` when a wake was attempted and not positively
   confirmed delivered; `2` on a read failure. Skips are **rc-neutral** — a healthy fleet is
   mostly skips (`skip:no-pane`, `skip:machine-wait`, `skip:open-card`). The tick therefore
   discriminates on output: at least one JSON line means the pass ran, none means it did not.

4. **A watchdog that cries wolf gets ignored.** The first version parsed the stamp with BSD
   `date -j -f "%Y-%m-%dT%H:%M:%SZ"`, whose `%Z` does not accept a literal trailing `Z`; it
   returned a bogus epoch and posted a card claiming a valid timestamp was unparseable — for a
   heartbeat eight minutes old. It parses with `datetime.strptime` now.

5. **A failed liveness read is not an empty fleet.** The tick originally discarded
   `session-liveness.py`'s stderr and exit status and parsed `[]` out of the failure, so a broken
   read rendered as "0 live session(s)", the tick exited 0 with a fresh heartbeat, and the
   watchdog — which reads only the heartbeat — stayed silent. It now separates the two: a
   non-zero exit or unparseable output is `TERMINAL:` and exit 1.

⚠️ **What the watchdog does not cover.** It detects a cadence that has *stopped*, not one that
runs and fails — the heartbeat is written before any work. A running-and-failing tick shows up in
`launchctl list` as a non-zero last exit and in the log as a `TERMINAL:` line.

## Why launchd and not a loop

The previous implementation was `/tmp/fleet-pusher-loop.sh` (40 rounds × 900 s). It had no
supervisor, was not running, and would have evaporated on reboot. A detached loop is the exact
failure mode `launchd/com.bborbe.fleet-keeper.plist` documents: *"one long-lived process that can
die silently and take the supervision with it."* `StartInterval` gives a fresh process per tick,
so there is no loop to die.

The same reasoning rejects two other hosts:

- **`/supervisor:fleet-loop` cannot host it.** That command is driven by `ScheduleWakeup`, which
  fires only when the REPL is idle (a manager fielding gate events starves its own round), and
  every round is a model turn — the exact cost this layer exists to avoid.
- **`fleet-keeper.py` cannot host it.** It is report-only by operator decision: it holds no kill,
  re-arm or spawn path and contains zero `pane-wake` references.

## Why the plist points at the clone and the tick reads the cache

Both halves are deliberate:

- The plist's `ProgramArguments` is the **marketplace clone**, so an upgrade does not orphan the
  agent — a version-pinned cache path would pin it to one release and silently run stale code
  after every upgrade.
- The tick resolves the **newest installed cache version** for `pane-wake.py`, so the work it
  drives is always current.

`test_a_pinned_version_is_observably_stale` runs that mutation as well as the real script: the
same test that demands the newest version shows a pinned copy producing the older one.

## Files

| Path | Role |
|---|---|
| `scripts/fleet-pusher-tick.sh` | one pass, exits |
| `scripts/fleet-pusher-watch.sh` | reads the heartbeat, posts when it ages |
| `launchd/com.bborbe.fleet-pusher.plist` | `StartInterval` 900, `RunAtLoad` |
| `launchd/com.bborbe.fleet-pusher-watch.plist` | `StartInterval` 300 |
| `scripts/tests/test_fleet_pusher.py` | the defects above, each as a case |

Logs: `~/Library/Logs/fleet-pusher.log` and `~/Library/Logs/fleet-pusher-watch.log`.
