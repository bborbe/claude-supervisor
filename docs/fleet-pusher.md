# The Fleet Pusher — the cheap wake cadence

Two layers keep the fleet moving, and they cost very different amounts:

| Layer | What it does | Cost | When |
|---|---|---|---|
| **Fleet Pusher** (inside `claude-wrapper`) | reads every live session, joins pid → tty → pane, runs the screen guard, and types one line into an idle composer | **no model turn** | every 15 min, in-process |
| **Fleet triage** (`/supervisor:fleet-triage`) | one pass that classifies, closes, drives and cards | one model turn | on demand |

The pusher is the layer that reaches an idle session without the operator. It is **not** a triage
pass: "the cadence is running" and "triage is running" are different answers, and a pusher that
wakes panes has done its whole job.

⚠️ **Never put `fleet-triage` on the tick.** Each run is a model turn, and a session that runs it
repeatedly fills its own context until it can no longer do the job.

## Where it runs

The tick and its watchdog are **interval jobs inside the `claude-wrapper` daemon**, not launchd
agents. The old `com.bborbe.fleet-pusher` / `com.bborbe.fleet-pusher-watch` plists and their bash
scripts are retired; the work they did is now `pkg/fleet_pusher.go` and
`pkg/fleet_pusher_watch.go` in `bborbe/claude-wrapper`.

The move exists because the service already held three of the four pieces — the live-session
registry, the pane writer, and the attention client. The fourth, the pid → tty → pane join, was
the only thing that had to be written (`pkg/pane_join.go`).

Flags on the daemon:

| Flag | Default | Meaning |
|---|---|---|
| `-fleet-pusher-interval` | `15m` | how often to wake; `0` disables the tick |
| `-fleet-pusher-heartbeat` | `~/.claude/state/fleet-pusher/heartbeat` | the file the tick stamps and the watchdog reads |
| `-fleet-pusher-watch-interval` | `5m` | how often the heartbeat is checked; `0` disables the watchdog |
| `-fleet-pusher-watch-max-age` | `30m` | heartbeat age past which the cadence is declared dead |
| `-fleet-pusher-once` | off | run one tick and exit, for a scoped probe; never binds the listener, never writes the heartbeat |
| `-send` | off | type into idle sessions in a `-fleet-pusher-once` probe; the daemon's own tick always sends |
| `-sessions` | empty | comma-separated ids to scope a `-fleet-pusher-once` probe to |

## What one tick does

1. **Live sessions** from the wrapper's own registry (`pkg/session_registry.go`), pid-checked.
2. **Join** pid → tty (`ps`) → pane (`wezterm cli list`) — `pkg/pane_join.go`.
3. **Skip classes**, in order: `skip:self`, `skip:fleet-manager`, `skip:pusher`, `skip:not-live`,
   `skip:no-pane`, `skip:open-card`.
4. **Guard** the screen (`pkg/pane_guard.go`, ported from `pane-wake-guard.py`): 13 screen verdicts
   plus the fail-closed `skip:state-unreadable`. Panes are read with `--escapes`, because the dim
   next-prompt suggestion is only distinguishable from typed text by its SGR attributes.
5. **Send** through the wrapper's own `SendWhenReady` (`pkg/tab.go`), with the wake path's own
   readiness and confirmation rules: `PaneIsReady` accepts a dim placeholder, and `WakeConfirmed`
   takes a spinner over an empty composer as proof — the same signal `pane-wake.py` used.

A wake is never an authorisation; the typed line says so.

## The log lines

Both jobs emit their line **unconditionally, zeros included** — the retired bash script printed its
counter only when non-empty, so a healthy idle tick and a dead one looked identical.

```
job=fleet-pusher action=tick sessions=<n> delivered=<n> skipped=<n>
job=fleet-pusher action=deliver session=<id> outcome=<delivered|dry-run|skip:*|failed:*>
job=fleet-pusher-watch action=ok age=<n>s
job=fleet-pusher-watch action=grace age=<n>s
job=fleet-pusher-watch action=post key=<key> card=<id>
job=fleet-pusher-watch action=skip key=<key> card=<id>
```

A fleet-wide read failure appends `error=<reason>` to the tick line rather than suppressing it.

## Why a watchdog

The tick **cannot report its own death**: if the tick is the thing that stopped, nothing inside it
runs. The watchdog runs in the same daemon, reads the heartbeat, and past the max age posts one
`ack` card. It is a **condition, not a question**, deduped on a fixed key so a long outage yields
one card rather than one per check.

Three dedup keys, one per failure mode:

| Key | Means |
|---|---|
| `fleet-pusher-heartbeat-missing` | the heartbeat file does not exist — the cadence never ran |
| `fleet-pusher-heartbeat-unparseable` | the file holds something that is not an ISO-8601 UTC stamp |
| `fleet-pusher-heartbeat-stale` | the stamp is older than the max age — the cadence has stopped |

Two behaviours keep it honest:

- **The first cycle waits one interval.** The tick writes its first heartbeat on its own first tick;
  a watchdog that ran first after a restart with a long downtime would post a false `stale` card.
- **A boot younger than the max age is a startup grace**, not a dead cadence. The machine's boot
  time is read from `sysctl kern.boottime` and injected, so the branch is testable.

The watchdog still detects a cadence that *stopped*, not one that runs and fails. A
running-and-failing tick shows up as `error=` on the tick line and as `failed:*` outcomes.

## Things that look healthy and are not

1. **A fresh heartbeat is not proof of a delivery.** The tick writes it *before* any work, so a tick
   with nothing to do and a dead tick produce the same stamp. Read `delivered=N`.
2. **`skip:*` is the normal outcome on a healthy fleet.** Most sessions are headless
   (`skip:no-pane`) or resting on a machine wait (`skip:machine-wait`). A tick with
   `delivered=0 skipped=40` has done its whole job.
3. **A watchdog that cries wolf gets ignored.** The original parsed the stamp with BSD `date`, whose
   `%Z` does not accept a literal trailing `Z`, and posted false cards for an eight-minute-old
   heartbeat. The port parses with Go's `time.Parse` on the exact layout.
4. **A failed liveness read is not an empty fleet.** The registry read is an error, and the tick
   line carries `error=registry: …` rather than `sessions=0`. Collapsing the two is the
   silent-nothing-woke failure this layer exists to remove.
5. **The dim composer is the wake target, not a refusal.** A long-idle pane always shows Claude
   Code's dim next-prompt suggestion; a guard that refused it would starve the wake layer of exactly
   the sessions it exists for. This is why panes are read with `--escapes` and why the wake path's
   confirmation is escape-safe.

## Rollback

If the in-process jobs must be retired before the fix is trusted:

```bash
git revert <the deletion commit>          # in bborbe/claude-supervisor
cd ~/.claude && make update               # refreshes the marketplace clone
cd ~/.claude/plugins/marketplaces/claude-supervisor
cp launchd/com.bborbe.fleet-pusher.plist ~/Library/LaunchAgents/
cp launchd/com.bborbe.fleet-pusher-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.bborbe.fleet-pusher.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.bborbe.fleet-pusher-watch.plist
```

The `cp` runs from the marketplace clone, not from `~/.claude` — the plists live in the
checkout, and a repo-relative path there resolves against `~/.claude/launchd`, which does not exist.

Then disable the in-process jobs with `-fleet-pusher-interval=0 -fleet-pusher-watch-interval=0` so
two wake layers do not run at once.

## Files

| Path | Role |
|---|---|
| `bborbe/claude-wrapper` `pkg/fleet_pusher.go` | the tick |
| `bborbe/claude-wrapper` `pkg/fleet_pusher_watch.go` | the watchdog |
| `bborbe/claude-wrapper` `pkg/pane_guard.go` | the screen → verdict function |
| `bborbe/claude-wrapper` `pkg/pane_join.go` | pid → tty → pane |
| `bborbe/claude-wrapper` `pkg/pane_text.go` | escape stripping, dim parsing, composer readiness |
| `bborbe/claude-wrapper` `pkg/subject_state.go` | the `SLEEPING` / awake subject read |

Logs: the daemon's own stderr log, `~/Library/Logs/claude-wrapper/stderr.log`.
