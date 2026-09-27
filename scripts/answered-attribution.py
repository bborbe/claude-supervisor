#!/usr/bin/env python3
"""Is this attention item's answer attributable to the operator?

The defect this closes: the board's answer controls post a caller-declared
`answered_by`, and nothing in the request separates a pointer event from a
synthesised one — the page's submit handler reads `event.submitter` and checks
no `isTrusted`, no pointer-event provenance, no gesture token, so a
Playwright/CDP `.click()` produces a **trusted** submit indistinguishable from
a human's. A consumer that reads only `answered` therefore treats a scripted
click as *the operator saw it*: the item leaves the board and the asking
session is never answered.

The store now records `answered_client` beside the answer — `user_agent` and
`remote_addr` read by the server from the request itself, plus an `automation`
hint the page reports from `navigator.webdriver`. That is the only evidence a
consumer has, and it is deliberately weak:

  ⚠️ **Only `remote_addr` is unspoofable.** `user_agent` is server-*read* yet
  caller-*set*, so it sits in the same weaker class as `automation`, and a
  `user_agent` match is not evidence about *who* answered.
  ⚠️ **A `false` does not exonerate the client — and that is the common case,
  measured rather than assumed.** A Playwright/CDP-driven Chrome reports
  `navigator.webdriver === false` (verified in-page 2026-09-26), so a scripted
  click stores `automation: false` and is **indistinguishable** from the
  operator's own click by every field this module reads. An extension driving
  Chrome through `chrome.debugger` behaves the same way. A `false` only fails
  to incriminate; it never clears.

⚠️ **Attribution cannot carry this, and from 2026-09-27 it does not have to.**
This module still refuses the two decidable cases — no client record, and
`automation: true` — but the case the surrounding work exists for is now closed
by **provenance rather than by a stronger predicate here**: a gate releases only
on an answer carrying `resolved_by`, a field the arm sources from its own
`CLAUDE_CODE_SESSION_ID` and the board's JavaScript never sends. A click on any
control the board renders therefore cannot release a gate, whatever
`answered_client` says about it.

  answered + `resolved_by` + not positively automated   ->  attributed
  answered + no `resolved_by`                           ->  unattributed
  answered + `resolved_by` but no client at all         ->  unattributed
  answered + `resolved_by` + `automation: true`         ->  automated
  closed (a clear, not an answer)                       ->  nothing
  nothing recorded                                      ->  open / reaped

⚠️ **A close is not a gate release.** `open` -> `closed` records that a card was
**cleared**: `answered_at` and `decision` stay unset and no answer is routed, so
the asking session stays frozen exactly as it was. Counting a close as an act
re-read the board's own Acknowledge control — and the corner X that aliases it —
as operator answers. Measured 2026-09-27 on a copy of the live store: **401** of
12,239 `closed` items carried `answered_by`, **387** of them `attention-board`.

The distinction matters because the client field is young: it landed with
attention-controller v0.12.0, so answers recorded before it carry no client and
are *unattributed* rather than operator-verified. That is the honest reading,
not a false negative — and it is why callers render the two differently instead
of collapsing them into one boolean.

Load it the way the tree loads its siblings (`fleet-board.py:77`):

    attribution = _load("answered_attribution", "answered-attribution.py")
"""

# The store's own state vocabulary. `closed` is **not** an act — see
# `was_acted_on`: a close records that a card was cleared, not that anything was
# answered. The state stays in the vocabulary because a sweep must still be able
# to name what it saw.
OPEN = "open"
ANSWERED = "answered"
CLOSED = "closed"

# What the answer's client is.
ATTRIBUTED = "attributed"
UNATTRIBUTED = "unattributed"
AUTOMATED = "automated"
NOTHING = "nothing"


def answered_client(item):
    """The store-read client record, or None when the item carries none.

    A non-dict is treated as absent rather than iterated: a malformed value must
    not raise in a sweep, and it must not be read as evidence either.
    """
    client = (item or {}).get("answered_client")
    return client if isinstance(client, dict) else None


def was_acted_on(item):
    """True when the record carries an act, rather than a state alone.

    ⚠️ **An act is not a release — 2026-09-27.** This function answers "did the
    record move", and a close *is* a move: the board's Acknowledge control closes
    an item and stamps the arm that did it, leaving `answered_at` unset because
    nothing is routed back. So `closed` + `answered_by` stays an act here.

    What changed is downstream, in `classify`: a close is an act but **not a
    release**, because the asking session is still frozen — treating the clear as
    an answer opens a gate nobody opened. Splitting the two is what keeps a reap
    distinguishable from an acknowledgement in the poll's terminal: a reap
    carries no actor at all and reads as OPEN, while an acknowledgement carries
    one and reads as NOT_OPERATOR_ANSWERED.

    ⚠️ The reap reading this function was written for is unchanged. The dominant
    closed shape in the live store is a reap — `closed` with no `answered_at`,
    no `answered_by` and no client (9,339 of 9,454 rows measured 2026-09-26).
    Those carry no actor and read as NOTHING.
    """
    item = item or {}
    if item.get("answered_at"):
        return True
    return bool(item.get("answered_by")) and item.get("state") in (ANSWERED, CLOSED)


def classify(item):
    """Return (verdict, reason) for one item — the single rule every caller uses.

    verdict is one of NOTHING, UNATTRIBUTED, AUTOMATED or ATTRIBUTED. The reason
    is a short clause a sweep can print beside the verdict, so an operator
    reading "unattributed" learns which evidence was missing rather than having
    to re-derive it.
    """
    item = item or {}
    if not was_acted_on(item):
        state = item.get("state") or OPEN
        return NOTHING, f"no answer recorded (state {state})"

    # ⚠️ **A close is a clear, not an answer — 2026-09-27.** `was_acted_on` still
    # reports the close as an act, because the card did move. What it is not is a
    # *release*. `answered_at` is the discriminator: an item that was answered and
    # later closed carries it and its answer stands, while the board's
    # Acknowledge control — and the corner X that aliases it — closes a card with
    # `answered_at` unset and nothing routed back, leaving the asking session
    # frozen. Releasing on that opens a gate nobody opened. The board's corner X
    # makes this the widest surface on the board: it renders on 88 of the 90 rows
    # measured 2026-09-27.
    if item.get("state") == CLOSED and not item.get("answered_at"):
        return (
            UNATTRIBUTED,
            "the item was closed without an answer — a close is a clear, not an "
            "answer, so the asking session is still frozen and the gate is not "
            "released",
        )

    # The client evidence is read next, so an item that is positively flagged as
    # automated still reads AUTOMATED rather than being flattened into the
    # generic unattributed case. `automation: true` is the more specific fact.
    client = answered_client(item)
    if client is None:
        return (
            UNATTRIBUTED,
            "the record carries no answered_client, so the answering client is unknown "
            "(answers recorded before attention-controller v0.12.0 carry none)",
        )
    if client.get("automation") is True:
        return (
            AUTOMATED,
            "the answering client reported automation: true, so a scripted browser drove this",
        )

    # The enforcement, and the reason it is not a sharper reading of the same
    # record: `resolved_by` is sourced by the arm from its own
    # CLAUDE_CODE_SESSION_ID and the board's JavaScript never sends it, so a
    # click on any control the board renders cannot produce this field. Every
    # other field the store holds is producible by a scripted click — a real
    # Playwright click records the operator's own Chrome UA with
    # `automation: false`.
    resolved_by = item.get("resolved_by")
    if not isinstance(resolved_by, str) or resolved_by == "":
        return (
            UNATTRIBUTED,
            "the item carries no resolved_by, so no arm delivered this answer — "
            "the board's JavaScript never sends that field, and a gate releases "
            "only on an arm answer",
        )

    return (
        ATTRIBUTED,
        f"answered by an arm carrying resolved_by ({resolved_by!r}) with no positive automation flag",
    )


def operator_answered(item):
    """True when an arm delivered the answer and nothing is positively flagged.

    ⚠️ **The name is the claim, and it is still stronger than what this proves.**
    It does NOT mean "the operator personally answered": an arm answer is
    attributable to *an arm*, and an arm may be driven by a script. What it does
    mean, from 2026-09-27, is that the answer carries `resolved_by` — provenance
    a board click cannot produce — which is the enforcement the surrounding work
    settled on after attribution was measured to be insufficient.

    The predicate a consumer gates on. It is deliberately not
    `state == "answered"` — see the module docstring for why that reading is the
    defect — and `closed` alone is no longer evidence either, because a close is
    a clear rather than an answer (see `was_acted_on`).
    """
    return classify(item)[0] == ATTRIBUTED
