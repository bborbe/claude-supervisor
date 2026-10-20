#!/usr/bin/env python3
"""Ask the operator a question on the attention stack, and read the answer back.

This is the asking half of the board-answers pair; `attention-answer.py` is the
answering half. A manager uses this one to post a question and end its turn,
instead of blocking its whole turn on AskUserQuestion.

  post   POST /api/1.0/attention, then print the item id it created
  poll   GET /api/1.0/attention/{ITEM_ID}, print the answer, or OPEN

**Five poll terminals, and exactly four of them are states of the card.** The
operator's ruling fixes the state vocabulary: *"the rest api is able to tell the
session what the state is — accepted, declined, skipped/hidden or not answered —
one of these 4"*. So:

  OPEN       still unanswered
  ANSWERED   accepted — the operator picked an option or typed an answer
  DECLINED   the bottom Dismiss: a deliberate decline, and NOT an acceptance
  HIDDEN     the corner X: handled elsewhere, nothing routed back

The fifth, `NOT_OPERATOR_ANSWERED:`, is not a state of the card — it is a
refusal to treat the card's movement as the operator's answer, covering an item
that moved on evidence the operator did not supply (no `answered_client` on the
record, or a client that reported `automation: true`). A caller gating on it, on
`OPEN`, on `DECLINED` or on `HIDDEN` must read all four the same way: **the gate
is not released.** Only `ANSWERED:` releases one. Reading only `ANSWERED` is the
defect these terminals exist to narrow — the board's controls post a
caller-declared `answered_by` and nothing in the request separates a pointer
event from a synthesised one.

⚠️ `DECLINED` was added 2026-10-20 and is a behaviour change, not a rename: a
dismissed card used to arrive as `ANSWERED: declined (no answer given)`, so
every caller gating on `ANSWERED:` treated a decline as a release.

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
the prose. The ones that would be silently wrong if guessed are defaulted rather
than inferred, and every default is visible in `--help`:

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
import math
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


# The largest TTL this script will accept.
#
# ⚠️ An UPPER bound, and it is not belt-and-braces on the `isfinite` check below:
# `nan` and `inf` are caught there, but a large *finite* value is not. `1e30`
# passes every other guard and then raises `OverflowError` inside
# `timedelta(hours=...)` — uncaught, so the caller gets a bare traceback and exit
# `1`, which is this script's *store failed* code. That is the same failure the
# helper exists to prevent, one bound short. A year is far past any ask's useful
# life and sits well inside `timedelta`'s range.
MAX_ASK_TTL_HOURS = 24 * 365


class _ConfigRefused(Exception):
    """A malformed environment value, refused where a caller can report it.

    ⚠️ An exception rather than a `SystemExit`, and the distinction is the whole
    point: a refusal has to reach the arm that owns `out` so it can print
    `REFUSED:` and return `2` like every other refusal in this file. Exiting from
    the helper instead would put the message on stderr and the code at `1` —
    which is this script's *store failed* code, so a bad argument would read as a
    dead store to anything branching on the documented tokens.
    """


def _positive_float_env(name, default):
    """Read a POSITIVE, FINITE float from the env; unset OR empty means `default`.

    ⚠️ Empty is the case this exists for, and it is not hypothetical. `VAR= cmd`
    is exactly what an unset shell variable expands to, and a bare
    `ATTENTION_ASK_TTL_HOURS=` used to raise ValueError before any subcommand
    dispatched, taking down `poll`, which never reads the TTL at all. The script
    already treats an empty value as absent one layer down
    (`_expires_at_or_default` guards with `if args.expires_at`); this is the same
    rule at the layer that reads the environment.

    ⚠️ A value that is present but unusable is REFUSED by name rather than
    silently defaulted: a typo'd bound is a real misconfiguration, and this is the
    place it can be named.

    ⚠️ It raises `_ConfigRefused`, which `_post_and_report` reports — it is NOT a
    `SystemExit` from here, which is what an earlier revision of this helper did.
    That shape wrote the message to stderr and exited 1, breaking the file's own
    convention: every other refusal prints `REFUSED:` to `out` and returns 2, and
    `1` is this script's store-failed code. A caller branching on the stdout token
    the docs teach saw nothing and read a bad argument as a dead store.

    ⚠️ And the environment is read on the POSTING path, not at import — see
    `DEFAULT_ASK_TTL_HOURS`. Only `post` and `post-batch` reach this, so a bad
    value cannot take down `poll`.

    ⚠️ `nan`, `inf` and `<= 0` are refused alongside the unparseable, and each is
    a distinct defect rather than a tidiness check. `float("nan")` and
    `float("inf")` PARSE, so a bare `except ValueError` lets them through to fail
    in a different function: `timedelta(hours=nan)` raises ValueError and
    `timedelta(hours=inf)` raises OverflowError, neither caught on the post path —
    exactly the bare traceback this helper exists to prevent. And a non-positive
    TTL is worse than a crash: `ATTENTION_ASK_TTL_HOURS=0` yields an `expires_at`
    of *now*, which the store removes on the first read, recreating this change's
    own defect — a `201` on the producer's side and nothing on the board — from
    one env-var misconfiguration.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or not math.isfinite(value) or value <= 0 or value > MAX_ASK_TTL_HOURS:
        raise _ConfigRefused(
            f"{name}={raw!r} is not a number in (0, {MAX_ASK_TTL_HOURS}]. Unset it "
            f"to use the default ({default}), or give a value in that range."
        )
    return value


# Local store; a hung one must cost a clear failure, never a stalled loop tick.
#
# ⚠️ 10 s, and the raise is measured rather than defensive. The store's create
# path is O(items): `Push` walks the whole items bucket on every push, and it
# queues behind the read path on one BoltDB write lock
# (`attention-controller` `pkg/attention-store-impl.go:1845`; 38,194 items when
# this was measured). Measured 2026-10-12: 0.10–0.19 s idle, 0.42–1.11 s under 6
# concurrent readers, 2.26 s peak under 20 — under 1.5x headroom at the old 3 s,
# and a gate lost to a timeout is worse than a gate that arrives late. 10 s is
# the value two siblings already use (`attention-card-lookup.py`,
# `answered-watch.py`).
#
# ⚠️ The repo therefore reads this variable with TWO defaults, and that is stated
# rather than hidden — it is the condition the note below sets. `attention-answer.py`,
# `pod-attention.py`, `manager-attention-watch.py`, `who-needs-me.py` and
# `notify-gate.py` still take 3 s, and unifying the policy is its own change.
# `post` is the deliberate exception: it raises a gate the OPERATOR answers, so it
# is the one call where paying latency beats losing the card.
#
# ⚠️ Deliberately left as the bare `float(...)` form rather than routed through
# `_positive_float_env`, and that is a scope decision, not an oversight: the same
# variable is read the same bare way by `pod-attention.py` and `answered-watch.py`,
# so hardening one of the three would leave the repo reading one env var two
# different ways with nothing saying which was intended. Hardening all three
# belongs in its own change.
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "10"))

# How long a posted ask stays on the board before it expires. ⚠️ This exists
# because the `owner:` liveness default makes a card outlive the session that
# posted it, and `OwnerLivenessModel` probes NOTHING — so without a bound an
# unanswered card would stay until something else closed it and the board would
# only grow. The store enforces the field: `attention-controller` PR #95
# (`feat/expires-at-enforcement`, merged 2026-10-06) removes an item past its
# deadline on the read path, so this default is what that enforcement acts on.
#
# ⚠️ **Requires attention-controller ≥ `v0.42.0`**, which carries both features
# this default depends on — `owner:` (`feat/owner-liveness`, PR #94) and the
# `expires_at` enforcement (PR #95). Against an older store `owner` is absent
# from `AvailableLivenessModels` and the push is rejected with a 400 naming the
# field, so the default `post` path fails outright rather than degrading; that is
# a version mismatch, not a bad declaration.
#
# ⚠️ It is NOT reported as one, and this says so rather than claiming otherwise:
# the 400 surfaces through the generic `except urllib.error.HTTPError` as
# `FAILED: store returned 400 for the push -- <detail>` with exit 1, which is
# byte-identical to a dead store, a 500, or any other rejection. The store's own
# detail names the offending field, so the cause is readable — but a caller
# branching on exit codes cannot tell a version mismatch from an outage, and a
# retry loop keyed on 1 would retry a request that can never succeed. Giving it a
# distinct token is a real improvement and belongs with whoever owns the retry
# policy, not folded in here. Same floor convention as
# `commands/attention-next.md`'s attempt-endpoint note.
#
# ⚠️ A LITERAL, and deliberately NOT read from the environment here. Resolving
# `$ATTENTION_ASK_TTL_HOURS` at import meant a set-but-invalid value raised
# SystemExit before any subcommand dispatched — including `poll`, which never
# reads the TTL at all. That is the very failure `_positive_float_env` exists to
# prevent, and closing only the empty case left the other half open. The env is
# read where it is used (`_ask_ttl_hours`), so a refusal lands on the two arms
# that actually post. It also makes this constant env-independent, which is what
# lets a test pin the documented 24 without failing on the very host the knob
# exists for.
DEFAULT_ASK_TTL_HOURS = 24


def _ask_ttl_hours():
    """The TTL to apply: `$ATTENTION_ASK_TTL_HOURS` when set, else the default.

    Kept a function so the environment is read on the posting path rather than at
    import — see `DEFAULT_ASK_TTL_HOURS`.
    """
    return _positive_float_env("ATTENTION_ASK_TTL_HOURS", DEFAULT_ASK_TTL_HOURS)

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
# The three levels the store's schema declares for `risk`. It is a producer
# declaration, stored unchanged; the board may render HIGHER than it and never
# lower, so a poster that under-declares still cannot make a destructive card
# render green. See [[Attention Item Schema]] § Fields.
RISKS = ("routine", "decision", "risk")


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


GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "manager-menu-guard.py")
# ⚠️ `manager-menu-corpus.py` carries a `_load(module_name, filename)` helper for
# this same guard. Kept separate rather than shared: both are standalone scripts,
# and importing one from the other drags its argparse into this path.


def _warn(msg):
    """Record a fail-open on stderr. stdout carries ITEM_ID / REFUSED only."""
    print(f"attention-ask: {msg}", file=sys.stderr)


def manager_guard():
    """The canonical predicate module, or None when it cannot be loaded.

    Loaded, never mirrored. `manager-menu-guard.py`'s docstring records that the
    naive ONE-CLAUSE predicate — a bare word-boundary match on any option label —
    was MEASURED WRONG on 2026-10-16, and that its two-clause
    `names_manager_owned_action` is the corrected form. The corrected form is
    what is loaded here.
    """
    try:
        spec = importlib.util.spec_from_file_location("manager_menu_guard", GUARD)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod if hasattr(mod, "names_manager_owned_action") else None
    except Exception as err:
        _warn(f"manager guard unavailable ({err}) — manager-owned check skipped")
        return None


def is_manager_session(guard, producer_id):
    """True when `producer_id` is a manager arm, by the guard's own rule.

    ⚠️ **Load-bearing scope, and the first draft of this rule got it wrong.** The
    guard's `evaluate()` refuses a manager-owned menu ONLY for a session that is
    a manager; a worker offering one is explicitly allowed (`REASON_NON_MANAGER`,
    verbs recorded so the occurrence stays visible), because a worker cannot take
    the action itself. Refusing a worker's card would tell it to do something it
    is not allowed to do.
    """
    if not producer_id:
        return False
    try:
        entry = guard.registry_entry(producer_id)
        if not entry:
            return False
        topics, goals = guard.vault_page_names()
        return guard.manager_arm(entry, topics, goals) is not None
    except Exception as err:
        _warn(f"manager scoping failed ({err}) — card allowed")
        return False


def manager_settleable_only(options, producer_id=""):
    """True when EVERY option names a manager-owned action AND the asker is a manager.

    The board-noise task's **class 2**: a card whose every choice is a move the
    asking session could have taken itself is a decision already made, and the
    operator spends a click ratifying it.

    ⚠️ Distinct from `~/.claude/hooks/attention-log.py`'s `manager_settleable`,
    which judges only the RECOMMENDED option. There an operator-owned
    alternative may still sit beside it, so the card is a real question. Here
    there is none.

    ⚠️ **`all`, not `any`.** The guard has one-option false positives — its
    proper-noun fallback makes `Open IntelliJ` manager-owned. With `any`, one
    misclassification would refuse a card that carries a genuine operator
    option; with `all`, it cannot.

    ⚠️ **Fails OPEN everywhere.** No guard, no registry entry, no manager arm, or
    any raise all return False, so the card posts — and every one of those paths
    writes a stderr line, because a rule that dies silently reads exactly like a
    rule that is working. Refusing a real question strands the asking session,
    which is strictly worse than one low-value card: the same trade the
    `Stop`-closer rule already refuses to make.

    ⚠️ An option-less card is never settleable: it carries no recommendation to
    judge, and `kind: text` is a legitimate answer shape for it.
    """
    labels = [o.get("label") or "" for o in (options or []) if isinstance(o, dict)]
    if not labels:
        return False
    guard = manager_guard()
    if guard is None:
        return False
    if not is_manager_session(guard, producer_id):
        return False
    try:
        return all(guard.names_manager_owned_action(label) for label in labels)
    except Exception as err:
        _warn(f"manager-owned check raised ({err}) — card allowed")
        return False


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
    risk,
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
    # Omitted rather than sent empty, for the reason `context` below gives: an
    # absent `risk` is the schema's "declared nothing" and renders `decision`,
    # while "" would be a present field holding an undeclared value.
    if risk:
        body["risk"] = risk
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


def _validated_expires_at(raw):
    """The caller's `--expires-at`, refused when it cannot be a usable deadline.

    ⚠️ The FLAG path needs the same guard as the environment path, and for the
    same reason — the two differ only in where the value comes from, so bounding
    one and not the other is the asymmetry rather than a scoping decision. A
    deadline already in the past is removed by the store on the first read, so
    the card vanishes after a `201`: this change's own defect, reachable here
    with no clock skew and no env var at all. Unparseable input is refused here
    too rather than left to the store's 400, so the failure names the argument a
    caller actually typed.
    """
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _ConfigRefused(
            f"--expires-at {raw!r} is not an RFC3339 timestamp."
        ) from None
    if parsed.tzinfo is None:
        raise _ConfigRefused(
            f"--expires-at {raw!r} carries no timezone offset, so it cannot be "
            f"compared against the store's clock."
        )
    if parsed <= datetime.now(timezone.utc):
        raise _ConfigRefused(
            f"--expires-at {raw!r} is already in the past, so the store would "
            f"remove the card on its first read."
        )
    return raw


def _expires_at_or_default(args):
    """The `expires_at` to send: the caller's, or now + `_ask_ttl_hours()`.

    ⚠️ Name the ACCESSOR, not the constant: `_ask_ttl_hours()` honours
    `$ATTENTION_ASK_TTL_HOURS` first and falls back to `DEFAULT_ASK_TTL_HOURS`,
    so the two differ exactly when the env knob is set — the case a reader
    checking this is most likely to be in.

    ⚠️ A DEFAULT rather than leaving the field absent, and that is the point of
    the change it ships with. `--expires-at` used to default to `""`, so every
    card went out unbounded — harmless while the store pruned a dead producer's
    ask on the next read, and a leak the moment the `owner:` default stopped
    that prune. The schema reads an absent `expires_at` as "no bound", so
    omitting it is the thing that had to change; `""` would be a *present* field
    holding nothing, which is the distinction `post_question` already draws for
    `context`.

    ⚠️ The deadline is computed from THIS host's clock and compared by the store
    against its own, so the bound assumes the two are within the TTL of each
    other. A producer whose clock runs more than `DEFAULT_ASK_TTL_HOURS` ahead
    posts a card that is born already expired — the store removes it on the first
    read, and the producer still sees its `201`. That is this change's own defect
    reachable from one misconfigured host rather than one env var, and it is
    stated rather than fixed here because the clamp belongs on the store side,
    where the authoritative clock is. NTP-synced hosts are the assumption; a
    `TZ`-only mistake is not one, since the value is absolute UTC.
    """
    if args.expires_at:
        return _validated_expires_at(args.expires_at)
    return (datetime.now(timezone.utc) + timedelta(hours=_ask_ttl_hours())).isoformat()


def _report_transport_failure(reason, item_may_exist, out):
    """Print a transport failure, naming a timeout as a timeout. Returns 1.

    ⚠️ **A timeout is not an unreachable store, and one `except` clause used to
    say it was.** `socket.timeout` is an alias of `TimeoutError`, which subclasses
    `OSError`, so a single `except (urllib.error.URLError, OSError)` swallowed
    both and printed "unreachable at <url>" for a store that had accepted the
    connection and was merely slow. Measured 2026-10-12 against a listener that
    accepts and never answers: `FAILED: attention store unreachable at
    http://127.0.0.1:18099 (timed out)` — the timeout was the fact, the store was
    reachable, and the message sent the reader to the network instead.

    ⚠️ Nor is reachability decidable from the error, which is why the old wording
    was never something this branch could know: an unroutable host raises a
    `URLError` whose `reason` is a `TimeoutError` — a connect that TIMED OUT, not
    one that was refused — so only the timeout is observable. A genuinely refused
    connection arrives as `URLError(reason=ConnectionRefusedError)` and keeps the
    unreachable wording; that is the negative control the tests pin, because a
    blanket relabel of every transport failure would pass the positive probe
    while making the message useless in the case it was written for.

    ⚠️ **On `post` the timeout also leaves the item's existence UNKNOWN, and the
    message has to say so.** The store may have committed the card while this
    client gave up waiting for the response, so a bare "failed" reads as "nothing
    happened" when a live card may be sitting on the board. Measured 2026-10-12:
    at a 0.15 s timeout against a store whose create path takes ~0.13 s, the
    client printed `FAILED` and the store had committed the item. The retry is
    safe and non-duplicating — the store suppresses a re-push on the same
    `producer_id` + `dedup_key` — so the message names that rather than leaving
    the caller to guess.

    ⚠️ That is why `item_may_exist` is a PARAMETER and not a property of the
    error: it is a fact about the CALLER, not about the failure. `poll` writes
    nothing, so the same timeout there carries no ambiguity and must not claim
    one.
    """
    if isinstance(reason, TimeoutError):
        print(
            f"FAILED: attention store at {STORE} did not answer within "
            f"{STORE_TIMEOUT:g}s (timed out — which is NOT proof the store is "
            "unreachable).",
            file=out,
        )
        if item_may_exist:
            print(
                "  ⚠️ The item's existence is UNKNOWN: the store may have created "
                "the card before this client gave up waiting. Re-post the SAME "
                "--dedup-key to retry — the store suppresses a duplicate push on "
                "the same producer_id + dedup_key, so the retry cannot post twice.",
                file=out,
            )
        return 1
    print(f"FAILED: attention store unreachable at {STORE} ({reason})", file=out)
    return 1


def _post_and_report(args, producer_id, liveness_ref, payload, options, out):
    """POST one item and print its id and poll line. Returns the exit code."""
    # ⚠️ Resolved BEFORE the store call, and reported HERE rather than raised out
    # of the helper, so a bad `$ATTENTION_ASK_TTL_HOURS` follows the file's own
    # refusal convention: `REFUSED:` on stdout and exit 2, like every other
    # refusal. Exiting from deep in the parse instead put the message on stderr
    # and the code at 1 — this script's *store failed* code — so a caller
    # branching on the documented token read a bad argument as a dead store.
    # Nothing is posted and no closer is recorded on this path.
    try:
        expires_at = _expires_at_or_default(args)
    except _ConfigRefused as err:
        print(f"REFUSED: {err}", file=out)
        return 2
    try:
        item = post_question(
            producer_id=producer_id,
            producer_kind=args.producer_kind,
            liveness_ref=liveness_ref,
            dedup_key=args.dedup_key,
            interrupt_class=args.interrupt_class,
            risk=args.risk,
            payload=payload,
            context=args.context,
            options=options,
            expires_at=expires_at,
        )
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")
        print(f"FAILED: store returned {err.code} for the push -- {detail}", file=out)
        return 1
    except urllib.error.URLError as err:
        # ⚠️ `HTTPError` is a `URLError` subclass, so its clause must stay ABOVE
        # this one. A timeout can arrive either way and both are one fact:
        # `urlopen` wraps an `OSError` raised around the request write, so a
        # connect that timed out reaches here as `URLError(reason=TimeoutError)`,
        # while a response read that timed out raises a bare `TimeoutError` past
        # the wrapper.
        #
        # `item_may_exist=True` — this is the POST path, and that is the whole
        # reason the parameter exists. See `_report_transport_failure`.
        return _report_transport_failure(err.reason, True, out)
    except TimeoutError as err:
        return _report_transport_failure(err, True, out)
    except OSError as err:
        return _report_transport_failure(err, True, out)
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
    # Class 2 of the board-noise task: every choice is a manager-owned action, so
    # the card asks the operator to ratify a decision already made. Refused by
    # default for a MANAGER asker only (see `is_manager_session`); `--force`
    # keeps a deliberate card postable.
    if not getattr(args, "force", False) and manager_settleable_only(
            options, producer[0]):
        print(
            "REFUSED: every --option names a manager-owned action, so this card "
            "asks the operator to ratify a decision the asking session could "
            "have taken itself. Take the action; if you need the operator to "
            "CHOOSE between manager-owned actions (a priority call), post it "
            "with no --option and let the answer be free text; or re-run with "
            "--force if the card is deliberate.",
            file=out)
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
    except urllib.error.URLError as err:
        # `item_may_exist=False`, and the asymmetry with `_post_and_report` is the
        # point: a poll writes nothing, so a timeout here carries no ambiguity
        # about what the store holds. The timeout itself is still named as one —
        # measured 2026-10-12, `poll` printed the same `unreachable … (timed out)`
        # misreport that the post path printed.
        return _report_transport_failure(err.reason, False, out)
    except TimeoutError as err:
        return _report_transport_failure(err, False, out)
    except OSError as err:
        return _report_transport_failure(err, False, out)
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
    # ⚠️ Two terminals of their own, from 2026-10-20. Both are answers the
    # operator GAVE, so neither is `NOT_OPERATOR_ANSWERED`, and neither is
    # `ANSWERED` — a decline is not an acceptance and a hidden close carries no
    # answer at all. The operator's ruling fixes the vocabulary at four: *"the
    # rest api is able to tell the session what the state is — accepted,
    # declined, skipped/hidden or not answered — one of these 4"*.
    #
    # ⚠️ Neither releases a gate, and a caller must read them the way it reads
    # `OPEN`. `DECLINED` used to arrive as `ANSWERED: declined (no answer
    # given)`, which every command gating on `ANSWERED:` treated as a release.
    if verdict == attribution.DECLINED:
        print(f"DECLINED: {content}", file=out)
        if answered_by:
            print(f"ANSWERED_BY: {answered_by}", file=out)
        return 0
    if verdict == attribution.HIDDEN:
        print("HIDDEN", file=out)
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
    post.add_argument(
        "--force",
        action="store_true",
        help="post even when every --option names a manager-owned action "
             "(refused by default: such a card asks the operator to ratify a "
             "decision the asking session could have taken itself)",
    )
    post.add_argument(
        "--producer-id",
        default="",
        help="the session that owns this item and polls it back; default $CLAUDE_CODE_SESSION_ID",
    )
    post.add_argument("--producer-kind", default="session", choices=PRODUCER_KINDS)
    post.add_argument(
        "--liveness-ref",
        default="",
        help="<model>:<value> deciding whether the store prunes this item; default owner:<producer-id>",
    )
    post.add_argument("--interrupt-class", default="pick")
    # The producer's declaration of how much care this deserves. Optional:
    # omitted, nothing is recorded and the board renders `decision`, which is
    # what every card posted before this flag existed reads as. The board may
    # render higher than whatever is passed here and never lower, so this is a
    # convenience for the poster rather than the safety rule itself.
    post.add_argument("--risk", default="", choices=("",) + RISKS)
    post.add_argument(
        "--expires-at",
        default="",
        help="RFC3339 deadline; default now + $ATTENTION_ASK_TTL_HOURS (24 h)",
    )
    # The `👤 You:` line this card answers, when the poster knows it. Optional:
    # omitted, nothing is recorded and the hook behaves exactly as it did before.
    post.add_argument("--closer", default="")

    batch = sub.add_parser("post-batch")
    batch.add_argument("--dedup-key", required=True)
    # `required=True` on an append action means "at least one occurrence", which
    # is the shape the batch needs: a batch of zero rows is not a question.
    batch.add_argument("--task", action="append", default=[], required=True)
    batch.add_argument("--context", default="")
    batch.add_argument(
        "--producer-id",
        default="",
        help="the session that owns this item and polls it back; default $CLAUDE_CODE_SESSION_ID",
    )
    batch.add_argument("--producer-kind", default="session", choices=PRODUCER_KINDS)
    batch.add_argument(
        "--liveness-ref",
        default="",
        help="<model>:<value> deciding whether the store prunes this item; default owner:<producer-id>",
    )
    batch.add_argument("--interrupt-class", default="pick")
    # Carried here as well as on `post`, because both reach the same
    # `_post_and_report` and therefore the same `post_question`: a flag on one
    # subcommand and not the other would leave the shared helper reading an
    # attribute half its callers never set.
    batch.add_argument("--risk", default="", choices=("",) + RISKS)
    batch.add_argument(
        "--expires-at",
        default="",
        help="RFC3339 deadline; default now + $ATTENTION_ASK_TTL_HOURS (24 h)",
    )

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
    except urllib.error.URLError as err:
        # The backstop. `_post_and_report` and `cmd_poll` catch their own
        # transport failures, so reaching here means the failure predates the
        # store call — and a caller that never reached the store cannot know
        # whether an item was involved, so this claims no ambiguity. That is the
        # honest default: `item_may_exist=True` is a fact about the POST path,
        # and only the two commands that own that fact pass it.
        #
        # ⚠️ `HTTPError` is a `URLError` subclass and its clause above must stay
        # first, or every store rejection would be reported as a transport
        # failure. `TimeoutError` is not a `URLError`, so it needs its own clause
        # below — it is an `OSError`, and the ordering is what keeps the timeout
        # out of the unreachable wording.
        return _report_transport_failure(err.reason, False, sys.stdout)
    except TimeoutError as err:
        return _report_transport_failure(err, False, sys.stdout)
    except OSError as err:
        return _report_transport_failure(err, False, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
