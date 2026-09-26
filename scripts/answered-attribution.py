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

So this module does not try to *prove* the operator answered, and it does not
claim to catch a scripted browser click — **it cannot.** What it answers is
narrower than "did the operator answer": *is there any client evidence at all,
and is it positively flagged?* It refuses the two cases that are decidable — no
client record, and `automation: true` — and it fails CLOSED on them, because the
two mistakes are not symmetric: a re-asked gate costs the operator a glance, a
silently dropped one costs a stuck worker.

⚠️ **Read that boundary before relying on this predicate.** A caller that treats
`operator_answered()` as proof the operator acted will be wrong for exactly the
case the surrounding work exists for. Closing that case needs something a
scripted click cannot produce; it is not a stronger predicate here.

  answered + client present + not positively automated  ->  attributed
  answered + no client at all                           ->  unattributed
  answered + `automation: true`                         ->  automated
  nothing recorded                                      ->  open / reaped

The distinction matters because the client field is young: it landed with
attention-controller v0.12.0, so answers recorded before it carry no client and
are *unattributed* rather than operator-verified. That is the honest reading,
not a false negative — and it is why callers render the two differently instead
of collapsing them into one boolean.

Load it the way the tree loads its siblings (`fleet-board.py:77`):

    attribution = _load("answered_attribution", "answered-attribution.py")
"""

# The store's own state vocabulary. `closed` is a state a consumer must still
# judge, because the board's Acknowledge control closes an item through a path
# that writes `answered_by` and never sets `answered_at` — so "closed" is not
# by itself evidence that nobody answered.
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

    ⚠️ This is the whole point of the split. The dominant closed shape in the
    live store is a reap — `closed` with no `answered_at`, no `answered_by` and
    no client (9,339 of 9,454 rows measured 2026-09-26). Reading `closed` as
    "answered" would make every reaped item read as an operator act, which is
    the same collapse in the other direction.
    """
    item = item or {}
    if item.get("answered_at"):
        return True
    # The Acknowledge path: it closes the item and stamps the arm that did it,
    # but leaves `answered_at` unset because nothing is routed back.
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
    return (
        ATTRIBUTED,
        "answered with a client record and no positive automation flag",
    )


def operator_answered(item):
    """True when the answer carries client evidence and is not positively flagged.

    ⚠️ **The name is the claim, and it is stronger than what this can prove.**
    It does NOT mean "the operator answered": a scripted browser click stores
    `automation: false` and passes this predicate (see the module docstring).
    It means *the answer is attributable at all* — there is a client record and
    nothing in it is positively flagged as automation. Callers must not read it
    as proof, and the two cases it does refuse are the decidable ones.

    The predicate a consumer gates on. It is deliberately not
    `state == "answered"` — see the module docstring for why that reading is the
    defect, and `was_acted_on` for why `closed` alone is not evidence either.
    """
    return classify(item)[0] == ATTRIBUTED
