#!/usr/bin/env python3
"""Emit `ANSWERED <item-id>` when the operator answers a board card this session posted.

The gap this closes: a manager learns an answer only when it next runs
`attention-ask.py poll <id>` at the head of a tick, so an answer can sit unseen
for up to the tick interval (default 15 min) — and a manager idle at its prompt
never learns at all until its `ScheduleWakeup` fires. Every armed watcher emits
on a NEW gate; nothing emitted on an ANSWERED one.

Where the signal comes from — measured 2026-10-01, not assumed:

  GET /api/1.0/attention/stream    the store's live change channel. It fires on
                                   every write including the answer path —
                                   `pkg/attention-store-notifying.go` calls
                                   `notifier.Notify()` from `Answer` as well as
                                   from `Push`. Push-driven, so the latency is a
                                   round trip rather than a poll interval.
  GET /api/1.0/attention/{id}      one item in any state — cheap, and the only
                                   thing that says WHAT changed.

⚠️ Two sources that look right and are not:

  - `GET /api/1.0/attention` (what `who-needs-me.py` reads) returns **open**
    items only and prunes dead askers, so an answered item is absent from it by
    construction. This line cannot be produced from that read path.
  - Polling `GET /api/1.0/attention/history` — measured 2026-10-01 at
    **16,358,494 bytes / 19,167 items** per call. Fine ONCE per connect as a
    baseline; not viable as a poll.

⚠️ The stream carries a **rendered row** (`{type, item_id, html}`) for the board
page, not structured item data. This arm reads only `item_id` from it and
re-fetches the item, so the page's row template is never a dependency.

⚠️ `state == "answered"` is NOT the predicate. The rule has one home —
`scripts/answered-attribution.py` — and `NOT_OPERATOR_ANSWERED` must fire
nothing here, exactly as it releases nothing in `attention-ask.py poll`. This
arm calls `operator_answered()` and does not restate the rule.

Keying: `Item.ProducerID` (`pkg/attention-item.go`), which `attention-ask.py`
sets from `$CLAUDE_CODE_SESSION_ID`. A card posted by a **different** session
must not fire here — that is the negative probe, and it is what makes this a
watcher for *this* session's cards rather than a second board reader.

⚠️ This is a DISTINCT signal from the NEW GATE watcher, not a second doorbell.
It emits on an answer and the doorbell emits on a raise, so arming both does not
double-notify and the runbook's "arm exactly ONE" rule is unaffected.
"""
import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "10"))
# A long-lived stream: the read blocks between events, so this timeout is
# generous and its expiry is treated as a drop-and-reconnect, not as an error.
STREAM_TIMEOUT = float(os.environ.get("ANSWERED_WATCH_STREAM_TIMEOUT", "900"))
RECONNECT_DELAY = float(os.environ.get("ANSWERED_WATCH_RECONNECT_DELAY", "5"))

# Sentinel yielded once per (re)connect, so the caller re-baselines against a
# store it has provably reached rather than one it merely intends to reach.
CONNECTED = object()


def _load(name, filename):
    """Load a sibling script by path — the repo's convention for hyphenated modules."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolved_session_id(environ=None):
    """The session id of whoever is running this arm, or "".

    Claude Code exports `CLAUDE_CODE_SESSION_ID` into any Bash a session runs, so
    the poster is resolvable without the caller passing a flag. "" is a real case
    and is refused by `main` rather than papered over: an empty session id would
    match every item whose producer declared none.
    """
    return (os.environ if environ is None else environ).get("CLAUDE_CODE_SESSION_ID", "")


def parse_ts(value):
    """Parse an RFC3339 timestamp, or None when it is absent or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def items_of(payload):
    """The item list from a store read, tolerating a bare list or a wrapper."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return items
    return []


def fetch_json(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_history(store):
    return items_of(fetch_json(f"{store}/api/1.0/attention/history", STORE_TIMEOUT))


def fetch_item(store, item_id):
    return fetch_json(f"{store}/api/1.0/attention/{item_id}", STORE_TIMEOUT)


def is_mine(item, session):
    """True when this session posted the item.

    ⚠️ Keyed on `producer_id`, never on the dedup key or the payload text: two
    sessions asking the same question must not answer for each other.
    """
    return bool(session) and item.get("producer_id") == session


def emit(item_id, out):
    print(f"ANSWERED {item_id}", file=out, flush=True)


def reconcile(store, session, seen, started_at, attribution, out):
    """Fold the store's full history into `seen`, emitting only post-start answers.

    Called on every (re)connect. On the first call `seen` is empty, so every one
    of this session's answered cards is adopted — and those answered **before**
    `started_at` are adopted silently, because a watcher must not wake its
    session for an answer that predates it. On a later call the same pass is the
    catch-up for anything answered while the stream was down.
    """
    for item in fetch_history(store):
        if not is_mine(item, session):
            continue
        item_id = item.get("item_id")
        if not item_id or item_id in seen:
            continue
        if not attribution.operator_answered(item):
            continue
        seen.add(item_id)
        answered_at = parse_ts(item.get("answered_at"))
        if answered_at is None or answered_at >= started_at:
            emit(item_id, out)
    return seen


def handle_item(store, session, seen, item_id, attribution, out, err):
    """Emit for one changed item, when it is this session's and the operator answered it.

    Returns `seen`, which it also mutates — symmetric with `reconcile`, and the
    return is what makes the adopt-vs-emit decision assertable in a test rather
    than only observable through `out`.
    """
    if item_id in seen:
        return seen
    try:
        item = fetch_item(store, item_id)
    except (urllib.error.URLError, OSError) as exc:
        # A single unreadable item must not kill the watch: the reconnect pass
        # picks it up from the history if it was a real answer.
        print(f"WARN: could not read {item_id}: {exc}", file=err, flush=True)
        return seen
    if not isinstance(item, dict):
        return seen
    if not is_mine(item, session) or not attribution.operator_answered(item):
        return seen
    seen.add(item_id)
    emit(item_id, out)
    return seen


def stream_item_ids(store, err):
    """Yield CONNECTED per (re)connect, then one item id per store change, forever."""
    url = f"{store}/api/1.0/attention/stream"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=STREAM_TIMEOUT) as resp:
                yield CONNECTED
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:"):].strip())
                    except ValueError:
                        continue
                    item_id = event.get("item_id") if isinstance(event, dict) else None
                    if item_id:
                        yield item_id
        except (urllib.error.URLError, OSError) as exc:
            print(f"WARN: stream dropped ({exc})", file=err, flush=True)
        time.sleep(RECONNECT_DELAY)


def watch(store, session, attribution, out, err):
    started_at = datetime.now(timezone.utc)
    seen = set()
    for event in stream_item_ids(store, err):
        if event is CONNECTED:
            seen = reconcile(store, session, seen, started_at, attribution, out)
            continue
        seen = handle_item(store, session, seen, event, attribution, out, err)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", default=STORE, help="attention store base URL")
    parser.add_argument(
        "--session",
        default=None,
        help="poster session id (default: $CLAUDE_CODE_SESSION_ID)",
    )
    args = parser.parse_args(argv)

    session = args.session if args.session is not None else resolved_session_id()
    if not session:
        print(
            "FAILED: no session id — $CLAUDE_CODE_SESSION_ID is unset and --session was not "
            "given. An empty id would match every item whose producer declared none.",
            file=sys.stderr,
        )
        return 1

    attribution = _load("answered_attribution", "answered-attribution.py")
    try:
        watch(args.store.rstrip("/"), session, attribution, sys.stdout, sys.stderr)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
