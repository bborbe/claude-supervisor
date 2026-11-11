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

⚠️ **A Stop-hook MIRROR card is the one exception, and it has NO compare-and-set.**
Such a card lives in the event log and never in the store (`who-needs-me.py`'s
`board_missing_gates()`), so there is nothing to POST: the branch resolves the
card through the folded log, prints a TARGET, and the caller delivers. Because
no CAS exists there, two arms answering one mirror id will BOTH deliver — the
operator should answer a mirror card once. A mirror whose card has closed, or
whose kind is `permission`, is refused rather than routed: a relay cannot release
another session's parked gate.

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
              this script never prints a TARGET: there is no session to send
              to, and printing one would invite a relay. It waits (bounded,
              ATTENTION_DELIVERY_WAIT, default 35 s) for the store's delivery
              report and prints one of:
                ANSWERED + DELIVERY: delivered by <carrier>      exit 0
                RECORDED + NOT_DELIVERED: failed|held by <c>     exit 3
                RECORDED + UNCONFIRMED: <why>                    exit 3
              Exit 3 means the verdict is stored but the asker is still parked.

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
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
# Local store; a hung one must cost a clear failure, never a stalled session.
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))

# Where the worker's own attention hook looks for the record of WHO answered it.
#
# ⚠️ This is the WRITER half of a two-repo change: `~/.claude/hooks/attention-log.py` reads
# `<session>.answer.json` from this directory and stamps `answerer_session` / `answerer_kind`
# onto the close line. Written alone it is a file nobody reads; the hook alone is a reader
# whose file never exists. They ship as a pair.
#
# The resolution mirrors the hook exactly (`ATTENTION_STATE_DIR`, then the default) — a
# writer pointed at a different directory than the reader is a silent no-op on both sides.
ATTENTION_STATE_DIR = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser(
    "~/.claude/state/attention")

# The kinds the hook accepts. Anything else is ignored there, so an unknown value here would
# be a write that can never be read.
ANSWERER_KINDS = ("manager", "operator", "other")

# A Claude session id, which is the only thing the hook keys a marker on. Validated before the
# value reaches a filesystem path — see `write_answer_marker`.
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def write_answer_marker(producer_id, answerer_session, answerer_kind):
    """Record who answered `producer_id`'s item, for its own hook to stamp on close.

    Best-effort by design and never raises: this is a measurement side-channel, and failing
    to record WHO answered must never stop the answer being delivered. The caller has already
    written the answer to the store; trading a missing attribution for a failed command would
    be the wrong way round.

    Returns True when the marker landed. A false return is not an error — the answer stands.
    """
    if answerer_kind not in ANSWERER_KINDS:
        return False
    # ⚠️ Validated as a UUID, not merely non-empty. `producer_id` is external store data —
    # the store is an unauthenticated localhost endpoint — and it is interpolated into a
    # filesystem path. An absolute value makes `os.path.join` discard ATTENTION_STATE_DIR
    # outright, and `..` segments walk out of it; both would write a marker outside the
    # directory the hook reads, or over a file that has nothing to do with attention.
    if not isinstance(producer_id, str) or not _UUID.match(producer_id):
        return False
    path = os.path.join(ATTENTION_STATE_DIR, f"{producer_id}.answer.json")
    # pid-suffixed temp + replace, the same reason `<sid>.posted.json` does it: a fixed `.tmp`
    # lets two concurrent writers interleave, and a crash between write and replace orphans it.
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(ATTENTION_STATE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "answerer_session": answerer_session or "",
                "answerer_kind": answerer_kind,
                # EPOCH SECONDS, not milliseconds: the hook parses with `float()` and
                # compares against `time.time()`. Milliseconds read as ~50 000 years ahead,
                # which the hook rejects as future-dated — silently.
                "ts": time.time(),
            }, f)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# How long a permission answer waits for the store's delivery report before it
# calls the delivery unconfirmed. ⚠️ It must outlast the poller's IDLE interval,
# not its busy one: `server/attention-poll.mjs` reads every `intervalMs = 2000`
# while an item is open but every `idleIntervalMs = 30000` when nothing is, so an
# answer can be picked up up to 30 s after it lands. A shorter bound would report
# a slow-but-working delivery as unconfirmed.
DEFAULT_DELIVERY_WAIT_SECONDS = 35.0
DELIVERY_POLL_SECONDS = 1.0


# A ceiling on the override. An unbounded wait is the pre-fix bug on a slower
# clock: the caller still cannot tell a delivered answer from an undelivered one,
# it just stops returning. Ten minutes outlasts every poller cadence with margin
# and still guarantees the command comes back.
MAX_DELIVERY_WAIT_SECONDS = 600.0


def delivery_wait_seconds(environ=None):
    """The bounded wait, from `ATTENTION_DELIVERY_WAIT` or the default.

    ⚠️ A bare `float()` is not enough. `float("nan")` and `float("inf")` both
    parse, and `wait_for_delivery` bounds itself with `_clock() >= deadline` —
    a `nan` deadline makes that comparison permanently False, so the loop never
    exits and the command hangs instead of reporting. Non-finite, non-positive
    and oversized values all fall back to a bound rather than becoming one.
    """
    raw = (os.environ if environ is None else environ).get("ATTENTION_DELIVERY_WAIT", "")
    try:
        value = float(raw) if raw else DEFAULT_DELIVERY_WAIT_SECONDS
    except ValueError:
        return DEFAULT_DELIVERY_WAIT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_DELIVERY_WAIT_SECONDS
    return min(value, MAX_DELIVERY_WAIT_SECONDS)


DELIVERY_WAIT_SECONDS = delivery_wait_seconds()
# A delivery report status that settles the question. `never_attempted` and
# `pre_trail_unknown` do not: they mean no carrier has written yet.
DELIVERY_TERMINAL = ("delivered", "failed", "held")
# Exit code for "the verdict is in the store but was not observed delivered" --
# distinct from 1 (store write failed) and 2 (refused before write), because the
# caller's next move differs: the worker is still parked, and nothing here can
# release it.
EXIT_NOT_DELIVERED = 3
# Indirection so tests can drive the bounded wait without sleeping.
_sleep = time.sleep
_clock = time.monotonic
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


def mirror_gate(item_id, state_dir=None):
    """`(session_id, kind)` for an EVENT-LOG-only card that is still OPEN, else None.

    ⚠️ **A Stop-hook mirror card is not in the store.** The hook mints a card for a
    closer and writes it to the event log; the store never carries it, which is exactly
    the class `who-needs-me.py`'s `board_missing_gates()` returns as *"open gates the
    EVENT LOG holds and the STORE did not return"*. So `fetch_item()` 404s on a mirror's
    id, and before this fallback that 404 escaped `cmd_answer` before any `TARGET:` line
    was printed — the operator's approve reached nobody and the session kept waiting
    (measured 2026-11-11: `urllib.error.HTTPError: HTTP Error 404`, fixture
    `MirrorCardLookup`).

    ⚠️ **The log is FOLDED, not scanned for a first match.** A record is a live card only
    when an `open` line names it (`type: "open"`) and no `close` line does anywhere
    (`closed_by`, which the close line carries instead of a `type`). The test is
    order-independent — any close wins, in any file. ⚠️ **That matches
    `who-needs-me.load_events()`, and it inherits its limitation:** `item_id` is
    `sha256(sid:kind:detail)`, so a byte-identical ask re-parked AFTER its card closed
    appends a second `open` for the same id, and any-close-wins then refuses a live card.
    The two surfaces agree, so this is consistent rather than novel — but it is a real
    edge, not a soundness guarantee. A first-match scan would be worse: it would route an
    answer for a card that closed, and the store path has a compare-and-set for exactly
    that, while the mirror path has none.

    `kind` is returned because the caller MUST NOT treat a `permission` card as a
    message: a session cannot release another session's parked gate, and relaying a
    verdict to it is permission laundering.
    """
    base = state_dir or ATTENTION_STATE_DIR
    opened = None
    closed = False
    for path in glob.glob(os.path.join(base, "*.events.jsonl")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # a torn final line is expected, not fatal
                    if not isinstance(rec, dict) or rec.get("item_id") != item_id:
                        continue
                    if rec.get("closed_by") is not None:
                        closed = True
                    elif rec.get("type") == "open":
                        opened = rec
        except OSError:
            continue
    if opened is None or closed:
        return None
    return (opened.get("session_id") or "", opened.get("kind") or "")


def fetch_delivery(item_id, timeout=None):
    """Return the store's delivery report for `item_id` (`GET …/attempt`)."""
    with urllib.request.urlopen(
        f"{STORE}/api/1.0/attention/{item_id}/attempt",
        timeout=STORE_TIMEOUT if timeout is None else timeout,
    ) as resp:
        return json.load(resp)


def wait_for_delivery(item_id):
    """Poll the delivery report until it settles or the bound runs out.

    Returns (status, carrier, reason). `status` is a terminal report status
    when one was observed; otherwise it is the last non-terminal status read, or
    None when no well-formed report was ever read -- and `reason` says why.

    ⚠️ An unreadable report is never read as delivered, nor as failed: like
    `restart-precheck.py`, only a report carrying `status` decides anything.

    ⚠️ Every read is clamped to the budget that is left, so the bound this
    returns within is the one the caller is told about. A read allowed its full
    `STORE_TIMEOUT` could push the real elapsed time past `DELIVERY_WAIT_SECONDS`
    while the UNCONFIRMED line still printed the nominal figure.
    """
    deadline = _clock() + DELIVERY_WAIT_SECONDS
    status, carrier, reason = None, "", "no report read"
    while True:
        remaining = deadline - _clock()
        if remaining <= 0:
            return status, carrier, reason
        try:
            report = fetch_delivery(item_id, timeout=min(STORE_TIMEOUT, remaining))
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as err:
            status, reason = None, f"delivery report unreadable ({err})"
        else:
            if isinstance(report, dict) and "status" in report:
                status, carrier, reason = report.get("status"), report.get("carrier") or "", ""
                if status in DELIVERY_TERMINAL:
                    return status, carrier, reason
            else:
                status, reason = None, "delivery report unreadable (no status field)"
        remaining = deadline - _clock()
        if remaining <= 0:
            return status, carrier, reason
        _sleep(min(DELIVERY_POLL_SECONDS, remaining))


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


# The answer endpoint returns HTTP 409 for more than one failure, and the status
# alone cannot tell them apart -- only the `error.code` in the body can. The two
# this arm can meet are different facts about the gate, so each gets its own
# line: ITEM_CLOSED means the card left the queue before the answer arrived and
# no arm can answer it now -- ⚠️ **and it does not say why, because the record
# does not.** A cleared card, a withdrawn one and one whose producer exited
# leave the same `closed` row with no actor, so naming any of them would be a
# claim the store cannot support; the line says so instead.
# ALREADY_ANSWERED means another arm's verdict landed first. Neither is a retry.
STORE_CODE_ITEM_CLOSED = "ITEM_CLOSED"
STORE_CODE_ALREADY_ANSWERED = "ALREADY_ANSWERED"


def store_error_code(err):
    """The store's `error.code` from a failed response, or "" when unreadable.

    Returns "" rather than a guess. A body this arm cannot read is not evidence
    of either failure, and naming one would report a cleared card as a lost race
    or the reverse -- exactly the conflation reading the body exists to end. A
    pre-code store, a body a proxy stripped, and a body carrying a code this arm
    does not know all land here.

    ⚠️ `http.client.IncompleteRead` is in the tuple because it is neither a
    ValueError nor an OSError -- a 409 whose body is truncated against its
    Content-Length is what a proxy or a mid-body reset produces, and it would
    otherwise escape as a traceback where the caller expects a label. Before this
    read existed the arm never touched the 409 body and could not fail here.
    """
    try:
        return json.load(err).get("error", {}).get("code", "")
    except (ValueError, OSError, AttributeError, http.client.HTTPException):
        return ""


def answer_conflict_line(item_id, err, verb):
    """The line for a 409 on an answer, named by the store's own code.

    `verb` is the act this branch must not take next -- "send" for a message
    item, "deliver" for a permission one -- so each branch keeps its own word for
    what it is refusing. An unreadable or unknown code falls back to the generic
    line rather than to either named one: a code this arm cannot read is not
    evidence of which failure happened.

    ⚠️ **`ITEM_CLOSED` does NOT name a cause, and that is deliberate.** It used
    to read "the card was cleared, or its producer exited" -- two causes offered
    as if the record settled which. It cannot. A `permission` gate that leaves
    the queue with no verdict carries no `answered_by` (the store refuses an
    arm-caused close of an open gate, so there is no actor to read) and that
    same no-actor close is produced by a producer exit, by a compact-crossing
    clear and by a forwarded `SessionEnd` -- so the record establishes that the
    gate was never answered, and establishes nothing about why. Saying so is the
    honest line; picking one cause would invent a fact.

    ⚠️ The `LOST:` fallback must not merge its two causes either. It fires on a
    code this arm could not read at all, which is strictly LESS evidence than
    `ITEM_CLOSED` carries -- so a line asserting "already answered or no longer
    open" claims two specific causes from zero information.
    """
    code = store_error_code(err)
    if code == STORE_CODE_ITEM_CLOSED:
        return (
            f"ITEM_CLOSED: {item_id} left the queue before this answer arrived and no "
            f"verdict was recorded; the store does not record why -- the dead-session "
            f"sweep, the compact-crossing clear, a forwarded SessionEnd and the "
            f"compact-approval clear all leave the same closed row with no actor, and "
            f"one of them is not a producer exit at all, so no arm can establish the "
            f"cause and none can answer it now; do not {verb}"
        )
    if code == STORE_CODE_ALREADY_ANSWERED:
        return (
            f"ALREADY_ANSWERED: {item_id} already has a verdict from another arm; "
            f"do not {verb}"
        )
    return (
        f"LOST: {item_id} was not answered by this arm -- the store refused it with a code "
        f"this arm cannot read, so it cannot say whether the card already had a verdict or "
        f"had left the queue; do not {verb}"
    )


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
    try:
        item = fetch_item(item_id)
    except urllib.error.HTTPError as err:
        if err.code != 404:
            print(f"FAILED: store returned {err.code} for {item_id}", file=out)
            return 1
        # A Stop-hook MIRROR card: the event log holds it and the store never did. There
        # is no store record to answer, so nothing is POSTed — the caller delivers the
        # answer straight to the producing session, which is what the TARGET line is for.
        gate = mirror_gate(item_id)
        if gate is None:
            print(f"FAILED: {item_id} is neither a store item nor an open event-log card", file=out)
            return 1
        producer, kind = gate
        if not producer:
            print(f"FAILED: {item_id} carries no producing session", file=out)
            return 1
        if kind == "permission":
            # ⚠️ Never relay a verdict for another session's parked gate. The store path
            # routes these through `answer_permission`, which the spawning server polls;
            # a SendMessage carrying `--decision allow` is permission laundering even when
            # the action looks small, and this arm cannot record a verdict anywhere.
            print(f"REFUSED: {item_id} is a permission card; a relay cannot release "
                  "another session's parked gate", file=out)
            return 2
        if kind != "question":
            # Only `question` is a card here, which is also what `board_missing_gates()`
            # offers (`is_open_gate()`). Every other kind is a turn record — an `idle`
            # line names no ask — so there is nothing to answer.
            print(f"FAILED: {item_id} is a {kind or 'typeless'} record, not a card", file=out)
            return 1
        # ⚠️ The marker goes down only for a card that can actually be DELIVERED to. The
        # store path writes it after the store accepted the answer, for the same reason it
        # states: "a marker for an answer that failed to land would attribute a question
        # nobody answered". A mirror has no store write to wait on, so deliverability —
        # `resolve_target` returning a name — is the equivalent gate.
        name, reason = resolve_target(producer, registry)
        if not name:
            # Nothing was recorded and nothing will be delivered, so `ANSWERED:` would be a
            # state claim this branch has not established — the store path earns that line
            # from a successful POST.
            print(f"UNDELIVERABLE: {reason}", file=out)
            return 0
        write_answer_marker(producer, "", "operator")
        print(f"ANSWERED: {item_id} (no store record)", file=out)
        print(f"TARGET: {name}", file=out)
        return 0
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
            # 409 covers more than one failure; only the body's code says which,
            # and either way this arm did not win, so it must not route.
            print(answer_conflict_line(item_id, err, "send"), file=out)
        else:
            print(f"FAILED: store returned {err.code} for {item_id}", file=out)
        return 1
    print(f"ANSWERED: {item_id} at {answered.get('answered_at')} by {answered.get('answered_by')}", file=out)
    print(f"RESOLVED_BY: {resolved_by}" if resolved_by else
          "RESOLVED_BY: unknown -- CLAUDE_CODE_SESSION_ID unset, so this item will read as unresolved", file=out)
    # Tell the asking session's own hook WHO answered, so its close line carries the party
    # rather than only the event that cleared the gate. Written AFTER the store accepted the
    # answer, never before: a marker for an answer that failed to land would attribute a
    # question nobody answered.
    #
    # ⚠️ `operator`, not `manager`. This arm is `/supervisor:attention-next`, which
    # `commands/attention-next.md` defines as relaying the operator's own words — *"This
    # command relays an operator answer; it is not a channel for a manager's own decisions."*
    # Hardcoding `manager` here would inflate the count this marker exists to produce, in the
    # exact direction that makes SC8 look better than it is. A manager answering a park goes
    # through `answer_permission`, which is a different writer.
    #
    # The session is `resolved_by` — the session that RAN the relay — but the party is the
    # operator, so `answerer_session` stays empty: the board and the operator are arms, not
    # sessions, and naming the relaying session as the answerer would be the same
    # misattribution one field over.
    write_answer_marker(item.get("producer_id", ""), "", "operator")
    print(target_line(item.get("producer_id", ""), registry), file=out)
    return 0


def cmd_answer_permission(item, answered_by, resolved_by, decision, out=sys.stdout):
    """Answer a permission-class item with a verdict, and report whether it landed.

    The delivery is the spawning supervisor server polling the store, so this
    never prints TARGET. A TARGET line here would be actively harmful rather
    than merely useless: the wrapping command branches on it, and a session
    cannot release another session's parked gate -- a relay is permission
    laundering even when the action looks small.

    ⚠️ The POST is not the delivery, so it is not reported as one. After the
    write this reads the store's delivery report (written by the poller after
    it settles, or fails to settle, the park) for a bounded wait, and claims
    `ANSWERED` only on an observed `delivered`. Anything else -- `failed`,
    `held`, or no report within the bound -- prints `RECORDED` plus a
    `NOT_DELIVERED` / `UNCONFIRMED` line and exits EXIT_NOT_DELIVERED: the
    verdict is in the store, and the worker is still parked. Measured
    2026-11-05: the pre-fix arm printed `ANSWERED` and exit 0 while the poller
    refused with `wrapper holds no question for session bdc2f1ed`.
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
            print(answer_conflict_line(item_id, err, "deliver"), file=out)
        else:
            print(f"FAILED: store returned {err.code} for {item_id}", file=out)
        return 1
    status, carrier, reason = wait_for_delivery(item_id)
    stamp = f"{item_id} at {answered.get('answered_at')} by {answered.get('answered_by')}"
    print(f"ANSWERED: {stamp}" if status == "delivered" else
          f"RECORDED: {stamp} -- verdict stored, NOT observed delivered", file=out)
    print(f"RESOLVED_BY: {resolved_by}" if resolved_by else
          "RESOLVED_BY: unknown -- CLAUDE_CODE_SESSION_ID unset, so this item will read as unresolved", file=out)
    print(f"DECISION: {decision}", file=out)
    by = carrier or "unknown carrier"
    if status == "delivered":
        print(f"DELIVERY: delivered by {by} -- do NOT SendMessage", file=out)
        return 0
    if status in ("failed", "held"):
        print(f"NOT_DELIVERED: {status} by {by} -- the asking session is still parked; "
              f"do NOT SendMessage, and do not report this gate as answered", file=out)
        return EXIT_NOT_DELIVERED
    print(f"UNCONFIRMED: {reason or status} after {DELIVERY_WAIT_SECONDS:g}s -- no carrier reported "
          f"delivery, so the asking session may still be parked; do NOT SendMessage, "
          f"and do not report this gate as answered", file=out)
    return EXIT_NOT_DELIVERED


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
    try:
        post_attempt(item_id, carrier, outcome)
    except urllib.error.HTTPError as err:
        # ⚠️ A 404 alone does NOT mean "mirror". `commands/attention-next.md` documents the
        # other cause: against a store older than attention-controller v0.30.0 the `/attempt`
        # endpoint does not exist, and that is "a missing endpoint, not a failed delivery".
        # Swallowing every 404 would delete that documented `FAILED:` signal and mislabel it
        # as a mirror card. The id must actually BE a mirror.
        if err.code != 404 or mirror_gate(item_id) is None:
            raise
        # A mirror card has no store record, so there is no attempt trail to append to.
        # Reporting that as a failure would turn a delivery that DID happen into a failed
        # attempt — the command runs this step unconditionally after every delivery.
        print(
            f"ATTEMPT: {item_id} {outcome} by {carrier} (mirror card — no store record, "
            "nothing recorded)",
            file=out,
        )
        return 0
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
