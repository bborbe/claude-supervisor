#!/usr/bin/env python3
"""Answer an item on the attention stack and name the session it routes back to.

The store records the answer; this script never delivers it. Delivery is a
cross-session SendMessage, which only a Claude session can send, so the command
wrapping this script does it from the TARGET line printed here.

  next              list open items, oldest first, each with its delivery target
  answer ITEM_ID    POST /api/1.0/attention/{ITEM_ID}/answer, then print the target

Order matters in `answer`: the store's open -> answered compare-and-set runs
first, and a caller that loses it (409) must NOT route its answer -- another arm
already did. Only the winner prints a TARGET.

The answer carries two identities: `answered_by` names the arm, and `resolved_by`
names the session that ran it. The session id is read from the environment, so a
resolution is attributable without the caller passing a flag -- and the invoking
command stays untouched. An arm with no `CLAUDE_CODE_SESSION_ID` (a spawned child
is stripped of it) sends no `resolved_by` at all and says so on its own line: an
empty string would read as a *set* value in the store's split, which is the false
positive this field exists to close.

Target resolution: the item's `producer_id` is a session id; the session
registry (`~/.claude/sessions/<pid>.json`) maps it to the session `name`, which
is the address `SendMessage` takes. One line, three shapes:

  TARGET: <name>                     deliver to this name
  UNDELIVERABLE: session exited      no registry entry -- the asker is gone
  UNDELIVERABLE: name ambiguous ...  >1 session shares the name; a bare name
                                     would reach the wrong one, so refuse

Two classes route an answer, and they route it differently:

  message     the answer is text for a session. The wrapping command delivers
              it by SendMessage, to the name on the TARGET line printed here.
  permission  the answer is a verdict, given with --decision allow|deny. The
              spawning supervisor server delivers it by polling the store, so
              this script prints a DELIVERY line and never a TARGET: there is
              no session to send to, and printing one would invite a relay.

`ack` items are listed but refused -- they ask nothing, so there is nothing to
route.

A `permission` answer without an explicit --decision is REFUSED, never
defaulted. Neither default is safe: an implicit allow would release a gate
nobody approved, and an implicit deny would refuse one nobody refused. That
refusal is the whole guard -- it is what keeps this arm from becoming a path
that settles an irreversible prompt without an operator-supplied decision.

Run: python3 attention-answer.py next | answer ITEM_ID [--by ARM] [--decision allow|deny]
     python3 attention-answer.py attempt ITEM_ID --outcome delivered|failed [--by ARM]
"""

import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.request

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
# Local store; a hung one must cost a clear failure, never a stalled session.
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))
SESSIONS_DIR = os.environ.get("CLAUDE_SESSIONS_DIR") or os.path.expanduser("~/.claude/sessions")
DEFAULT_ARM = "supervisor:attention-next"
PAYLOAD_WIDTH = 160
# The only verdicts a permission-class item can be answered with. Kept here as
# the arm's own guard rather than only as documentation: the store rejects an
# unknown value too, but a local refusal costs no round trip and names the fix.
DECISIONS = ("allow", "deny")


def load_registry(sessions_dir=None):
    """Return every readable registry entry. An unreadable file is skipped, not fatal."""
    entries = []
    for path in glob.glob(os.path.join(sessions_dir or SESSIONS_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                entries.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return entries


def resolve_target(producer_id, registry):
    """Return (name, None) when deliverable, else (None, reason)."""
    matches = [e for e in registry if e.get("sessionId") == producer_id]
    if not matches:
        return None, "session exited"
    name = matches[0].get("name") or ""
    if not name:
        return None, "session has no name"
    sharing = [e for e in registry if e.get("name") == name]
    if len(sharing) > 1:
        return None, f"name ambiguous ({len(sharing)} sessions named {name!r})"
    return name, None


def target_line(producer_id, registry):
    name, reason = resolve_target(producer_id, registry)
    return f"TARGET: {name}" if name else f"UNDELIVERABLE: {reason}"


def fetch_open():
    with urllib.request.urlopen(f"{STORE}/api/1.0/attention", timeout=STORE_TIMEOUT) as resp:
        items = json.load(resp)
    items = [i for i in items if i.get("state") == "open"]
    return sorted(items, key=lambda i: i.get("created_at", ""))


def fetch_item(item_id):
    with urllib.request.urlopen(f"{STORE}/api/1.0/attention/{item_id}", timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def resolved_session_id(environ=None):
    """The session id of whoever is running this arm, or "".

    Claude Code exports CLAUDE_CODE_SESSION_ID into any Bash a session runs, so
    the arm's own session is resolvable here without the caller passing a flag.
    A spawned child is deliberately stripped of it, so "" is a real case rather
    than a bug, and it is reported rather than papered over: the store's split
    counts a *set* `resolved_by` as a manager resolution, so a blank string would
    read as one and reintroduce the false positive this field exists to close.
    """
    return (os.environ if environ is None else environ).get("CLAUDE_CODE_SESSION_ID", "")


def post_answer(item_id, answered_by, resolved_by="", decision=""):
    body = {"answered_by": answered_by}
    # Omitted rather than sent empty: downstream, "" is a set value, not an absent one.
    if resolved_by:
        body["resolved_by"] = resolved_by
    # The same rule as resolved_by, and it matters more here: the schema adds no
    # write-time rejection for an omitted decision, so sending "" would store a
    # *present* field holding no verdict instead of an absent one.
    if decision:
        body["decision"] = decision
    req = urllib.request.Request(
        f"{STORE}/api/1.0/attention/{item_id}/answer",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def cmd_next(registry, out=sys.stdout):
    items = fetch_open()
    if not items:
        print("No open items.", file=out)
        return 0
    for n, item in enumerate(items, 1):
        payload = " ".join(str(item.get("payload", "")).split())[:PAYLOAD_WIDTH]
        mech = item.get("answer_mechanism", "?")
        if mech == "message":
            route = target_line(item.get("producer_id", ""), registry)
        elif mech == "permission":
            # No session to name: the spawning server polls the store for the
            # verdict, so what the operator needs is the way in, not a recipient.
            route = "answerable here with --decision allow|deny (delivered by supervisor poll)"
        else:
            route = f"not answerable here ({mech})"
        print(f"{n}. [{item.get('interrupt_class', '?')}] {item.get('item_id')} -- {payload}", file=out)
        print(f"   {route}", file=out)
    return 0


def cmd_answer(item_id, answered_by, registry, out=sys.stdout, resolved_by=None, decision=""):
    item = fetch_item(item_id)
    mech = item.get("answer_mechanism")
    if mech == "permission":
        return cmd_answer_permission(item, answered_by, resolved_by, decision, out)
    if mech != "message":
        print(f"REFUSED: {item_id} is {mech}-class; it asks nothing, so there is nothing to route", file=out)
        return 2
    if resolved_by is None:
        resolved_by = resolved_session_id()
    try:
        answered = post_answer(item_id, answered_by, resolved_by)
    except urllib.error.HTTPError as err:
        if err.code == 409:
            # 409 covers both a lost compare-and-set and an item no longer open;
            # either way this arm did not win, so it must not route.
            print(f"LOST: {item_id} already answered or no longer open -- do not send", file=out)
        else:
            print(f"FAILED: store returned {err.code} for {item_id}", file=out)
        return 1
    print(f"ANSWERED: {item_id} at {answered.get('answered_at')} by {answered.get('answered_by')}", file=out)
    print(f"RESOLVED_BY: {resolved_by}" if resolved_by else
          "RESOLVED_BY: unknown -- CLAUDE_CODE_SESSION_ID unset, so this item will read as unresolved", file=out)
    print(target_line(item.get("producer_id", ""), registry), file=out)
    return 0


def cmd_answer_permission(item, answered_by, resolved_by, decision, out=sys.stdout):
    """Answer a permission-class item with a verdict, and name how it is delivered.

    The delivery is the spawning supervisor server polling the store, so this
    prints DELIVERY and never TARGET. A TARGET line here would be actively
    harmful rather than merely useless: the wrapping command branches on it, and
    a session cannot release another session's parked gate -- a relay is
    permission laundering even when the action looks small.
    """
    item_id = item.get("item_id")
    if decision not in DECISIONS:
        choices = "|".join(DECISIONS)
        given = decision if decision else "nothing"
        print(
            f"REFUSED: {item_id} is permission-class and needs --decision {choices}; "
            f"got {given!r}. Neither default is safe, so this arm has none.",
            file=out,
        )
        return 2
    if resolved_by is None:
        resolved_by = resolved_session_id()
    try:
        answered = post_answer(item_id, answered_by, resolved_by, decision)
    except urllib.error.HTTPError as err:
        if err.code == 409:
            print(f"LOST: {item_id} already answered or no longer open -- do not deliver", file=out)
        else:
            print(f"FAILED: store returned {err.code} for {item_id}", file=out)
        return 1
    print(f"ANSWERED: {item_id} at {answered.get('answered_at')} by {answered.get('answered_by')}", file=out)
    print(f"RESOLVED_BY: {resolved_by}" if resolved_by else
          "RESOLVED_BY: unknown -- CLAUDE_CODE_SESSION_ID unset, so this item will read as unresolved", file=out)
    print(f"DECISION: {decision}", file=out)
    print("DELIVERY: supervisor poll -- no session to send to, so do NOT SendMessage", file=out)
    return 0


def post_attempt(item_id, carrier, outcome):
    """Record what this arm observed about the delivery attempt.

    ⚠️ Called AFTER the delivery step, never at answer time. An attempt is a
    fact about the carrier: recording it where the answer is recorded would make
    every answer read as attempted, which is the defect the delivery trail
    exists to close. The store stamps `attempted_at` from its own clock, so an
    arm cannot date its own attempt.
    """
    body = {"carrier": carrier, "outcome": outcome}
    req = urllib.request.Request(
        f"{STORE}/api/1.0/attention/{item_id}/attempt",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def cmd_attempt(item_id, carrier, outcome, out=sys.stdout):
    """Record the outcome of a delivery this arm attempted.

    The outcome is refused rather than defaulted, on the same reasoning
    `--decision` carries: neither value is safe to assume. A default of
    `delivered` would record a success nobody observed, and a default of
    `failed` would record a failure nobody observed — and both would be
    indistinguishable, downstream, from a real measurement.
    """
    if outcome not in ("delivered", "failed"):
        print(
            f"REFUSED: --outcome must be delivered or failed, got {outcome!r}",
            file=out,
        )
        return 1
    post_attempt(item_id, carrier, outcome)
    print(f"ATTEMPT: {item_id} {outcome} by {carrier}", file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next")
    ans = sub.add_parser("answer")
    ans.add_argument("item_id")
    ans.add_argument("--by", default=DEFAULT_ARM)
    # Deliberately no `choices=` and no default: argparse must not supply a
    # verdict on the caller's behalf, and the refusal belongs to this script so
    # its wording stays testable rather than becoming an argparse usage dump.
    ans.add_argument("--decision", default="")
    att = sub.add_parser("attempt")
    att.add_argument("item_id")
    att.add_argument("--by", default=DEFAULT_ARM)
    # Same rule as --decision: no `choices=` and no default, so argparse cannot
    # record an outcome the caller never observed. The refusal belongs to this
    # script so its wording stays testable.
    att.add_argument("--outcome", default="")
    args = parser.parse_args(argv)
    registry = load_registry()
    try:
        if args.cmd == "next":
            return cmd_next(registry)
        if args.cmd == "attempt":
            return cmd_attempt(args.item_id, args.by, args.outcome)
        return cmd_answer(args.item_id, args.by, registry, decision=args.decision)
    except urllib.error.HTTPError as err:
        print(f"FAILED: store returned {err.code}")
        return 1
    except (urllib.error.URLError, OSError) as err:
        print(f"FAILED: attention store unreachable at {STORE} ({err})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
