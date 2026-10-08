#!/usr/bin/env python3
"""The declared-wait reader — session ids whose newest `Stop` record declares `⏰ Ends:`.

**One home for the read, because three consumers take it.** `scripts/fleet-board.py`
(its `Unblocks: nudge` value), `scripts/manager-predispatch.py` (its `idle_stuck`
branch (b)), and the fleet sweep agent's `parked-on-watcher` class all ask the same
question. A private copy in each is the drift `agents/manager-drive.md:134` forbids
verbatim — *"Reuse that shipped reading; do not add a fourth definition of 'idle'"* —
and it is a separate script rather than a function inside `who-needs-me.py` for the
same reason `session-liveness.py` is: the consumers do not share a module, and only
one of them already loads that one.

⚠️ **The marker is a CONTRACT, not a text heuristic.** `~/.claude/CLAUDE.md`
§ Async State Closer scopes `⏰ Ends:` to the 🟡 WAITING panel alone — the one state
whose validity rule is *a machine produces the change*. Deliberately **not**
`⏰ Next:`, the general-purpose forward pointer, which appears on rows waiting on
nothing at all (measured 2026-10-04 over 21,349 `Stop` records: `⏰ Ends:` on 4,967,
`⏰ Next:` on 843).

⚠️ **The NEWEST `Stop` record is the rule.** The log is append-only, so an older
`⏰ Ends:` outlives the wait it declared and would pin the row forever.

⚠️ **A `Stop` record is a rendered closer panel, never a raised gate** — the same
discriminator `who-needs-me.py`'s `is_open_gate()` reads to reach the *opposite*
conclusion. A session that is genuinely gated is therefore never claimed here, and
this reader cannot steal a `waiting-on-human` row from the gate path.

⚠️ **A failed read is not a negative.** A missing, unreadable or torn log leaves the
session **absent from the set**, which is the safe direction for every consumer: each
uses this to *remove* a false `stuck`/`problem`/`nudge` verdict, so a failed read
leaves today's behaviour exactly as it was rather than inventing a park.
"""
import glob
import json
import os
import sys

STATE = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser(
    "~/.claude/state/attention"
)

ENDS_MARKER = "⏰ Ends:"


def declared_wait(sid, state_dir=None):
    """True when `sid`'s newest `Stop` record carries a non-empty `⏰ Ends:` slot.

    Returns a bool. A log that is missing, unreadable or malformed, or that holds no
    `Stop` record at all, is **False** — see the module docstring's failed-read rule.
    """
    if not sid:
        return False
    path = os.path.join(state_dir or STATE, f"{sid}.events.jsonl")
    newest = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    # A torn final line is expected, not exceptional: the hook
                    # appends while this reads.
                    continue
                # ⚠️ **A line that parses to a bare string or list is SKIPPED, not fatal
                # to the file.** `rec.get` on a non-dict raises, and letting that reach the
                # outer handler would discard a valid Stop record already collected earlier
                # in the same log — flipping the session back to "not declared", which is
                # the false-positive direction this reader exists to remove.
                # `who-needs-me.py`'s `load_events` guards the same way.
                if not isinstance(rec, dict):
                    continue
                if rec.get("event") == "Stop":
                    newest = rec
    except Exception:
        return False
    if newest is None:
        return False
    return _has_ends(newest.get("detail"))


def _has_ends(detail):
    """Whether a `Stop` record's `detail` carries a NON-EMPTY `⏰ Ends:` slot.

    `detail` holds one rendered line, so the slot is non-empty exactly when text
    follows the marker. A bare marker with nothing after it declares no wait.

    ⚠️ **A non-string `detail` is "no declaration", never an exception.** The field is
    hook-written, but this reader's own contract is that a malformed log leaves the
    session absent from the set rather than raising — and a truthy dict or list would
    sail through `detail or ""` and raise on `.find`, escaping into `fleet-board.py`'s
    `collect_signals` and into every `manager-predispatch.py` row. Guarding the type at
    the one place the value is read is what keeps that contract true.
    """
    if not isinstance(detail, str):
        return False
    idx = detail.find(ENDS_MARKER)
    if idx < 0:
        return False
    return bool(detail[idx + len(ENDS_MARKER):].strip())


def declared_wait_ids(state_dir=None):
    """Every session id whose newest `Stop` record declares a non-empty `⏰ Ends:`.

    The whole-store form, for the renderers: they classify every live row at once and
    a per-session call would re-open the directory for each. Reading one session's log
    per file is still O(files), which is what `who-needs-me.py`'s own `load_events()`
    pays — this is the same shape, not a new cost.
    """
    base = state_dir or STATE
    out = set()
    for path in glob.glob(os.path.join(base, "*.events.jsonl")):
        sid = os.path.basename(path)[: -len(".events.jsonl")]
        if declared_wait(sid, state_dir=base):
            out.add(sid)
    return out


def main(argv):
    ids = declared_wait_ids()
    if "--json" in argv:
        print(json.dumps(sorted(ids)))
        return 0
    for sid in sorted(ids):
        print(sid)
    print(f"{len(ids)} session(s) declare a wait", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
