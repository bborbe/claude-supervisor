#!/usr/bin/env python3
"""permission-answer-poll.py -- answer a live permission prompt from outside its pane.

Runs as the plugin's `PermissionRequest` hook (`hooks/hooks.json`). While the
permission box is on screen, it polls the attention store for the card this
prompt produced. If an arm answers that card, this hook returns the decision and
the prompt closes with no keystroke in the worker's pane.

Measured 2026-09-27 on live gates, and the design depends on each point:

  * The permission box renders WHILE this hook runs. Blocking here never hides
    a prompt from the human in the pane.
  * An in-pane answer wins while the hook still runs, and a late decision from
    this hook is then ignored. The race is safe both ways: first answer decides.
  * An in-pane approval does NOT kill this hook, which is why the loop exits
    once the gate closes instead of sleeping to its deadline.

Store contract, mirrored from `server/attention-poll.mjs` rather than invented:
`GET {store}/api/1.0/attention/{item_id}`. An answer counts only when
`state == "answered"`, `decision` is `allow` or `deny`, and `resolved_by` is set.
That last field is the operator's 2026-09-26 ruling: a permission gate is
released only by an arm answer.

Fail-open by construction: an error, a malformed response or the deadline emits
NOTHING and exits 0, so the normal prompt stays for the human. This script never
returns a decision it did not read -- a false `allow` would run a command nobody
approved.

⚠️ Who may answer is a governance question, not this script's. The auto-mode
classifier refuses a manager's answer to a Bash/Write prompt unless the operator
authorized that approval in the manager's conversation (measured 2026-09-27).
This hook only carries a decision; it never makes one.

Kill switch: SUPERVISOR_PERMISSION_POLL=off makes the hook exit at once.
Headless supervisor workers (SUPERVISOR_WORKER_MODE=headless) are skipped: their
server already parks and answers the prompt, and polling first would delay that park.

Usage (manual / tests):
  permission-answer-poll.py --item-id <id>      [--timeout 540] [--interval 2]
  permission-answer-poll.py --session-id <sid>  [--timeout 540]
  permission-answer-poll.py --once --item-id <id>   # one poll, diagnosis on stderr
As a hook it takes no arguments and reads the hook JSON from stdin.
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


attention_store_auth = _load("attention_store_auth", "attention-store-auth.py")

DECISIONS = ("allow", "deny")
DEFAULT_TIMEOUT = 540.0   # under the harness's 600 s command-hook default
DEFAULT_INTERVAL = 2.0
HTTP_TIMEOUT = 5.0


def store_url() -> str:
    """Same two env names the server reads, so the arm and the poll cannot disagree."""
    return (
        os.environ.get("SUPERVISOR_ATTENTION_STORE")
        or os.environ.get("ATTENTION_STORE_URL")
        or "http://localhost:18080"
    ).rstrip("/")


def get_json(url: str):
    """(ok, data_or_reason). Never raises — a transport failure must not become a decision."""
    try:
        req = urllib.request.Request(
            url, headers=attention_store_auth.headers({"Accept": "application/json"})
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            body = r.read().decode("utf-8", errors="replace")
        return True, json.loads(body)
    except urllib.error.HTTPError as e:
        return False, f"store returned HTTP {e.code} for {url}"
    except Exception as e:  # transport, DNS, timeout, malformed JSON
        return False, f"{type(e).__name__}: {e}"


def verdict(item):
    """(ok, decision, reason) — the three conditions, in the order the versioned half checks them."""
    if not isinstance(item, dict):
        return False, None, "the store returned no item object"
    state = item.get("state")
    if state != "answered":
        return False, None, f"state is {state!r}, not 'answered'"
    decision = item.get("decision")
    if decision in (None, ""):
        return False, None, "the item carries no decision, so there is nothing to settle"
    if decision not in DECISIONS:
        return False, None, f"decision {decision!r} is not one of {'/'.join(DECISIONS)}"
    resolved_by = item.get("resolved_by")
    if not isinstance(resolved_by, str) or resolved_by == "":
        return (
            False,
            None,
            "the item carries no resolved_by, so no arm delivered this answer, "
            "and a permission gate releases only on an arm answer",
        )
    return True, decision, f"answered by arm {resolved_by!r}"


def resolve_item_for_session(store: str, sid: str, expect=None, not_before=None):
    """(ok, item_id_or_reason). Newest open permission-class item produced by `sid`."""
    ok, data = get_json(f"{store}/api/1.0/attention")
    if not ok:
        return False, data
    items = data if isinstance(data, list) else (data.get("items") or data.get("data") or [])
    if not isinstance(items, list):
        return False, "the store's list endpoint returned no item array"
    mine = [
        i for i in items
        if isinstance(i, dict) and i.get("producer_id") == sid
    ]
    if not mine:
        return False, f"no attention item names producer {sid}"
    # Permission-class ONLY. A fallback to any item from this producer would let an
    # answered message/question card be read as a permission decision.
    perms = [i for i in mine if i.get("answer_mechanism") == "permission"]
    # A card left open by an earlier, already-answered gate of the same session must
    # never be matched: answering it would release THIS gate under another's subject.
    # Measured 2026-09-27: an in-pane approval left its store card `open` for 9+ min.
    if expect is not None:
        perms = [i for i in perms if str(i.get("payload") or "").startswith(expect)]
    if not_before is not None:
        from datetime import datetime
        def created(i):
            try: return datetime.fromisoformat(str(i.get("created_at")).replace("Z","+00:00")).timestamp()
            except Exception: return 0.0
        perms = [i for i in perms if created(i) >= not_before - 5]
    if not perms:
        return False, f"producer {sid} has no permission-class item yet"
    if len(perms) > 1:
        return False, f"producer {sid} has {len(perms)} permission items — ambiguous, refusing to guess"
    return True, perms[0].get("item_id")


def gate_still_open(sid: str) -> bool:
    """False once the session's local feed item is no longer this permission gate.

    Measured 2026-09-27: an operator's in-pane answer does NOT kill a running
    PermissionRequest hook — it runs to its own deadline — and a late decision is
    ignored. So a poll that never checks would sleep up to its deadline after every
    prompt answered in-pane. Unreadable counts as open: stopping early is the only
    thing this check may cause, never a decision.
    """
    if not sid:
        return True
    # Same env name attention-log.py honours, so writer and reader cannot disagree.
    state = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser("~/.claude/state/attention")
    p = os.path.join(state, f"{sid}.open.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh).get("kind") == "permission"
    except FileNotFoundError:
        return False
    except Exception:
        return True


def emit(decision: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": decision},
        }
    }))


def main() -> int:
    if os.environ.get("SUPERVISOR_PERMISSION_POLL", "").lower() == "off":
        return 0
    # A headless supervisor worker already has an answer path: its server parks the
    # prompt in `canUseTool` and `attention-poll.mjs` settles it. This hook fires
    # BEFORE that park, so polling here would hold every headless gate up to the
    # deadline before it ever parked. `workerEnvFor` (server/spawn-mode.mjs) exports
    # the mode into the worker's environment, and hooks inherit it.
    if os.environ.get("SUPERVISOR_WORKER_MODE", "") == "headless":
        return 0
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--item-id")
    ap.add_argument("--session-id")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--once", action="store_true",
                    help="poll once, print a diagnosis to stderr, never emit a decision")
    args = ap.parse_args()

    store = store_url()
    item_id = args.item_id
    expect = None
    if not item_id and not args.session_id and not sys.stdin.isatty():
        try:
            hook = json.load(sys.stdin) or {}
            args.session_id = hook.get("session_id")
            ti = hook.get("tool_input") or {}
            # Same summary attention-log.py's tool_detail() writes, so the store payload
            # for THIS gate reads "<tool>: <detail>".
            detail = ti.get("command") or ti.get("description") or ti.get("file_path") or json.dumps(ti)[:120]
            expect = f"{hook.get('tool_name','')}: {detail}"[:200]
        except Exception:
            pass
    hook_started = time.time()

    if not item_id and not args.session_id:
        print("[poll] need --item-id or --session-id", file=sys.stderr)
        return 0

    deadline = time.monotonic() + args.timeout
    last_reason = "no poll completed"

    # Hooks run in parallel, so attention-log.py may not have written the gate's feed
    # item yet when this poll starts. "Closed" therefore means: seen open, then gone.
    # Never seen open within GRACE seconds -> not a feed-tracked gate; stop quietly.
    GRACE = 10.0
    started = time.monotonic()
    seen_open = False
    while True:
        if args.session_id and not args.once:
            is_open = gate_still_open(args.session_id)
            if is_open:
                seen_open = True
            elif seen_open:
                print("[poll] gate closed locally — answered elsewhere; exiting", file=sys.stderr)
                return 0
            elif time.monotonic() - started > GRACE:
                print(f"[poll] no permission gate in the feed after {GRACE:.0f}s; exiting", file=sys.stderr)
                return 0
        if not item_id:
            # The watcher pushes to the store asynchronously, so the item usually does
            # not exist yet when the hook fires. Resolve on every pass, not once.
            ok, res = resolve_item_for_session(store, args.session_id, expect, hook_started)
            if ok and res:
                item_id = res
            else:
                last_reason = res
                ok, data = False, res
        if item_id:
            ok, data = get_json(f"{store}/api/1.0/attention/{item_id}")
        if ok:
            good, decision, reason = verdict(data)
            last_reason = reason
            # A card that is neither open nor answered will never carry a decision.
            # Measured 2026-09-27: a poll kept polling a `closed` card to its deadline.
            if not good and isinstance(data, dict) and data.get("state") not in ("open", "answered", None):
                print(f"[poll] card {item_id} is {data.get('state')!r}; exiting", file=sys.stderr)
                return 0
            if good:
                if args.once:
                    print(f"[poll] WOULD EMIT behavior={decision!r} ({reason})", file=sys.stderr)
                    return 0
                emit(decision)
                return 0
        else:
            last_reason = data

        if args.once:
            print(f"[poll] no decision yet: {last_reason}", file=sys.stderr)
            return 0

        if time.monotonic() + args.interval >= deadline:
            # Fail open: emit nothing, so the human gets the normal prompt.
            print(f"[poll] deadline after {args.timeout:.0f}s — last: {last_reason}", file=sys.stderr)
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never take the gate down with the poll
        print(f"[poll] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)
