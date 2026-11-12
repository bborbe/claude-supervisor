#!/bin/bash
# Fleet pusher watchdog — report a dead cadence, ask nothing.
#
# WHY THIS IS A SEPARATE JOB: the pusher tick cannot report its own death. If the tick is the
# thing that stopped, nothing inside it runs. This is a second, independent launchd agent whose
# only job is to read the first one's heartbeat and say so when it ages.
#
# WHY THE HEARTBEAT AND NOT `launchctl list`: measured 2026-11-12, the tick ran for ~35 minutes
# with the launchd PATH missing `wezterm`. Every tick exited 0 and woke nothing, so the job
# looked perfectly healthy in `launchctl list` while doing no work at all. Only the heartbeat —
# which is written by the tick itself — distinguishes "running" from "running and succeeding".
#
# Threshold is 2x the tick's StartInterval (900s), so one slow tick does not raise a card.
#
# Install (the plist lives beside this script, in the plugin's launchd/):
#   cp launchd/com.bborbe.fleet-pusher-watch.plist ~/Library/LaunchAgents/
#   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.bborbe.fleet-pusher-watch.plist
# Remove:
#   launchctl bootout gui/$UID/com.bborbe.fleet-pusher-watch
set -uo pipefail

# Resolve the sibling poster from THIS script's own directory, never a hardcoded clone path: the
# watchdog ships in the same scripts/ dir as attention-ask.py, and an absolute path would break
# for anyone whose marketplace clone lives elsewhere.
HERE="$(dirname "${BASH_SOURCE[0]}")"
ASK="$HERE/attention-ask.py"

HB="$HOME/.claude/state/fleet-pusher/heartbeat"
MAX_AGE=1800

if [ ! -f "$HB" ]; then
  echo "$(date +%H:%M:%S) heartbeat missing — posting"
  python3 "$ASK" post \
    --dedup-key fleet-pusher-heartbeat-missing \
    --producer-kind cron \
    --mechanism ack \
    --interrupt-class routine \
    --payload "Fleet pusher cadence: the heartbeat file ${HB} does not exist, so the wake cadence has never run on this machine. launchd may report the job as loaded and healthy while nothing ticks." \
    >/dev/null 2>&1
  exit 0
fi

STAMP="$(cat "$HB" 2>/dev/null | tr -d '[:space:]')"
# Parse with python, NOT `date -j -f`: measured 2026-11-12, BSD `date`'s `%Z` does not accept a
# literal trailing `Z`, so it returned a bogus epoch and the watchdog posted two false cards for
# a heartbeat that was only 8 minutes old. A watchdog that cries wolf gets ignored, which is the
# same silent failure it exists to catch — so the case is pinned by a test, not just by this note.
EPOCH="$(printf '%s' "$STAMP" | python3 -c '
import sys, datetime
s = sys.stdin.read().strip()
try:
    d = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    print(int(d.timestamp()))
except Exception:
    pass
' 2>/dev/null)"

if [ -z "${EPOCH:-}" ]; then
  echo "$(date +%H:%M:%S) unparseable heartbeat '${STAMP}' — posting"
  python3 "$ASK" post \
    --dedup-key fleet-pusher-heartbeat-unparseable \
    --producer-kind cron \
    --mechanism ack \
    --interrupt-class routine \
    --payload "Fleet pusher cadence: the heartbeat file holds '${STAMP}', which is not an ISO-8601 UTC timestamp. The watchdog cannot tell whether the cadence is alive." \
    >/dev/null 2>&1
  exit 0
fi

NOW="$(date -u +%s)"
AGE=$((NOW - EPOCH))

if [ "$AGE" -gt "$MAX_AGE" ]; then
  echo "$(date +%H:%M:%S) STALE age=${AGE}s > ${MAX_AGE}s — posting"
  python3 "$ASK" post \
    --dedup-key fleet-pusher-heartbeat-stale \
    --producer-kind cron \
    --mechanism ack \
    --interrupt-class routine \
    --payload "Fleet pusher cadence: the heartbeat is ${AGE}s old (threshold ${MAX_AGE}s), so the cheap wake layer has stopped. launchd may still list com.bborbe.fleet-pusher as loaded — that is exactly the state that looks healthy and wakes nothing. Check ~/Library/Logs/fleet-pusher.log." \
    >/dev/null 2>&1
  exit 0
fi

echo "$(date +%H:%M:%S) ok age=${AGE}s"
exit 0
