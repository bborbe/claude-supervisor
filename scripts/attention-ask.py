#!/usr/bin/env python3
"""Ask the operator a question on the attention stack, and read the answer back.

This is the asking half of the board-answers pair; `attention-answer.py` is the
answering half. A manager uses this one to post a question and end its turn,
instead of blocking its whole turn on AskUserQuestion.

  post   POST /api/1.0/attention, then print the item id it created
  poll   GET /api/1.0/attention/{ITEM_ID}, print the answer, or OPEN

**Three poll terminals, not two.** `OPEN` means still unanswered, and
`ANSWERED:` means the item carries client evidence that is not positively
flagged as automated. A third, `NOT_OPERATOR_ANSWERED:`, covers the item that
moved on evidence the operator did not supply — no `answered_client` on the
record, or a client that reported `automation: true`. A caller gating on this
must read that the way it reads `OPEN`: **the gate is not released.** Reading
only `ANSWERED` is the defect this terminal exists to narrow — the board's
controls post a caller-declared `answered_by` and nothing in the request
separates a pointer event from a synthesised one.

⚠️ **`ANSWERED:` is not proof the operator answered, and this arm does not claim
it is.** A Playwright/CDP-driven browser reports `navigator.webdriver === false`
(verified in-page 2026-09-26), so a scripted click stores `automation: false` and
prints `ANSWERED:` exactly as the operator's own click does. What is closed here
is the two decidable cases; a scripted browser click is **not** one of them, and
closing it needs something a scripted click cannot produce. The rule lives in
`answered-attribution.py`, which carries the full boundary — read it before
relying on this terminal.

**Why a manager polls rather than being sent to.** The asking session is the
item's `producer_id`, so it can read its own item back; no cross-session
SendMessage is involved. A browser button cannot send one, and neither can a
cron tick, so polling is the honest mechanism rather than a degraded one. The
cost is latency, and it is worth naming: the answer arrives on the manager's
next loop tick (~5 min for manager-loop, ~15 for fleet-loop), not instantly.

**Posting does not block.** The command returns as soon as the store has the
item. Nothing here waits for the operator.

Every field the producer owns is a declaration, so they are all flags: this
script never infers a dedup key, an interrupt class, or a liveness model from
the prose. The two that would be silently wrong if guessed are defaulted from
the environment instead, and both defaults are visible in `--help`:

  --producer-id    defaults to $CLAUDE_CODE_SESSION_ID
  --liveness-ref   defaults to owner:<producer-id>
  --expires-at     defaults to now + $ATTENTION_ASK_TTL_HOURS (24)

⚠️ The `--liveness-ref` default is `owner:` and NOT `session:`, and the
difference decides whether the card survives at all rather than merely how it is
tagged: the store prunes an open item that asked (any mechanism but `ack`) whose
liveness subject is not live, and removes it from the history index too. A
`session:` default tied every card to the life of the session that posted it, so
a manager's card died with the manager — silently, after a `201`. See
`_producer_or_refuse` for the full account.

⚠️ Because the card now outlives its producer, it is bounded instead by
`--expires-at`, which is why that default exists: without it an `owner:` card
would stay on the board until something else closed it.

A spawned child is stripped of CLAUDE_CODE_SESSION_ID, so a missing producer id
is a real case rather than a bug, and it is refused rather than posted: an item
with no producer can never be polled back by anyone, so it would be a question
asked into a void.

`--closer` is the one declaration that is OPTIONAL. It names the `👤 You:` line
this card answers, and it exists because a session that posts a card and then
ends its turn with the same ask on that line put ONE ask on the board twice: the
deliberate card declares a `dedup_key` slug while the hook's echo derives
sha256(sid:kind:detail), and those two keys can never collide. Passing it writes
<sid>.posted.json, which the hook's `Stop` branch and the feed's `reclassify_idle`
both read to decline the echo. Omitted, nothing is recorded and the echo is
raised exactly as before — the mechanism is opt-in, so the board stays dirty
until a poster passes it.

Run: python3 attention-ask.py post --dedup-key KEY --payload "..." [--option L]... [--closer "..."]
     python3 attention-ask.py post-batch --dedup-key KEY --task "..." [--task "..."]...
     python3 attention-ask.py poll ITEM_ID
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


attribution = _load("answered_attribution", "answered-attribution.py")

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
# Local store; a hung one must cost a clear failure, never a stalled loop tick.
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))

# How long a posted ask stays on the board before it expires. ⚠️ This exists
# because the `owner:` liveness default makes a card outlive the session that
# posted it, and `OwnerLivenessModel` probes NOTHING — so without a bound an
# unanswered card would stay until something else closed it and the board would
# only grow. The store is what enforces the field (`attention-controller`); this
# is the value a producer declares, which is the half the schema gives the
# producer.
DEFAULT_ASK_TTL_HOURS = float(os.environ.get("ATTENTION_ASK_TTL_HOURS", "24"))

# A store item id is the store's own: 32 lowercase hex characters, hex over 16
# random bytes (`attention-controller`, `pkg/item-id-generator.go`). `post`
# prints it on its `ITEM_ID:` line, and that line is the only place a pollable id
# comes from. Everything else that reaches `poll` is a different kind of id, and
# until this guard existed the difference was invisible: an 8-char id — a store
# id truncated by hand, or the `open-items` ledger's own display id — went to the
# store, 404'd, and printed `FAILED: no such item <id>`, which a manager read as
# the card being gone. Measured 2026-10-03 at tick 127 of the Manager Layer loop:
# a live card was recorded absent, and the false finding reached the goal page.
# Refusing the shape here is what lets the message name the mistake; the 404
# cannot, because by then both cases are one.
STORE_ITEM_ID_LEN = 32
_HEX_DIGITS = frozenset("0123456789abcdef")


def is_store_item_id(candidate):
    """Whether `candidate` has the shape the store mints for an item id.

    Shape only: this asks whether the string *could* be one of the store's ids,
    never whether the store holds it. A well-formed id that is unknown is a
    genuine absence, and still reaches the store to be told so.
    """
    return len(candidate) == STORE_ITEM_ID_LEN and all(c in _HEX_DIGITS for c in candidate)


# The answer mechanisms this script posts. It asks questions, so `message` is
# the only one it offers: a `permission` item is approve-shaped and only the
# operator may answer it, in the session that raised it, and an `ack` item asks
# nothing. Offering either here would let a manager post a gate it cannot
# legally answer.
MECHANISM = "message"


# The producer kinds the store accepts. It validates this field against a closed
# enum and rejects anything else with a `400` naming the field, so an invented
# value costs a round trip and still does not tell the caller what the valid set
# is — the failure reads as a store fault rather than a bad argument. Pinning the
# enum here lets argparse reject it before the request is sent, and list the
# accepted values in `--help`.
PRODUCER_KINDS = ("session", "agent", "cron", "dark-factory")


def resolved_session_id(environ=None):
    """The session id of whoever is running this arm, or "".

    Claude Code exports CLAUDE_CODE_SESSION_ID into any Bash a session runs, so
    the asker is resolvable here without the caller passing a flag. "" is a real
    case (a spawned child is deliberately stripped of it) and is refused by the
    caller rather than papered over.
    """
    return (os.environ if environ is None else environ).get("CLAUDE_CODE_SESSION_ID", "")


# Resolved exactly as `attention-log.py` resolves it, so the record lands where the
# hook's `Stop` branch looks for it. A mismatch here would be silent: the hook would
# simply never find the record and the echo would return, with nothing to see.
STATE_DIR = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser(
    "~/.claude/state/attention")


def record_posted_closer(producer_id, closer, dedup_key):
    """Name the closer line this card corresponds to, for the hook's `Stop` branch.

    A session that posts a card and then ends its turn with the same ask on its
    `👤 You:` line used to produce TWO store items: this card, whose `dedup_key` is
    the declared slug, and the hook's echo, whose key is a derived sha256 of
    `sid:kind:detail`. Those keys can never collide, so the store's key-based
    suppression had nothing to match on and the board carried one ask twice —
    measured 2026-10-02, cards `d393d3e9…` and `3d9263ae…`, one session, two cards,
    one question.

    Writing the closer here is what supplies the identity the hook cannot derive:
    `attention-log.py`'s `Stop` branch reads `<sid>.posted.json` and declines to
    mint the echo when its closer matches.

    Best-effort by design. This is a local hint, not part of the question: if it
    cannot be written the card is still posted, and the hook falls back to today's
    behaviour rather than to a wrong suppression.
    """
    # `closer` arrives as a Mock under the test suite's `mock.Mock(**defaults)`
    # args, and a Mock is TRUTHY — so the type check, not just the emptiness
    # check, is what keeps the suite from writing into the real state dir.
    if not isinstance(closer, str) or not closer:
        return
    # Written under BOTH the producer id and this session's own id when they
    # differ. The hook keys the record by its `session_id` and the feed keys it by
    # the store item's `producer_id`; those agree only while `--producer-id` is
    # left to its default, so an explicit producer id would otherwise write the
    # record where nothing looks. The miss is silent — the echo just returns —
    # which is why both keys are written rather than one being chosen.
    for key in {producer_id, resolved_session_id()} - {""}:
        path = os.path.join(STATE_DIR, f"{key}.posted.json")
        # pid-suffixed: a fixed `.tmp` per session id lets two concurrent posts
        # interleave, and a crash between write and replace orphans it.
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"closer": closer, "dedup_key": dedup_key, "ts": time.time()}, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass


def build_options(labels, recommended):
    """Return the `options` list, or raise ValueError naming the bad input.

    The store enforces the same two rules (a label is required; at most one is
    recommended), but checking here costs no round trip and lets the error name
    the fix rather than quoting a store payload.
    """
    options = []
    for label in labels:
        if not label.strip():
            raise ValueError("--option must not be empty")
        options.append({"label": label, "recommended": label == recommended})
    if recommended:
        if recommended not in labels:
            raise ValueError(f"--recommend {recommended!r} is not one of the --option labels")
    return options


def build_batch_payload(tasks):
    """Return the ONE batched card's payload, or raise ValueError naming the bad input.

    The batch is a single `message` item listing every unapproved ready row, not
    one item per row. That is the whole point: a manager posting one card per
    unapproved row has reintroduced the per-item reporting this exists to remove,
    and it asks the operator the same question N times instead of once.

    The rows are numbered because the answer names them back — the caller approves
    exactly the rows the operator's words name, so the card has to make each row
    nameable. Names are printed **verbatim**: a task's name is the key
    `vault-cli task approve` takes, so abbreviating one here would produce a card
    whose answer cannot be acted on.
    """
    names = [task.strip() for task in tasks]
    if not names:
        raise ValueError("--task must be given at least once")
    empty = next((i for i, name in enumerate(names, 1) if not name), None)
    if empty is not None:
        raise ValueError(f"--task must not be empty (entry {empty})")
    numbered = "\n".join(f"{i}. {name}" for i, name in enumerate(names, 1))
    return (
        f"{len(names)} task(s) are ready but unapproved — each sits at `phase: todo`, so "
        "nothing has been opened and no worker is on them. Reply naming the ones to "
        "approve; each is approved, then verified and planned, before any session opens."
        f"\n\n{numbered}"
    )


def post_question(
    producer_id,
    producer_kind,
    liveness_ref,
    dedup_key,
    interrupt_class,
    payload,
    context,
    options,
    expires_at,
):
    body = {
        "producer_id": producer_id,
        "producer_kind": producer_kind,
        "liveness_ref": liveness_ref,
        "dedup_key": dedup_key,
        "interrupt_class": interrupt_class,
        "payload": payload,
        "answer_mechanism": MECHANISM,
    }
    # Omitted rather than sent empty. The schema treats an absent `context` and
    # an absent `expires_at` as pre-change/optional values, while "" would be a
    # *present* field holding nothing — the same distinction attention-answer.py
    # draws for resolved_by and decision.
    if context:
        body["context"] = context
    if options:
        body["options"] = options
    if expires_at:
        body["expires_at"] = expires_at
    req = urllib.request.Request(
        f"{STORE}/api/1.0/attention",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def fetch_item(item_id):
    with urllib.request.urlopen(f"{STORE}/api/1.0/attention/{item_id}", timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def describe_one(answer):
    """Render one `answer` / `answers` entry, or None when it carries no kind.

    The shape is the schema's `answer`: `{kind: option|skip|text, value, values}`.
    A `skip` carries no value, so it is rendered as the bare word rather than as
    an empty one — printing `skip ` with nothing after it reads as a truncated
    option answer. `values` is a `multiple` question's several labels, and it is
    carried by the answer shape rather than by the `answers` entry alone.
    """
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

    ⚠️ **Two fields carry the operator's content and they are mutually
    exclusive by construction**, so a reader that stops at one is reading half
    the schema: `answer` holds a **single-question** item's content, and
    `answers` holds a **multi-question** item's, one entry per tab. The rule is
    the store's, not this arm's — an item carrying `questions` is answered
    through `answers` and an item carrying both is rejected — and it means an
    arm that read only `answer` would report a multi-question item as having no
    answer at all, which is the state a manager polls to escape.

    `decision` is deliberately NOT read here even though it is the third shape
    a store item can carry: it holds a `permission` item's **verdict**, and the
    schema is explicit that `decision` and `answer` *"neither substitutes for
    the other"*. Folding a verdict into a function named for the answer would
    blur exactly the distinction that sentence draws, and `poll` is not the
    permission reader anyway — `cmd_answer` refuses a permission item outright.
    Such an item still reaches the attributed branch and is named as
    contentless there rather than being silently reported as `OPEN`.
    """
    answers = item.get("answers")
    if isinstance(answers, list) and answers:
        rendered = [
            f"{a.get('question')}: {d}"
            for a in answers
            if (d := describe_one(a)) is not None
        ]
        if rendered:
            return "; ".join(rendered)
    return describe_one(item.get("answer"))


def _producer_or_refuse(args, out):
    """Return `(producer_id, liveness_ref)`, or None once the refusal is printed.

    Shared by both posting arms so the producer gate cannot drift between them:
    an item's `producer_id` is the only thing that can poll it back, so an item
    with none is a question asked into a void.
    """
    producer_id = args.producer_id or resolved_session_id()
    if not producer_id:
        print(
            "REFUSED: no --producer-id and CLAUDE_CODE_SESSION_ID is unset, so this item "
            "would have no producer to poll it back. Pass --producer-id explicitly.",
            file=out,
        )
        return None
    # ⚠️ `owner:`, not `session:` — and the difference decides whether the card
    # SURVIVES at all, not merely how it is tagged. The store prunes an open item
    # that *asked* (any mechanism but `ack`) whose liveness subject is not live
    # (`attention-controller` `pkg/attention-store-impl.go` `classifyForRead`:
    # `isAsked` + `!live` → `remove`), and `removeItem` clears the item from the
    # items bucket and every index — so the card leaves no history row either.
    # A `session:<producer-id>` default therefore tied every card this script
    # posts to the life of the session that posted it, and a session ends on
    # every turn boundary, every fleet restart and every crash: the card was
    # created with a `201` and deleted on the first read after the poster
    # exited. Measured 2026-10-06 — a fleet restart killed the posting session
    # and the operator's approval question vanished silently, with a `201` on
    # the producer's side and no history row on the store's.
    #
    # The `owner:` model is the one built for this: `isProducerLiveWith` returns
    # true for it unconditionally and deliberately, because a human owner's
    # absence cannot be read from the session registry (a never-registered owner
    # and an exited one produce the same signal, so reading either as gone
    # prunes a gate the operator can still answer). Every card this script posts
    # is an ask the OPERATOR answers on the board — the asking session only
    # polls the answer back — so the operator's liveness is the honest subject
    # and the poster's is not. An explicit `--liveness-ref` still wins.
    #
    # ⚠️ This makes the card outlive its producer, so it must also be BOUNDED —
    # which is why `_expires_at_or_default` moves with it and why the store's
    # own `expires_at` enforcement is the other half of this change.
    return producer_id, (args.liveness_ref or f"owner:{producer_id}")


def _expires_at_or_default(args):
    """The `expires_at` to send: the caller's, or now + `DEFAULT_ASK_TTL_HOURS`.

    ⚠️ A DEFAULT rather than leaving the field absent, and that is the point of
    the change it ships with. `--expires-at` used to default to `""`, so every
    card went out unbounded — harmless while the store pruned a dead producer's
    ask on the next read, and a leak the moment the `owner:` default stopped
    that prune. The schema reads an absent `expires_at` as "no bound", so
    omitting it is the thing that had to change; `""` would be a *present* field
    holding nothing, which is the distinction `post_question` already draws for
    `context`.
    """
    if args.expires_at:
        return args.expires_at
    return (datetime.now(timezone.utc) + timedelta(hours=DEFAULT_ASK_TTL_HOURS)).isoformat()


def _post_and_report(args, producer_id, liveness_ref, payload, options, out):
    """POST one item and print its id and poll line. Returns the exit code."""
    try:
        item = post_question(
            producer_id=producer_id,
            producer_kind=args.producer_kind,
            liveness_ref=liveness_ref,
            dedup_key=args.dedup_key,
            interrupt_class=args.interrupt_class,
            payload=payload,
            context=args.context,
            options=options,
            expires_at=_expires_at_or_default(args),
        )
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")
        print(f"FAILED: store returned {err.code} for the push -- {detail}", file=out)
        return 1
    record_posted_closer(producer_id, getattr(args, "closer", ""), args.dedup_key)
    print(f"ITEM_ID: {item.get('item_id')}", file=out)
    print(f"POLL: python3 attention-ask.py poll {item.get('item_id')}", file=out)
    return 0


def cmd_post(args, out=sys.stdout):
    producer = _producer_or_refuse(args, out)
    if producer is None:
        return 2
    try:
        options = build_options(args.option, args.recommend)
    except ValueError as err:
        print(f"REFUSED: {err}", file=out)
        return 2
    return _post_and_report(args, *producer, args.payload, options, out)


def cmd_post_batch(args, out=sys.stdout):
    """Post ONE card listing every unapproved ready row — never one card per row.

    ⚠️ **No options are offered, deliberately.** The answer is the operator's own
    words naming which rows to approve, and the caller matches that set against
    the rows it flips. A fixed option list would make an all-or-nothing click the
    only answer to a question whose real answer is a subset — and the subset is
    the thing the criterion measures.
    """
    producer = _producer_or_refuse(args, out)
    if producer is None:
        return 2
    try:
        payload = build_batch_payload(args.task)
    except ValueError as err:
        print(f"REFUSED: {err}", file=out)
        return 2
    return _post_and_report(args, *producer, payload, [], out)


def cmd_poll(item_id, out=sys.stdout):
    if not is_store_item_id(item_id):
        # ⚠️ Refused here rather than at the store's 404, and that placement is
        # the whole point: a short id and an unknown full id both printed
        # `FAILED: no such item <id>`, so a caller could not tell "you polled
        # the wrong id" from "the card is gone" — and read a live card as gone.
        # The exit code is 2 for the same reason: this is a bad argument, not a
        # failed lookup, and it must not read as the `1` a real absence returns.
        print(
            f"REFUSED: {item_id!r} is not a store item id — one is "
            f"{STORE_ITEM_ID_LEN} hex characters, and this is {len(item_id)}. "
            "A store id truncated by hand and the open-items ledger's own "
            "display id both land here. Neither is pollable and NEITHER MEANS "
            "THE ITEM IS GONE: poll the id from the post's `ITEM_ID:` line.",
            file=out,
        )
        return 2
    try:
        item = fetch_item(item_id)
    except urllib.error.HTTPError as err:
        # Handled here rather than in main so the message can name the item and
        # carry the store's explanation. A poll runs unattended on a loop tick,
        # so "FAILED: store returned 500" naming nothing is not something a
        # manager can act on. The 404 is the ordinary case: the item was
        # answered, closed or pruned between ticks.
        if err.code == 404:
            print(f"FAILED: no such item {item_id}", file=out)
        else:
            detail = err.read().decode(errors="replace")
            print(f"FAILED: store returned {err.code} for {item_id} -- {detail}", file=out)
        return 1
    verdict, reason = attribution.classify(item)
    described = describe_answer(item)
    # `OPEN` is the one terminal that means "still unanswered", and it is
    # reached only when the record carries neither an answer nor an act. An
    # item that moved but whose mover cannot be attributed falls through to
    # NOT_OPERATOR_ANSWERED rather than being read as open — failing closed in
    # that direction is what keeps a re-ask from looking like a lost gate.
    #
    # ⚠️ **This branch is deliberately NOT keyed on `state`, and 2026-09-27 is
    # why it was checked.** A `closed` item that carries an actor still classifies
    # as an act and falls through to NOT_OPERATOR_ANSWERED — the item moved, and
    # saying "open" would invite a manager to keep waiting on a card that is
    # gone. Only a record with no actor at all — the reaped close — reaches OPEN
    # here, which is what makes a reap and an acknowledgement distinguishable in
    # the terminal rather than collapsed into one word.
    if verdict == attribution.NOTHING and described is None:
        print("OPEN", file=out)
        return 0
    answered_by = item.get("answered_by") or ""
    # `state`, never the word "closed": an attributed item that renders no
    # content is `answered` with nothing this renderer reads, and naming the
    # other state would misdescribe the transition that happened.
    content = described or f"(no answer content recorded; item is {item.get('state')})"
    if verdict == attribution.ATTRIBUTED:
        print(f"ANSWERED: {content}", file=out)
        if answered_by:
            print(f"ANSWERED_BY: {answered_by}", file=out)
        return 0
    # ⚠️ Not a failure of the poll — the poll succeeded and the item DID move.
    # It is a refusal to treat that movement as the operator's answer, so a
    # caller gating on this must read it the way it reads `OPEN`: the gate is
    # not released. Both terminals return 0 for that reason; a non-zero exit
    # would read as a broken poll and invite a retry.
    print(f"NOT_OPERATOR_ANSWERED: {content} -- {reason}", file=out)
    if answered_by:
        print(f"ANSWERED_BY: {answered_by}", file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    post = sub.add_parser("post")
    post.add_argument("--dedup-key", required=True)
    post.add_argument("--payload", required=True)
    post.add_argument("--context", default="")
    post.add_argument("--option", action="append", default=[])
    post.add_argument("--recommend", default="")
    post.add_argument("--producer-id", default="")
    post.add_argument("--producer-kind", default="session", choices=PRODUCER_KINDS)
    post.add_argument("--liveness-ref", default="")
    post.add_argument("--interrupt-class", default="pick")
    post.add_argument("--expires-at", default="")
    # The `👤 You:` line this card answers, when the poster knows it. Optional:
    # omitted, nothing is recorded and the hook behaves exactly as it did before.
    post.add_argument("--closer", default="")

    batch = sub.add_parser("post-batch")
    batch.add_argument("--dedup-key", required=True)
    # `required=True` on an append action means "at least one occurrence", which
    # is the shape the batch needs: a batch of zero rows is not a question.
    batch.add_argument("--task", action="append", default=[], required=True)
    batch.add_argument("--context", default="")
    batch.add_argument("--producer-id", default="")
    batch.add_argument("--producer-kind", default="session", choices=PRODUCER_KINDS)
    batch.add_argument("--liveness-ref", default="")
    batch.add_argument("--interrupt-class", default="pick")
    batch.add_argument("--expires-at", default="")

    poll = sub.add_parser("poll")
    poll.add_argument("item_id")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "post":
            return cmd_post(args)
        if args.cmd == "post-batch":
            return cmd_post_batch(args)
        return cmd_poll(args.item_id)
    except urllib.error.HTTPError as err:
        # Each command handles its own HTTP errors, so reaching here means the
        # failure predates the item lookup — which is why no item is named: this
        # branch cannot know one. It previously read `args.item_id`, which only
        # `poll` carries; that was unreachable rather than live, because
        # `cmd_post` catches its own HTTPError first. It was one refactor away
        # from an AttributeError, not a defect in the shipped path.
        print(f"FAILED: store returned {err.code}")
        return 1
    except (urllib.error.URLError, OSError) as err:
        print(f"FAILED: attention store unreachable at {STORE} ({err})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
