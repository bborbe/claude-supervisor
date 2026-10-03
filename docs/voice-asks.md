# Voice asks are board cards too

Every question spoken to the operator through `mcp__tts__say` is **also** an attention-board card. Never voice-only.

Voice reaches the operator only while they are near audio; the board is the durable inbox they answer from. A voice-only ask is lost the moment they step away. Measured 2026-10-02 over one week of local transcripts: 212 voiced questions, **6** paired with a card.

## The rule

- **A question** is a `say` whose text contains `?` anywhere — mid-text counts ("Want a worker on it? It has been idle since noon.").
- Before or within 60 s of voicing it, post the same question with `scripts/attention-ask.py post` (dedup key, payload, options, your recommendation), then `poll` for the answer. ⚠️ **Conditionally append `--closer "<the exact text you will end this turn on, WITHOUT the 👤 You: prefix>"` — and ONLY when that text repeats the ask you just voiced.** A voiced question is usually the closer's own ask, which is precisely the shape that mints a duplicate card: the Stop hook mirrors the closer as a second item for one question, and the operator spends an action dismissing the echo. ⚠️ **The record is compared against what the hook EXTRACTS — everything after `👤 You: ` — so a value carrying the prefix never matches and the suppression stays inert.** ⚠️ **It is matched against YOUR CLOSER, not against the card**, so declaring one makes the hook decline to mirror that line *whatever it asks*: append it for a closer that asks something **else** and a genuine, distinct question is silently swallowed. ⚠️ **And quote the text you will actually emit** — an invented or paraphrased closer never matches, so the flag becomes a silent no-op and the duplicate stays live while every check reads green. Opt-in: omitting it costs nothing but leaves the duplicate live.
- The card carries the substance; the voice only points at it. One question, two channels, **one** answer path: the board.
- A `say` with no question (verdicts, progress) needs no card.

## Detection

`scripts/voice-ask-pairing.py [--days N] [--json] [TRANSCRIPT ...]` scans transcripts and reports every question `say` with no `attention-ask.py post` within 60 s in the same transcript. It reports; it never blocks a `say`.
