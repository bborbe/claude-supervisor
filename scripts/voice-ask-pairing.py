#!/usr/bin/env python3
"""Flag voiced questions that never reached the attention board.

Every question spoken to the operator through `mcp__tts__say` must also be an
attention-board card (`attention-ask.py post`). A voice-only ask is lost the
moment the operator is away from audio; the board is the durable inbox.

The rule, in one place:

- **A question** is a `mcp__tts__say` whose text contains `?` anywhere.
  Not "ends with `?`" — spoken asks put the question mid-text and keep talking
  ("Want a worker on it? It's been idle since noon."). Measured 2026-10-02 over
  228 sampled says: an ends-with rule caught 3, contains-`?` caught every ask.
  No verb list: verbs ("approve", "pick") appear in statements as often as in
  questions, so they add false positives without catching anything `?` misses.
- **Paired** means the same transcript holds a Bash `tool_use` running
  `attention-ask.py post` (or `post-batch`) within PAIR_WINDOW seconds of the
  say, before or after — posting first and voicing second is just as good.

It reports, it never enforces: nothing here blocks a `say`.

Usage:
  voice-ask-pairing.py [--days N] [--json] [TRANSCRIPT ...]

With no transcripts, scans ~/.claude/projects/*/*.jsonl modified in the last
--days (default 7). Exit 0 always; the count is the result.
"""

import argparse
import datetime
import glob
import json
import os
import sys
import time

PAIR_WINDOW = 60


def is_question(text):
    return "?" in (text or "")


def _ts(record):
    raw = record.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_post(command):
    return "attention-ask.py" in command and (" post" in command)


def scan(path):
    """Return (questions, unpaired) for one transcript; each a list of (ts, text)."""
    says, posts = [], []
    try:
        fh = open(path, errors="ignore")
    except OSError:
        return [], []
    with fh:
        for line in fh:
            if "mcp__tts__say" not in line and "attention-ask.py" not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            content = (record.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            ts = _ts(record)
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                inp = block.get("input") or {}
                if block.get("name") == "mcp__tts__say":
                    says.append((ts, inp.get("text", "")))
                elif block.get("name") == "Bash" and _is_post(inp.get("command", "")):
                    posts.append(ts)
    questions = [s for s in says if is_question(s[1])]
    unpaired = [
        (ts, text)
        for ts, text in questions
        if ts is None or not any(p is not None and abs(p - ts) <= PAIR_WINDOW for p in posts)
    ]
    return questions, unpaired


def default_transcripts(days):
    cutoff = time.time() - days * 86400
    pattern = os.path.expanduser("~/.claude/projects/*/*.jsonl")
    return sorted(f for f in glob.glob(pattern) if os.path.getmtime(f) >= cutoff)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("transcripts", nargs="*")
    args = ap.parse_args(argv)

    files = args.transcripts or default_transcripts(args.days)
    total_q = 0
    flagged = []
    for path in files:
        questions, unpaired = scan(path)
        total_q += len(questions)
        for ts, text in unpaired:
            flagged.append({"transcript": path, "ts": ts, "text": text[:120]})

    if args.json:
        json.dump({"questions": total_q, "unpaired": len(flagged), "flags": flagged}, sys.stdout, indent=2)
        print()
    else:
        for f in flagged:
            print(f"UNPAIRED {os.path.basename(f['transcript'])}: {f['text']}")
        print(f"questions={total_q} paired={total_q - len(flagged)} unpaired={len(flagged)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
