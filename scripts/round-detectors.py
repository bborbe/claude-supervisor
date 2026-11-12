#!/usr/bin/env python3
"""round-detectors — the round's SC1 and SC2 detectors, batched into one call.

SC1: a session parked on a waiting closer (`pick` / `review:` / `you run:` / `approve:`)
must resolve to a live card in the attention store. A card-less one is `NO-CARD` — the
operator cannot answer it, so the session is indistinguishable from an idle one.
SC2: a `🔴 BLOCKED` closer that names no owner must route to the subject's manager, and
the routing record must be readable in the round's digest.

⚠️ **ONE call per round, never one per row, and the chain is built once.** A real
`escalation-manager` resolution measured **387 s** on 2026-11-12 (`vault_index` 64 s,
`colour_census` 37 s, `build_task_index` 87 s under load). A per-row call would make a
round take tens of minutes; `resolve_many()` builds the grouping once and
`manager_on_chain` is free per title. The owner decision is the cheap regex — it never
needs the chain — so a round with no ownerless blocked row pays nothing.

⚠️ **This composes the shipped scripts; it re-derives none of them.**
  * the closer is `closer-gate-lint.py`'s single transcript scan (`last_closer_scan`),
    which also returns the whole message — the `🔴 BLOCKED` line and its `⏰ Ends:` live
    there, not in the one `👤 You:` line;
  * `NO-CARD` is `operator-ask-card.py`'s `verdict()`, whose join is the SESSION
    (`producer_id`), never an id parsed out of the closer;
  * the routing decision and record are `blocked-row-routing.py`'s `routing()`, which
    composes `closer-gate-lint.owner_named()`; only its resolver is injected, so the
    expensive chain is built once instead of per row.

⚠️ **An unreadable input is `UNKNOWN`, never `(none)`.** The exit code is 2 and the
caller renders `UNKNOWN (pass not run)`; a clean `(none)` for a read that did not happen
is the same defect the digest's other sections guard against. A store outage or a failed
chain read is narrower: the affected rows render `UNKNOWN` and the rest of the block
still reports, because NO-CARD and routing fail independently.

Usage:
    round-detectors.py --stdin           # {session_id: {"name": …, "task_file": …}}
    round-detectors.py --stdin --fixture items.json

Prints the `DETECTORS` block. Exit 0 when the block was computed; 2 when the input could
not be read.
"""
import argparse
import functools
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

NO_CARD = "NO-CARD"
CARD = "CARD"
UNKNOWN = "UNKNOWN"


@functools.lru_cache(maxsize=None)
def _load(filename, module_name):
    """A sibling script, loaded by path — every filename here carries a hyphen."""
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clip(text, limit=60):
    """One line, bounded — a closer can carry newlines and the digest is one row per ask."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def sessions_of(document):
    """The session map from either accepted shape — the bare dict or a snapshot file.

    ⚠️ The round pipes the sessions dict step 9 persists; accepting the persisted
    snapshot too means an agent that hands over the file it already wrote is not a
    malformed input. Anything else is not a session map and must not be read as one.
    """
    if not isinstance(document, dict):
        return None
    inner = document.get("sessions")
    if isinstance(inner, dict):
        return inner
    return document


def task_title(task_file):
    """The task's title is its filename — the same key `fleet-board.py` indexes by."""
    if not task_file:
        return ""
    return os.path.splitext(os.path.basename(task_file))[0]


def session_closers(document, transcript_path=None):
    """`[{name, session, task, closer, body, message, unreadable}]` — one lint scan each.

    `document` is the round's session map (`{sid: {name, task_file}}`). A session with no
    transcript, or a closer salvaged from a partially-written line (`message is None`),
    carries an `unreadable` reason so the blocked test reports UNKNOWN instead of reading
    the block as absent — `reap-closer.py` returns `None` there for exactly that reason.
    A session whose last message carries no closer is skipped: there is nothing to detect.
    """
    lint = _load("closer-gate-lint.py", "closer_gate_lint")
    wnm = _load("who-needs-me.py", "who_needs_me")
    find = transcript_path or wnm.transcript_path
    out = []
    for sid, rec in (document or {}).items():
        rec = rec if isinstance(rec, dict) else {}
        row = {
            "session": sid,
            "name": (rec.get("name") or "").strip() or sid[:8],
            "task": task_title(rec.get("task_file")),
            "closer": "",
            "body": "",
            "message": None,
            "unreadable": None,
        }
        path = find(sid)
        if not path:
            row["unreadable"] = "no transcript on disk"
            out.append(row)
            continue
        found = lint.last_closer_scan(path)
        if found is None:
            continue
        _ts, you_line, _ends, message = found
        row["closer"] = you_line
        row["body"] = lint.closer_body(you_line)
        row["message"] = message
        if message is None:
            row["unreadable"] = "closer salvaged from a partial line — the block cannot be read"
        out.append(row)
    return out


def routing_titles(rows):
    """The distinct task titles of rows that need routing — the chain is built for these.

    The decision is `closer-gate-lint.owner_named()` alone (a regex over the body and the
    whole message). It is the same predicate `blocked-row-routing.routing()` applies, so
    this pre-pass cannot admit a row that script would refuse.
    """
    lint = _load("closer-gate-lint.py", "closer_gate_lint")
    titles = []
    for row in rows:
        if row["unreadable"]:
            continue
        message = row["message"] or ""
        if not lint.BLOCKED_LINE_RE.search(message):
            continue
        if lint.owner_named(row["body"], message):
            continue
        if row["task"] not in titles:
            titles.append(row["task"])
    return titles


def classify(rows, items, managers, chain_error=None):
    """`(no_card, routing, unknown, carded)` for one round's rows.

    `items` is the store's open items, or `None` when the read failed; `managers` maps a
    task title to `(manager, source)` and is `None` when the chain could not be built.
    """
    oac = _load("operator-ask-card.py", "operator_ask_card")
    brr = _load("blocked-row-routing.py", "blocked_row_routing")
    lint = _load("closer-gate-lint.py", "closer_gate_lint")
    no_card, routing, unknown = [], [], []
    carded = 0
    for row in rows:
        if row["unreadable"]:
            unknown.append((row, row["unreadable"]))
            continue
        if oac.is_waiting_ask(row["closer"]):
            if items is None:
                unknown.append((row, "attention store unreadable"))
                continue
            verdict, detail = oac.verdict(row["closer"], row["session"], items)
            if verdict == oac.NO_CARD:
                no_card.append((row, detail))
            elif verdict == oac.CARD:
                carded += 1
            elif verdict == oac.UNKNOWN:
                unknown.append((row, detail))
            continue
        message = row["message"] or ""
        if not lint.BLOCKED_LINE_RE.search(message):
            continue
        if managers is None:
            unknown.append((row, f"manager chain unreadable ({chain_error})"))
            continue
        try:
            record = brr.routing(row["body"], message, row["task"],
                                 resolver=managers.__getitem__)
        except Exception as exc:  # a missing title is a chain bug, never "no routing"
            unknown.append((row, f"routing failed ({exc})"))
            continue
        if record:
            routing.append((row, record))
    return no_card, routing, unknown, carded


def render(no_card, routing, unknown, carded, total, chain_error=None):
    """The `DETECTORS` block — every section printed, `(none)` rather than dropped.

    `total` is every session whose closer was read, not only the ones that produced a
    record: the caller's coverage claim is that the examined count covers the round's
    census, and a denominator counting only detections cannot support it.
    """
    lines = [
        f"DETECTORS  {total} sessions · no-card={len(no_card)} · routing={len(routing)}"
        f" · carded={carded} · unknown={len(unknown)}"
    ]
    lines.append("NO-CARD (parked ask, no live card in the store)")
    if no_card:
        for row, detail in no_card:
            lines.append(f"  {row['session'][:8]} · {row['name']} · {_clip(detail)}")
    else:
        lines.append("  (none)")
    lines.append("ROUTING (blocked closer naming no owner → the subject's manager)")
    if routing:
        for row, record in routing:
            lines.append(f"  {row['session'][:8]} · {record['manager']} ({record['source']})"
                         f" · {row['name']} · {_clip(row['body'], 50)}")
    elif chain_error:
        lines.append(f"  UNKNOWN (manager chain unreadable — {_clip(chain_error, 90)})")
    else:
        lines.append("  (none)")
    if unknown:
        lines.append("UNKNOWN (could not be classified)")
        for row, why in unknown:
            lines.append(f"  {row['session'][:8]} · {row['name']} · {_clip(why)}")
    return "\n".join(lines)


def detect(document, store=None, fixture=None, transcript_path=None, resolve_many=None):
    """The whole block for one round — the seam tests inject every external read through."""
    rows = session_closers(document, transcript_path=transcript_path)
    oac = _load("operator-ask-card.py", "operator_ask_card")
    try:
        items = oac.load_fixture(fixture) if fixture else oac.fetch_open(store or oac.STORE)
    except Exception:  # a store outage narrows the block; it never turns it into (none)
        items = None
    titles = routing_titles(rows)
    managers, chain_error = None, None
    if titles:
        try:
            resolve = resolve_many or _load(
                "escalation-manager.py", "escalation_manager").resolve_many
            managers = resolve(titles)
        except Exception as exc:
            chain_error = str(exc)
    no_card, routing, unknown, carded = classify(rows, items, managers, chain_error)
    return render(no_card, routing, unknown, carded, len(rows), chain_error)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stdin", action="store_true",
                        help="read the round's session map from stdin")
    parser.add_argument("--document", default=None, help="read the session map from a file")
    parser.add_argument("--store", default=None, help="attention store base URL")
    parser.add_argument("--fixture", default=None, help="read store items from a JSON file")
    args = parser.parse_args(argv)

    try:
        if args.document:
            with open(args.document, encoding="utf-8") as fh:
                document = json.load(fh)
        else:
            document = json.load(sys.stdin)
    except Exception as exc:
        print(f"DETECTORS UNKNOWN (pass not run — session map unreadable: {exc})")
        return 2
    sessions = sessions_of(document)
    if sessions is None:
        print("DETECTORS UNKNOWN (pass not run — session map is not an object)")
        return 2

    print(detect(sessions, store=args.store, fixture=args.fixture))
    return 0


if __name__ == "__main__":
    sys.exit(main())
