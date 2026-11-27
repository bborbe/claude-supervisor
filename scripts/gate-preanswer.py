#!/usr/bin/env python3
"""Pre-answer `allowlist`-tier gates in-process — the model-turn-free half of the gate path.

The manager's gate path costs two model turns: the feed line wakes the manager (Turn 1), which
dispatches `supervisor:gate-answerer` (Turn 2). For the mechanical `allowlist` tier neither turn
carries judgment, so this script settles those cards inside the `Monitor` that already holds the
session's identity — no dispatch, no sub-agent.

Reads gate lines on stdin (the `gate-owner-filter.py --feed --digest` output) and, for each
`hst:<item-id>` token:

  1. probes it with `gate-answer-check.py --dry` — classification WITHOUT a ledger booking;
  2. when the probe reads `answerable` AND `tier: allowlist`, checks it (which books the card)
     and answers it with `--by supervisor:gate-preanswer`;
  3. re-emits every token it did NOT answer, so the manager still dispatches the gate-answerer
     for the `manager` and `operator` tiers.

⚠️ **The `--dry` probe is load-bearing, not an optimisation.** `--item` books every
`answerable`/`escalate` card as `checked` in this manager's per-session ledger, and a booked card
is `skip` for that manager's lifetime. Probing with `--item` would therefore starve exactly the
`manager`-tier cards the dispatched gate-answerer must answer.

⚠️ **The allowlist ceiling is enforced at every acting step, not only at the probe.** Both the
check and the answer call carry `--max-tier allowlist`. The tier is read from the parked call AND
the card payload, while the `expect` hash covers only the call — so a payload edit between probe
and answer can raise the tier with the hash still matching, and a probe-only check would settle a
judgment-required gate with no rubric behind it.

⚠️ **A failed answer releases its booking.** A booked card is `skip` for this manager's lifetime,
so leaving it booked would strand the gate — the dispatched gate-answerer would read `skip` and
never look again. `--release` is the repair.

⚠️ **Identity is the environment, never an argument.** The script reads
`CLAUDE_CODE_SESSION_ID` from its own environment — the `Monitor` runs inside the manager session
and inherits it — and refuses when it is unset or malformed. Passing a session id in would make
ownership an assertion; reading it makes it a fact.

Usage:
  gate-owner-filter.py --feed --digest --self <sid> --interval 60 | gate-preanswer.py
  gate-preanswer.py --item <store-item-id> [--item …]        # one-shot, no feed

Exit: 0 on a clean pass · 2 on a usage or identity refusal. Per-card failures never abort the
run: an unanswered card is re-emitted for the manager, which is the correct fallback.
"""

import argparse
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(_HERE, "gate-answer-check.py")

#: The answering path the store records. Distinct from `supervisor:gate-answerer` on purpose —
#: the pre-answer criterion reads `answered_by` back out of the store, so a shared value would
#: make a pre-answered row indistinguishable from one the sub-agent settled.
PREANSWER_BY = "supervisor:gate-preanswer"

#: A store item id, as the feed prints it (`hst:<32 lowercase hex>`).
TOKEN = re.compile(r"hst:([0-9a-f]{32})")

#: A Claude session id. Same shape `gate-answer-check.py`'s `resolve_self` insists on: a
#: malformed value is refused rather than passed through to a path that names a ledger file.
SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def session_id(environ=None):
    """`(session_id, refusal)` — this session's identity from its own environment."""
    value = (os.environ if environ is None else environ).get("CLAUDE_CODE_SESSION_ID", "")
    if not value:
        return None, "CLAUDE_CODE_SESSION_ID is unset, so this Monitor cannot prove who it is"
    if not SESSION_ID.match(value):
        return None, "CLAUDE_CODE_SESSION_ID is not a session id"
    return value, None


def _run(argv):
    """`(rc, first stdout line, stderr)` — a card script's verdict line, never its traceback."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as err:
        return 1, "", str(err)
    first = (done.stdout or "").splitlines()[:1]
    return done.returncode, (first[0] if first else ""), (done.stderr or "")


def _verdict(self_id, item_id, *extra):
    """One `gate-answer-check.py` verdict dict, or `None` when the line was unreadable."""
    rc, line, _ = _run([sys.executable, CHECK, "--self", self_id, "--item", item_id] + list(extra))
    if rc != 0 or not line:
        return None
    try:
        verdict = json.loads(line)
    except ValueError:
        return None
    return verdict if isinstance(verdict, dict) else None


def preanswer(self_id, item_id, run=_run, verdict=_verdict):
    """`(settled, why)` for one card — `settled` True only when THIS path answered it.

    Probe first, then check, then answer. The three steps are separate processes on purpose:
    each is the existing script's own contract, and `--answer` refuses without the booking the
    middle step makes, so a card that changed between them is refused rather than settled.
    """
    probe = verdict(self_id, item_id, "--dry")
    if probe is None:
        return False, "probe unreadable"
    if probe.get("verdict") != "answerable":
        return False, "not answerable (%s)" % (probe.get("verdict") or "no verdict")
    if probe.get("tier") != "allowlist":
        return False, "tier %r is not allowlist" % (probe.get("tier"),)
    expect = probe.get("expect")
    if not expect:
        return False, "probe carried no expect hash"
    checked = verdict(self_id, item_id, "--max-tier", "allowlist")
    if checked is None or checked.get("verdict") != "answerable":
        return False, "re-check did not read answerable at the allowlist ceiling"
    rc, _, err = run([sys.executable, CHECK, "--self", self_id, "--item", item_id,
                      "--answer", "--expect", expect, "--by", PREANSWER_BY,
                      "--max-tier", "allowlist"])
    if rc != 0:
        # Release the booking. A booked card is `skip` for this manager's lifetime, so leaving
        # it booked after a failed answer would strand the gate — the dispatched gate-answerer
        # would read `skip` and never look again. Releasing is the only repair.
        run([sys.executable, CHECK, "--self", self_id, "--item", item_id, "--release"])
        return False, "answer refused (rc %d) %s" % (rc, err.strip()[:120])
    return True, "answered %s" % item_id


def process(line, self_id, pre=preanswer):
    """`(output_line, settled_ids, passed_ids)` for one feed line.

    Every token this path settles is stripped from the re-emitted line; the rest keep their
    place, so the manager's dispatch sees exactly the cards still needing a model turn.
    """
    settled, passed = [], []
    for item_id in dict.fromkeys(TOKEN.findall(line)):
        ok, _ = pre(self_id, item_id)
        (settled if ok else passed).append(item_id)
    if not settled:
        # Nothing settled: re-emit the line byte-identical. Normalising whitespace here would
        # rewrite the filter's own `LIVENESS  <counts>` heartbeat into something it never said.
        return line, settled, passed
    out = line
    for item_id in settled:
        out = out.replace("hst:%s" % item_id, "")
    return re.sub(r"[ \t]{2,}", " ", out).strip(), settled, passed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--item", action="append", default=[],
                        help="one-shot: an item id to pre-answer (repeatable), instead of stdin")
    args = parser.parse_args(argv)

    self_id, refusal = session_id()
    if refusal:
        print("REFUSED: %s" % refusal)
        return 2

    if args.item:
        for item_id in args.item:
            ok, why = preanswer(self_id, item_id.strip())
            print(json.dumps({"item_id": item_id.strip(), "settled": ok, "why": why}))
        return 0

    for line in sys.stdin:
        line = line.rstrip("\n")
        if not TOKEN.search(line):
            print(line, flush=True)
            continue
        out, settled, passed = process(line, self_id)
        print(json.dumps({"settled": settled, "passed": passed}), file=sys.stderr, flush=True)
        if out:
            print(out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
