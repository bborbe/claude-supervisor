#!/usr/bin/env python3
"""The hand-back contract's ≤ 1 KB cap, asserted rather than described.

`docs/handback-contract.md` § The shape says a leg returns **≤ 1 KB** and the detail goes to
the payload. Nothing measured it, so every leg could exceed the cap on every round and no
check would fail — the same "a rule that lives only in prose" defect `check-handback-keys.py`
catches one layer down for *keys*. This asserts the size.

**What is measured, and what it does not prove.** The agent's own `<output_format>` hand-back
fence, minus the lines the contract declares **variable** (the truncation exception:
`crash-loop:` / `cannot-tell:` / `park-nudge:` rows, `refused:` lines). Those are unbounded by
design — the payload carries the remainder — so counting them would fail a leg that is behaving
correctly.

⚠️ **The default mode pins the authored shape; `--transcript` measures a live round.** A
placeholder is not always longer than its value: `<n>` and `<pct>` are, but `<path>` and
`<id>` stand in for strings that can exceed them, so a template under the cap does not prove
every instance is. What the template check does prove is that the *fixed* text — the keys, the
separators, the labels a leg controls — leaves room for the values.

⚠️ **That gap is why the transcript modes exist.** Measured 2026-11-18 on eight live
`supervisor:fleet-round` delegates: seven emitted **no verdict at all**, and the eighth emitted
**1 510 B** against the 1 024 B cap — and the template check reported green through all of it.
`--transcript` / `--transcript-dir` measure what a delegate actually emitted, and **an absent
hand-back is a failure, not 0 B**.

⚠️ **Three delivery shapes count, because the live legs emit all three.** The authored
`**Hand-back**` fence; a prose block naming the round id; and — measured 2026-11-20 — the
`SubagentHandback` **tool call**, which the fleet legs' own prompts name as the delivery
channel: *"call SubagentHandback({message: <your full report>}) … plain text you write at the
end is not delivered"*. Reading only text blocks graded a compliant 514 B hand-back as absent.
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAX_BYTES = 1024

#: The lines the contract declares variable per leg — never counted against the cap. Keys are
#: the leg names `check-handback-keys.LEGS` uses. A leg absent here has no variable lines and
#: its whole hand-back must fit.
VARIABLE = {
    "supervisor:fleet-keepalive-actor": ("crash-loop:", "cannot-tell:", "park-nudge:"),
}


def load_legs():
    """`check-handback-keys.LEGS` — one registry, so a new leg is guarded by both checks."""
    path = REPO / "scripts" / "check-handback-keys.py"
    spec = importlib.util.spec_from_file_location("check_handback_keys", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LEGS


def handback_fence(text: str) -> str | None:
    """Every fenced block under `**Hand-back**`, joined, or None when there is not one.

    ⚠️ **All fences, not the first.** A leg may put its counts in one block and its
    per-row lines in a second (the keep-alive leg's `crash-loop:` rows are exactly that), so reading
    only the first would leave that leg's declared variable lines uncounted and its
    exclusion inert — the check would report a pass for a measurement it never took.
    """
    _, sep, rest = text.partition("**Hand-back**")
    if not sep:
        return None
    body = rest.partition("</output_format>")[0]
    blocks = re.findall(r"```[a-z]*\n(.*?)```", body, re.DOTALL)
    return "\n".join(blocks) if blocks else None


def fixed_bytes(fence: str, variable: tuple[str, ...]) -> int:
    """Byte length of the fence minus its declared variable lines."""
    kept = [ln for ln in fence.splitlines()
            if not any(ln.lstrip().startswith(p) for p in variable)]
    return len("\n".join(kept).rstrip("\n").encode("utf-8"))


def assistant_blocks(transcript: Path):
    """Every content block of every `assistant` record in a transcript, in order.

    ⚠️ **One walk, two readers.** `assistant_texts()` and `handback_tool_messages()` differ
    only in which blocks they keep; a second copy of this JSONL walk would drift the moment
    one gained a guard (a skipped record type, a malformed line) and the other did not —
    and the drift would show up as a silent under-count, i.e. as a clean pass.
    """
    with transcript.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "assistant":
                continue
            content = (record.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict):
                    yield block


def assistant_texts(transcript: Path) -> list[str]:
    """Every non-empty assistant text block in a transcript, in order.

    ⚠️ **Text blocks only.** The delegate's hand-back is authored prose; the same tokens
    appear inside tool calls and results (`self-context=` is the recorded case), so reading
    those would measure something the manager never sees as a verdict.

    The one exception is `handback_tool_messages()` below: it reads the `SubagentHandback`
    call **by name**, because that tool's input is the verdict rather than a lookalike.
    """
    texts: list[str] = []
    for block in assistant_blocks(transcript):
        if block.get("type") != "text":
            continue
        text = block.get("text", "")
        if text.strip():
            texts.append(text)
    return texts


#: The tool a leg delivers its report through. The fleet legs' own prompts name it — *"call
#: SubagentHandback({message: <your full report>}) … plain text you write at the end is not
#: delivered"* — so its `message` input IS the hand-back.
HANDBACK_TOOL = "SubagentHandback"


def handback_tool_messages(transcript: Path) -> list[str]:
    """Every non-empty `SubagentHandback` `message` input in a transcript, in order.

    ⚠️ **Keyed on the tool NAME, never on "a `tool_use` carrying a `message`".** `SendMessage`
    carries a `message` input too — measured 2026-11-20 on `agent-ab4dfffd5c9330c85.jsonl`,
    five calls at 295–547 B alongside the one 514 B hand-back. A name-blind reading would
    measure a message the leg sent to a peer as its verdict, and would pass a leg that never
    handed back at all — the absent-hand-back defect this mode exists to fail.

    ⚠️ **This is why `assistant_texts()` is still text-only.** That function's own rule (the
    `self-context=` case) holds: arbitrary tool-call text is not a verdict. This reads the
    one tool whose contract *is* the verdict, and only its `message` field.
    """
    messages: list[str] = []
    for block in assistant_blocks(transcript):
        if block.get("type") != "tool_use" or block.get("name") != HANDBACK_TOOL:
            continue
        message = (block.get("input") or {}).get("message")
        if isinstance(message, str) and message.strip():
            messages.append(message)
    return messages


#: A block that names the round it reports on — the delegate's own verdict heading, e.g.
#: `**Round 1795010883.8338208 — final verdict**`. This is the discriminator SC3 names.
ROUND_ID = re.compile(r"\bround[\s=]\d{6,}", re.IGNORECASE)


def emitted_reading(transcript: Path, variable: tuple[str, ...] = ()) -> tuple[int | None, str]:
    """The EMITTED hand-back's byte size, or None and the reason there is none.

    ⚠️ **Two text shapes count, because the live delegates emit the second.** The contract's
    `**Hand-back**` fence is the authored shape, but measured 2026-11-18 on eight live
    `supervisor:fleet-round` delegates **none** emitted that fence: the one that handed
    back at all wrote prose — `**Round <id> — final verdict**` plus bullets — at 1 510 B.
    So a block is the hand-back when it carries the fence **or** names the round id.

    ⚠️ **The `SubagentHandback` call is a third shape, and the fleet legs' primary one.**
    Measured 2026-11-20 on round `agent-ab4dfffd5c9330c85`: a 514 B hand-back delivered
    through the tool call, and this function returned "absent" because it read text blocks
    only — grading every compliant leg a failure. A tool message is a hand-back
    **unconditionally** (the call is the contract); it needs no fence and no round id.

    ⚠️ **An absent hand-back is a FAILURE, not 0 B — the reason this mode exists.** Seven
    of those eight emitted a 54–111 B closing remark and no verdict at all, so a literal
    "≤ 1 KB" test passed them while the contract was violated.
    """
    texts = assistant_texts(transcript)
    tool_messages = handback_tool_messages(transcript)
    if not texts and not tool_messages:
        return None, "no assistant text block and no SubagentHandback call"
    # ⚠️ **The WHOLE block, never just the fence.** A delegate that uses the authored
    # fence shape still wraps it in a heading, a table and a trailing note; measuring the
    # fence alone under-reported a 1 098 B message as 40 B and passed it 74 B over the cap.
    # ⚠️ **The LARGEST matching block, never the last.** A verdict followed by a short
    # `Round <id> complete — hand-back delivered.` remark would otherwise be measured as
    # the remark (1 639 B read as 50 B). Measuring the maximum fails closed.
    sizes = [
        fixed_bytes(text, variable)
        for text in texts
        if handback_fence(text) is not None or ROUND_ID.search(text)
    ]
    # ⚠️ **`fixed_bytes` on the tool message too, so the variable-line carve-out holds
    # whichever shape a leg delivers in.** A keepalive leg handing back through the tool
    # still carries its `crash-loop:` rows, and the contract declares those unbounded.
    sizes += [fixed_bytes(message, variable) for message in tool_messages]
    if sizes:
        return max(sizes), "emitted hand-back found"
    return None, (
        f"no `**Hand-back**` fence and no round-id block in {len(texts)} assistant "
        f"text block(s), and no {HANDBACK_TOOL} call"
    )


def transcript_readings(directory: Path, agent_type: str) -> tuple[list[str], list[str]]:
    """Measure every delegate transcript under `directory`. (readings, failures).

    ⚠️ **A missing, empty or non-matching directory is a FAILURE, not a clean zero.**
    Otherwise a typo'd session id prints `ok: 0 emitted hand-back(s)` and greens the gate —
    the directory-level form of the absent-hand-back defect this mode exists to close.
    """
    if not directory.is_dir():
        return [], [f"{directory}: not a directory — nothing was measured"]
    variable = VARIABLE.get(agent_type, ())
    readings: list[str] = []
    failures: list[str] = []
    matched = 0
    for transcript in sorted(directory.glob("*.jsonl")):
        meta = transcript.with_suffix(".meta.json")
        if meta.exists():
            try:
                declared = json.loads(meta.read_text(encoding="utf-8")).get("agentType")
            except (OSError, json.JSONDecodeError):
                declared = None
            if declared and declared != agent_type:
                continue
        matched += 1
        try:
            size, reason = emitted_reading(transcript, variable)
        except (OSError, UnicodeDecodeError) as exc:
            # ⚠️ **One unreadable file must not abort the scan.** A corrupt transcript used
            # to raise out of the loop and hide every other file's verdict.
            failures.append(f"{transcript.name}: unreadable ({type(exc).__name__})")
            continue
        if size is None:
            failures.append(
                f"{transcript.name}: {reason} — an absent hand-back is a failure, not 0 B"
            )
            continue
        readings.append(f"{transcript.name}={size}B")
        if size > MAX_BYTES:
            failures.append(
                f"{transcript.name}: emitted hand-back is {size} B over its {MAX_BYTES} B cap"
            )
    if matched == 0:
        failures.append(
            f"{directory}: no transcript matched agent type {agent_type!r} — "
            f"an empty set is a failure, not a clean zero"
        )
    return readings, failures


def main(argv=None, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--transcript", type=Path,
        help="measure the EMITTED hand-back of one subagent transcript, not the authored template",
    )
    source.add_argument(
        "--transcript-dir", type=Path,
        help="measure every *.jsonl under this subagents directory",
    )
    parser.add_argument(
        "--agent-type", default="supervisor:fleet-round",
        help="with --transcript-dir, keep only transcripts whose .meta.json agentType matches",
    )
    args = parser.parse_args(argv)

    if args.transcript is not None:
        if not args.transcript.is_file():
            print(f"handback-size FAILED: {args.transcript}: not a file", file=sys.stderr)
            return 1
        size, reason = emitted_reading(
            args.transcript, VARIABLE.get(args.agent_type, ())
        )
        if size is None:
            print(
                f"handback-size FAILED: {args.transcript.name}: {reason} — an absent "
                f"hand-back is a failure, not 0 B",
                file=sys.stderr,
            )
            return 1
        if size > MAX_BYTES:
            print(
                f"handback-size FAILED: {args.transcript.name}: emitted hand-back is {size} B "
                f"over its {MAX_BYTES} B cap",
                file=sys.stderr,
            )
            return 1
        print(f"handback-size ok: {args.transcript.name}={size}B (emitted)")
        return 0

    if args.transcript_dir is not None:
        readings, failures = transcript_readings(args.transcript_dir, args.agent_type)
        if failures:
            print(f"handback-size FAILED: {len(failures)} transcript problem(s)", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
            return 1
        print(
            f"handback-size ok: {len(readings)} emitted hand-back(s) under {MAX_BYTES} B — "
            f"{', '.join(readings)}"
        )
        return 0

    root = Path(root) if root is not None else REPO
    legs = load_legs()
    failures: list[str] = []
    readings: list[str] = []
    for name, leg in sorted(legs.items()):
        agent = root / leg["agent"]
        if not agent.exists():
            failures.append(f"{leg['agent']}: missing — {name}'s hand-back cannot be measured")
            continue
        fence = handback_fence(agent.read_text(encoding="utf-8"))
        if fence is None:
            failures.append(f"{leg['agent']}: no `**Hand-back**` fenced block — nothing to measure")
            continue
        size = fixed_bytes(fence, VARIABLE.get(name, ()))
        readings.append(f"{name}={size}B")
        if size > MAX_BYTES:
            failures.append(
                f"{leg['agent']}: hand-back is {size} B over its {MAX_BYTES} B cap "
                f"(fixed lines only) — move detail to the payload, or declare the variable "
                f"lines in this script's VARIABLE map"
            )
    if failures:
        print(f"handback-size FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"handback-size ok: {len(readings)} leg(s) under {MAX_BYTES} B — {', '.join(readings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
