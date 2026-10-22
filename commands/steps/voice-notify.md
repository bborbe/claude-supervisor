---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Voice + notification

When: every tick — run the `{"gates": []}` prune on every round, and publish only the gates the operator must decide (§ Gate triage classes C, D, E)

    **The notification follows the same test, through the plugin's publisher — never a vault command.** Once per round, with every gate the round raised:

    ```bash
    echo '{"gates": [{"owner": "<session id or pane id>", "text": "<the gate line>", "session": "<the BLOCKED session id>"}]}' \
      | python3 $P/notify-gate.py --layer fleet
    ```

    Send `{"gates": []}` on a round that raised none — the empty call prunes **your own** cleared gates only, never another manager's. **`--layer fleet` is required** (the ledger is per layer; this sweep is a subset). Publish only gates the operator must decide — § Gate triage classes **C, D, E** in the Manager Session runbook; **A and B** are yours to clear and stay silent. Surface the script's message rather than swallowing it. **Do not pre-filter for cross-layer duplicates** — the script de-dups by escalating session id and prints the skip; publish and let it decide.

    ### Speak the asks; keep the detail in chat

    **The round's operator asks are spoken, once, as a count and a pointer.** The operator's standing instruction, 2026-10-14: *"if u write only … i will not read it most the time"* — *"combine this talking with the chat. You can tell me, hey, I have four tasks that need your attention. The links are in the chat."*

    - **Speak every item that needs the operator** — a new gate, an approval ask, a decision. An ask that reaches only the chat is an ask the operator does not read.
    - **The spoken line is count + pointer and nothing else:** `<N> items need your attention, the links are in the chat`. **Never details, never URLs, never a pane or session list.** One `mcp__tts__say` call per round however many items it names — never N calls for N items.
    - **The details and the jump links go in the chat in the same turn** — § Output shape items 4 (`📋 Open with the operator`) and 5 (Needs-input) already render the details and the jump links; item 2 (`Next focus`) renders a proposal ask as `name — why` and **carries no link**. ⚠️ **A round whose only items are proposals must not speak the `the links are in the chat` wording** — say `<N> items need your attention, the details are in the chat` instead, because there is no link to point at. Do not restate any of them in voice.
    - **Silence when nothing needs the operator.** No gate, no ask, no decision → no `mcp__tts__say` at all.

    **How this composes — it changes nothing about the gate, and nothing about the silence test.**

    - **`~/.claude/hooks/voice-mode.py` decides *whether*; these rules decide *what shape*.** Voice is off unless the operator turned it on (the manager verbs switch it on with mode `interview`); an explicit `/tts-mcp:off` always wins. These rules never resurrect voice over an `off` — with voice off the asks still reach the chat and the board.
    - **`commands/steps/rules.md` already carries the silence test** — *"Silence is the default; the test is an ACTION, not a finding."* The rule above is that test applied to this step; cited, not restated.
    - **`docs/voice-asks.md` pairs a `?`-bearing `say` with a board card within 60 s.** The count-and-pointer line carries no `?`, so it mints no card — the gates already reach the board through the `notify-gate.py --layer fleet` publish above. One question, two channels, one answer path: the board.
    - **The global rules say the same thing one level up** — *"Attention Costs The Operator"* (the DO-test) and *"Voice Chat Detail On Request"* (long detail goes to chat on request, never into speech). The count-and-pointer shape is those rules made concrete for this round.
