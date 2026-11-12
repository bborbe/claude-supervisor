#!/bin/bash
# Fleet pusher watchdog — report a dead cadence, ask nothing.
#
# WHY THIS IS A SEPARATE JOB: the pusher tick cannot report its own death. If the tick is the
# thing that stopped, nothing inside it runs. This is a second, independent launchd agent whose
# only job is to read the first one's heartbeat and say so when it ages.
#
# WHY THE HEARTBEAT AND NOT `launchctl list`: measured 2026-11-12, the tick ran for ~35 minutes
# with the launchd PATH missing `wezterm`. Every tick exited 0 and woke nothing. `launchctl list`
# cannot tell that from a working tick — and neither can the heartbeat alone. What the heartbeat
# DOES answer is whether the tick is still being STARTED at all, which is the failure this job
# owns; a tick that starts and then fails is a different question, answered below.
#
# WHAT THIS DOES NOT DETECT, stated so it is not assumed: a tick that RUNS AND FAILS. The
# heartbeat is written before any work, so a tick that dies mid-pass still leaves a fresh stamp
# and this watchdog stays quiet. That failure surfaces in `launchctl list` as a non-zero last
# exit and in the log as a `TERMINAL:` line — a running-and-failing cadence is a different
# question from a stopped one, and it is answered by those two reads, not here.
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

# Post one card, and FAIL LOUDLY when the post itself fails. The previous form sent every post
# through `>/dev/null 2>&1` and ignored the exit status, so a broken `attention-ask.py` made the
# watchdog silent in exactly the situation it exists for — a dead cadence, reported to nobody.
post() {
  if ! python3 "$ASK" post \
      --dedup-key "$1" \
      --producer-kind cron \
      --mechanism ack \
      --interrupt-class routine \
      --payload "$2" >/dev/null 2>&1; then
    echo "TERMINAL: attention-ask.py post failed for '$1' — the card was NOT posted"
    exit 1
  fi
}

if [ ! -f "$HB" ]; then
  echo "$(date +%H:%M:%S) heartbeat missing — posting"
  post fleet-pusher-heartbeat-missing "Fleet pusher cadence: the heartbeat file ${HB} does not exist, so the wake cadence has never run on this machine. launchd may report the job as loaded and healthy while nothing ticks."
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
  post fleet-pusher-heartbeat-unparseable "Fleet pusher cadence: the heartbeat file holds '${STAMP}', which is not an ISO-8601 UTC timestamp. The watchdog cannot tell whether the cadence is alive."
  exit 0
fi

NOW="$(date -u +%s)"
AGE=$((NOW - EPOCH))

if [ "$AGE" -gt "$MAX_AGE" ]; then
  echo "$(date +%H:%M:%S) STALE age=${AGE}s > ${MAX_AGE}s — posting"
  post fleet-pusher-heartbeat-stale "Fleet pusher cadence: the heartbeat is ${AGE}s old (threshold ${MAX_AGE}s), so the cheap wake layer has stopped. launchd may still list com.bborbe.fleet-pusher as loaded — that is exactly the state that looks healthy and wakes nothing. Check ~/Library/Logs/fleet-pusher.log."
  exit 0
fi

echo "$(date +%H:%M:%S) ok age=${AGE}s"
exit 0
