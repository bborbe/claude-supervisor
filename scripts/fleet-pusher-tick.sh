#!/bin/bash
# Fleet pusher cadence — ONE tick. Repeats via launchd StartInterval, never via an internal loop.
#
# WHY NO LOOP: a detached loop is the failure mode com.bborbe.fleet-keeper.plist documents —
# "one long-lived process that can die silently and take the supervision with it". This script
# does one pass and exits; `StartInterval` gives a fresh process per tick, so there is no loop
# to die. The previous implementation was /tmp/fleet-pusher-loop.sh (40 rounds x 900s), which
# had no supervisor, was not running, and would have evaporated on reboot.
#
# WHY NO MODEL TURN: this is the CHEAP layer. It runs pane-wake.py, which types one line into an
# idle session's composer and exits. The expensive layer is /supervisor:fleet-triage — one model
# turn, run on demand. Do not put fleet-triage on this tick: each run is a model turn, and a
# session that runs it repeatedly fills its own context until it can no longer do the job.
#
# Install (the plist lives beside this script, in the plugin's launchd/):
#   cp launchd/com.bborbe.fleet-pusher.plist ~/Library/LaunchAgents/
#   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.bborbe.fleet-pusher.plist
# Reload:
#   launchctl kickstart -k gui/$UID/com.bborbe.fleet-pusher
set -uo pipefail

STATE="$HOME/.claude/state/fleet-pusher"
mkdir -p "$STATE"
HB="$STATE/heartbeat"

# Resolve the NEWEST installed supervisor, never a pinned version: a pinned path silently runs
# pre-fix behaviour after every upgrade (0.260.7 still called `activate-pane` and stole focus).
# The plist's ProgramArguments points at the marketplace clone — the stable entry point, so an
# upgrade does not orphan the agent — while THIS resolution deliberately reads the cache, so the
# tick always drives the current code. Both halves are needed; neither alone is enough.
SUP_ROOT="$HOME/.claude/plugins/cache/claude-supervisor/supervisor"
LATEST="$(ls -1 "$SUP_ROOT" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
S="$SUP_ROOT/$LATEST/scripts"

if [ -z "$LATEST" ] || [ ! -f "$S/pane-wake.py" ]; then
  echo "TERMINAL: pane-wake.py not found under $SUP_ROOT (resolved: '${LATEST:-none}')"
  exit 1
fi

# Heartbeat first: a dead tick leaves this ageing, which is the only signal a dead agent gives.
# ⚠️ Because it is written BEFORE any work, a fresh heartbeat does NOT mean wakes were delivered
# — a tick that resolves zero sessions writes it and exits 0. Never read it as a success signal;
# fleet-pusher-watch.sh reads it only to detect death, and SC1 of the task that shipped this
# requires a `delivered=N` log line instead.
date -u +%Y-%m-%dT%H:%M:%SZ > "$HB"

# ⚠️ A FAILED LIVENESS READ IS NOT AN EMPTY FLEET. The previous form discarded stderr and the
# exit status and parsed `[]` out of a failure, so a broken `session-liveness.py` rendered as
# "0 live session(s)", the tick exited 0 with a fresh heartbeat, and the watchdog — which reads
# only the heartbeat — never fired. That is the same silent-nothing-woke failure this script
# exists to eliminate, one level up. The read is now distinguished from the empty result.
LIVE_RAW="$(python3 "$S/session-liveness.py" --list --json 2>/dev/null)"
LIVE_RC=$?
IDS="$(printf '%s' "$LIVE_RAW" | python3 -c '
import sys, json
raw = sys.stdin.read().strip()
if not raw:
    sys.exit(3)
try:
    d = json.loads(raw)
except Exception:
    sys.exit(3)
rows = d if isinstance(d, list) else d.get("sessions", [])
print(" ".join(x["sessionId"] for x in rows if x.get("alive")))
')"
PARSE_RC=$?

if [ "$LIVE_RC" -ne 0 ] || [ "$PARSE_RC" -ne 0 ]; then
  echo "TERMINAL: session-liveness.py unreadable (rc=$LIVE_RC, parse=$PARSE_RC) — cannot tell an idle fleet from a broken read"
  exit 1
fi

ARGS=()
n=0
for i in $IDS; do
  ARGS+=(--session "$i")
  n=$((n + 1))
done

echo "$(date +%H:%M:%S) tick — plugin $LATEST — $n live session(s)"

if [ "$n" -gt 0 ]; then
  # pane-wake.py's exit status, stated correctly (an earlier version of this comment had it
  # wrong and the error was nearly copied forward): 0 when every session ended `dry-run`,
  # `delivered` or a `skip:*`; 1 when any wake was attempted and not positively confirmed
  # delivered (`unverified` / `failed:*`); 2 on a read failure that prevented classification.
  # SKIPS ARE rc-NEUTRAL — a healthy fleet is mostly skips (`skip:no-pane`, `skip:machine-wait`,
  # `skip:open-card`), so its exit status says nothing about whether the pass ran.
  #
  # `pipefail` would surface rc=1 from an ordinary partially-delivered pass, which is why this
  # discriminates on OUTPUT instead: a pass that produced at least one JSON line ran; a pass
  # that produced none did not run at all, and only that is a failure.
  OUT="$(python3 "$S/pane-wake.py" "${ARGS[@]}" --send 2>&1)"
  STATS="$(printf '%s\n' "$OUT" | python3 -c '
import sys, json, collections
n_err = 0
c = collections.Counter()
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("error"):
        n_err += 1
        print("  ERROR: " + str(d["error"]), file=sys.stderr)
        continue
    c[str(d.get("outcome") or d.get("verdict") or "unknown")] += 1
if c:
    print("  " + " · ".join(f"{k}={v}" for k, v in c.most_common()))
else:
    print("  (no per-session verdicts)")
print("COUNTS %d %d" % (n_err, sum(c.values())))
')"
  printf '%s\n' "$STATS" | grep -v '^COUNTS ' || true
  ERR=$(printf '%s\n' "$STATS" | awk '/^COUNTS /{print $2}')
  NJSON=$(printf '%s\n' "$STATS" | awk '/^COUNTS /{print $3}')
  # A pane-wake `{"error": ...}` line is a broken ENVIRONMENT, not a skipped session — the
  # launchd PATH missing `wezterm` produced exactly this on 2026-11-12 while exiting 0. Never
  # let that read as a healthy tick: a green agent that wakes nothing is worse than a red one.
  if [ "${ERR:-0}" -gt 0 ]; then
    echo "TERMINAL: pane-wake.py returned $ERR error line(s) — environment is broken, not the fleet"
    exit 1
  fi
  # The success test is a COUNT of parsed JSON rows, not a glob over the raw text: a stray `{`
  # in a diagnostic line would satisfy a substring match while nothing was classified.
  if [ "${NJSON:-0}" -eq 0 ]; then
    echo "TERMINAL: pane-wake.py produced no parseable JSON row for $n session(s)"
    exit 1
  fi
  exit 0
fi

exit 0
