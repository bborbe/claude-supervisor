---
description: What's next on the attention stack — show open items, answer one in chat, and deliver the answer to the session that asked
allowed-tools:
  - Bash(python3:*)
  - SendMessage
argument-hint: "[ITEM_ID ANSWER...]"
---

The conversational arm of the attention stack. Two modes, picked by `$ARGUMENTS`.

**No arguments — the pull ("what's next?").** Run exactly:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/attention-answer.py next
```

Print the output verbatim, then stop. Each item shows its `item_id`, its question, and where an answer would go (`TARGET: <name>`, `UNDELIVERABLE: <reason>`, or `not answerable here (<mechanism>)`). The operator answers with `/supervisor:attention-next <item_id> <answer>`.

**`ITEM_ID ANSWER...` — answer one item.** The first token is the item id; the rest is the answer text, verbatim.

1. Run exactly:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/attention-answer.py answer <ITEM_ID>
   ```
2. Branch on the output — never on anything else:
   - `ANSWERED:` then `TARGET: <name>` → call `SendMessage` with `to` = `<name>` exactly, and `message` = `Operator answer, via attention stack (item <ITEM_ID>): <ANSWER>`. Report the item id and the recipient.
   - `ANSWERED:` then `UNDELIVERABLE: <reason>` → do **not** send. Report: the item is answered in the store, but the answer was not delivered — `<reason>`.
   - `LOST:` → do **not** send; another arm already answered. Report it.
   - `REFUSED:` / `FAILED:` → report the line verbatim; do not send.

The store is written **before** the message is sent, and only the arm that wins the store's compare-and-set sends — so two arms answering the same item deliver exactly one answer. Never retry a `LOST`, and never guess a recipient for an `UNDELIVERABLE` item: a bare name shared by two sessions reaches the wrong one.

Only the operator's own words go into `ANSWER`. This command relays an operator answer; it is not a channel for a manager's own decisions, and it never releases a permission prompt — `permission`-class items are listed but refused.
