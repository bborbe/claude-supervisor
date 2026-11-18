#!/usr/bin/env python3
"""Per-round main-thread context delta, from a session transcript.

A fleet round's cost to its manager is the text it adds to the **main thread** —
the prompt, the step files it reads, the script output it keeps, the hand-backs
it receives. That number was measured once by hand (2026-11-17) and never
reproduced, which is why the task that depends on it names "the same breakdown
script" and nothing could run it.

This is that script. It segments a transcript into rounds at the scheduler's own
markers and reports each round's byte delta, so "≤ 3 KB per round" becomes a
reading rather than an estimate.

**What counts.** Only records on the main thread (`isSidechain` excluded — a
sub-agent's transcript is its own file and its output reaches the manager as one
hand-back message, which *is* counted here when it lands). Within a record, the
text a reader would have to hold: user text and tool results, assistant text and
thinking, and a tool call's input. Images and file attachments are not text and
are not counted.

**Where rounds begin.** At a record the scheduler stamped — `scheduledFireId` (or
`scheduledTaskId`) — whose message content starts with the round prompt. A
transcript with no such marker is not a loop transcript: exit 2, never a zero
dressed as a clean reading.

⚠️ **The first segment is reported separately.** Anything before the first marker
is the session's setup, not a round; folding it into round 1 would charge the
round for the arm.
"""

import argparse
import datetime
import json
import statistics
import sys
from pathlib import Path

#: The text-bearing keys of a content block. `input` is a tool call's arguments and is
#: serialized because it is what the model read, not what it typed.
TEXT_KEYS = ("text", "thinking", "content")


def text_bytes(value) -> int:
    """Bytes of text a content value carries, across the transcript's shapes."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, list):
        return sum(text_bytes(item) for item in value)
    if isinstance(value, dict):
        total = sum(text_bytes(value.get(key)) for key in TEXT_KEYS)
        if "input" in value:
            total += len(json.dumps(value["input"], ensure_ascii=False).encode("utf-8"))
        return total
    return 0


def record_bytes(record: dict) -> int:
    """Bytes one record adds to the main thread."""
    return text_bytes(record.get("message", {}).get("content"))


def marker_text(record: dict) -> str:
    """The prompt text of a scheduler-fired record, or '' when it is not one."""
    content = record.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def is_round_marker(record: dict, prefix: str) -> bool:
    """A scheduler fire whose prompt names the round."""
    if not (record.get("scheduledFireId") or record.get("scheduledTaskId")):
        return False
    return marker_text(record).strip().startswith(prefix)


def load(path: Path) -> list[dict]:
    """Every parsable record. A malformed line is skipped, never fatal — a transcript
    is appended to while it is read and its last line can be partial."""
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def segment(records: list[dict], prefix: str) -> list[dict]:
    """Rounds, each with the bytes added after its marker.

    ⚠️ **The final round is marked `open`** — nothing closed it, so everything
    after its marker (a later unrelated turn, a whole session's tail) is charged
    to it. Measured 2026-11-18 on `cfaf1455`: one marker in the whole transcript
    produced a single "round" of **8.5 MB**. A round is measured from its fire to
    the next fire, so an unclosed one is not a measurement; `main` drops it unless
    `--include-open` asks for it.
    """
    rounds: list[dict] = []
    current = None
    for record in records:
        if is_round_marker(record, prefix):
            current = {
                "start": record.get("timestamp"),
                "fire": record.get("scheduledFireId") or record.get("scheduledTaskId"),
                "bytes": record_bytes(record),
            }
            rounds.append(current)
            continue
        if current is None:
            continue
        if record.get("isSidechain"):
            continue
        current["bytes"] += record_bytes(record)
    if rounds:
        rounds[-1]["open"] = True
    return rounds


def window_bytes(records: list[dict], start: float, end: float) -> int:
    """Main-thread bytes whose record timestamp falls in `[start, end]`.

    ⚠️ **This is the only exact measurement this tool offers**, and it needs the
    receipt's `round_ended_at`. Every other mode infers where a round ended — the next
    fire, or the first closer — and each inference charges unrelated work to the round
    (see `segment`'s note and `manager-receipt.py --finish`). A record with no
    parsable timestamp is counted, not dropped: an unreadable clock is not evidence
    that the record was outside the window.
    """
    total = 0
    for record in records:
        if record.get("isSidechain"):
            continue
        stamp = record.get("timestamp")
        if isinstance(stamp, str):
            try:
                ts = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = None
            if ts is not None and not (start <= ts <= end):
                continue
        total += record_bytes(record)
    return total


def load_receipt(path: Path) -> tuple[float, float] | None:
    """A COMPLETED round window from a receipt, or None when the receipt holds none.

    ⚠️ **The `previous_*` pair is preferred, and it is the one that survives.** A receipt
    is rewritten at every round head, and the head carries the previous round's start and
    end forward precisely so a finished window outlives the next round's `--start`. The
    current pair is complete only in the gap between a round's tail and the next head —
    measured 2026-11-18, that gap is not reliably observable, and reading only the current
    pair reported `None` on a round that had in fact finished.

    Falls back to the current pair for a receipt written before the end was carried.
    """
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # ⚠️ **Current pair FIRST.** Both pairs complete is the ordinary state between a
    # round's `--finish` and the next `--start`, and preferring the carried pair there
    # would report the round BEFORE the one just finished. At a round head the current
    # pair is incomplete by construction — the fresh `--start` dict carries no
    # `round_ended_at` — so it falls through to the carried window, which is the case
    # this fallback exists for.
    for start_key, end_key in (
        ("round_started_at", "round_ended_at"),
        ("previous_round_started_at", "previous_round_ended_at"),
    ):
        start, end = d.get(start_key), d.get(end_key)
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            return float(start), float(end)
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--transcript", required=True, type=Path,
                        help="the session transcript (JSONL)")
    parser.add_argument("--prompt-prefix", default="/supervisor:fleet-drive",
                        help="the round prompt a scheduler fire must start with")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of lines")
    parser.add_argument(
        "--receipt",
        type=Path,
        help="measure the EXACT round window from this receipt's round_started_at -> "
             "round_ended_at, instead of inferring boundaries from fires or closers",
    )
    parser.add_argument(
        "--include-open",
        action="store_true",
        help="also report the trailing round, which nothing closed — its bytes include "
             "everything after its marker, so it is not a round measurement",
    )
    args = parser.parse_args(argv)

    if not args.transcript.exists():
        print(f"transcript not found: {args.transcript}", file=sys.stderr)
        return 2
    records = load(args.transcript)

    if args.receipt is not None:
        window = load_receipt(args.receipt)
        if window is None:
            print(
                f"{args.receipt} carries no round_started_at / round_ended_at pair. A "
                f"receipt with only a start cannot say where the round ended, which is "
                f"the whole question — run `manager-receipt.py --finish` on that round "
                f"first. Inferring the end is what produced three answers from one "
                f"dataset.",
                file=sys.stderr,
            )
            return 2
        start, end = window
        total = window_bytes(records, start, end)
        kb = total / 1024
        if args.json:
            print(json.dumps({
                "mode": "receipt",
                "start": datetime.datetime.fromtimestamp(start).isoformat(),
                "end": datetime.datetime.fromtimestamp(end).isoformat(),
                "bytes": total,
                "kb": round(kb, 1),
            }, indent=1))
            return 0
        print(
            f"round {datetime.datetime.fromtimestamp(start):%Y-%m-%dT%H:%M:%S}"
            f" -> {datetime.datetime.fromtimestamp(end):%H:%M:%S}  {total} B  {kb:.1f} KB"
        )
        return 0

    rounds = segment(records, args.prompt_prefix)
    if rounds and not args.include_open:
        rounds = [r for r in rounds if not r.get("open")]
        if not rounds:
            print(
                f"{args.transcript} has no CLOSED round — its only marker(s) were never "
                f"followed by another fire, so the trailing segment includes everything "
                f"after them and is not a round measurement. Re-run with --include-open "
                f"to see it anyway.",
                file=sys.stderr,
            )
            return 2
    if not rounds:
        print(
            f"no round markers found in {args.transcript} — a fire must carry "
            f"scheduledFireId and a prompt starting {args.prompt_prefix!r}. "
            f"An empty result is not a zero-cost round.",
            file=sys.stderr,
        )
        return 2

    kb = [r["bytes"] / 1024 for r in rounds]
    median = statistics.median(kb)
    if args.json:
        print(json.dumps({
            "rounds": [
                {"n": i + 1, "start": r["start"], "fire": r["fire"],
                 "bytes": r["bytes"], "kb": round(r["bytes"] / 1024, 1)}
                for i, r in enumerate(rounds)
            ],
            "count": len(rounds),
            "median_kb": round(median, 1),
            "max_kb": round(max(kb), 1),
        }, indent=1))
        return 0

    for i, r in enumerate(rounds):
        print(f"round {i + 1:>2}  {r['start'] or '—':<24}  {r['bytes']:>7} B  {kb[i]:>6.1f} KB")
    print(f"\n{len(rounds)} round(s) · median {median:.1f} KB · max {max(kb):.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
