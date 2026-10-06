#!/usr/bin/env python3
"""The pod-side half of the operator channel: ask a question or raise a gate, then read the answer back.

A `claude-interactive` pod has no WezTerm pane and no spawning supervisor server, so
neither existing carrier reaches it. `attention-ask.py` posts questions but cannot post a
gate (it refuses the permission class by design — a manager must not post a gate it cannot
legally answer), and both its delivery routes assume something a pod does not have: a
`message` answer routes by `SendMessage` to a name resolved from the local
`~/.claude/sessions/<pid>.json`, and a `permission` answer routes by the *spawning server*
polling the store. A pod has neither a registry entry nor a spawner.

So this arm does both halves itself: it posts the card, and it polls its own item back.

  ask   POST a `message` card, print its item id
  gate  POST a `permission` card, then BLOCK until a verdict, or the deadline
  poll  GET an item, print one of the three terminals

**Three poll terminals, not two** — inherited from `attention-ask.py` rather than
re-derived, so the two arms cannot drift on what counts as an answer:

  OPEN                     still unanswered
  ANSWERED:                carries client evidence not positively flagged as automated
  NOT_OPERATOR_ANSWERED:   moved on evidence the operator did not supply

A caller gating on this must read the third **exactly as it reads `OPEN`**: the gate is not
released. And `ANSWERED:` is not proof the operator answered — a Playwright/CDP browser
reports `navigator.webdriver === false`, so a scripted click stores `automation: false` and
prints `ANSWERED:` exactly as the operator's own click does. The boundary lives in
`answered-attribution.py`; read it before relying on either terminal.

**A gate releases only on a verdict carrying `resolved_by`.** That is the whole legitimacy
basis for releasing a pod's gate without a pane: `attention-answer.py` sources
`resolved_by` from the answering arm's own `CLAUDE_CODE_SESSION_ID`, and the board's
JavaScript sends neither `decision` nor `resolved_by`. So no board click can release a
gate — only an arm answer. `gate` applies that rule verbatim:

  state == "answered" AND decision in {allow, deny} AND resolved_by is a non-empty string

**Fail-open, never fail-allow.** Any error, malformed response or deadline prints
`NO_DECISION:` and exits non-zero *without* a verdict, so the gate stays for whatever
fallback the pod has. This arm never returns a decision it did not read.

## Auth — a deliberate seam, not a stub

The store carries no credentials today and defaults to `localhost:18080`, which is safe
only because it is localhost. A pod reaching a remote store needs both a base URL and a
token, injected at runtime and never baked into an image or manifest. Both are read from
the environment here, so the mechanism drops in behind this seam unchanged:

  POD_ATTENTION_STORE_URL / ATTENTION_STORE_URL   base URL
  POD_ATTENTION_TOKEN                             bearer token, sent as Authorization

A **non-local** base URL with no token is REFUSED rather than attempted: an
unauthenticated remote store lets anything that can reach the port read every card and
release every gate, which is strictly worse than the gap this arm closes. The refusal is
what stops an unauthenticated localhost default being read later as the shipped shape.

Run: python3 pod-attention.py ask  --dedup-key KEY --payload "..." [--option L]... [--recommend L]
     python3 pod-attention.py gate --dedup-key KEY --payload "..." [--timeout 900]
     python3 pod-attention.py poll ITEM_ID
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


attribution = _load("answered_attribution", "answered-attribution.py")

STORE = (
    os.environ.get("POD_ATTENTION_STORE_URL")
    or os.environ.get("ATTENTION_STORE_URL")
    or "http://localhost:18080"
).rstrip("/")

# The injected half of the seam. Absent is the legitimate local case; present is sent.
TOKEN = os.environ.get("POD_ATTENTION_TOKEN", "")

# Local store; a hung one must cost a clear failure, never a stalled pod.
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))

# The two verdicts a permission-class item can carry. Kept here as this arm's own guard:
# the store rejects an unknown value too, but a local refusal costs no round trip and names
# the fix rather than quoting a store payload.
DECISIONS = ("allow", "deny")

# ⚠️ **Both of these are validated against a closed enum by the store, and an unknown
# value is rejected with a 400 naming the field.** A pod is a long-lived Claude Code
# session, so it is `session` for both — there is no `pod` member, and inventing one
# fails every push, which is exactly what the first e2e run found.
#   producer_kind: session | agent | cron | dark-factory
#   liveness_ref:  <session|heartbeat|owner>:<value>
# Read from `attention-controller` `pkg/producer-kind.go` and `pkg/liveness-ref.go`, and
# confirmed against the running store.
PRODUCER_KIND = "session"
LIVENESS_MODEL = "session"

DEFAULT_GATE_TIMEOUT = 900.0
DEFAULT_GATE_INTERVAL = 2.0

# Hosts for which "no token" is a safe, intended configuration. Anything else is remote by
# construction and must be authenticated — see the module docstring.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def is_local(store):
    """True when the store URL names a loopback host."""
    try:
        host = (urllib.parse.urlsplit(store).hostname or "").strip("[]")
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


def auth_refusal(store, token):
    """Return a refusal reason for an unauthenticated remote store, or None to proceed.

    This is the guard the seam exists for: not that a token is always required, but that
    *absent* and *safe* must coincide. A remote store with no token is the state that must
    never be reachable by default.
    """
    if token or is_local(store):
        return None
    return (
        f"REFUSED: store {store} is not loopback and no POD_ATTENTION_TOKEN is set. "
        "An unauthenticated remote store lets anything that can reach the port read every "
        "card and release every gate. Inject the token as a runtime-only secret."
    )


def headers():
    """Request headers, carrying the bearer token only when one is injected."""
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def resolved_session_id(environ=None):
    """The session id of whoever is running this arm, or "".

    Claude Code exports CLAUDE_CODE_SESSION_ID into any Bash a session runs, so the pod's
    own id is resolvable without a flag. A spawned child is deliberately stripped of it, so
    "" is a real case rather than a bug — and it is refused by the caller rather than
    papered over, because an item with no producer can never be polled back by anyone.
    """
    return (os.environ if environ is None else environ).get("CLAUDE_CODE_SESSION_ID", "")


def build_options(labels, recommended):
    """Return the `options` list, or raise ValueError naming the bad input."""
    options = []
    for label in labels:
        if not label.strip():
            raise ValueError("--option must not be empty")
        options.append({"label": label, "recommended": label == recommended})
    if recommended and recommended not in labels:
        raise ValueError(f"--recommend {recommended!r} is not one of the --option labels")
    return options


def post_item(
    mechanism,
    producer_id,
    dedup_key,
    payload,
    options=None,
    interrupt_class="pick",
    liveness_ref="",
):
    """POST one item and return the store's response.

    `mechanism` is this arm's own argument rather than a constant: the pod raises both
    classes, and the class is the whole difference in how the answer comes back.

    ⚠️ **`liveness_ref` decides whether the card SURVIVES, not merely how it is tagged.**
    The store prunes an open item whose producer is not live and that *asked* rather than
    reported (`attention-store-impl.go` `classifyForRead`: `isAsked` + `!live` →
    `remove`). So a card posted by a producer the store cannot see live is created with a
    201 and removed on the very first board read — it never reaches the operator.
    """
    body = {
        "producer_id": producer_id,
        "producer_kind": PRODUCER_KIND,
        "liveness_ref": liveness_ref or f"{LIVENESS_MODEL}:{producer_id}",
        "dedup_key": dedup_key,
        "interrupt_class": interrupt_class,
        "payload": payload,
        "answer_mechanism": mechanism,
    }
    if options:
        body["options"] = options
    req = urllib.request.Request(
        f"{STORE}/api/1.0/attention",
        data=json.dumps(body).encode(),
        headers=headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def fetch_item(item_id):
    req = urllib.request.Request(f"{STORE}/api/1.0/attention/{item_id}", headers=headers())
    with urllib.request.urlopen(req, timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def describe_one(answer):
    """Render one `answer` / `answers` entry, or None when it carries no kind."""
    answer = answer or {}
    kind = answer.get("kind")
    if not kind:
        return None
    values = answer.get("values") or []
    if values:
        return f"{kind}: {', '.join(str(v) for v in values)}"
    value = answer.get("value") or ""
    return f"{kind}: {value}" if value else kind


def describe_answer(item):
    """Render the stored answer, or None when the item carries no answer content.

    `answer` holds a single-question item's content and `answers` a multi-question item's;
    they are mutually exclusive by construction, so a reader stopping at one is reading
    half the schema. `decision` is deliberately not read here — it is a `permission` item's
    verdict, and folding it into a function named for the answer would blur the distinction
    `verdict()` exists to draw.
    """
    answers = item.get("answers")
    if isinstance(answers, list) and answers:
        rendered = [
            f"{a.get('question')}: {d}" for a in answers if (d := describe_one(a)) is not None
        ]
        if rendered:
            return "; ".join(rendered)
    return describe_one(item.get("answer"))


def verdict(item):
    """(ok, decision, reason) for a permission-class item — the three conditions, verbatim.

    Mirrored from `permission-answer-poll.py` rather than re-derived, so the pod and the
    in-pane hook cannot disagree about what releases a gate. The third condition is the
    legitimacy basis: a gate releases only on an arm answer.
    """
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
        return False, None, (
            "the item carries no resolved_by, so no arm delivered this answer, "
            "and a permission gate releases only on an arm answer"
        )
    return True, decision, f"answered by arm {resolved_by!r}"


def render_poll(item, out):
    """Print the poll terminal for any item class. Returns the exit code."""
    if item.get("answer_mechanism") == "permission":
        ok, decision, reason = verdict(item)
        if ok:
            print(f"ANSWERED: {decision} -- {reason}", file=out)
            return 0
        # A permission item that has not settled is OPEN-shaped to a caller: the gate is
        # not released. Reporting the reason is what separates "not yet" from "moved on
        # evidence nobody supplied".
        state = item.get("state")
        print("OPEN" if state == "open" else f"NOT_OPERATOR_ANSWERED: {reason}", file=out)
        return 0
    described = describe_answer(item)
    # `classify` returns (verdict, reason) — unpack it, never compare the tuple.
    classified, _reason = attribution.classify(item)
    if classified == attribution.NOTHING and described is None:
        print("OPEN", file=out)
        return 0
    content = described or f"(no answer content recorded; item is {item.get('state')})"
    if classified == attribution.ATTRIBUTED:
        print(f"ANSWERED: {content}", file=out)
    else:
        # Not a failed poll — the poll succeeded and the item moved. It is a refusal to
        # treat that movement as the operator's answer, so the caller reads it as OPEN.
        print(f"NOT_OPERATOR_ANSWERED: {content}", file=out)
    return 0


def _producer_or_refuse(args, out):
    """Return the producer id, or None once the refusal is printed."""
    producer_id = args.producer_id or resolved_session_id()
    if not producer_id:
        print(
            "REFUSED: no --producer-id and CLAUDE_CODE_SESSION_ID is unset, so this item "
            "would have no producer to poll it back. Pass --producer-id explicitly.",
            file=out,
        )
        return None
    return producer_id


def _guard(out):
    """Print the auth refusal if the store is remote and unauthenticated. True = refuse."""
    reason = auth_refusal(STORE, TOKEN)
    if reason:
        print(reason, file=out)
        return True
    return False


def cmd_ask(args, out=sys.stdout):
    if _guard(out):
        return 2
    producer_id = _producer_or_refuse(args, out)
    if producer_id is None:
        return 2
    try:
        options = build_options(args.option, args.recommend)
    except ValueError as err:
        print(f"REFUSED: {err}", file=out)
        return 2
    try:
        item = post_item(
            "message",
            producer_id,
            args.dedup_key,
            args.payload,
            options,
            args.interrupt_class,
            args.liveness_ref,
        )
    except urllib.error.HTTPError as err:
        # The store's own explanation is carried through: a bare "returned 400" names no
        # field, and this is the message that would have pointed straight at the bad
        # `producer_kind` instead of costing a round of probing.
        detail = err.read().decode(errors="replace")
        print(f"FAILED: store returned {err.code} for the push -- {detail}", file=out)
        return 1
    print(f"ITEM_ID: {item.get('item_id')}", file=out)
    return 0


def cmd_gate(args, out=sys.stdout, sleep=time.sleep, clock=time.monotonic):
    """Post a permission-class card, then block until a verdict or the deadline.

    Fail-open on every path: an error, a malformed response, or the deadline prints
    `NO_DECISION:` and exits non-zero WITHOUT a verdict. A default of allow would release a
    gate nobody approved; a default of deny would refuse one nobody refused. Neither is
    safe to assume, so this arm has no default — the same rule `attention-answer.py`
    applies to `--decision`.
    """
    if _guard(out):
        return 2
    producer_id = _producer_or_refuse(args, out)
    if producer_id is None:
        return 2
    try:
        item = post_item(
            "permission",
            producer_id,
            args.dedup_key,
            args.payload,
            liveness_ref=args.liveness_ref,
        )
    except urllib.error.HTTPError as err:
        # The store's own explanation is carried through: a bare "returned 400" names no
        # field, and this is the message that would have pointed straight at the bad
        # `producer_kind` instead of costing a round of probing.
        detail = err.read().decode(errors="replace")
        print(f"FAILED: store returned {err.code} for the push -- {detail}", file=out)
        return 1
    item_id = item.get("item_id")
    print(f"ITEM_ID: {item_id}", file=out)

    deadline = clock() + args.timeout
    last_reason = "no poll completed"
    while True:
        try:
            current = fetch_item(item_id)
        except urllib.error.HTTPError as err:
            # A 404 is the ordinary case: the item was closed or pruned. Either way there
            # is no verdict to read, and guessing one is the failure this arm forbids.
            last_reason = f"store returned {err.code} for {item_id}"
            current = None
        except (urllib.error.URLError, OSError) as err:
            last_reason = f"store unreachable ({err})"
            current = None
        if current is not None:
            ok, decision, reason = verdict(current)
            last_reason = reason
            if ok:
                print(f"DECISION: {decision} -- {reason}", file=out)
                return 0
        if clock() + args.interval >= deadline:
            print(f"NO_DECISION: {last_reason}", file=out)
            return 1
        sleep(args.interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    ask = sub.add_parser("ask")
    ask.add_argument("--dedup-key", required=True)
    ask.add_argument("--payload", required=True)
    ask.add_argument("--option", action="append", default=[])
    ask.add_argument("--recommend", default="")
    ask.add_argument("--producer-id", default="")
    ask.add_argument("--interrupt-class", default="pick")
    # How the store is to see this producer as live. Defaults to `session:<producer-id>`,
    # which is right for anything the local session registry can resolve. A pod that
    # cannot be resolved that way must declare its own — `heartbeat:<path>` — or its card
    # is pruned on the first read.
    #
    # ⚠️ `session:` is kept HERE while `attention-ask.py` moved its default to `owner:`,
    # and the difference is the producer rather than an oversight — do not "fix" the
    # inconsistency without reading this. A pod's `gate` blocks on its own item, so the
    # pod is alive for as long as the gate matters and its exit makes the gate moot: that
    # is exactly what `session:` expresses, and it is why a pod needs no `owner:` model.
    # `attention-ask.py` posts cards the OPERATOR answers after the asking session has
    # ended its turn, which is what `owner:` expresses. Swapping this default would make
    # a dead pod's gate outlive the pod for no one's benefit.
    ask.add_argument("--liveness-ref", default="")

    gate = sub.add_parser("gate")
    gate.add_argument("--dedup-key", required=True)
    gate.add_argument("--payload", required=True)
    gate.add_argument("--producer-id", default="")
    gate.add_argument("--liveness-ref", default="")
    gate.add_argument("--timeout", type=float, default=DEFAULT_GATE_TIMEOUT)
    gate.add_argument("--interval", type=float, default=DEFAULT_GATE_INTERVAL)

    poll = sub.add_parser("poll")
    poll.add_argument("item_id")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "ask":
            return cmd_ask(args)
        if args.cmd == "gate":
            return cmd_gate(args)
        if _guard(sys.stdout):
            return 2
        return render_poll(fetch_item(args.item_id), sys.stdout)
    except urllib.error.HTTPError as err:
        print(f"FAILED: store returned {err.code}")
        return 1
    except (urllib.error.URLError, OSError) as err:
        print(f"FAILED: attention store unreachable at {STORE} ({err})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
