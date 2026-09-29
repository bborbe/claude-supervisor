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

Print the output verbatim, then stop. Each item shows its `item_id`, its question, and how an answer would travel — `TARGET: <name>` (a session to message), `UNDELIVERABLE: <reason>`, `answerable here with --decision allow|deny (delivered by supervisor poll)`, or `not answerable here (<mechanism>)`.

**`ITEM_ID ANSWER...` — answer one item.** The first token is the item id; the rest is the answer text, verbatim.

1. Run exactly, adding `--decision` **only** when `<ANSWER>` is exactly `allow` or `deny`:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/attention-answer.py answer <ITEM_ID> [--decision allow|deny]
   ```
   A `permission`-class item **needs** the flag and is refused without it. A `message`-class item ignores it, so passing it for a message whose answer happens to be the word "allow" changes nothing.
2. Branch on the output — never on anything else:
   - `ANSWERED:` then `TARGET: <name>` → call `SendMessage` with `to` = `<name>` exactly, and `message` = `Operator answer, via attention stack (item <ITEM_ID>): <ANSWER>`. Report the item id and the recipient.
   - `ANSWERED:` then `DELIVERY: supervisor poll` → do **not** send anything. Report: the verdict is recorded and the spawning supervisor server will settle the parked prompt. **There is no session to message here**, and messaging one would be a relay — a session cannot release another session's parked gate.
   - `ANSWERED:` then `UNDELIVERABLE: <reason>` → do **not** send. Report: the item is answered in the store, but the answer was not delivered — `<reason>`.
   - `LOST:` → do **not** send; another arm already answered. Report it.
   - `REFUSED:` / `FAILED:` → report the line verbatim; do not send.

3. **Record the attempt — this command is the arm that ATTEMPTS delivery, so this is where the trail is written.** Run exactly:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/attention-answer.py attempt <ITEM_ID> --outcome delivered|failed
   ```

   Which outcome, per the branch taken above:

   | Branch above | Outcome | Why |
   |---|---|---|
   | `TARGET:` and `SendMessage` returned without error | `delivered` | The answer reached the session. |
   | `TARGET:` and `SendMessage` **errored** | `failed` | A carrier looked and could not deliver. |
   | `UNDELIVERABLE: <reason>` | `failed` | The attempt failed at target resolution; nothing was sent. |
   | `DELIVERY: supervisor poll` | **nothing** | The supervisor server delivers the verdict and records its own attempt. Posting here would record an attempt by an arm that delivered nothing. |
   | `LOST:` / `REFUSED:` / `FAILED:` | **nothing** | No attempt happened — another arm won the compare-and-set, or the answer was refused before any delivery was tried. ⚠️ Recording `failed` here would claim a carrier tried and could not deliver, which is a different and false statement. |

   ⚠️ **Never record at answer time.** An attempt is a fact about the carrier, so writing it where the *answer* is recorded would make every answer read as attempted — the exact defect this trail exists to close. The store stamps the attempt's time from its own clock, so this command cannot date its own attempt.

   ⚠️ **Requires attention-controller ≥ `v0.30.0`**, which adds the attempt endpoint. Against an older store the `attempt` call fails with `FAILED:`; that is a missing endpoint, not a failed delivery, so report it as such and do not retry the delivery.

The store is written **before** the message is sent, and only the arm that wins the store's compare-and-set sends — so two arms answering the same item deliver exactly one answer. Never retry a `LOST`, and never guess a recipient for an `UNDELIVERABLE` item: a bare name shared by two sessions reaches the wrong one.

Only the operator's own words go into `ANSWER`. This command relays an operator answer; it is not a channel for a manager's own decisions. A `permission` item is answered here with an **explicit verdict** and is delivered by the **supervisor server polling the store**, never by this command — a relay is permission laundering even when the action looks small, so this command never messages a session about a permission item.
